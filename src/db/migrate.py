"""
Phase 6 migration: flat 'ads' / 'analyses' tables -> normalized schema (src/db/models.py).

    python -m src.db.migrate --dry-run     # prints what it would do and the expected counts
    python -m src.db.migrate               # backup (SQLite), migrate, verify

Rules
- No ad is lost: every legacy ad becomes one row in 'ads'. Ads listed in data/rejected_ads.csv are
  kept with status 'rejected' and the reason in relevance_json.
- Media paths are stored relative to DOWNLOADS_DIR with forward slashes (rel_path); a path that is
  absolute on another machine and cannot be relativized is reported, never stored.
- Analyses are migrated only if produced with the CURRENT rubric (config/rubric*.yaml version).
  Older evaluations and the legacy heuristic cognitive_score are not migrated.
- Legacy tables are renamed to legacy_ads_phase5 / legacy_analyses_phase5 (nothing is dropped).
- Old Whisper transcripts become Analysis rows (analysis_type='forensics'), not columns.
- Idempotent: schema_meta.schema_version marks completion; re-running changes nothing, and an
  interrupted run resumes without duplicating rows.
- Portable: SQLAlchemy only; the backup step is SQLite-specific and skipped for other databases.
- Then the multi-company step (src/db/tenancy.py) runs: companies / projects, every competitor in the
  default Cosmiq project, UNIQUE(project_id, name). A database already at schema_version 6 only gets this step.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import inspect, or_, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.db.competitors import (
    CompetitorIndex,
    competitor_from_row,
    load_competitors_config,
)
from src.db.engine import (
    DATA_DIR,
    add_missing_columns,
    LEGACY_ADS_TABLE,
    LEGACY_ANALYSES_TABLE,
    SCHEMA_VERSION,
    column_names,
    default_database_url,
    has_table,
    is_legacy_schema,
    make_engine,
    schema_version,
)
from src.db.util import normalize_rel_path, parse_dt as _parse_dt
from src.db.models import (
    STATUS_EVALUATED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Ad,
    Analysis,
    Base,
    Competitor,
    MarketSignal,
    Media,
    MediaInfo,
    SchemaMeta,
    from_json,
    get_default_project,
    to_json,
    utcnow,
)

logger = logging.getLogger("Jinora.Migrate")

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
REJECTED_CSV = DATA_DIR / "rejected_ads.csv"
RUBRIC_VIDEO = ROOT_DIR / "config" / "rubric.yaml"
RUBRIC_STATIC = ROOT_DIR / "config" / "rubric_static.yaml"
FORENSICS_MODEL = "faster-whisper (legacy)"


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def current_rubric_versions() -> Dict[str, str]:
    """Rubric version of the evaluators that are active now (video: rubric.yaml, static: rubric_static.yaml)."""
    versions = {}
    for kind, path in (("video", RUBRIC_VIDEO), ("static", RUBRIC_STATIC)):
        try:
            versions[kind] = str(yaml.safe_load(path.read_text(encoding="utf-8")).get("version", ""))
        except Exception:
            versions[kind] = ""
    return versions


def load_rejected(csv_path: Path = REJECTED_CSV) -> Dict[str, str]:
    """ad_id -> reason from data/rejected_ads.csv (empty if the file does not exist)."""
    if not Path(csv_path).exists():
        return {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        return {str(r["ad_id"]).strip(): (r.get("motivo") or "").strip() for r in csv.DictReader(f) if r.get("ad_id")}


def table_counts(engine: Engine) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    with engine.connect() as conn:
        for name in sorted(inspect(engine).get_table_names()):
            if name.startswith("sqlite_"):
                continue
            counts[name] = int(conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar() or 0)
    return counts


def _read_table(engine: Engine, table: str) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f'SELECT * FROM "{table}"')).mappings().all()]


# --------------------------------------------------------------------------------------
# Backup (SQLite only)
# --------------------------------------------------------------------------------------

def backup_sqlite(engine: Engine, backup_dir: Optional[Path] = None, label: str = "phase6") -> Optional[Path]:
    """
    Copies the SQLite file to <data>/backup/<name>_before_<label>_<YYYYMMDD>.db and verifies that
    the copy opens, passes integrity_check and has the same row count in every table.
    Returns None for non-SQLite databases (use pg_dump there).
    """
    if engine.url.get_backend_name() != "sqlite":
        logger.warning("Backup automático solo para SQLite; haz un pg_dump antes de migrar PostgreSQL.")
        return None
    db_file = engine.url.database
    if not db_file or db_file == ":memory:" or not Path(db_file).exists():
        return None

    bdir = Path(backup_dir) if backup_dir else Path(db_file).parent / "backup"
    bdir.mkdir(parents=True, exist_ok=True)
    target = bdir / f"{Path(db_file).stem}_before_{label}_{datetime.now():%Y%m%d}.db"
    if target.exists():
        target = bdir / f"{Path(db_file).stem}_before_{label}_{datetime.now():%Y%m%d_%H%M%S}.db"

    src = sqlite3.connect(db_file)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            a = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            b = dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            if a != b:
                raise RuntimeError(f"Backup inválido: tabla {t} tiene {a} filas y el respaldo {b}")
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup inválido: integrity_check falló")
    finally:
        dst.close()
        src.close()
    return target


# --------------------------------------------------------------------------------------
# Plan (pure: builds the rows to write; the same plan feeds --dry-run and the real run)
# --------------------------------------------------------------------------------------

@dataclass
class Plan:
    ads: List[Dict[str, Any]] = field(default_factory=list)
    media: List[Dict[str, Any]] = field(default_factory=list)
    media_info: List[Dict[str, Any]] = field(default_factory=list)
    analyses: List[Dict[str, Any]] = field(default_factory=list)
    market_signals: List[Dict[str, Any]] = field(default_factory=list)
    discarded: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)

    def discard(self, table: str, row_id: Any, reason: str) -> None:
        self.discarded.append({"table": table, "id": str(row_id), "reason": reason})


def _file_details(rel_path: str, downloads_dir: Path, compute: bool, known_sha: Optional[str]) -> Tuple[Optional[int], Optional[str], bool]:
    """(size, sha256, exists). Hashing only when compute=True (never in dry-run)."""
    f = downloads_dir / rel_path
    if not f.exists():
        return None, None, False
    size = f.stat().st_size
    if known_sha or not compute:
        return size, known_sha, True
    from src.media.fingerprint import compute_file_sha256

    return size, compute_file_sha256(f), True


def _media_probe(kind: str, f: Path) -> Dict[str, Any]:
    """Local container metadata (pymediainfo / Pillow); {} when unavailable. Never raises."""
    try:
        from src.media.media_info import get_image_info, get_video_info

        return get_video_info(f) if kind == "video" else get_image_info(f)
    except Exception as exc:  # missing libmediainfo, corrupt file...
        logger.debug(f"Sin metadata de {f.name}: {exc}")
        return {}


def build_plan(
    legacy_ads: List[Dict[str, Any]],
    legacy_analyses: List[Dict[str, Any]],
    competitor_index: CompetitorIndex,
    rejected: Dict[str, str],
    downloads_dir: Path,
    rubric_versions: Dict[str, str],
    details: bool,
    weights: Tuple[float, float],
) -> Plan:
    from src.scoring.jinora import compute_jinora_score

    plan = Plan()
    legacy_ids = {str(a["id"]) for a in legacy_ads}

    # 1. Analyses: keep only the current rubric; archive the rest
    kept_scores: Dict[str, int] = {}
    for a in sorted(legacy_analyses, key=lambda r: r["id"]):
        ad_id = str(a["ad_id"])
        a_type = a.get("analysis_type") or "video"
        current = rubric_versions.get(a_type, "")
        if ad_id not in legacy_ids:
            plan.discard("analyses", a["id"], "anuncio inexistente en ads")
        elif not current or str(a.get("rubric_version")) != current:
            plan.discard("analyses", a["id"], f"evaluación previa a la rúbrica {current or '?'} (rubric_version={a.get('rubric_version')})")
        elif a.get("creative_score") is None:
            plan.discard("analyses", a["id"], "sin creative_score")
        else:
            plan.analyses.append({
                "id": a["id"], "ad_id": ad_id, "model": a["model"],
                "prompt_version": a.get("prompt_version"), "rubric_version": a.get("rubric_version"),
                "creative_hash": a.get("creative_hash"), "analysis_type": a_type,
                "creative_score": int(a["creative_score"]), "result_json": a["result_json"],
                "cost_usd": a.get("cost_usd") or 0.0, "created_at": _parse_dt(a.get("created_at")) or utcnow(),
            })
            kept_scores[ad_id] = int(a["creative_score"])  # ordered by id: the latest wins

    # 2. Ads, media, media_info, market signals, forensic transcripts
    stats = dict.fromkeys([
        "ads_sin_competitor", "media_sin_archivo", "anuncios_video_sin_media", "rutas_no_relativizables",
        "cognitive_score_descartado", "transcripciones_migradas", "anuncios_rechazados", "competitor_asignado",
    ], 0)
    old_analysis_ads = {str(a["ad_id"]) for a in legacy_analyses}

    for r in legacy_ads:
        ad_id = str(r["id"])
        competitor_id = competitor_index.resolve(r.get("page_id"), r.get("page_name"))
        stats["competitor_asignado" if competitor_id else "ads_sin_competitor"] += 1

        if ad_id in rejected:
            status, relevance = STATUS_REJECTED, to_json({"is_relevant": False, "reason": rejected[ad_id], "source": "data/rejected_ads.csv"})
            stats["anuncios_rechazados"] += 1
        else:
            status, relevance = (STATUS_EVALUATED if ad_id in kept_scores else STATUS_PENDING), None
        if r.get("cognitive_score") is not None:
            stats["cognitive_score_descartado"] += 1

        platforms = r.get("publisher_platforms") or "[]"
        if from_json(platforms, None) is None:
            platforms = "[]"
        plan.ads.append({
            "id": ad_id, "competitor_id": competitor_id, "page_id": (str(r["page_id"]) if r.get("page_id") else None),
            "page_name": r.get("page_name"), "country_list": r.get("countries") or None,
            "start_date": _parse_dt(r.get("delivery_start_time")), "stop_date": _parse_dt(r.get("delivery_stop_time")),
            "days_active": int(r.get("days_active") or 0), "is_active": bool(r.get("is_active") if r.get("is_active") is not None else True),
            "has_video": bool(r.get("has_video") or r.get("video_path")),
            "platforms": platforms, "headline": r.get("headline"), "body_text": r.get("body_text"),
            "cta_text": r.get("cta_text"), "link_url": r.get("link_url"), "pillar": r.get("pillar") or "general",
            "search_query": r.get("search_query"), "copy_hash": r.get("copy_hash"), "creative_hash": r.get("creative_hash"),
            "creative_group_id": r.get("creative_group_id"), "variant_count": r.get("variant_count"),
            "relevance_json": relevance, "status": status,
            "created_at": _parse_dt(r.get("created_at")) or utcnow(),
            "updated_at": _parse_dt(r.get("updated_at")) or utcnow(),
        })

        # media: one row per file, rel_path relative to DOWNLOADS_DIR
        kinds: Dict[str, str] = {}
        for kind, col in (("video", "video_path"), ("image", "image_path")):
            if not r.get(col):
                continue
            rel = normalize_rel_path(r[col])
            if rel is None:
                stats["rutas_no_relativizables"] += 1
                plan.discard("media", f"{ad_id}:{kind}", f"ruta no relativizable: {r[col]}")
                continue
            kinds[kind] = rel
        if r.get("has_video") and "video" not in kinds:
            stats["anuncios_video_sin_media"] += 1

        primary = "video" if "video" in kinds else ("image" if "image" in kinds else None)
        primary_sha = r.get("media_sha256")
        for kind, rel in kinds.items():
            size, sha, exists = _file_details(rel, downloads_dir, details, primary_sha if kind == primary else None)
            if not exists:
                stats["media_sin_archivo"] += 1
            plan.media.append({"ad_id": ad_id, "kind": kind, "rel_path": rel, "size": size, "sha256": sha})

        if primary or r.get("phash") or r.get("duration_seconds") is not None:
            info: Dict[str, Any] = {
                "ad_id": ad_id, "duration_s": r.get("duration_seconds"), "width": None, "height": None,
                "aspect_ratio": None, "has_audio_track": None,
                "sha256": next((m["sha256"] for m in plan.media if m["ad_id"] == ad_id and m["kind"] == primary), None) or primary_sha,
                "phash": r.get("phash"),
            }
            if details and primary and (downloads_dir / kinds[primary]).exists():
                probe = _media_probe(primary, downloads_dir / kinds[primary])
                info["width"], info["height"] = probe.get("width"), probe.get("height")
                info["aspect_ratio"] = probe.get("aspect_ratio")
                if primary == "video":
                    info["has_audio_track"] = probe.get("has_audio_track")
                    if info["duration_s"] is None:
                        info["duration_s"] = probe.get("duration_s")
            plan.media_info.append(info)

        # market signal (Phase 5); jinora_score recomputed from the analyses that survive
        if r.get("market_score") is not None or r.get("market_components"):
            ms = r.get("market_score")
            plan.market_signals.append({
                "ad_id": ad_id, "market_score": ms, "market_components": r.get("market_components"),
                "jinora_score": compute_jinora_score(kept_scores.get(ad_id), ms, weights),
                "updated_at": _parse_dt(r.get("updated_at")) or utcnow(),
            })

        # legacy Whisper transcript -> Analysis(forensics), not a column
        if (r.get("full_transcript") or "").strip():
            stats["transcripciones_migradas"] += 1
            plan.analyses.append({
                "id": None, "ad_id": ad_id, "model": FORENSICS_MODEL, "prompt_version": "n/a", "rubric_version": "n/a",
                "creative_hash": None, "analysis_type": "forensics", "creative_score": None,
                "result_json": json.dumps({
                    "full_transcript": r["full_transcript"],
                    "transcript_timeline": from_json(r.get("transcript_timeline"), []),
                    "total_frames": r.get("total_frames") or 0,
                    "duration_seconds": r.get("duration_seconds"),
                    "source": "legacy ads.full_transcript"
                              + ("; anuncio evaluado con Gemini antes del 21-oct: puede contener texto de Gemini" if ad_id in old_analysis_ads else ""),
                }, ensure_ascii=False),
                "cost_usd": 0.0, "created_at": _parse_dt(r.get("updated_at")) or utcnow(),
            })

    plan.stats = stats
    return plan


# --------------------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------------------

def _insert_missing(session: Session, model: Any, rows: List[Dict[str, Any]], keyfunc, existing: set) -> int:
    new_rows = [r for r in rows if keyfunc(r) not in existing]
    if new_rows:
        session.bulk_insert_mappings(model, new_rows)
    return len(new_rows)


def upsert_competitors(session: Session, config_path: Optional[Path] = None) -> int:
    """Mirrors config/competitors_database.json into the default project's competitors (by name). Returns row count."""
    rows = load_competitors_config(config_path)
    project = get_default_project(session)
    session.flush()
    scope = or_(Competitor.project_id == project.id, Competitor.project_id.is_(None))
    existing = {c.name: c for c in session.query(Competitor).filter(scope).all()}
    for row in rows:
        fresh = competitor_from_row(row)
        current = existing.get(row["name"])
        if current is None:
            fresh.project = project
            session.add(fresh)
        else:
            for col in ("website", "pillar", "priority", "markets", "page_ids", "meta_json"):
                setattr(current, col, getattr(fresh, col))
    session.flush()
    return len(rows)


def _drop_indexes_and_rename(engine: Engine) -> None:
    insp = inspect(engine)
    renames = [("ads", LEGACY_ADS_TABLE)]
    if insp.has_table("analyses"):
        renames.append(("analyses", LEGACY_ANALYSES_TABLE))
    with engine.begin() as conn:
        for old, new in renames:
            for idx in insp.get_indexes(old):
                conn.execute(text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
            conn.execute(text(f'ALTER TABLE "{old}" RENAME TO "{new}"'))


def migrate(
    engine: Engine,
    dry_run: bool = False,
    backup: bool = True,
    backup_dir: Optional[Path] = None,
    competitors_path: Optional[Path] = None,
    rejected_csv: Optional[Path] = None,
    downloads_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Runs (or simulates) the migration. Returns a report dict; see format_report()."""
    from src.config import settings
    from src.scoring.jinora import get_score_weights

    downloads = Path(downloads_dir) if downloads_dir else settings.downloads_path
    rejected = load_rejected(rejected_csv or REJECTED_CSV)
    rubrics = current_rubric_versions()
    weights = get_score_weights()

    report: Dict[str, Any] = {"dry_run": dry_run, "rubric_versions": rubrics, "before": table_counts(engine)}

    if schema_version(engine) == SCHEMA_VERSION:
        report.update(status="already_migrated", after=report["before"], plan_counts={}, discarded=[], stats={})
        if not dry_run:
            _tenancy_step(engine, report, backup, backup_dir)
        return report

    legacy_pending = has_table(engine, LEGACY_ADS_TABLE)  # interrupted earlier run
    if is_legacy_schema(engine):
        ads_table, analyses_table = "ads", "analyses"
    elif legacy_pending:
        ads_table, analyses_table = LEGACY_ADS_TABLE, LEGACY_ANALYSES_TABLE
    else:
        ads_table = None  # empty / brand-new database: only create the schema

    legacy_ads = _read_table(engine, ads_table) if ads_table else []
    legacy_analyses = _read_table(engine, analyses_table) if ads_table and has_table(engine, analyses_table) else []

    if dry_run:
        # In-memory competitor ids for the simulation only (nothing is written)
        simulated = [competitor_from_row(r) for r in load_competitors_config(competitors_path)]
        for i, c in enumerate(simulated, 1):
            c.id = i
        plan = build_plan(legacy_ads, legacy_analyses, CompetitorIndex(simulated), rejected, downloads, rubrics, False, weights)
        report.update(status="dry_run", plan_counts=_plan_counts(plan, len(simulated)), discarded=plan.discarded, stats=plan.stats,
                      expected_after=_expected_after(report["before"], plan, len(simulated), ads_table is not None))
        return report

    if backup:
        backup_file = backup_sqlite(engine, backup_dir)
        report["backup_path"] = str(backup_file) if backup_file else None

    if ads_table == "ads":  # legacy layout still in place: set it aside before creating the new tables
        _drop_indexes_and_rename(engine)
    Base.metadata.create_all(bind=engine)
    add_missing_columns(engine, Base.metadata)  # e.g. competitors.project_id on a schema older than this code

    with Session(engine) as session:
        n_comp = upsert_competitors(session, competitors_path)
        index = CompetitorIndex(session.query(Competitor).all())
        plan = build_plan(legacy_ads if ads_table else [], legacy_analyses, index, rejected, downloads, rubrics, True, weights)

        existing_ads = {r[0] for r in session.query(Ad.id)}
        _insert_missing(session, Ad, plan.ads, lambda r: r["id"], existing_ads)
        session.flush()
        existing_media = {(m.ad_id, m.kind, m.rel_path) for m in session.query(Media)}
        _insert_missing(session, Media, plan.media, lambda r: (r["ad_id"], r["kind"], r["rel_path"]), existing_media)
        _insert_missing(session, MediaInfo, plan.media_info, lambda r: r["ad_id"], {r[0] for r in session.query(MediaInfo.ad_id)})
        _insert_missing(session, MarketSignal, plan.market_signals, lambda r: r["ad_id"], {r[0] for r in session.query(MarketSignal.ad_id)})

        existing_analysis_ids = {r[0] for r in session.query(Analysis.id)}
        forensic_ads = {r[0] for r in session.query(Analysis.ad_id).filter(Analysis.analysis_type == "forensics")}
        scored = [a for a in plan.analyses if a["id"] is not None]
        forensic = [a for a in plan.analyses if a["id"] is None]
        _insert_missing(session, Analysis, scored, lambda r: r["id"], existing_analysis_ids)
        _insert_missing(session, Analysis, [{k: v for k, v in a.items() if k != "id"} for a in forensic], lambda r: r["ad_id"], forensic_ads)

        for key, value in (("schema_version", SCHEMA_VERSION), ("phase6_migrated_at", utcnow().isoformat())):
            row = session.get(SchemaMeta, key)
            if row is None:
                session.add(SchemaMeta(key=key, value=value))
            else:
                row.value = value
        session.commit()

    _tenancy_step(engine, report, backup=False, backup_dir=None)  # the Phase 6 backup above already covers it
    report.update(status="migrated", after=table_counts(engine), plan_counts=_plan_counts(plan, n_comp), discarded=plan.discarded, stats=plan.stats)
    report["integrity"] = check_integrity(engine)
    return report


def _tenancy_step(engine: Engine, report: Dict[str, Any], backup: bool, backup_dir: Optional[Path]) -> None:
    """Multi-company step (src/db/tenancy.py); backs up first only when there is something to change."""
    from src.db.tenancy import apply_tenancy, plan_tenancy

    plan = plan_tenancy(engine)
    pending = plan["create_tables"] or plan["add_columns"] or plan["competitors_to_attach"] or plan["rebuild_unique"] \
        or not plan["default_project_exists"] or plan["default_profile_missing"]
    if not pending:
        return
    if backup:
        backup_file = backup_sqlite(engine, backup_dir, label="multiempresa")
        report["backup_path"] = str(backup_file) if backup_file else None
    report["tenancy"] = apply_tenancy(engine)
    if report["status"] == "already_migrated":
        report["after"] = table_counts(engine)


def _plan_counts(plan: Plan, n_competitors: int) -> Dict[str, int]:
    return {
        "competitors": n_competitors, "ads": len(plan.ads), "media": len(plan.media), "media_info": len(plan.media_info),
        "analyses (evaluaciones)": sum(1 for a in plan.analyses if a["analysis_type"] != "forensics"),
        "analyses (forensics)": sum(1 for a in plan.analyses if a["analysis_type"] == "forensics"),
        "market_signals": len(plan.market_signals), "jobs": 0,
    }


def _expected_after(before: Dict[str, int], plan: Plan, n_competitors: int, has_legacy: bool) -> Dict[str, int]:
    pc = _plan_counts(plan, n_competitors)
    expected = {k: v for k, v in before.items() if k in ("llm_usage", "llm_attempts")}
    expected.update({
        "competitors": pc["competitors"], "ads": pc["ads"], "media": pc["media"], "media_info": pc["media_info"],
        "analyses": pc["analyses (evaluaciones)"] + pc["analyses (forensics)"], "market_signals": pc["market_signals"],
        "jobs": 0, "schema_meta": 2,
    })
    if has_legacy:
        expected[LEGACY_ADS_TABLE] = before.get("ads", 0)
        expected[LEGACY_ANALYSES_TABLE] = before.get("analyses", 0)
    return expected


def check_integrity(engine: Engine) -> Dict[str, Any]:
    """Foreign-key and orphan checks on the migrated schema (portable queries)."""
    checks = {
        "media_huerfanas": "SELECT COUNT(*) FROM media WHERE ad_id NOT IN (SELECT id FROM ads)",
        "analyses_huerfanas": "SELECT COUNT(*) FROM analyses WHERE ad_id NOT IN (SELECT id FROM ads)",
        "media_info_huerfanas": "SELECT COUNT(*) FROM media_info WHERE ad_id NOT IN (SELECT id FROM ads)",
        "market_signals_huerfanas": "SELECT COUNT(*) FROM market_signals WHERE ad_id NOT IN (SELECT id FROM ads)",
        "ads_competitor_invalido": "SELECT COUNT(*) FROM ads WHERE competitor_id IS NOT NULL AND competitor_id NOT IN (SELECT id FROM competitors)",
    }
    out: Dict[str, Any] = {}
    with engine.connect() as conn:
        for name, sql in checks.items():
            out[name] = int(conn.execute(text(sql)).scalar() or 0)
        if engine.url.get_backend_name() == "sqlite":
            out["foreign_key_check"] = len(conn.execute(text("PRAGMA foreign_key_check")).fetchall())
    out["ok"] = all(v == 0 for v in out.values())
    return out


# --------------------------------------------------------------------------------------
# Report / CLI
# --------------------------------------------------------------------------------------

def _tenancy_summary(t: Dict[str, Any]) -> str:
    return (f"proyecto por defecto id={t['default_project_id']}, competidores asignados={t['competitors_attached']}, "
            f"tabla reconstruida={'sí' if t['unique_rebuilt'] else 'no'}, verificación ok={t['verify']['ok']}")


def format_report(report: Dict[str, Any]) -> str:
    lines = ["=" * 78, f"MIGRACIÓN FASE 6 — {report['status'].upper()}", "=" * 78]
    if report["status"] == "already_migrated":
        if report.get("tenancy"):
            lines.append("La base ya estaba en la Fase 6; se aplicó solo el paso multiempresa.")
            if report.get("backup_path"):
                lines.append(f"Backup verificado: {report['backup_path']}")
            lines.append(f"Multiempresa: {_tenancy_summary(report['tenancy'])}")
        else:
            lines.append("La base ya está en el esquema de la Fase 6 (schema_version=6). No se cambió nada.")
        lines += ["", f"{'tabla':<26}{'filas':>8}"] + [f"{t:<26}{n:>8}" for t, n in report["before"].items()]
        return "\n".join(lines)

    if report.get("backup_path"):
        lines.append(f"Backup verificado: {report['backup_path']}")
    after = report.get("after") or report.get("expected_after", {})
    label = "filas esperadas" if report["dry_run"] else "filas después"
    lines += ["", f"{'tabla':<26}{'filas antes':>12}{label:>18}"]
    for t in sorted(set(report["before"]) | set(after)):
        lines.append(f"{t:<26}{report['before'].get(t, 0):>12}{after.get(t, 0):>18}")
    lines += ["", "Detalle del plan:"] + [f"  {k}: {v}" for k, v in report["plan_counts"].items()]
    lines += ["", "Verificaciones:"] + [f"  {k}: {v}" for k, v in report["stats"].items()]
    if "integrity" in report:
        lines.append(f"  integridad (FK/huérfanas): {report['integrity']}")
    if report.get("tenancy"):
        lines.append(f"  multiempresa: {_tenancy_summary(report['tenancy'])}")
    discarded = report.get("discarded", [])
    lines += ["", f"Filas descartadas (no migradas, conservadas en las tablas legacy): {len(discarded)}"]
    reasons: Dict[Tuple[str, str], List[str]] = {}
    for d in discarded:
        reasons.setdefault((d["table"], d["reason"]), []).append(d["id"])
    for (table, reason), ids in reasons.items():
        lines.append(f"  - {table}: {len(ids)} × {reason} (ids: {', '.join(ids[:12])}{'…' if len(ids) > 12 else ''})")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Migración Fase 6: ads/analyses planos -> esquema normalizado")
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime lo que haría y los conteos esperados")
    parser.add_argument("--no-backup", action="store_true", help="No crear backup (SQLite)")
    parser.add_argument("--db-url", default=None, help="URL SQLAlchemy (por defecto DATABASE_URL o data/meta_ads.db)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = make_engine(args.db_url or default_database_url())
    report = migrate(engine, dry_run=args.dry_run, backup=not args.no_backup)
    print(format_report(report))
    ok = report.get("integrity", {"ok": True})["ok"] and report.get("tenancy", {"verify": {"ok": True}})["verify"]["ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
