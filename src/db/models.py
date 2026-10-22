"""
Jinora Data Model (Phase 6)
Normalized SQLAlchemy schema, portable between SQLite (default) and PostgreSQL (DATABASE_URL):
no SQLite-only types or functions. Dates are naive UTC datetimes.

  Competitor 1-N Ad 1-N Media / 1-1 MediaInfo / 1-N Analysis / 1-1 MarketSignal / 1-N Job

Transcript, has_voiceover, language and scene_cuts_estimate live inside Analysis.result_json,
never in columns. llm_usage / llm_attempts (quota audit) are not modeled here: QuotaGuard
owns them and they are left exactly as they are.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship, synonym

Base = declarative_base()

# Ad.status: how far an ad got in the pipeline (src/pipeline.py). Each stage only moves ads forward.
#   discovered -> filtered -> downloaded -> pending_evaluation -> evaluated -> scored
STATUS_DISCOVERED = "discovered"  # saved from Meta, relevance not decided yet
STATUS_FILTERED = "filtered"  # relevant and in the catalog; waiting for its media
STATUS_DOWNLOADED = "downloaded"  # media files on disk
STATUS_PENDING = "pending_evaluation"  # media ready: this is the evaluation queue (also after a deferral/failure)
STATUS_EVALUATED = "evaluated"  # has a creative_score
STATUS_SCORED = "scored"  # market signal and jinora_score computed
# Terminal states, never queued and never deleted
STATUS_REJECTED = "rejected"  # judged irrelevant (relevance filter / data/rejected_ads.csv)
STATUS_OUT_OF_CATALOG = "out_of_catalog"  # page is not one of the catalogued competitors
STATUS_NEEDS_REVIEW = "needs_manual_review"  # doubtful relevance: a person decides
STATUS_INACTIVE_DATA_ONLY = "inactive_data_only"  # inactive <= 60 days: data only, no LLM

EXCLUDED_FROM_QUEUE = (STATUS_REJECTED, STATUS_OUT_OF_CATALOG)

# Analysis.analysis_type values that carry a creative_score
SCORED_ANALYSIS_TYPES = ("video", "static")


def utcnow() -> datetime:
    """Naive UTC now (columns are naive UTC by convention)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_json(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def from_json(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class SchemaMeta(Base):
    """Key/value store: schema_version marks a completed Phase 6 migration."""

    __tablename__ = "schema_meta"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)


class Competitor(Base):
    __tablename__ = "competitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    website = Column(String(512), nullable=True)
    pillar = Column(String(64), nullable=True)
    priority = Column(Integer, nullable=False, default=2)
    markets = Column(Text, nullable=True)  # JSON list of ISO country codes
    page_ids = Column(Text, nullable=True)  # JSON list of Meta page ids
    meta_json = Column(Text, nullable=True)  # region, focus, notes, resolution_confidence, ...

    ads = relationship("Ad", back_populates="competitor")

    __table_args__ = (
        Index("ix_competitors_pillar", "pillar"),
        Index("ix_competitors_priority", "priority"),
    )


class Ad(Base):
    __tablename__ = "ads"

    id = Column(String(64), primary_key=True)  # Meta ad_id
    competitor_id = Column(Integer, ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True)
    page_id = Column(String(64), nullable=True)
    page_name = Column(String(255), nullable=True)
    country_list = Column(Text, nullable=True)  # JSON list; NULL = unknown (never invented)
    start_date = Column(DateTime, nullable=True)
    stop_date = Column(DateTime, nullable=True)
    days_active = Column(Integer, nullable=False, default=0)  # cached by the scraper; see start/stop
    is_active = Column(Boolean, nullable=False, default=True)
    # The creative contains a video, even if the file was never downloaded (then there is no Media row)
    has_video = Column(Boolean, nullable=False, default=False)
    platforms = Column(Text, nullable=False, default="[]")  # JSON list (FACEBOOK, INSTAGRAM, ...)
    headline = Column(Text, nullable=True)
    body_text = Column(Text, nullable=True)
    cta_text = Column(String(128), nullable=True)
    link_url = Column(Text, nullable=True)
    pillar = Column(String(64), nullable=False, default="general")
    search_query = Column(String(128), nullable=True)
    copy_hash = Column(String(64), nullable=True)
    creative_hash = Column(String(64), nullable=True)
    creative_group_id = Column(String(64), nullable=True)
    variant_count = Column(Integer, nullable=True)
    relevance_json = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default=STATUS_PENDING)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    competitor = relationship("Competitor", back_populates="ads")
    media = relationship("Media", back_populates="ad", cascade="all, delete-orphan", order_by="Media.id")
    media_info = relationship("MediaInfo", back_populates="ad", uselist=False, cascade="all, delete-orphan")
    analyses = relationship("Analysis", back_populates="ad", cascade="all, delete-orphan", order_by="Analysis.id")
    market_signal = relationship("MarketSignal", back_populates="ad", uselist=False, cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="ad", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_ads_competitor_id", "competitor_id"),
        Index("ix_ads_page_id", "page_id"),
        Index("ix_ads_page_name", "page_name"),
        Index("ix_ads_days_active", "days_active"),
        Index("ix_ads_pillar", "pillar"),
        Index("ix_ads_search_query", "search_query"),
        Index("ix_ads_creative_hash", "creative_hash"),
        Index("ix_ads_creative_group_id", "creative_group_id"),
        Index("ix_ads_status", "status"),
    )

    # --- read-only conveniences so existing callers keep working -------------------------

    def _media_path(self, kind: str) -> Optional[str]:
        for m in self.media:
            if m.kind == kind:
                return m.rel_path
        return None

    @property
    def video_path(self) -> Optional[str]:
        """Path relative to DOWNLOADS_DIR (same convention as before Phase 6)."""
        return self._media_path("video")

    @property
    def image_path(self) -> Optional[str]:
        return self._media_path("image")

    @property
    def publisher_platforms(self) -> str:
        return self.platforms

    def _forensics(self) -> Dict[str, Any]:
        rows = [a for a in self.analyses if a.analysis_type == "forensics"]
        return from_json(rows[-1].result_json, {}) if rows else {}

    @property
    def full_transcript(self) -> Optional[str]:
        return self._forensics().get("full_transcript") or None

    @property
    def transcript_timeline(self) -> Optional[str]:
        timeline = self._forensics().get("transcript_timeline")
        return None if timeline is None else json.dumps(timeline, ensure_ascii=False)

    @property
    def total_frames(self) -> int:
        return int(self._forensics().get("total_frames") or 0)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.media_info is not None and self.media_info.duration_s is not None:
            return self.media_info.duration_s
        return self._forensics().get("duration_seconds")


class Media(Base):
    __tablename__ = "media"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_id = Column(String(64), ForeignKey("ads.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(16), nullable=False)  # video | image
    rel_path = Column(Text, nullable=False)  # relative to DOWNLOADS_DIR, forward slashes
    size = Column(BigInteger, nullable=True)  # bytes; NULL if the file is not on disk
    sha256 = Column(String(64), nullable=True)

    ad = relationship("Ad", back_populates="media")

    __table_args__ = (
        UniqueConstraint("ad_id", "kind", "rel_path", name="uq_media_ad_kind_path"),
        Index("ix_media_ad_id", "ad_id"),
        Index("ix_media_sha256", "sha256"),
    )


class MediaInfo(Base):
    __tablename__ = "media_info"

    ad_id = Column(String(64), ForeignKey("ads.id", ondelete="CASCADE"), primary_key=True)
    duration_s = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    aspect_ratio = Column(String(16), nullable=True)
    has_audio_track = Column(Boolean, nullable=True)
    sha256 = Column(String(64), nullable=True)  # exact file hash of the primary media (video, else image)
    phash = Column(String(32), nullable=True)  # perceptual hash of the thumbnail/image

    ad = relationship("Ad", back_populates="media_info")

    __table_args__ = (
        Index("ix_media_info_sha256", "sha256"),
        Index("ix_media_info_phash", "phash"),
    )


class Analysis(Base):
    """AI evaluation (Phase 4) or forensic transcription. transcript/language/... live in result_json."""

    __tablename__ = "analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_id = Column(String(64), ForeignKey("ads.id", ondelete="CASCADE"), nullable=False)
    model = Column(String(64), nullable=False)
    prompt_version = Column(String(32), default="v1")
    rubric_version = Column(String(32), default="v1")
    creative_hash = Column(String(128), nullable=True)
    analysis_type = Column(String(32), nullable=False, default="video")  # video | static | forensics
    creative_score = Column(Integer, nullable=True)  # NULL only for non-scored types (forensics)
    result_json = Column(Text, nullable=False)
    cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    ad = relationship("Ad", back_populates="analyses")

    __table_args__ = (
        Index("ix_analyses_ad_id", "ad_id"),
        Index("ix_analyses_creative_hash", "creative_hash"),
        Index("ix_analyses_cache", "ad_id", "model", "prompt_version"),
    )


class MarketSignal(Base):
    __tablename__ = "market_signals"

    ad_id = Column(String(64), ForeignKey("ads.id", ondelete="CASCADE"), primary_key=True)
    market_score = Column(Float, nullable=True)
    market_components = Column(Text, nullable=True)  # JSON: components used/missing (Phase 5)
    breakdown_json = synonym("market_components")
    jinora_score = Column(Float, nullable=True)  # NULL until the ad has a creative_score
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    ad = relationship("Ad", back_populates="market_signal")

    __table_args__ = (Index("ix_market_signals_market_score", "market_score"),)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(32), nullable=False)  # run | evaluate | forensics (worker) | download (pipeline internal)
    ad_id = Column(String(64), ForeignKey("ads.id", ondelete="CASCADE"), nullable=True)
    payload = Column(Text, nullable=True)  # JSON
    status = Column(String(32), nullable=False, default="pending")  # pending | running | done | failed
    error = Column(Text, nullable=True)
    result = Column(Text, nullable=True)  # JSON summary written when the job ends
    attempts = Column(Integer, nullable=True, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    ad = relationship("Ad", back_populates="jobs")

    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_type_ad", "type", "ad_id"),
    )
