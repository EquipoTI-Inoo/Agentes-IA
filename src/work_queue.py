"""
Prioritized evaluation queue (Phase 7, B3/B4).

Queue = ads with status 'pending_evaluation' whose media is on disk (an ad without its file cannot be
evaluated). rejected and out_of_catalog ads are never queued. Order:
  (0) active before inactive (Phase 0.1 policy: inactive ads have the lowest priority)
  (a) competitors with priority 1 first (then 2, 3; ads without a competitor last)
  (b) longer-running first (days_active)
  (c) more variants first (variant_count)
Videos are evaluated by Gemini and statics by Claude (src/ai/router.py).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session, selectinload

from src.config import settings
from src.db.models import (
    EXCLUDED_FROM_QUEUE,
    SCORED_ANALYSIS_TYPES,
    STATUS_EVALUATED,
    STATUS_OUT_OF_CATALOG,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SCORED,
    Ad,
    Analysis,
)
from src.media.fingerprint import detect_ad_format

NO_COMPETITOR_PRIORITY = 99


def ad_eval_dict(ad: Ad) -> Dict[str, Any]:
    """The dict the evaluators/router expect (never carries brand-revealing scores or days to the LLM prompt)."""
    return {
        "id": ad.id,
        "ad_id": ad.id,
        "page_name": ad.page_name,
        "video_path": ad.video_path,
        "image_path": ad.image_path,
        "headline": ad.headline,
        "body_text": ad.body_text,
        "cta_text": ad.cta_text,
        "link_url": ad.link_url,
        "days_active": ad.days_active,
        "is_active": ad.is_active,
        "creatives": [{
            "has_video": bool(ad.has_video),
            "has_image": bool(ad.image_path),
            "title": ad.headline or "",
            "body": ad.body_text or "",
        }],
    }


def ad_format(ad: Ad) -> str:
    return detect_ad_format(ad_eval_dict(ad))


def is_evaluable(ad: Ad, downloads_dir: Optional[Path] = None) -> bool:
    """True when the media the evaluator needs (video for video ads, image for the rest) exists on disk."""
    base = Path(downloads_dir) if downloads_dir else settings.downloads_path
    rel = ad.video_path if ad_format(ad) == "video" else ad.image_path
    return bool(rel) and (base / rel).exists()


def priority_key(ad: Ad) -> Tuple[int, int, int, int, str]:
    """Sort key implementing the queue order (lower sorts first)."""
    competitor_priority = ad.competitor.priority if ad.competitor is not None else NO_COMPETITOR_PRIORITY
    return (
        0 if ad.is_active else 1,
        competitor_priority,
        -(ad.days_active or 0),
        -(ad.variant_count or 1),
        ad.id,
    )


def select_queue(
    db: Session,
    pillar: Optional[str] = None,
    competitor_id: Optional[int] = None,
    include_evaluated: bool = False,
    limit: Optional[int] = None,
    downloads_dir: Optional[Path] = None,
    only_evaluable: bool = True,
) -> List[Ad]:
    """Ads to evaluate, already sorted. include_evaluated=True (used by --force) also re-queues evaluated ones."""
    statuses = [STATUS_PENDING] + ([STATUS_EVALUATED, STATUS_SCORED] if include_evaluated else [])
    q = (
        db.query(Ad)
        .options(selectinload(Ad.media), selectinload(Ad.competitor))
        .filter(Ad.status.in_(statuses), Ad.status.notin_(EXCLUDED_FROM_QUEUE))
    )
    if pillar:
        q = q.filter(Ad.pillar == pillar)
    if competitor_id is not None:
        q = q.filter(Ad.competitor_id == competitor_id)
    ads = sorted(q.all(), key=priority_key)
    if only_evaluable:
        ads = [a for a in ads if is_evaluable(a, downloads_dir)]
    return ads[:limit] if limit else ads


def has_scored_analysis(db: Session, ad_id: str, prompt_version: Optional[str] = None) -> bool:
    q = db.query(Analysis.id).filter(
        Analysis.ad_id == ad_id, Analysis.analysis_type.in_(SCORED_ANALYSIS_TYPES), Analysis.creative_score.isnot(None)
    )
    if prompt_version:
        q = q.filter(Analysis.prompt_version == prompt_version)
    return q.first() is not None


def estimate_days_to_clear(pending: int, rpd: int, reserve: int) -> int:
    """Days needed to empty the queue = ceil(pending / (RPD - RESERVE_REQUESTS)); 0 when nothing is pending."""
    if pending <= 0:
        return 0
    return math.ceil(pending / max(1, rpd - reserve))


def queue_stats(db: Session, downloads_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Numbers shown by `queue --status` (database side only; quota/cost numbers come from QuotaGuard)."""
    ads = db.query(Ad).options(selectinload(Ad.media)).all()
    evaluated_ids = {
        r[0]
        for r in db.query(Analysis.ad_id).filter(
            Analysis.analysis_type.in_(SCORED_ANALYSIS_TYPES), Analysis.creative_score.isnot(None)
        )
    }
    group_count, grouped_ads = db.query(func.count(distinct(Ad.creative_group_id)), func.count(Ad.creative_group_id)).one()

    pending = [a for a in ads if a.status == STATUS_PENDING]
    queue = [a for a in pending if is_evaluable(a, downloads_dir)]
    queue_video = sum(1 for a in queue if ad_format(a) == "video")
    return {
        "total_ads": len(ads),
        "queue": len(queue),
        "queue_video": queue_video,
        "queue_static": len(queue) - queue_video,
        "pending_without_media": len(pending) - len(queue),
        "evaluated": len(evaluated_ids),
        "unevaluated": sum(1 for a in ads if a.id not in evaluated_ids),
        "rejected": sum(1 for a in ads if a.status == STATUS_REJECTED),
        "out_of_catalog": sum(1 for a in ads if a.status == STATUS_OUT_OF_CATALOG),
        "creative_groups": group_count,
        "grouped_ads": grouped_ads,
        "by_status": {s: sum(1 for a in ads if a.status == s) for s in sorted({a.status for a in ads})},
    }
