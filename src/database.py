"""
Database Layer (SQLAlchemy)
Supports SQLite locally (zero config) and PostgreSQL in Docker/Cloud via DATABASE_URL.

Since Phase 6 the schema lives in src/db/models.py (competitors, ads, media, media_info,
analyses, market_signals, jobs). This module keeps the engine/session and the names the rest
of the code already imports (AdRecord = Ad, AnalysisRecord = Analysis, save_or_update_ad_record,
get_ads_from_db, ...). A database still in the pre-Phase-6 layout is left untouched until
`python -m src.db.migrate` is run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import desc
from sqlalchemy.orm import selectinload, sessionmaker

from src.config import settings
from src.db.competitors import CompetitorIndex
from src.db.engine import DATA_DIR, add_missing_columns, default_database_url, is_legacy_schema, make_engine
from src.db.models import (
    STATUS_EVALUATED,
    STATUS_PENDING,
    Ad,
    Analysis,
    Base,
    Competitor,
    Job,
    MarketSignal,
    Media,
    MediaInfo,
    to_json,
    utcnow,
)
from src.db.util import normalize_rel_path, parse_dt

logger = logging.getLogger("Jinora.Database")

DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = default_database_url()
engine = make_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Backward-compatible names used across the code base
AdRecord = Ad
AnalysisRecord = Analysis

FORENSICS_MODEL = "faster-whisper"


def init_db() -> None:
    """Creates the Phase 6 tables. Never touches a database that still has the legacy layout."""
    if is_legacy_schema(engine):
        logger.warning(
            "La base tiene el esquema anterior a la Fase 6. Ejecuta: python -m src.db.migrate --dry-run "
            "y luego python -m src.db.migrate"
        )
        return
    Base.metadata.create_all(bind=engine)
    added = add_missing_columns(engine, Base.metadata)  # additive upgrades (e.g. Phase 7 jobs.result)
    if added:
        logger.info(f"Esquema actualizado, columnas añadidas: {', '.join(added)}")

    # Multi-company (src/db/tenancy.py): the cheap, additive part runs on every start; the table rebuild does not
    from src.db.tenancy import attach_to_default_project, needs_unique_rebuild

    with SessionLocal() as db:
        attached = attach_to_default_project(db)
        db.commit()
    if attached["competitors_attached"]:
        logger.info(f"{attached['competitors_attached']} competidores asignados al proyecto por defecto (Cosmiq)")
    if needs_unique_rebuild(engine):
        logger.warning("La tabla competitors aún tiene UNIQUE(name) global. Ejecuta: python -m src.db.tenancy --dry-run "
                       "y luego python -m src.db.tenancy")


init_db()


def normalize_to_relative_path(path_str: Optional[str]) -> Optional[str]:
    """Path relative to DOWNLOADS_DIR (forward slashes); None if it cannot be anchored there."""
    return normalize_rel_path(path_str)


# --------------------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------------------

def _upsert_media(db, ad: Ad, kind: str, rel_path: str) -> None:
    f = settings.downloads_path / rel_path
    size = f.stat().st_size if f.exists() else None
    for m in ad.media:
        if m.kind == kind:
            if m.rel_path != rel_path:
                m.rel_path, m.size, m.sha256 = rel_path, size, None
            return
    ad.media.append(Media(kind=kind, rel_path=rel_path, size=size))


def register_downloaded_media(db, ad: Ad, files: List[str]) -> int:
    """Creates/updates Media rows for downloaded files (.mp4 = video, .jpg = image). Returns how many were stored."""
    stored = 0
    for f in files:
        kind = "video" if str(f).endswith(".mp4") else ("image" if str(f).endswith(".jpg") else None)
        if kind is None:
            continue
        rel = normalize_rel_path(f)
        if rel is None:
            logger.warning(f"Ruta de media no relativizable, no se guarda: {f}")
            continue
        _upsert_media(db, ad, kind, rel)
        if kind == "video":
            ad.has_video = True
        stored += 1
    return stored


def save_forensics(db, ad: Ad, report: Dict[str, Any]) -> None:
    """Stores a local forensic (Whisper) report as an Analysis row + media duration; no columns."""
    payload = {
        "full_transcript": report.get("full_transcript"),
        "transcript_timeline": report.get("transcript_timeline") or [],
        "total_frames": report.get("total_frames_extracted", 0),
        "duration_seconds": report.get("duration_seconds"),
    }
    row = next((a for a in ad.analyses if a.analysis_type == "forensics" and a.model == FORENSICS_MODEL), None)
    if row is None:
        ad.analyses.append(Analysis(
            model=FORENSICS_MODEL, prompt_version="n/a", rubric_version="n/a", analysis_type="forensics",
            creative_score=None, result_json=json.dumps(payload, ensure_ascii=False),
        ))
    else:
        row.result_json = json.dumps(payload, ensure_ascii=False)
    if report.get("duration_seconds") is not None:
        if ad.media_info is None:
            ad.media_info = MediaInfo()
        ad.media_info.duration_s = report["duration_seconds"]


def save_or_update_ad_record(
    ad_data: Dict[str, Any],
    pillar: str = "general",
    search_query: str = "",
    status: Optional[str] = None,
    country: Optional[str] = None,
) -> Ad:
    """
    Insert or update ad (and its media / forensics) in the DB from a scraper dict.
    `status` applies only to NEW ads (existing ads keep theirs); `country` is merged into country_list.
    """
    db = SessionLocal()
    try:
        ad_id = str(ad_data.get("ad_id"))
        existing = db.get(Ad, ad_id)
        creatives = ad_data.get("creatives", [])
        c0 = creatives[0] if creatives else {}

        if existing is None:
            existing = Ad(id=ad_id, status=status or STATUS_PENDING)
            db.add(existing)

        existing.page_id = str(ad_data.get("page_id") or "") or None
        existing.page_name = ad_data.get("page_name") or "Desconocido"
        existing.start_date = parse_dt(ad_data.get("delivery_start_time"))
        existing.stop_date = parse_dt(ad_data.get("delivery_stop_time"))
        seen_in = set(json.loads(existing.country_list or "[]")) | set(ad_data.get("countries") or [])
        if country:
            seen_in.add(country.upper())
        if seen_in:
            existing.country_list = json.dumps(sorted(seen_in))
        existing.days_active = ad_data.get("days_active", 0)
        existing.is_active = ad_data.get("is_active", True)
        existing.has_video = any(c.get("has_video") for c in creatives) or existing.has_video or any(
            str(f).endswith(".mp4") for f in ad_data.get("downloaded_files", [])
        )
        existing.platforms = json.dumps(ad_data.get("publisher_platforms") or [])
        existing.headline = c0.get("title")
        existing.body_text = c0.get("body")
        existing.cta_text = c0.get("cta_text")
        existing.link_url = c0.get("link_url")
        existing.pillar = pillar
        existing.search_query = search_query

        competitor_id = CompetitorIndex(db.query(Competitor).all()).resolve(existing.page_id, existing.page_name)
        if competitor_id:
            existing.competitor_id = competitor_id

        register_downloaded_media(db, existing, ad_data.get("downloaded_files", []))

        if ad_data.get("forensics"):
            save_forensics(db, existing, ad_data["forensics"])

        db.commit()
        db.refresh(existing)
        return existing
    finally:
        db.close()


def ensure_ad(db, ad_dict: Dict[str, Any]) -> Ad:
    """Returns the Ad row for ad_dict, creating a minimal one if the ad is not stored yet (FK safety)."""
    ad_id = str(ad_dict.get("ad_id") or ad_dict.get("id"))
    ad = db.get(Ad, ad_id)
    if ad is None:
        creatives = ad_dict.get("creatives") or []
        c0 = creatives[0] if creatives else {}
        ad = Ad(
            id=ad_id,
            page_name=ad_dict.get("page_name"),
            headline=ad_dict.get("headline") or c0.get("title"),
            body_text=ad_dict.get("body_text") or c0.get("body"),
            cta_text=ad_dict.get("cta_text") or c0.get("cta_text"),
            link_url=ad_dict.get("link_url") or c0.get("link_url"),
            status=STATUS_PENDING,
        )
        db.add(ad)
        db.flush()
    return ad


def mark_evaluation_failed(db, ad_dict: Dict[str, Any], error: str) -> None:
    """Leaves the ad in pending_evaluation and records the failure as a Job (retry_after = now)."""
    ad = ensure_ad(db, ad_dict)
    ad.status = STATUS_PENDING
    payload = {"status": "pending_evaluation", "last_error": error, "failed_at": utcnow().isoformat(), "retry_after": utcnow().isoformat()}
    job = next((j for j in ad.jobs if j.type == "evaluate" and j.status == "pending"), None)
    if job is None:
        ad.jobs.append(Job(type="evaluate", status="pending", error=error, payload=to_json(payload)))
    else:
        job.error, job.payload = error, to_json(payload)
    db.commit()


def mark_evaluated(ad: Ad) -> None:
    """Sets status 'evaluated' and closes any pending evaluate job (caller commits)."""
    ad.status = STATUS_EVALUATED
    for job in ad.jobs:
        if job.type == "evaluate" and job.status == "pending":
            job.status, job.finished_at = "done", utcnow()


# --------------------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------------------

def get_ads_from_db(
    pillar: Optional[str] = None,
    min_days: int = 0,
    has_video_only: bool = False,
    limit: int = 50,
) -> List[Ad]:
    """Query ads with optional filters (relationships are eager-loaded; safe to use after return)."""
    db = SessionLocal()
    try:
        q = db.query(Ad).options(selectinload(Ad.media), selectinload(Ad.analyses), selectinload(Ad.media_info))
        if pillar and pillar != "all":
            q = q.filter(Ad.pillar == pillar)
        if min_days > 0:
            q = q.filter(Ad.days_active >= min_days)
        if has_video_only:
            q = q.filter(Ad.has_video.is_(True))
        return q.order_by(desc(Ad.days_active)).limit(limit).all()
    finally:
        db.close()
