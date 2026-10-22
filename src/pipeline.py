"""
Unified pipeline (Phase 7, B1): discover -> filter -> download -> media -> evaluate -> signals.

Every stage is idempotent (it only touches what is not processed yet) and records its progress in Ad.status:

  discover  new ads            -> discovered        (existing ads keep their status; media URLs are queued as
                                                      per-ad 'download' jobs so a crash never loses them)
  filter    discovered         -> filtered | rejected | out_of_catalog | needs_manual_review | inactive_data_only
  download  filtered           -> downloaded        (files already on disk are never fetched again)
  media     downloaded         -> pending_evaluation (hashes, pHash, container metadata; optional Whisper)
  evaluate  pending_evaluation -> evaluated         (priority queue; videos -> Gemini, statics -> Claude)
  signals   evaluated          -> scored            (market_score / jinora_score, local only)

An interrupted run is resumed by simply running it again: finished work is detected from the data (files on disk,
Media rows, Analysis rows), not from memory. A daily 429 stops the evaluation without counting failures.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from src import jobs
from src.ai.quota import QuotaExhausted, get_quota_guard
from src.collect.ratelimit import MetaBlockedError
from src.config import settings
from src.cost import CostSnapshot, cost_delta, format_cost_summary
from src.database import SessionLocal, register_downloaded_media, save_forensics, save_or_update_ad_record
from src.db.competitors import normalize_name
from src.db.models import (
    STATUS_DISCOVERED,
    STATUS_DOWNLOADED,
    STATUS_EVALUATED,
    STATUS_FILTERED,
    STATUS_INACTIVE_DATA_ONLY,
    STATUS_NEEDS_REVIEW,
    STATUS_OUT_OF_CATALOG,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SCORED,
    Ad,
    Competitor,
    MediaInfo,
    from_json,
)
from src.work_queue import ad_eval_dict, ad_format, has_scored_analysis, select_queue

logger = logging.getLogger("Jinora.Pipeline")

STAGES = ("discover", "filter", "download", "media", "evaluate", "signals")
MAX_DOWNLOAD_ATTEMPTS = 3
INACTIVE_MIN_DAYS = 60  # inactive ads shorter than this are kept as data only (Phase 3 policy)


class StopRequested(Exception):
    """Raised between units of work when the caller asked the pipeline to stop (worker shutdown)."""


@dataclass
class RunOptions:
    pillar: Optional[str] = None
    competitor: Optional[str] = None
    query: Optional[str] = None  # ad-hoc brand/keyword search (API /api/search)
    all_pillars: bool = False
    countries: List[str] = field(default_factory=lambda: list(settings.DEFAULT_COUNTRIES))
    limit: int = 20  # ads per query
    stages: Sequence[str] = STAGES
    media_filter: str = "all"  # video | image | all
    keywords: bool = False  # also discover with the pillar keywords
    forensics: bool = False  # local Whisper transcription in the media stage
    download_min_days: int = 0  # only download ads running at least N days
    max_ads: Optional[int] = None  # stop discovering after N ads seen
    force: bool = False  # re-evaluate even if already evaluated
    prompt_version: Optional[str] = None
    eval_limit: Optional[int] = None
    ad_ids: Optional[List[str]] = None  # evaluate only these ads (API "analyze these")

    def __post_init__(self) -> None:
        self.stages = tuple(self.stages)
        unknown = [s for s in self.stages if s not in STAGES]
        if unknown:
            raise ValueError(f"Etapas desconocidas: {unknown}. Válidas: {', '.join(STAGES)}")
        self.countries = [c.strip().upper() for c in self.countries if c and c.strip()]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunOptions":
        allowed = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**allowed)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["stages"] = list(self.stages)
        return d


def compute_media_info(db, ad: Ad, downloads_dir: Path, forensics_fn: Optional[Callable[[Path], Dict[str, Any]]] = None) -> None:
    """Local media facts for one ad: size/sha256 per file, pHash of the image, container metadata, optional Whisper."""
    from src.media.fingerprint import compute_file_sha256, compute_image_phash
    from src.media.media_info import get_image_info, get_video_info

    info = ad.media_info
    if info is None:
        info = MediaInfo()
        ad.media_info = info

    by_kind: Dict[str, Any] = {}
    for m in ad.media:
        f = downloads_dir / m.rel_path
        if not f.exists():
            continue
        m.size = f.stat().st_size
        if not m.sha256:
            m.sha256 = compute_file_sha256(f)
        by_kind[m.kind] = (m, f)

    primary = by_kind.get("video") or by_kind.get("image")
    if primary:
        info.sha256 = primary[0].sha256
    if "image" in by_kind:
        info.phash = info.phash or compute_image_phash(by_kind["image"][1])
    try:
        probe: Dict[str, Any] = {}
        if "video" in by_kind:
            probe = get_video_info(by_kind["video"][1])
            info.duration_s = probe.get("duration_s") or None  # 0 means "unknown"
            info.has_audio_track = probe.get("has_audio_track")
        if not (probe.get("width") and probe.get("height")) and "image" in by_kind:
            probe = {**probe, **get_image_info(by_kind["image"][1])}  # no video dimensions: use the thumbnail's
        info.width, info.height = probe.get("width") or None, probe.get("height") or None
        info.aspect_ratio = probe.get("aspect_ratio") if info.width else None
    except Exception as exc:  # libmediainfo missing / corrupt file: hashes are still useful
        logger.debug(f"Sin metadata de contenedor para {ad.id}: {exc}")

    if forensics_fn is not None and "video" in by_kind:
        save_forensics(db, ad, forensics_fn(by_kind["video"][1]))


class Pipeline:
    def __init__(
        self,
        hunter_factory: Optional[Callable[[], Any]] = None,
        relevance_filter_factory: Optional[Callable[[], Any]] = None,
        evaluate_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        quota_check: Optional[Callable[[], None]] = None,
        forensics_factory: Optional[Callable[[], Callable[[Path], Dict[str, Any]]]] = None,
        signals_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
        downloads_dir: Optional[Path] = None,
    ) -> None:
        self._hunter_factory = hunter_factory
        self._relevance_factory = relevance_filter_factory
        self._evaluate_fn = evaluate_fn
        self._quota_check = quota_check
        self._forensics_factory = forensics_factory
        self._signals_fn = signals_fn
        self.stop_check = stop_check or (lambda: False)
        self.downloads_dir = Path(downloads_dir) if downloads_dir else settings.downloads_path
        self._hunter = None
        self._relevance = None
        self._forensics = None

    # ---- lazy collaborators (so stages that do not need them never import or build them) ----

    def _get_hunter(self):
        if self._hunter is None:
            if self._hunter_factory:
                self._hunter = self._hunter_factory()
            else:
                from src.scraper import MetaAdsHunter

                self._hunter = MetaAdsHunter(output_dir=str(self.downloads_dir))
        return self._hunter

    def _get_relevance(self):
        if self._relevance is None:
            if self._relevance_factory:
                self._relevance = self._relevance_factory()
            else:
                from src.collect.relevance import TwoLayerRelevanceFilter

                self._relevance = TwoLayerRelevanceFilter()
        return self._relevance

    def _get_evaluate_fn(self):
        if self._evaluate_fn is None:
            from src.ai.router import route_and_evaluate_ad

            self._evaluate_fn = route_and_evaluate_ad
        return self._evaluate_fn

    def _check_quota(self) -> None:
        """Pre-flight for Gemini: raises QuotaExhausted when today's usable requests are gone."""
        if self._quota_check is not None:
            self._quota_check()
        else:
            guard = get_quota_guard()
            guard.check_available(settings.LLM_MODEL, key_fingerprint=guard.key_fingerprint)

    def _check_stop(self) -> None:
        if self.stop_check():
            raise StopRequested()

    def close(self) -> None:
        if self._hunter is not None and hasattr(self._hunter, "close"):
            self._hunter.close()
        self._hunter = None

    # ---- scoping ----

    def _scope(self, q, opts: RunOptions, db):
        """Restricts an Ad query to what this run is about (competitor / pillar / ad-hoc query)."""
        if opts.competitor:
            comp = self._find_competitor(db, opts.competitor)
            # its own ads plus the ones its search brought in from other pages (those become out_of_catalog)
            q = q.filter((Ad.competitor_id == comp.id) | (Ad.search_query == comp.name))
        elif opts.query:
            q = q.filter(Ad.search_query == opts.query)
        elif opts.pillar:
            q = q.filter(Ad.pillar == opts.pillar)
        return q

    @staticmethod
    def _find_competitor(db, name: str) -> Competitor:
        wanted = normalize_name(name)
        for comp in db.query(Competitor).all():
            if normalize_name(comp.name) == wanted:
                return comp
        raise ValueError(f"Competidor '{name}' no está en el catálogo (tabla competitors).")

    def _targets(self, db, opts: RunOptions) -> List[Dict[str, Any]]:
        targets: List[Dict[str, Any]] = []
        if opts.query:
            targets.append({"name": opts.query, "page_ids": [], "pillar": opts.pillar or "general", "kind": "query"})
        else:
            use_competitors = bool(opts.competitor or opts.pillar or opts.all_pillars)
            if not use_competitors and not opts.keywords:
                raise ValueError("Indica --pillar, --competitor, --query o --all.")
            if use_competitors:
                if db.query(Competitor).count() == 0:
                    raise ValueError("El catálogo de competidores está vacío. Ejecuta primero: python -m src.db.migrate")
                q = db.query(Competitor)
                if opts.competitor:
                    q = q.filter(Competitor.id == self._find_competitor(db, opts.competitor).id)
                elif opts.pillar:
                    q = q.filter(Competitor.pillar == opts.pillar)
                for comp in q.order_by(Competitor.priority, Competitor.name).all():
                    targets.append({"name": comp.name, "page_ids": from_json(comp.page_ids, []), "pillar": comp.pillar or "general", "kind": "competitor"})
        if opts.keywords:
            from src.pillars import PILLARS

            for pillar in ([opts.pillar] if opts.pillar else list(PILLARS)):
                for kw in PILLARS.get(pillar, {}).get("keywords", []):
                    targets.append({"name": kw, "page_ids": [], "pillar": pillar, "kind": "keyword"})
        return targets

    # ---- stages ----

    def stage_discover(self, opts: RunOptions) -> Dict[str, Any]:
        stats = {"queries": 0, "seen": 0, "new": 0, "updated": 0, "errors": 0, "blocked": False, "download_jobs": 0}
        with SessionLocal() as db:
            targets = self._targets(db, opts)
        hunter = self._get_hunter()
        for target in targets:
            for country in opts.countries:
                if opts.max_ads and stats["seen"] >= opts.max_ads:
                    return stats
                self._check_stop()
                stats["queries"] += 1
                try:
                    ads = hunter.search_ads(
                        query=target["name"], country=country, media_filter=opts.media_filter, limit=opts.limit,
                        competitor={"name": target["name"], "page_ids": target["page_ids"]} if target["page_ids"] else None,
                        filter_relevance=False,
                    )
                except MetaBlockedError as exc:
                    logger.error(f"Meta bloquea las consultas; se detiene el descubrimiento: {exc}")
                    stats["blocked"] = True
                    return stats
                except Exception as exc:
                    stats["errors"] += 1
                    logger.warning(f"Error buscando '{target['name']}' en {country}: {exc}")
                    continue
                for ad in ads:
                    if opts.max_ads and stats["seen"] >= opts.max_ads:
                        break
                    self._check_stop()
                    self._store_discovered(ad, target, country, stats)
        return stats

    def _store_discovered(self, ad: Dict[str, Any], target: Dict[str, Any], country: str, stats: Dict[str, Any]) -> None:
        ad_id = str(ad.get("ad_id"))
        with SessionLocal() as db:
            existed = db.get(Ad, ad_id) is not None
        save_or_update_ad_record(ad, pillar=target["pillar"], search_query=target["name"], status=STATUS_DISCOVERED, country=country)
        stats["seen"] += 1
        stats["updated" if existed else "new"] += 1
        with SessionLocal() as db:
            record = db.get(Ad, ad_id)
            has_files = bool(record.media) and all((self.downloads_dir / m.rel_path).exists() for m in record.media)
        if not has_files:
            # Media URLs expire, so they travel with the job and are refreshed by every discovery
            jobs.upsert_pending("download", ad_id, {"ad": ad, "subfolder": ad.get("page_name") or target["name"]})
            stats["download_jobs"] += 1

    def stage_filter(self, opts: RunOptions) -> Dict[str, Any]:
        stats = {"checked": 0, "filtered": 0, "rejected": 0, "out_of_catalog": 0, "needs_manual_review": 0,
                 "inactive_data_only": 0, "pending_relevance": 0}
        with SessionLocal() as db:
            ads = self._scope(db.query(Ad), opts, db).filter(Ad.status == STATUS_DISCOVERED).all()
            if not ads:
                return stats
            dicts = [self._filter_dict(a) for a in ads]
            by_id = {a.id: a for a in ads}
            for ad_dict, rel in self._get_relevance().filter_ads(dicts):
                self._check_stop()
                ad = by_id[str(ad_dict["ad_id"])]
                stats["checked"] += 1
                if rel.pending_relevance:
                    stats["pending_relevance"] += 1  # could not classify now (quota): stays 'discovered'
                    continue
                ad.relevance_json = json.dumps(rel.model_dump(), ensure_ascii=False)
                if rel.needs_manual_review:
                    ad.status = STATUS_NEEDS_REVIEW
                elif not rel.is_relevant:
                    ad.status = STATUS_REJECTED
                elif ad.competitor_id is None:
                    ad.status = STATUS_OUT_OF_CATALOG
                elif not ad.is_active and (ad.days_active or 0) <= INACTIVE_MIN_DAYS:
                    ad.status = STATUS_INACTIVE_DATA_ONLY
                else:
                    ad.status = STATUS_FILTERED
                key = {STATUS_NEEDS_REVIEW: "needs_manual_review", STATUS_REJECTED: "rejected", STATUS_OUT_OF_CATALOG: "out_of_catalog",
                       STATUS_INACTIVE_DATA_ONLY: "inactive_data_only", STATUS_FILTERED: "filtered"}[ad.status]
                stats[key] += 1
            db.commit()
        return stats

    @staticmethod
    def _filter_dict(ad: Ad) -> Dict[str, Any]:
        return {
            "ad_id": ad.id, "page_id": ad.page_id, "page_name": ad.page_name, "is_active": ad.is_active,
            "days_active": ad.days_active, "publisher_platforms": from_json(ad.platforms, []),
            "creatives": [{"title": ad.headline, "body": ad.body_text, "link_url": ad.link_url, "cta_text": ad.cta_text}],
        }

    def stage_download(self, opts: RunOptions) -> Dict[str, Any]:
        stats = {"candidates": 0, "downloaded": 0, "already_on_disk": 0, "skipped_min_days": 0, "no_source": 0, "failed": 0}
        with SessionLocal() as db:
            ad_ids = [a.id for a in self._scope(db.query(Ad), opts, db).filter(Ad.status == STATUS_FILTERED).order_by(Ad.id).all()]
        pending = {j["ad_id"]: j for j in jobs.pending_jobs("download", ad_ids)}
        for ad_id in ad_ids:
            self._check_stop()
            stats["candidates"] += 1
            with SessionLocal() as db:
                ad = db.get(Ad, ad_id)
                if opts.download_min_days and (ad.days_active or 0) < opts.download_min_days:
                    stats["skipped_min_days"] += 1
                    continue
                if ad.media and all((self.downloads_dir / m.rel_path).exists() for m in ad.media):
                    ad.status = STATUS_DOWNLOADED  # a previous run got this far
                    db.commit()
                    if ad_id in pending:
                        jobs.complete(pending[ad_id]["id"], {"already_on_disk": True})
                    stats["already_on_disk"] += 1
                    continue
            job = pending.get(ad_id)
            if job is None:
                stats["no_source"] += 1  # no stored media URLs: needs a new discover
                continue
            try:
                result = self._get_hunter().download_ad(job["payload"]["ad"], subfolder=job["payload"]["subfolder"])
            except Exception as exc:
                stats["failed"] += 1
                logger.warning(f"Descarga fallida para {ad_id}: {exc}")
                if (job["attempts"] or 0) + 1 >= MAX_DOWNLOAD_ATTEMPTS:
                    jobs.fail(job["id"], str(exc))
                else:
                    jobs.note_error(job["id"], str(exc))
                continue
            with SessionLocal() as db:
                ad = db.get(Ad, ad_id)
                register_downloaded_media(db, ad, result.get("downloaded_files", []))
                ad.status = STATUS_DOWNLOADED
                db.commit()
            jobs.complete(job["id"], {"files": result.get("downloaded_files", [])})
            stats["downloaded"] += 1
        return stats

    def stage_media(self, opts: RunOptions) -> Dict[str, Any]:
        stats = {"processed": 0, "forensics": 0}
        forensics_fn = None
        if opts.forensics:
            if self._forensics is None and self._forensics_factory:
                self._forensics = self._forensics_factory()
            elif self._forensics is None:
                from src.video_forensics import VideoForensicsEngine

                engine = VideoForensicsEngine("base")
                self._forensics = engine.analyze_video
            forensics_fn = self._forensics
        with SessionLocal() as db:
            ad_ids = [a.id for a in self._scope(db.query(Ad), opts, db).filter(Ad.status == STATUS_DOWNLOADED).order_by(Ad.id).all()]
        for ad_id in ad_ids:
            self._check_stop()
            with SessionLocal() as db:
                ad = db.get(Ad, ad_id)
                compute_media_info(db, ad, self.downloads_dir, forensics_fn)
                ad.status = STATUS_PENDING
                db.commit()
            stats["processed"] += 1
            stats["forensics"] += bool(forensics_fn)
        return stats

    def stage_evaluate(self, opts: RunOptions) -> Dict[str, Any]:
        stats = {"queue": 0, "evaluated": 0, "skipped_already_evaluated": 0, "deferred": 0, "failed": 0,
                 "quota_stopped": False, "claude_blocked": False}
        with SessionLocal() as db:
            competitor_id = self._find_competitor(db, opts.competitor).id if opts.competitor else None
            queue = select_queue(db, pillar=opts.pillar if not opts.competitor else None, competitor_id=competitor_id,
                                 include_evaluated=opts.force or bool(opts.ad_ids), downloads_dir=self.downloads_dir)
            if opts.ad_ids:
                queue = [a for a in queue if a.id in set(opts.ad_ids)]
            items = [(a.id, ad_format(a), ad_eval_dict(a)) for a in queue]
        stats["queue"] = len(items)
        done = 0
        for ad_id, fmt, ad_dict in items:
            self._check_stop()
            if opts.eval_limit and done >= opts.eval_limit:
                break
            if not opts.force:
                with SessionLocal() as db:
                    if has_scored_analysis(db, ad_id, opts.prompt_version):
                        ad = db.get(Ad, ad_id)  # an earlier run saved the analysis but died before the status update
                        ad.status = STATUS_EVALUATED
                        db.commit()
                        stats["skipped_already_evaluated"] += 1
                        continue
            if fmt != "video" and stats["claude_blocked"]:
                stats["deferred"] += 1
                continue
            try:
                if fmt == "video":
                    self._check_quota()
                result = self._get_evaluate_fn()(ad_dict, force=opts.force, prompt_version=opts.prompt_version)
            except QuotaExhausted as exc:
                # Daily 429 / cap: the ad stays pending_evaluation, it is NOT a failure; resume tomorrow
                logger.warning(f"Cuota diaria agotada: {exc}. Se detiene la evaluación; continúa mañana.")
                stats["quota_stopped"] = True
                break
            except Exception as exc:
                stats["failed"] += 1
                logger.error(f"Evaluación fallida para {ad_id}: {exc}")
                continue
            done += 1
            if result.get("status") == "pending_evaluation":
                stats["deferred"] += 1  # e.g. Claude key/workspace/budget: deferred, not failed
                stats["claude_blocked"] = True
                logger.warning(f"Evaluación aplazada para {ad_id}: {result.get('notice')}")
            else:
                stats["evaluated"] += 1
        return stats

    def stage_signals(self, opts: RunOptions) -> Dict[str, Any]:
        if self._signals_fn is None:
            from src.scoring.jinora import rescore_all

            self._signals_fn = rescore_all
        summary = dict(self._signals_fn())
        with SessionLocal() as db:
            ads = self._scope(db.query(Ad), opts, db).filter(Ad.status == STATUS_EVALUATED).all()
            scored = 0
            for ad in ads:
                if ad.market_signal is not None and ad.market_signal.market_score is not None:
                    ad.status = STATUS_SCORED
                    scored += 1
            db.commit()
        summary["marked_scored"] = scored
        return summary

    # ---- run ----

    def run(self, opts: RunOptions) -> Dict[str, Any]:
        """Runs the requested stages in pipeline order. Returns a summary; KeyboardInterrupt etc. propagate."""
        try:
            before = CostSnapshot.take()
        except Exception:
            before = None
        summary: Dict[str, Any] = {"options": opts.to_dict(), "stages": {}, "interrupted": False, "quota_stopped": False}
        try:
            for stage in STAGES:
                if stage not in opts.stages:
                    continue
                logger.info(f"▶ Etapa {stage}")
                try:
                    result = getattr(self, f"stage_{stage}")(opts)
                except StopRequested:
                    summary["interrupted"] = True
                    logger.warning(f"Corrida detenida a petición durante la etapa {stage}; se reanuda con el mismo comando.")
                    break
                summary["stages"][stage] = result
                summary["quota_stopped"] |= bool(result.get("quota_stopped"))
                logger.info(f"  {stage}: {result}")
        finally:
            self.close()
        if before is not None:
            try:
                summary["cost"] = cost_delta(before, CostSnapshot.take())
                logger.info(format_cost_summary(summary["cost"]))
            except Exception:  # cost reporting must never break a run
                pass
        return summary


def format_summary(summary: Dict[str, Any]) -> str:
    lines = ["=" * 70, "RESUMEN DE LA CORRIDA" + (" (INTERRUMPIDA: se reanuda con el mismo comando)" if summary["interrupted"] else ""), "=" * 70]
    for stage, res in summary["stages"].items():
        lines.append(f"{stage:<10} " + ", ".join(f"{k}={v}" for k, v in res.items()))
    if summary["quota_stopped"]:
        lines.append("⏸ Cuota diaria agotada: los anuncios siguen en pending_evaluation (sin contarse como fallidos); continúa mañana.")
    if summary.get("cost"):
        lines.append(format_cost_summary(summary["cost"]))
    return "\n".join(lines)
