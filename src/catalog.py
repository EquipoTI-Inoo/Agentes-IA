"""
Catalog hygiene (Phase 7, A4).
Ads whose page is not one of the catalogued competitors (competitor_id IS NULL) are marked
status='out_of_catalog': they stay in the database (nothing is deleted) but never enter the evaluation queue.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.db.models import SCORED_ANALYSIS_TYPES, STATUS_OUT_OF_CATALOG, STATUS_REJECTED, Ad, Analysis


def _candidates(db: Session) -> List[Ad]:
    """Ads without a competitor that are not already rejected or marked."""
    return (
        db.query(Ad)
        .filter(Ad.competitor_id.is_(None), Ad.status.notin_((STATUS_REJECTED, STATUS_OUT_OF_CATALOG)))
        .order_by(Ad.page_name, Ad.id)
        .all()
    )


def cleanup_report(db: Session) -> Dict[str, Any]:
    """What `cleanup --apply` would mark: totals and the pages involved (read-only)."""
    ads = _candidates(db)
    evaluated_ids = {
        r[0]
        for r in db.query(Analysis.ad_id).filter(
            Analysis.analysis_type.in_(SCORED_ANALYSIS_TYPES), Analysis.creative_score.isnot(None)
        )
    }
    pages: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"ads": 0, "pillars": set(), "queries": set(), "statuses": set(), "page_ids": set()})
    for ad in ads:
        page = pages[ad.page_name or "(sin nombre)"]
        page["ads"] += 1
        page["pillars"].add(ad.pillar)
        page["statuses"].add(ad.status)
        if ad.search_query:
            page["queries"].add(ad.search_query)
        if ad.page_id:
            page["page_ids"].add(ad.page_id)
    return {
        "total": len(ads),
        "with_evaluation": sum(1 for a in ads if a.id in evaluated_ids),
        "already_marked": db.query(Ad).filter(Ad.status == STATUS_OUT_OF_CATALOG).count(),
        "rejected_untouched": db.query(Ad).filter(Ad.competitor_id.is_(None), Ad.status == STATUS_REJECTED).count(),
        "pages": [
            {"page_name": name, "ads": p["ads"], "pillars": sorted(p["pillars"]), "queries": sorted(p["queries"]),
             "statuses": sorted(p["statuses"]), "page_ids": sorted(p["page_ids"])}
            for name, p in sorted(pages.items(), key=lambda kv: (-kv[1]["ads"], kv[0]))
        ],
    }


def mark_out_of_catalog(db: Session) -> int:
    """Marks every candidate ad as out_of_catalog (status only; rows, media and analyses are untouched)."""
    ads = _candidates(db)
    for ad in ads:
        ad.status = STATUS_OUT_OF_CATALOG
    db.commit()
    return len(ads)


def format_cleanup_report(report: Dict[str, Any], applied: bool) -> str:
    verb = "Marcados" if applied else "Serían marcados"
    lines = [
        "=" * 78,
        f"CLEANUP DE CATÁLOGO — {'APLICADO' if applied else 'DRY-RUN (no se cambió nada)'}",
        "=" * 78,
        f"{verb} como out_of_catalog: {report['total']} anuncios de {len(report['pages'])} páginas que no son competidores del catálogo",
        f"  (con evaluación guardada entre ellos: {report['with_evaluation']}; no se borra nada, solo cambia el status)",
        f"Ya marcados antes: {report['already_marked']} | Rechazados sin competidor (no se tocan): {report['rejected_untouched']}",
        "",
        f"{'página':<40}{'page_id':<17}{'anuncios':>9}  pilares / búsqueda que los trajo",
    ]
    for p in report["pages"]:
        ids = ",".join(p["page_ids"]) or "-"
        lines.append(f"{p['page_name'][:39]:<40}{ids[:16]:<17}{p['ads']:>9}  {','.join(p['pillars'])} / {', '.join(p['queries'])[:40]}")
    return "\n".join(lines)

