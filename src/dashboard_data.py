"""
Read-only data layer of the dashboard and the reports (Phase 8).

Everything here only READS the stored scores (creative_score, market_score, jinora_score) and the stored
evaluation JSON: it never recomputes a score and never calls an evaluator or an LLM.
Text coming from ads is returned as plain data; the UI escapes it when rendering (see static/dashboard.js).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session, selectinload

from src.db.competitors import normalize_name
from src.db.models import (
    EXCLUDED_FROM_QUEUE,
    SCORED_ANALYSIS_TYPES,
    STATUS_PENDING,
    Ad,
    Competitor,
    from_json,
)
from src.work_queue import ad_format, is_evaluable

PILLARS = ("voz", "conversacional", "crm")
# Mirror of the Literal types in src/ai/schemas.py (a test keeps them in sync)
VIDEO_FORMATS = ("ugc", "demo_pantalla", "talking_head", "animacion", "testimonial", "mixto", "otro")
STATIC_STYLES = ("captura_producto", "chat_whatsapp", "persona_rostro", "meme", "infografia", "testimonial",
                 "mockup_dispositivo", "ilustracion", "foto_stock", "otro")
FORMATS = tuple(dict.fromkeys(VIDEO_FORMATS + STATIC_STYLES))
# Statuses a user may filter by. rejected / out_of_catalog ads are never shown in rankings.
STATUS_FILTER = ("pending_evaluation", "evaluated", "scored", "discovered", "filtered", "downloaded", "needs_manual_review", "inactive_data_only")
AI_MENTION = ("ninguna", "mencionada", "central")

VIDEO_DIMENSIONS = (("hook", "Captación"), ("mechanism", "Mecanismo y prueba"), ("narrative", "Narrativa y ritmo"), ("offer", "Oferta y CTA"))
STATIC_DIMENSIONS = (("stop_power", "Poder de detención"), ("message_clarity", "Claridad del mensaje"), ("proof", "Prueba visible"), ("offer_cta", "Oferta y CTA"))

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$")
_TS_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")


class FilterError(ValueError):
    """A filter value is not in the whitelist (the API answers 422)."""


@lru_cache(maxsize=1)
def current_rubrics() -> Dict[str, str]:
    from src.db.migrate import current_rubric_versions

    return current_rubric_versions()


def parse_timestamp_start(value: Optional[str]) -> Optional[int]:
    """Start second of 'MM:SS', 'MM:SS-MM:SS' (any dash) or 'H:MM:SS'; None when there is no timestamp."""
    if not value:
        return None
    m = _TS_RE.search(str(value))
    if not m:
        return None
    hours, minutes, seconds = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def safe_http_url(url: Optional[str]) -> Optional[str]:
    """Third-party links are only exposed when they are plain http(s) (never javascript:, data:, ...)."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    return url.strip() if parsed.scheme in ("http", "https") and parsed.netloc else None


# --------------------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------------------

@dataclass
class TopFilters:
    pillar: Optional[str] = None
    country: Optional[str] = None
    competitor_id: Optional[int] = None
    competitor: Optional[str] = None
    format: Optional[str] = None
    language: Optional[str] = None
    has_voiceover: Optional[bool] = None
    status: Optional[str] = None

    def applied(self) -> Dict[str, Any]:
        return {k: v for k, v in {"pillar": self.pillar, "country": self.country, "competitor": self.competitor, "format": self.format,
                                  "language": self.language, "has_voiceover": self.has_voiceover, "status": self.status}.items() if v is not None}


def parse_filters(db: Session, pillar: Optional[str] = None, country: Optional[str] = None, competitor: Optional[str] = None,
                  format: Optional[str] = None, language: Optional[str] = None, has_voiceover: Optional[bool] = None,
                  status: Optional[str] = None) -> TopFilters:
    """Whitelist validation of every filter; anything else raises FilterError."""
    f = TopFilters(has_voiceover=has_voiceover)
    if pillar:
        if pillar not in PILLARS:
            raise FilterError(f"pillar inválido: {pillar!r}. Válidos: {', '.join(PILLARS)}")
        f.pillar = pillar
    if country:
        code = country.strip().upper()
        if not _COUNTRY_RE.match(code):
            raise FilterError("country debe ser un código ISO de 2 letras (p. ej. PE)")
        f.country = code
    if competitor:
        wanted = normalize_name(competitor)
        match = next((c for c in db.query(Competitor).all() if normalize_name(c.name) == wanted), None)
        if match is None:
            raise FilterError(f"competitor desconocido: {competitor!r}")
        f.competitor_id, f.competitor = match.id, match.name
    if format:
        if format not in FORMATS:
            raise FilterError(f"format inválido: {format!r}. Válidos: {', '.join(FORMATS)}")
        f.format = format
    if language:
        lang = language.strip().lower()
        if not _LANGUAGE_RE.match(lang):
            raise FilterError("language debe ser un código de 2-3 letras (p. ej. es, en)")
        f.language = lang
    if status:
        if status not in STATUS_FILTER:
            raise FilterError(f"status inválido: {status!r}. Válidos: {', '.join(STATUS_FILTER)}")
        f.status = status
    return f


# --------------------------------------------------------------------------------------
# Evaluation view helpers
# --------------------------------------------------------------------------------------

def _latest_scored(ad: Ad):
    rows = [a for a in ad.analyses if a.analysis_type in SCORED_ANALYSIS_TYPES and a.creative_score is not None]
    return rows[-1] if rows else None


def _eval_facts(analysis) -> Dict[str, Any]:
    """Descriptive facts of a stored evaluation (format, language, voice, feature_focus, ai_mention)."""
    data = from_json(analysis.result_json, {}) or {}
    is_static = analysis.analysis_type == "static"
    return {
        "data": data,
        "format": data.get("visual_style") if is_static else data.get("format"),
        "language": (data.get("language") or "").lower() or None,
        "has_voiceover": False if is_static else data.get("has_voiceover"),
        "feature_focus": data.get("feature_focus") or [],
        "ai_mention": data.get("ai_mention"),
        "value_proposition": data.get("value_proposition"),
    }


def _outdated(analysis) -> bool:
    """True when the evaluation used a rubric version other than the one in force ('rúbrica anterior')."""
    return str(analysis.rubric_version) != current_rubrics().get(analysis.analysis_type, "")


def _candidate_ads(db: Session, f: TopFilters) -> List[Ad]:
    q = (
        db.query(Ad)
        .options(selectinload(Ad.analyses), selectinload(Ad.market_signal), selectinload(Ad.competitor), selectinload(Ad.media), selectinload(Ad.media_info))
        .filter(Ad.status.notin_(EXCLUDED_FROM_QUEUE))
    )
    if f.pillar:
        q = q.filter(Ad.pillar == f.pillar)
    if f.competitor_id is not None:
        q = q.filter(Ad.competitor_id == f.competitor_id)
    if f.status:
        q = q.filter(Ad.status == f.status)
    ads = q.all()
    if f.country:
        ads = [a for a in ads if f.country in (from_json(a.country_list, []) or [])]
    return ads


def _group_key(ad: Ad) -> str:
    return ad.creative_group_id or f"solo:{ad.id}"


def _thumbnail(ad: Ad) -> Optional[str]:
    return f"/api/media/{ad.id}?kind=image" if ad.image_path else None


def _row(ad: Ad, analysis, facts: Optional[Dict[str, Any]], variants: int) -> Dict[str, Any]:
    signal = ad.market_signal
    return {
        "ad_id": ad.id,
        "group_id": _group_key(ad),
        "variants": variants,
        "competitor": ad.competitor.name if ad.competitor else None,
        "page_name": ad.page_name,
        "pillar": ad.pillar,
        "status": ad.status,
        "is_active": bool(ad.is_active),
        "days_active": ad.days_active or 0,
        "headline": ad.headline,
        "thumbnail_url": _thumbnail(ad),
        "has_video": bool(ad.has_video),
        "media_format": ad_format(ad),
        "creative_score": analysis.creative_score if analysis else None,
        "market_score": signal.market_score if signal else None,
        "jinora_score": signal.jinora_score if signal else None,
        "format": facts["format"] if facts else None,
        "language": facts["language"] if facts else None,
        "has_voiceover": facts["has_voiceover"] if facts else None,
        "feature_focus": facts["feature_focus"] if facts else [],
        "ai_mention": facts["ai_mention"] if facts else None,
        "rubric_outdated": _outdated(analysis) if analysis else False,
    }


def _matches_eval_filters(facts: Dict[str, Any], f: TopFilters) -> bool:
    if f.format and facts["format"] != f.format:
        return False
    if f.language and facts["language"] != f.language:
        return False
    if f.has_voiceover is not None and bool(facts["has_voiceover"]) != f.has_voiceover:
        return False
    return True


# --------------------------------------------------------------------------------------
# Public queries
# --------------------------------------------------------------------------------------

def top_ads(db: Session, f: TopFilters, limit: int = 50, offset: int = 0, unevaluated_limit: int = 50) -> Dict[str, Any]:
    """
    Ranking by stored jinora_score, ONE row per creative (creative_group_id) with its variant count.
    Only evaluated ads are ranked; unevaluated ones are listed apart. rejected / out_of_catalog never appear.
    """
    ads = _candidate_ads(db, f)
    group_sizes = Counter(_group_key(a) for a in db.query(Ad).filter(Ad.status.notin_(EXCLUDED_FROM_QUEUE)).all())

    best: Dict[str, Tuple[Ad, Any, Dict[str, Any]]] = {}
    unevaluated: Dict[str, Ad] = {}
    for ad in ads:
        analysis = _latest_scored(ad)
        if analysis is None:
            if not (f.format or f.language or f.has_voiceover is not None):  # those filters describe an evaluation
                key = _group_key(ad)
                if key not in unevaluated or (ad.days_active or 0) > (unevaluated[key].days_active or 0):
                    unevaluated[key] = ad
            continue
        facts = _eval_facts(analysis)
        if not _matches_eval_filters(facts, f):
            continue
        key = _group_key(ad)
        score = ad.market_signal.jinora_score if ad.market_signal and ad.market_signal.jinora_score is not None else -1.0
        current = best.get(key)
        if current is None or (score, analysis.creative_score) > (
            current[0].market_signal.jinora_score if current[0].market_signal and current[0].market_signal.jinora_score is not None else -1.0,
            current[1].creative_score,
        ):
            best[key] = (ad, analysis, facts)

    rows = [_row(ad, an, facts, group_sizes[key]) for key, (ad, an, facts) in best.items()]
    rows.sort(key=lambda r: (r["jinora_score"] is None, -(r["jinora_score"] or 0), -(r["creative_score"] or 0), -r["days_active"], r["ad_id"]))

    pending_rows = []
    for key, ad in unevaluated.items():
        row = _row(ad, None, None, group_sizes[key])
        evaluable = ad.status == STATUS_PENDING and is_evaluable(ad)
        row["can_enqueue"] = evaluable
        row["enqueue_reason"] = None if evaluable else (
            "sin archivo local" if ad.status == STATUS_PENDING else f"etapa del pipeline: {ad.status}")
        pending_rows.append(row)
    pending_rows.sort(key=lambda r: (-r["days_active"], r["ad_id"]))

    return {
        "items": rows[offset: offset + limit],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "unevaluated": {"total": len(pending_rows), "items": pending_rows[:unevaluated_limit]},
        "filters": f.applied(),
    }


def _dimension_list(analysis, data: Dict[str, Any]) -> List[Dict[str, Any]]:
    static = analysis.analysis_type == "static"
    out = []
    for key, label in (STATIC_DIMENSIONS if static else VIDEO_DIMENSIONS):
        d = data.get(key) or {}
        evidence = []
        for e in d.get("evidence") or []:
            if static:
                where = e.get("zone") if e.get("card") is None else f"tarjeta {e.get('card')} · {e.get('zone')}"
                evidence.append({"timestamp": None, "seconds": None, "source": where, "quote": e.get("quote")})
            else:
                evidence.append({"timestamp": e.get("timestamp"), "seconds": parse_timestamp_start(e.get("timestamp")),
                                 "source": e.get("source"), "quote": e.get("quote")})
        out.append({"key": key, "label": label, "score": d.get("score"), "max": 25, "anchor_level": d.get("anchor_level"),
                    "reasoning": d.get("reasoning"), "evidence": evidence})
    return out


def ad_detail(db: Session, ad_id: str) -> Optional[Dict[str, Any]]:
    ad = (
        db.query(Ad)
        .options(selectinload(Ad.analyses), selectinload(Ad.market_signal), selectinload(Ad.competitor), selectinload(Ad.media), selectinload(Ad.media_info))
        .filter(Ad.id == ad_id)
        .first()
    )
    if ad is None:
        return None
    analysis = _latest_scored(ad)
    signal = ad.market_signal
    variants = []
    if ad.creative_group_id:
        for v in db.query(Ad).filter(Ad.creative_group_id == ad.creative_group_id, Ad.status.notin_(EXCLUDED_FROM_QUEUE)).order_by(Ad.id).all():
            variants.append({"ad_id": v.id, "page_name": v.page_name, "status": v.status, "days_active": v.days_active or 0,
                             "is_current": v.id == ad.id})
    forensic = next((a for a in reversed(ad.analyses) if a.analysis_type == "forensics"), None)

    evaluation = None
    if analysis is not None:
        facts = _eval_facts(analysis)
        d = facts["data"]
        evaluation = {
            "type": analysis.analysis_type,
            "model": analysis.model,
            "prompt_version": analysis.prompt_version,
            "rubric_version": analysis.rubric_version,
            "rubric_current": current_rubrics().get(analysis.analysis_type),
            "rubric_outdated": _outdated(analysis),
            "evaluated_at": analysis.created_at.isoformat() if analysis.created_at else None,
            "creative_score": analysis.creative_score,
            "dimensions": _dimension_list(analysis, d),
            "value_proposition": d.get("value_proposition"),
            "feature_focus": facts["feature_focus"],
            "ai_mention": facts["ai_mention"],  # descriptive data: never a bonus or a penalty
            "format": facts["format"],
            "language": facts["language"],
            "has_voiceover": facts["has_voiceover"],
            "transcript": d.get("transcript") or [],
            "hook_description": d.get("hook_description") or d.get("main_promise"),
            "target_niche": d.get("target_niche"),
            "text_in_image": d.get("text_in_image"),
            "what_to_model": d.get("what_to_model"),
            "what_to_beat": d.get("what_to_beat"),
            "cosmiq_adaptation_idea": d.get("cosmiq_adaptation_idea"),
            "confidence": d.get("confidence"),
        }
    evaluable = ad.status == STATUS_PENDING and is_evaluable(ad)
    return {
        "ad": {
            "id": ad.id, "page_name": ad.page_name, "competitor": ad.competitor.name if ad.competitor else None,
            "pillar": ad.pillar, "status": ad.status, "is_active": bool(ad.is_active), "days_active": ad.days_active or 0,
            "start_date": ad.start_date.isoformat() if ad.start_date else None,
            "stop_date": ad.stop_date.isoformat() if ad.stop_date else None,
            "countries": from_json(ad.country_list, []) or [], "platforms": from_json(ad.platforms, []) or [],
            "headline": ad.headline, "body_text": ad.body_text, "cta_text": ad.cta_text,
            "link_url": safe_http_url(ad.link_url), "duration_s": ad.media_info.duration_s if ad.media_info else None,
        },
        "evaluation": evaluation,
        "market": {
            "market_score": signal.market_score if signal else None,
            "market_components": from_json(signal.market_components, None) if signal else None,
            "jinora_score": signal.jinora_score if signal else None,
        },
        "variants": variants,
        "media": {"video_url": f"/api/media/{ad.id}?kind=video" if ad.video_path else None,
                  "image_url": f"/api/media/{ad.id}?kind=image" if ad.image_path else None},
        "local_transcript": from_json(forensic.result_json, {}).get("full_transcript") if forensic else None,
        "can_enqueue": evaluable and analysis is None,
        "enqueue_reason": None if evaluable else ("sin archivo local" if ad.status == STATUS_PENDING else f"etapa del pipeline: {ad.status}"),
    }


def competitors_overview(db: Session, include_empty: bool = False) -> List[Dict[str, Any]]:
    """Per competitor: counts, media formats, average stored scores, recurring feature_focus and month-by-month evolution."""
    ads = (
        db.query(Ad)
        .options(selectinload(Ad.analyses), selectinload(Ad.market_signal))
        .filter(Ad.status.notin_(EXCLUDED_FROM_QUEUE), Ad.competitor_id.isnot(None))
        .all()
    )
    by_comp: Dict[int, List[Ad]] = defaultdict(list)
    for ad in ads:
        by_comp[ad.competitor_id].append(ad)

    out = []
    for comp in db.query(Competitor).order_by(Competitor.priority, Competitor.name).all():
        group = by_comp.get(comp.id, [])
        if not group and not include_empty:
            continue
        creative, jinora = [], []
        focus, formats_eval, ai_mention = Counter(), Counter(), Counter()
        months: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"ads": 0, "evaluated": 0, "scores": []})
        evaluated = 0
        for ad in group:
            month = ad.start_date.strftime("%Y-%m") if ad.start_date else "sin fecha"
            months[month]["ads"] += 1
            analysis = _latest_scored(ad)
            if analysis is None:
                continue
            evaluated += 1
            facts = _eval_facts(analysis)
            creative.append(analysis.creative_score)
            months[month]["evaluated"] += 1
            months[month]["scores"].append(analysis.creative_score)
            if ad.market_signal and ad.market_signal.jinora_score is not None:
                jinora.append(ad.market_signal.jinora_score)
            focus.update(facts["feature_focus"])
            if facts["format"]:
                formats_eval[facts["format"]] += 1
            if facts["ai_mention"]:
                ai_mention[facts["ai_mention"]] += 1
        timeline = [
            {"month": m, "ads": v["ads"], "evaluated": v["evaluated"],
             "avg_creative_score": round(sum(v["scores"]) / len(v["scores"]), 1) if v["scores"] else None}
            for m, v in sorted(months.items(), key=lambda kv: (kv[0] == "sin fecha", kv[0]))
        ]
        out.append({
            "competitor_id": comp.id, "name": comp.name, "pillar": comp.pillar, "priority": comp.priority,
            "ads_total": len(group),
            "active_ads": sum(1 for a in group if a.is_active),
            "evaluated": evaluated,
            "pending": sum(1 for a in group if a.status == STATUS_PENDING and _latest_scored(a) is None),
            "media_formats": dict(Counter(ad_format(a) for a in group)),
            "creative_formats": dict(formats_eval),
            "avg_creative_score": round(sum(creative) / len(creative), 1) if creative else None,
            "avg_jinora_score": round(sum(jinora) / len(jinora), 1) if jinora else None,
            "recurring_angles": [{"tag": t, "count": c} for t, c in focus.most_common(6)],
            "ai_mention": dict(ai_mention),  # descriptive distribution
            "timeline": timeline,
        })
    out.sort(key=lambda c: (-c["active_ads"], c["name"]))
    return out


def facets(db: Session) -> Dict[str, Any]:
    """Values available for the filter controls (the whitelists are the same ones parse_filters enforces)."""
    ads = _candidate_ads(db, TopFilters())
    countries, languages, formats = set(), set(), set()
    for ad in ads:
        countries.update(c for c in (from_json(ad.country_list, []) or []) if _COUNTRY_RE.match(str(c)))
        analysis = _latest_scored(ad)
        if analysis:
            facts = _eval_facts(analysis)
            if facts["language"] and _LANGUAGE_RE.match(facts["language"]):
                languages.add(facts["language"])
            if facts["format"] in FORMATS:
                formats.add(facts["format"])
    return {
        "pillars": list(PILLARS),
        "countries": sorted(countries),
        "competitors": [c.name for c in db.query(Competitor).order_by(Competitor.name).all()],
        "formats": sorted(formats),
        "languages": sorted(languages),
        "statuses": list(STATUS_FILTER),
    }
