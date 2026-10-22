"""
Competitor loading and ad -> competitor resolution (Phase 6).
config/competitors_database.json stays the source of truth; the competitors table mirrors it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.db.models import Competitor, from_json, to_json

COMPETITORS_JSON = Path(__file__).resolve().parent.parent.parent / "config" / "competitors_database.json"
_META_KEYS = ("region", "focus", "notes", "resolution_confidence", "resolved_at", "needs_review")


def normalize_name(name: Optional[str]) -> str:
    """Lowercase alphanumerics only: 'Bland AI' == 'BlandAI' == 'bland.ai'."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load_competitors_config(path: Union[str, Path, None] = None) -> List[Dict[str, Any]]:
    """Flattens the pillars structure into one dict per competitor (as stored in the table)."""
    data = json.loads(Path(path or COMPETITORS_JSON).read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = []
    for pillar, pdata in (data.get("pillars") or {}).items():
        for comp in pdata.get("competitors", []):
            rows.append({
                "name": comp["name"],
                "website": comp.get("website"),
                "pillar": pillar,
                "priority": int(comp.get("priority", 2) or 2),
                "markets": comp.get("markets") or [],
                "page_ids": [str(p) for p in (comp.get("page_ids") or [])],
                "meta": {k: comp[k] for k in _META_KEYS if k in comp},
            })
    return rows


def competitor_from_row(row: Dict[str, Any]) -> Competitor:
    return Competitor(
        name=row["name"],
        website=row.get("website"),
        pillar=row.get("pillar"),
        priority=row.get("priority", 2),
        markets=to_json(row.get("markets") or []),
        page_ids=to_json(row.get("page_ids") or []),
        meta_json=to_json(row.get("meta") or {}),
    )


class CompetitorIndex:
    """In-memory lookup: exact page_id first, then normalized page name. No fuzzy guessing."""

    def __init__(self, competitors: List[Competitor]) -> None:
        self.by_page_id: Dict[str, int] = {}
        self.by_name: Dict[str, int] = {}
        for c in competitors:
            for pid in from_json(c.page_ids, []):
                self.by_page_id.setdefault(str(pid), c.id)
            self.by_name.setdefault(normalize_name(c.name), c.id)

    def resolve(self, page_id: Optional[str], page_name: Optional[str]) -> Optional[int]:
        if page_id and str(page_id) in self.by_page_id:
            return self.by_page_id[str(page_id)]
        key = normalize_name(page_name)
        return self.by_name.get(key) if key else None
