"""
Jinora Market Signal (Phase 5)
market_score 0-100 measures how much a creative is working for the competitor, using ONLY
platform data (dates, variants, countries, active status, platforms). Never AI, never content.
- Weights and scale caps come from config/scoring.yaml; weights must sum to 1.0
- Missing data excludes the component and rescales the remaining weights (no invented values)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml

SCORING_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "scoring.yaml"
MARKET_COMPONENTS = ("longevity", "variants", "countries", "is_active", "platforms")
WEIGHT_TOLERANCE = 1e-6


def load_scoring_config(path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Loads config/scoring.yaml and validates market weights (all present, >= 0, sum 1.0)."""
    cfg_path = Path(path) if path else SCORING_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    market = cfg.get("market_score") or {}
    weights = market.get("weights") or {}
    missing = [k for k in MARKET_COMPONENTS if k not in weights]
    if missing:
        raise ValueError(f"scoring.yaml: faltan pesos de market_score: {missing}")
    if any(float(w) < 0 for w in weights.values()):
        raise ValueError("scoring.yaml: los pesos de market_score no pueden ser negativos")
    total = sum(float(weights[k]) for k in MARKET_COMPONENTS)
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise ValueError(f"scoring.yaml: los pesos de market_score deben sumar 1.0 (suman {total:.4f})")

    for cap in ("longevity_cap_days", "variants_cap", "countries_cap"):
        if float(market.get(cap, 0)) <= (1 if cap == "variants_cap" else 0):
            raise ValueError(f"scoring.yaml: {cap} inválido ({market.get(cap)})")
    if not market.get("counted_platforms"):
        raise ValueError("scoring.yaml: counted_platforms vacío")
    return cfg


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, (list, tuple)):
        return None
    return [str(v) for v in value]


def compute_active_days(ad: Dict[str, Any], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Days the ad has run. Active: start -> now. Inactive: start -> stop date; without a stop
    date, falls back to the scraper's days_active (computed start -> stop at collection time).
    Returns None when there is no start date.
    """
    start = _parse_dt(ad.get("delivery_start_time"))
    if start is None:
        return None
    if ad.get("is_active"):
        end = now or datetime.now(timezone.utc)
        source = "start_to_today"
    else:
        stop = _parse_dt(ad.get("delivery_stop_time"))
        if stop is not None:
            end, source = stop, "start_to_stop"
        elif ad.get("days_active") is not None:
            return {"days": max(0, int(ad["days_active"])), "source": "stored_days_active"}
        else:
            return None
    return {"days": max(0, (end - start).days), "source": source}


def compute_market_score(
    ad: Dict[str, Any],
    config: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Returns {"market_score": float 0-100 or None, "market_components": dict}.
    Each component is normalized to 0-1; missing components are excluded and the remaining
    weights rescaled to sum 1.0.
    """
    market = config["market_score"]
    weights = market["weights"]
    raw: Dict[str, Any] = {}
    values: Dict[str, float] = {}

    days_info = compute_active_days(ad, now=now)
    if days_info is not None:
        cap = float(market["longevity_cap_days"])
        raw["longevity"] = days_info
        values["longevity"] = min(math.log1p(days_info["days"]) / math.log1p(cap), 1.0)

    variant_count = ad.get("variant_count")
    if variant_count:
        cap = float(market["variants_cap"])
        raw["variants"] = int(variant_count)
        values["variants"] = min(math.log(max(1, int(variant_count))) / math.log(cap), 1.0)

    countries = _parse_list(ad.get("countries"))
    if countries:
        cap = float(market["countries_cap"])
        unique = sorted({c.upper() for c in countries})
        raw["countries"] = unique
        values["countries"] = min(len(unique) / cap, 1.0)

    if ad.get("is_active") is not None:
        raw["is_active"] = bool(ad["is_active"])
        values["is_active"] = 1.0 if ad["is_active"] else 0.0

    platforms = _parse_list(ad.get("publisher_platforms"))
    if platforms:
        counted = [p.upper() for p in market["counted_platforms"]]
        present = sorted({p.upper() for p in platforms} & set(counted))
        raw["platforms"] = present
        values["platforms"] = len(present) / len(counted)

    used_weight = sum(float(weights[k]) for k in values)
    components: Dict[str, Any] = {
        "used": {},
        "missing": [k for k in MARKET_COMPONENTS if k not in values],
    }
    if used_weight <= 0:
        return {"market_score": None, "market_components": components}

    score = 0.0
    for name, value in values.items():
        effective = float(weights[name]) / used_weight
        score += effective * value
        components["used"][name] = {
            "raw": raw[name],
            "value": round(value, 4),
            "weight": float(weights[name]),
            "effective_weight": round(effective, 4),
        }
    return {"market_score": round(score * 100, 2), "market_components": components}
