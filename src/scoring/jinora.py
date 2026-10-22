"""
Jinora Final Score and Local Rescoring (Phase 5)
- jinora_score = SCORE_WEIGHT_CREATIVE * creative_score + SCORE_WEIGHT_MARKET * market_score
- NULL while the ad has no creative_score (never computed from market_score alone)
- rescore_all() recomputes fingerprints, variant groups, market_score and jinora_score
  from local data only: no Gemini, no Claude, no network
"""

from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from src.config import settings
from src.media.fingerprint import (
    build_creative_hash,
    compute_copy_hash,
    compute_file_sha256,
    compute_image_phash,
    group_creative_variants,
)
from src.scoring.market import WEIGHT_TOLERANCE, compute_market_score, load_scoring_config

logger = logging.getLogger("Jinora.Scoring")


def get_score_weights(cfg: Any = None) -> Tuple[float, float]:
    """Reads SCORE_WEIGHT_CREATIVE / SCORE_WEIGHT_MARKET (from .env) and validates they sum 1.0."""
    cfg = cfg or settings
    w_c = float(cfg.SCORE_WEIGHT_CREATIVE)
    w_m = float(cfg.SCORE_WEIGHT_MARKET)
    if w_c < 0 or w_m < 0 or abs(w_c + w_m - 1.0) > WEIGHT_TOLERANCE:
        raise ValueError(
            f"SCORE_WEIGHT_CREATIVE + SCORE_WEIGHT_MARKET deben sumar 1.0 (suman {w_c + w_m:.4f})"
        )
    return w_c, w_m


def compute_jinora_score(
    creative_score: Optional[float],
    market_score: Optional[float],
    weights: Optional[Tuple[float, float]] = None,
) -> Optional[float]:
    """Weighted blend; None if either score is missing (no defaults, no caps)."""
    if creative_score is None or market_score is None:
        return None
    w_c, w_m = weights or get_score_weights()
    return round(w_c * float(creative_score) + w_m * float(market_score), 2)


def _resolve_media(rel_path: Optional[str], downloads_dir: Path) -> Optional[Path]:
    if not rel_path:
        return None
    p = Path(rel_path)
    if not p.is_absolute():
        p = downloads_dir / p
    return p if p.exists() else None


def _fingerprint_ad(db: Any, ad: Any, downloads_dir: Path, refingerprint: bool) -> None:
    """Fills copy_hash / creative_hash on the ad and sha256 / phash on its media_info (local files only)."""
    from src.db.models import MediaInfo

    ad.copy_hash = compute_copy_hash(ad.body_text, ad.headline)
    info = ad.media_info
    if refingerprint or info is None or (info.sha256 is None and info.phash is None):
        video = _resolve_media(ad.video_path, downloads_dir)
        image = _resolve_media(ad.image_path, downloads_dir)
        primary = video or image
        if primary is not None or info is not None:
            if info is None:
                info = MediaInfo()
                ad.media_info = info
            info.sha256 = compute_file_sha256(primary) if primary else None
            info.phash = compute_image_phash(image) if image else None
    ad.creative_hash = build_creative_hash(
        info.phash if info else None, info.sha256 if info else None, ad.copy_hash
    )


def rescore_all(
    session_factory: Any = None,
    now: Optional[datetime] = None,
    refingerprint: bool = False,
    downloads_dir: Optional[Path] = None,
    config_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """Recomputes fingerprints, groups, market_score and jinora_score for every ad. Local only."""
    from sqlalchemy.orm import selectinload

    from src.database import SessionLocal
    from src.db.models import SCORED_ANALYSIS_TYPES, Ad, Analysis, MarketSignal

    config = load_scoring_config(config_path)
    weights = get_score_weights()
    downloads = downloads_dir or settings.downloads_path
    phash_max = int(config["fingerprint"]["phash_max_distance"])

    db = (session_factory or SessionLocal)()
    try:
        ads = (
            db.query(Ad)
            .options(selectinload(Ad.media), selectinload(Ad.media_info), selectinload(Ad.market_signal))
            .all()
        )
        for ad in ads:
            _fingerprint_ad(db, ad, downloads, refingerprint)

        groups = group_creative_variants(
            [
                {
                    "id": a.id,
                    "media_sha256": a.media_info.sha256 if a.media_info else None,
                    "phash": a.media_info.phash if a.media_info else None,
                    "copy_hash": a.copy_hash,
                }
                for a in ads
            ],
            phash_max_distance=phash_max,
        )

        # Latest Phase 4 evaluation per ad (analyses table); forensic rows carry no score
        creative_scores: Dict[str, int] = {}
        for a in (
            db.query(Analysis)
            .filter(Analysis.analysis_type.in_(SCORED_ANALYSIS_TYPES), Analysis.creative_score.isnot(None))
            .order_by(Analysis.id)
            .all()
        ):
            creative_scores[str(a.ad_id)] = a.creative_score

        scored = 0
        market_values = []
        for ad in ads:
            ad.creative_group_id = groups[ad.id]["creative_group_id"]
            ad.variant_count = groups[ad.id]["variant_count"]
            market = compute_market_score(
                {
                    "delivery_start_time": ad.start_date,
                    "delivery_stop_time": ad.stop_date,
                    "days_active": ad.days_active,
                    "is_active": ad.is_active,
                    "variant_count": ad.variant_count,
                    "countries": ad.country_list,
                    "publisher_platforms": ad.platforms,
                },
                config,
                now=now,
            )
            signal = ad.market_signal
            if signal is None:
                signal = MarketSignal()
                ad.market_signal = signal
            signal.market_score = market["market_score"]
            signal.market_components = json.dumps(market["market_components"], ensure_ascii=False)
            signal.jinora_score = compute_jinora_score(creative_scores.get(ad.id), signal.market_score, weights)
            scored += signal.jinora_score is not None
            if signal.market_score is not None:
                market_values.append(signal.market_score)
        db.commit()

        return {
            "ads": len(ads),
            "groups": len({g["creative_group_id"] for g in groups.values()}),
            "multi_variant_groups": len({g["creative_group_id"] for g in groups.values() if g["variant_count"] > 1}),
            "market_min": min(market_values) if market_values else None,
            "market_median": statistics.median(market_values) if market_values else None,
            "market_max": max(market_values) if market_values else None,
            "jinora_scored": scored,
            "jinora_null": len(ads) - scored,
        }
    finally:
        db.close()
