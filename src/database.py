"""
Database Layer (SQLAlchemy)
Supports SQLite locally (zero config) and PostgreSQL in Docker/Cloud via DATABASE_URL.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
    desc,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_URL = f"sqlite:///{DATA_DIR / 'meta_ads.db'}"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_DB_URL)

# SQLite multi-thread compatibility
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class AdRecord(Base):
    """Archived Meta Advertisement Record."""

    __tablename__ = "ads"

    id = Column(String(64), primary_key=True, index=True)  # ad_id
    page_id = Column(String(64), index=True, nullable=True)
    page_name = Column(String(255), index=True, nullable=True)
    delivery_start_time = Column(String(64), nullable=True)
    days_active = Column(Integer, default=0, index=True)
    is_active = Column(Boolean, default=True)
    publisher_platforms = Column(Text, default="[]")  # JSON list
    headline = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    cta_text = Column(String(128), nullable=True)
    link_url = Column(Text, nullable=True)
    video_path = Column(Text, nullable=True)
    image_path = Column(Text, nullable=True)
    has_video = Column(Boolean, default=False)
    pillar = Column(String(64), default="general", index=True)
    search_query = Column(String(128), index=True, nullable=True)

    # Forensic Analysis Fields
    duration_seconds = Column(Float, nullable=True)
    full_transcript = Column(Text, nullable=True)
    transcript_timeline = Column(Text, nullable=True)  # JSON list
    total_frames = Column(Integer, default=0)
    cognitive_score = Column(Integer, nullable=True)
    score_breakdown = Column(Text, nullable=True)  # JSON dict

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AnalysisRecord(Base):
    """Archived Deep AI Evaluation Analysis (Video or Static)."""

    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_id = Column(String(64), index=True, nullable=False)
    model = Column(String(64), nullable=False)
    prompt_version = Column(String(32), default="v1")
    rubric_version = Column(String(32), default="v1")
    creative_hash = Column(String(128), index=True, nullable=True)
    analysis_type = Column(String(32), default="video")  # video / static
    creative_score = Column(Integer, nullable=False)
    result_json = Column(Text, nullable=False)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# Create tables
Base.metadata.create_all(bind=engine)


def normalize_to_relative_path(path_str: Optional[str]) -> Optional[str]:
    """Convert any full/absolute or messy path to relative path with respect to DOWNLOADS_DIR."""
    if not path_str:
        return None
    clean = str(path_str).replace("\\", "/").strip()
    if "/downloads/" in clean:
        return clean.split("/downloads/", 1)[1]
    if clean.startswith("downloads/"):
        return clean[len("downloads/"):]
    return clean


def save_or_update_ad_record(ad_data: Dict[str, Any], pillar: str = "general", search_query: str = "") -> AdRecord:
    """Insert or update ad record in DB from scraper dict."""
    db = SessionLocal()
    try:
        ad_id = str(ad_data.get("ad_id"))
        existing = db.query(AdRecord).filter(AdRecord.id == ad_id).first()

        creatives = ad_data.get("creatives", [])
        c0 = creatives[0] if creatives else {}

        # Check local files
        downloaded = ad_data.get("downloaded_files", [])
        v_path = next((f for f in downloaded if f.endswith(".mp4")), None)
        i_path = next((f for f in downloaded if f.endswith(".jpg")), None)

        forensics = ad_data.get("forensics") or {}

        if not existing:
            existing = AdRecord(id=ad_id)
            db.add(existing)

        existing.page_id = str(ad_data.get("page_id") or "")
        existing.page_name = ad_data.get("page_name") or "Desconocido"
        existing.delivery_start_time = ad_data.get("delivery_start_time")
        existing.days_active = ad_data.get("days_active", 0)
        existing.is_active = ad_data.get("is_active", True)
        existing.publisher_platforms = json.dumps(ad_data.get("publisher_platforms") or [])
        existing.headline = c0.get("title")
        existing.body_text = c0.get("body")
        existing.cta_text = c0.get("cta_text")
        existing.link_url = c0.get("link_url")
        existing.has_video = any(c.get("has_video") for c in creatives)
        existing.pillar = pillar
        existing.search_query = search_query

        if v_path:
            existing.video_path = normalize_to_relative_path(v_path)
        if i_path:
            existing.image_path = normalize_to_relative_path(i_path)

        if forensics:
            existing.duration_seconds = forensics.get("duration_seconds")
            existing.full_transcript = forensics.get("full_transcript")
            existing.transcript_timeline = json.dumps(forensics.get("transcript_timeline") or [])
            existing.total_frames = forensics.get("total_frames_extracted", 0)

        ev = ad_data.get("evaluation") or {}
        if ev:
            existing.cognitive_score = ev.get("total_score")
            existing.score_breakdown = json.dumps(ev)

        db.commit()
        db.refresh(existing)
        return existing
    finally:
        db.close()


def get_ads_from_db(
    pillar: Optional[str] = None,
    min_days: int = 0,
    has_video_only: bool = False,
    limit: int = 50,
) -> List[AdRecord]:
    """Query ads with optional filters."""
    db = SessionLocal()
    try:
        q = db.query(AdRecord)
        if pillar and pillar != "all":
            q = q.filter(AdRecord.pillar == pillar)
        if min_days > 0:
            q = q.filter(AdRecord.days_active >= min_days)
        if has_video_only:
            q = q.filter(AdRecord.has_video == True)
        
        q = q.order_by(desc(AdRecord.days_active))
        return q.limit(limit).all()
    finally:
        db.close()
