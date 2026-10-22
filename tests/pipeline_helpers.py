"""Shared fakes and seeders for the Phase 7 tests (0 external APIs, temporary database, temporary downloads dir)."""

from __future__ import annotations

import collections
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from PIL import Image

from src.ai.quota import QuotaExhausted
from src.collect.relevance import RelevanceResult
from src.database import SessionLocal, mark_evaluated
from src.db.migrate import current_rubric_versions
from datetime import datetime

from src.db.models import Ad, Analysis, Competitor, MarketSignal, Media, STATUS_PENDING

RUBRIC = current_rubric_versions()


class Interrupted(BaseException):
    """Simulates the process dying mid-run (BaseException: nothing in the pipeline may swallow it)."""


def seed_competitors() -> Dict[str, int]:
    """A (priority 1, page 1), B (priority 2, page 2). Returns name -> id."""
    with SessionLocal() as s:
        a = Competitor(name="Alpha CRM", pillar="crm", priority=1, page_ids=json.dumps(["1"]))
        b = Competitor(name="Beta Chat", pillar="conversacional", priority=2, page_ids=json.dumps(["2"]))
        s.add_all([a, b])
        s.commit()
        return {"Alpha CRM": a.id, "Beta Chat": b.id}


def scraper_ad(ad_id: str, page_id: str = "1", page_name: str = "Alpha CRM", days: int = 30, video: bool = True, **extra) -> Dict[str, Any]:
    """An ad dict shaped like MetaAdsHunter._ad_to_dict output."""
    ad = {
        "ad_id": ad_id, "page_id": page_id, "page_name": page_name, "is_active": True, "days_active": days,
        "delivery_start_time": "2026-09-01T07:00:00+00:00", "publisher_platforms": ["FACEBOOK", "INSTAGRAM"],
        "creatives": [{"index": 0, "title": f"Titulo {ad_id}", "body": f"Cuerpo {ad_id}", "cta_text": "Comprar",
                       "link_url": "https://x.test", "has_video": video, "video_hd_url": "https://cdn.test/v.mp4" if video else None,
                       "image_url": "https://cdn.test/i.jpg"}],
    }
    ad.update(extra)
    return ad


class FakeHunter:
    """Stands in for MetaAdsHunter: canned search results; download_ad writes files and counts real 'fetches'."""

    def __init__(self, downloads_dir: Path, ads_by_query: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                 interrupt_on_download: Optional[int] = None):
        self.downloads_dir = Path(downloads_dir)
        self.ads_by_query = ads_by_query or {}
        self.interrupt_on_download = interrupt_on_download
        self.fetches: collections.Counter = collections.Counter()  # file -> times it was actually written
        self.download_calls: List[str] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.closed = False

    def search_ads(self, query: str = "", country: str = "US", media_filter: str = "all", limit: int = 10, competitor=None,
                   filter_relevance: bool = True, **kwargs):
        self.search_calls.append({"query": query, "country": country, "limit": limit, "competitor": competitor, "filter_relevance": filter_relevance})
        return [dict(a) for a in self.ads_by_query.get(query, [])][:limit]

    def download_ad(self, ad_data: Dict[str, Any], subfolder: str = "general") -> Dict[str, Any]:
        self.download_calls.append(str(ad_data["ad_id"]))
        target = self.downloads_dir / subfolder / str(ad_data["ad_id"])
        target.mkdir(parents=True, exist_ok=True)
        files = []
        for name, writer in (("video_0.mp4", lambda p: p.write_bytes(b"v" * 64)), ("image_0.jpg", lambda p: Image.new("RGB", (30, 20), "red").save(p))):
            if name.endswith(".mp4") and not any(c.get("has_video") for c in ad_data.get("creatives", [])):
                continue
            path = target / name
            if not path.exists():  # a real downloader never refetches a file that is already on disk
                writer(path)
                self.fetches[str(path)] += 1
            files.append(f"{subfolder}/{ad_data['ad_id']}/{name}")
        if self.interrupt_on_download is not None and len(self.download_calls) == self.interrupt_on_download:
            raise Interrupted()  # the file is on disk but nothing was recorded yet
        return {**ad_data, "downloaded_files": files}

    def close(self) -> None:
        self.closed = True


class FakeRelevance:
    def __init__(self, irrelevant: Iterable[str] = (), review: Iterable[str] = (), pending: Iterable[str] = ()):
        self.irrelevant, self.review, self.pending = set(irrelevant), set(review), set(pending)
        self.calls = 0

    def filter_ads(self, ads):
        self.calls += 1
        out = []
        for ad in ads:
            ad_id = str(ad["ad_id"])
            out.append((ad, RelevanceResult(
                ad_id=ad_id, is_relevant=ad_id not in self.irrelevant, pillar="crm", is_competitor_of_cosmiq=True,
                reason="fake", pending_relevance=ad_id in self.pending, needs_manual_review=ad_id in self.review,
            )))
        return out


class FakeEvaluator:
    """evaluate_fn replacement: writes a real Analysis row like the evaluators do, with configurable misbehavior."""

    def __init__(self, score: int = 70, quota_ids: Iterable[str] = (), fail_ids: Iterable[str] = (),
                 pending_ids: Iterable[str] = (), interrupt_after_write: Iterable[str] = ()):
        self.score = score
        self.quota_ids, self.fail_ids, self.pending_ids = set(quota_ids), set(fail_ids), set(pending_ids)
        self.interrupt_after_write = set(interrupt_after_write)
        self.calls: List[str] = []
        self.kwargs: List[Dict[str, Any]] = []

    def __call__(self, ad_dict: Dict[str, Any], force: bool = False, prompt_version: Optional[str] = None) -> Dict[str, Any]:
        ad_id = str(ad_dict["ad_id"])
        self.calls.append(ad_id)
        self.kwargs.append({"force": force, "prompt_version": prompt_version})
        if ad_id in self.quota_ids:
            raise QuotaExhausted(model="m", day_pt="2026-10-22", requests_used=1, limit=1, reason="429 diario")
        if ad_id in self.fail_ids:
            raise RuntimeError("boom")
        if ad_id in self.pending_ids:
            return {"ad_id": ad_id, "status": "pending_evaluation", "notice": "Claude aplazado"}
        a_type = "video" if ad_dict["creatives"][0]["has_video"] else "static"
        with SessionLocal() as db:
            ad = db.get(Ad, ad_id)
            db.add(Analysis(ad_id=ad_id, model="fake", prompt_version=prompt_version or "v1", rubric_version=RUBRIC[a_type],
                            analysis_type=a_type, creative_score=self.score, result_json=json.dumps({"format": "ugc"})))
            mark_evaluated(ad)
            db.commit()
        if ad_id in self.interrupt_after_write:
            raise Interrupted()
        return {"ad_id": ad_id, "creative_score": self.score}


def add_ad(ad_id: str, downloads: Path, competitor_id: Optional[int] = None, *, status: str = STATUS_PENDING, days: int = 10,
           variants: Optional[int] = None, active: bool = True, video: bool = True, with_media: bool = True,
           pillar: str = "crm", page_name: str = "Alpha CRM") -> None:
    """Inserts an ad (and its media file on disk) directly in the database."""
    with SessionLocal() as s:
        ad = Ad(id=ad_id, competitor_id=competitor_id, page_name=page_name, days_active=days, is_active=active, status=status,
                variant_count=variants, pillar=pillar, has_video=video, headline="h", body_text="b")
        s.add(ad)
        if with_media:
            rel = f"{page_name}/{ad_id}/{'video_0.mp4' if video else 'image_0.jpg'}"
            f = downloads / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x" * 64)
            ad.media.append(Media(kind="video" if video else "image", rel_path=rel, size=64))
        s.commit()


def video_result(fmt: str = "ugc", language: str = "es", voice: bool = True, focus=("automatizacion",), ai: str = "ninguna",
                 scores=(20, 18, 16, 14), transcript_text: str = "hola equipo", **over) -> Dict[str, Any]:
    """A stored video evaluation (result_json) with the shape the evaluators persist."""
    keys = ("hook", "mechanism", "narrative", "offer")
    dims = {k: {"score": s, "anchor_level": "13-19", "reasoning": f"razón {k}",
                "evidence": [{"timestamp": "00:02-00:05", "source": "audio", "quote": f"cita {k}"}]} for k, s in zip(keys, scores)}
    result = {
        "transcript": [{"start_s": 0.0, "end_s": 2.0, "text": transcript_text}], "has_voiceover": voice, **dims,
        "hook_description": "gancho de prueba", "value_proposition": "propuesta de prueba", "feature_focus": list(focus),
        "ai_mention": ai, "format": fmt, "language": language, "target_niche": "clínicas", "what_to_model": "copiar esto",
        "what_to_beat": "superar esto", "cosmiq_adaptation_idea": "idea de prueba", "confidence": 0.9,
        "creative_score": sum(scores),
    }
    result.update(over)
    return result


def add_evaluated_ad(ad_id: str, downloads: Path, competitor_id: Optional[int] = None, *, jinora: Optional[float] = 70.0,
                     market: Optional[float] = 60.0, group: Optional[str] = None, days: int = 30, status: str = "evaluated",
                     result: Optional[Dict[str, Any]] = None, rubric: Optional[str] = None, pillar: str = "crm",
                     page_name: str = "Alpha CRM", countries=("PE",), start: Optional[datetime] = None, video: bool = True,
                     headline: str = "h", body: str = "b", link: Optional[str] = None, duration: Optional[float] = None,
                     with_media: bool = True, active: bool = True, a_type: str = "video") -> None:
    """Inserts an evaluated ad: Ad + media file + Analysis + MarketSignal, straight into the (temporary) database."""
    result = result or video_result()
    with SessionLocal() as s:
        ad = Ad(id=ad_id, competitor_id=competitor_id, page_name=page_name, pillar=pillar, status=status, days_active=days,
                is_active=active, has_video=video, creative_group_id=group, country_list=json.dumps(list(countries)),
                start_date=start or datetime(2026, 9, 1), headline=headline, body_text=body, link_url=link)
        s.add(ad)
        if with_media:
            files = [("video", f"{page_name}/{ad_id}/video_0.mp4")] if video else []
            files.append(("image", f"{page_name}/{ad_id}/image_0.jpg"))
            for kind, rel in files:
                f = downloads / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"0123456789" * 10)
                ad.media.append(Media(kind=kind, rel_path=rel, size=100))
        s.flush()
        s.add(Analysis(ad_id=ad_id, model="fake-model", prompt_version="v1", rubric_version=rubric or RUBRIC[a_type], analysis_type=a_type,
                       creative_score=int(result.get("creative_score", 68)), result_json=json.dumps(result)))
        s.add(MarketSignal(ad_id=ad_id, market_score=market, jinora_score=jinora,
                           market_components=json.dumps({"used": {"longevity": {"raw": {"days": days}, "value": 0.5, "weight": 0.4, "effective_weight": 0.4}}, "missing": ["countries"]})))
        if duration:
            from src.db.models import MediaInfo
            ad.media_info = MediaInfo(duration_s=duration)
        s.commit()
