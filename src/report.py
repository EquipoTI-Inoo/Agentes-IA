"""Ranking report (Phase 7, `cli report`): top ads by jinora_score, read straight from the database."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, selectinload

from src.db.models import EXCLUDED_FROM_QUEUE, SCORED_ANALYSIS_TYPES, Ad, from_json


def _latest_scored(ad: Ad):
    rows = [a for a in ad.analyses if a.analysis_type in SCORED_ANALYSIS_TYPES and a.creative_score is not None]
    return rows[-1] if rows else None


def build_report(db: Session, pillar: Optional[str] = None, top: int = 10) -> Dict[str, Any]:
    """
    Ads ranked by jinora_score (creative 70 % + market 30 %). Ads without creative_score have no jinora_score and
    are never ranked as if they had one; they are only counted. rejected / out_of_catalog ads are left out.
    """
    q = (
        db.query(Ad)
        .options(selectinload(Ad.analyses), selectinload(Ad.market_signal), selectinload(Ad.competitor))
        .filter(Ad.status.notin_(EXCLUDED_FROM_QUEUE))
    )
    if pillar:
        q = q.filter(Ad.pillar == pillar)
    ads = q.all()

    ranked: List[Dict[str, Any]] = []
    unevaluated = 0
    for ad in ads:
        analysis = _latest_scored(ad)
        signal = ad.market_signal
        if analysis is None or signal is None or signal.jinora_score is None:
            unevaluated += 1
            continue
        result = from_json(analysis.result_json, {})
        ranked.append({
            "ad_id": ad.id,
            "competitor": ad.competitor.name if ad.competitor else (ad.page_name or "?"),
            "pillar": ad.pillar,
            "jinora_score": signal.jinora_score,
            "creative_score": analysis.creative_score,
            "market_score": signal.market_score,
            "days_active": ad.days_active,
            "variants": ad.variant_count or 1,
            "format": result.get("format") or result.get("visual_style") or "-",
            "model": analysis.model,
        })
    ranked.sort(key=lambda r: (-r["jinora_score"], -r["days_active"], r["ad_id"]))
    return {"pillar": pillar, "top": ranked[:top], "evaluated": len(ranked), "unevaluated": unevaluated}


def format_report(report: Dict[str, Any]) -> str:
    title = f"TOP {len(report['top'])} POR jinora_score" + (f" — pilar {report['pillar']}" if report["pillar"] else "")
    lines = ["=" * 92, title, "=" * 92]
    if not report["top"]:
        lines.append("Todavía no hay anuncios evaluados con puntaje final (jinora_score) en este alcance.")
    else:
        lines.append(f"{'#':>2}  {'competidor':<22}{'jinora':>7}{'creativo':>9}{'mercado':>8}{'días':>6}{'var.':>5}  {'formato':<14}{'ad_id'}")
        for i, r in enumerate(report["top"], 1):
            lines.append(
                f"{i:>2}  {r['competitor'][:21]:<22}{r['jinora_score']:>7.1f}{r['creative_score']:>9}{(r['market_score'] or 0):>8.1f}"
                f"{r['days_active']:>6}{r['variants']:>5}  {str(r['format'])[:13]:<14}{r['ad_id']}"
            )
    lines.append(f"\nEvaluados con jinora_score: {report['evaluated']} | sin evaluar (sin jinora_score, no se rankean): {report['unevaluated']}")
    return "\n".join(lines)
