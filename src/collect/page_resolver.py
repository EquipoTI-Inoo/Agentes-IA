"""
Jinora - Competitor Page Resolver
Resolves official Meta Ad Library page_ids for mapped competitors. NO Gemini calls are used in this module.

Guarantees
- It never guesses: a page is assigned ONLY when its name equals the competitor's name exactly or after normalization
  (lowercase letters and digits only, with or without the parenthesis). Similar names, verified badges and categories are
  never enough; those pages are only listed as review_candidates for a person.
- Link-domain verification (ads of the page pointing to the competitor's website) raises confidence to 'alta'.
- Every Meta query goes through MetaRateLimiter: a random 1-3 s pause and exponential backoff when Meta blocks;
  if the block persists the run stops (MetaBlockedError) and keeps what it already resolved.
- resolution_confidence, needs_review and resolved_at are stored for every competitor it processes.
- The JSON is backed up before the first change and rewritten atomically after EACH competitor, so a cut run resumes
  exactly where it stopped without repeating queries.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from meta_ads_collector import MetaAdsCollector
from meta_ads_collector.constants import SEARCH_PAGE
from meta_ads_collector.filters import FilterConfig

from src.collect.ratelimit import MetaBlockedError, MetaRateLimiter
from src.db.competitors import normalize_name

# Ensure stdout handles UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Jinora.PageResolver")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
COMPETITORS_FILE = BASE_DIR / "config" / "competitors_database.json"


def extract_base_domain(url: str) -> str:
    """Extract clean domain without protocol or www (e.g., 'https://www.kommo.com/es' -> 'kommo.com')."""
    if not url:
        return ""
    clean = url.strip()
    if not clean.startswith(("http://", "https://")):
        clean = "https://" + clean
    try:
        netloc = urlparse(clean).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        netloc = netloc.split(":")[0]
        parts = netloc.split(".")
        if len(parts) > 2 and parts[-2] not in ("com", "co", "org", "net", "gov", "edu"):
            netloc = ".".join(parts[-2:])
        elif len(parts) > 3:
            netloc = ".".join(parts[-3:])
        return netloc
    except Exception:
        return ""


def backup_competitors_file(db_path: Path) -> Path:
    """Copies the catalog next to itself as <name>.backup_resolve_<YYYYMMDD_HHMMSS>.json before anything changes."""
    db_path = Path(db_path)
    dst = db_path.with_name(f"{db_path.stem}.backup_resolve_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}")
    shutil.copy2(db_path, dst)
    return dst


def classify_competitor(comp: Dict[str, Any], retry_unresolved: bool = False) -> str:
    """
    What a run does with one competitor:
      skip_existing   already has page_ids (never overwritten; a 'baja' guess is only revisited with retry_unresolved)
      skip_attempted  was already tried and ended unresolved (only retried with retry_unresolved)
      process_new     never tried
      process_retry   tried before without a confirmed page (or a low-confidence guess) and retry_unresolved is on
    """
    has_ids = bool(comp.get("page_ids"))
    attempted = bool(comp.get("resolved_at"))
    if has_ids:
        return "process_retry" if (retry_unresolved and comp.get("resolution_confidence") == "baja") else "skip_existing"
    if attempted:
        return "process_retry" if retry_unresolved else "skip_attempted"
    return "process_new"


def meta_queries_for(comp: Dict[str, Any]) -> Tuple[int, int]:
    """(minimum, maximum) Meta queries to resolve one competitor: typeahead (1, or 2 with a parenthesis name) + up to 2 ads checks."""
    name = comp.get("name", "")
    stripped = re.sub(r"\(.*?\)", "", name).strip()
    typeahead_max = 2 if stripped and stripped.lower() != name.lower() else 1
    return 1, typeahead_max + (2 if comp.get("website") else 0)


def plan_resolution(data: Dict[str, Any], priorities: Tuple[int, ...] = (2, 3), retry_unresolved: bool = False) -> Dict[str, Any]:
    """Read-only plan for --dry-run: who would be processed, how many Meta queries and how long (1-3 s pause per query)."""
    rows: List[Dict[str, Any]] = []
    for pillar in data.get("pillars", {}).values():
        for comp in pillar.get("competitors", []):
            if comp.get("priority") in priorities:
                rows.append(comp)
    by_priority: Dict[int, Dict[str, Any]] = {}
    pending: List[Dict[str, Any]] = []
    for pr in priorities:
        sub = [c for c in rows if c.get("priority") == pr]
        kinds = [classify_competitor(c, retry_unresolved) for c in sub]
        by_priority[pr] = {
            "total": len(sub),
            "without_page_id": sum(1 for c in sub if not c.get("page_ids")),
            "never_attempted": kinds.count("process_new"),
            "unresolved_already_tried": sum(1 for c, k in zip(sub, kinds) if not c.get("page_ids") and c.get("resolved_at")),
            "low_confidence_with_id": sum(1 for c in sub if c.get("page_ids") and c.get("resolution_confidence") == "baja"),
            "to_process": sum(1 for k in kinds if k.startswith("process")),
        }
        pending += [c for c, k in zip(sub, kinds) if k.startswith("process")]
    qmin = sum(meta_queries_for(c)[0] for c in pending)
    qmax = sum(meta_queries_for(c)[1] for c in pending)
    qexp = round((qmin + qmax) / 2)
    return {
        "by_priority": by_priority,
        "to_process": len(pending),
        "names": [c["name"] for c in pending],
        "queries_min": qmin, "queries_max": qmax, "queries_expected": qexp,
        "seconds_min": qmin * 1.0, "seconds_max": qmax * 3.0, "seconds_expected": qexp * 2.0,  # average pause 2 s
    }


class PageResolver:
    """Discovers and verifies official Meta Ads Library page_ids for competitors."""

    def __init__(self, collector: Optional[MetaAdsCollector] = None, limiter: Optional[MetaRateLimiter] = None) -> None:
        self.collector = collector or MetaAdsCollector()
        self._own_collector = collector is None
        self.limiter = limiter or MetaRateLimiter()  # 1-3 s random pause per query + backoff when Meta blocks
        self.last_run: Dict[str, Any] = {}

    def close(self) -> None:
        if self._own_collector and self.collector:
            try:
                self.collector.close()
            except Exception:
                pass

    def __enter__(self) -> PageResolver:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _meta(self, fn, *args, **kwargs):
        """Every Meta query: random pause, call, backoff on blocks. Raises MetaBlockedError if the block persists."""
        return self.limiter.call(fn, *args, **kwargs)

    @staticmethod
    def _result(page_ids: List[str], confidence: str, needs_review: bool, note: str, candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "page_ids": page_ids,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolution_confidence": confidence,
            "needs_review": needs_review,
            "resolution_note": note,
        }
        if candidates:
            out["review_candidates"] = candidates
        return out

    def resolve_competitor(
        self,
        competitor: Dict[str, Any],
        country: Optional[str] = None,
        verify_ads_domain: bool = True,
    ) -> Dict[str, Any]:
        """
        Finds the official page_id by EXACT (or normalized) name; never by similarity.
        Returns page_ids, resolved_at, resolution_confidence ('alta' | 'media' | 'baja'), needs_review and, when nothing
        could be assigned, review_candidates (up to 3 similar pages for a person to look at).
        """
        name = competitor.get("name", "").strip()
        target_domain = extract_base_domain(competitor.get("website", ""))
        markets = competitor.get("markets", [])
        search_country = country or (markets[0] if markets else "US")

        logger.info(f"🔎 Resolviendo page_id para: '{name}' (Dominio esperado: {target_domain or 'N/A'}, país: {search_country})")

        queries = [name]
        name_no_parens = re.sub(r"\(.*?\)", "", name).strip()
        if name_no_parens and name_no_parens.lower() != name.lower():
            queries.append(name_no_parens)

        raw_candidates: List[Dict[str, Any]] = []
        for q in queries:
            found = self._meta(self.collector.client.search_pages, q, country=search_country)
            for f in found or []:
                if f.get("page_id") and not any(r.get("page_id") == f.get("page_id") for r in raw_candidates):
                    raw_candidates.append(f)
            if raw_candidates:
                break

        if not raw_candidates:
            logger.warning(f"  [X] No se encontraron páginas en Meta para '{name}'")
            return self._result([], "baja", True, "Meta no devolvió páginas para ese nombre")

        accepted_names = {normalize_name(name), normalize_name(name_no_parens)} - {""}
        exact = [c for c in raw_candidates if normalize_name(c.get("page_name", "")) in accepted_names]
        verified_first = lambda c: 0 if c.get("page_verified") in ("VERIFIED", "BLUE_VERIFIED") else 1

        if not exact:
            weak = sorted(raw_candidates, key=lambda c: (not (normalize_name(c.get("page_name", "")) and any(
                a in normalize_name(c.get("page_name", "")) or normalize_name(c.get("page_name", "")) in a for a in accepted_names)), verified_first(c)))
            review = [{"page_id": str(c["page_id"]), "page_name": c.get("page_name", "")} for c in weak[:3]]
            logger.warning(f"  [X] Ninguna página se llama exactamente '{name}': no se asigna nada (revisar a mano)")
            return self._result([], "baja", True, "ninguna página coincide exactamente con el nombre; no se adivina", review)

        exact.sort(key=verified_first)
        confirmed: List[Dict[str, Any]] = []
        contradicted = False
        for cand in exact[:2]:  # at most 2 ads checks
            cand_id = str(cand.get("page_id", ""))
            if not (verify_ads_domain and target_domain and cand_id):
                continue
            try:
                sample_ads = self._meta(lambda: list(self.collector.search(
                    query="", country=search_country, search_type=SEARCH_PAGE, page_ids=[cand_id], max_results=3)))
            except MetaBlockedError:
                raise
            except Exception as exc:  # could not verify this one; it stays an unconfirmed name match
                logger.debug(f"Error verificando anuncios para {cand_id}: {exc}")
                continue
            matched = any(
                target_domain in (ad_domain := extract_base_domain(c.link_url or "")) or (ad_domain and ad_domain in target_domain)
                for ad in sample_ads for c in (ad.creatives or [])
            )
            if matched:
                confirmed.append(cand)
                logger.info(f"  [✓] Dominio confirmado en anuncio para '{cand.get('page_name')}' (ID: {cand_id}): {target_domain}")
            elif sample_ads:
                contradicted = True

        if confirmed:
            ids = [str(c["page_id"]) for c in confirmed]
            logger.info(f"  -> Resultado: {ids} | Confianza: ALTA | needs_review=False")
            return self._result(ids, "alta", False, "nombre exacto y dominio confirmado en sus anuncios")
        if len(exact) == 1:
            only = exact[0]
            review = contradicted
            note = ("nombre exacto, pero los anuncios apuntan a otro dominio: confirmar a mano" if review
                    else "nombre exacto (dominio no verificable o sin anuncios)")
            logger.info(f"  -> Resultado: {only.get('page_name')} (ID: {only['page_id']}) | Confianza: MEDIA | needs_review={review}")
            return self._result([str(only["page_id"])], "media", review, note)

        review_list = [{"page_id": str(c["page_id"]), "page_name": c.get("page_name", "")} for c in exact[:3]]
        logger.warning(f"  [?] Varias páginas se llaman exactamente '{name}' y ninguna confirmó el dominio: no se asigna")
        return self._result([], "baja", True, "varias páginas con el mismo nombre exacto sin dominio confirmado; ambiguo", review_list)

    # ------------------------------------------------------------------ batch

    @staticmethod
    def _persist(data: Dict[str, Any], db_path: Path) -> None:
        """Atomic rewrite (temp file + replace): a cut never leaves a half-written catalog."""
        tmp = db_path.with_name(db_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, db_path)

    def resolve_all_priority(
        self,
        priority: int = 1,
        save: bool = True,
        db_path: Path = COMPETITORS_FILE,
        max_competitors: Optional[int] = None,
        retry_unresolved: bool = False,
        backup: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Resolves the competitors of one priority and saves progress after EACH one (resumable).
        Skips competitors that already have page_ids and, unless retry_unresolved, those already tried.
        A persistent Meta block stops the run (last_run['blocked']) keeping everything resolved so far.
        """
        db_path = Path(db_path)
        if not db_path.exists():
            raise FileNotFoundError(f"Competitors database not found: {db_path}")

        data = json.loads(db_path.read_text(encoding="utf-8"))
        self.last_run = {"priority": priority, "processed": 0, "skipped_existing": 0, "skipped_attempted": 0,
                         "errors": [], "blocked": False, "backup": None, "queries": 0}
        if save and backup:
            self.last_run["backup"] = backup_competitors_file(db_path).name
        queries_before = self.limiter.queries
        updated_list: List[Dict[str, Any]] = []

        try:
            for pillar_data in data.get("pillars", {}).values():
                for comp in pillar_data.get("competitors", []):
                    if comp.get("priority") != priority:
                        continue
                    if max_competitors and self.last_run["processed"] >= max_competitors:
                        return updated_list
                    action = classify_competitor(comp, retry_unresolved)
                    if action == "skip_existing":
                        logger.info(f"⏭️ Saltando '{comp['name']}': ya tiene page_ids ({comp['page_ids'][0]})")
                        self.last_run["skipped_existing"] += 1
                        continue
                    if action == "skip_attempted":
                        logger.info(f"⏭️ Saltando '{comp['name']}': ya se intentó y quedó sin resolver (usa --retry-unresolved)")
                        self.last_run["skipped_attempted"] += 1
                        continue

                    try:
                        res = self.resolve_competitor(comp)
                    except MetaBlockedError as exc:
                        logger.error(f"Meta sigue bloqueando; se detiene la resolución (lo hecho queda guardado): {exc}")
                        self.last_run["blocked"] = True
                        return updated_list
                    except Exception as exc:  # not a block: leave this one untouched so it is retried next run
                        logger.warning(f"Error resolviendo '{comp['name']}': {exc}")
                        self.last_run["errors"].append({"name": comp["name"], "error": str(exc)[:200]})
                        continue

                    previous = comp.get("page_ids") or []
                    if previous and not res["page_ids"]:
                        # a low-confidence guess from an older run is withdrawn, but kept for a person to review
                        res.setdefault("review_candidates", []).extend(
                            {"page_id": str(pid), "page_name": None, "source": "resolución anterior (confianza baja)"} for pid in previous)
                    for stale in ("review_candidates", "resolution_note"):
                        comp.pop(stale, None)
                    comp["page_ids"] = res["page_ids"]
                    for key in ("resolved_at", "resolution_confidence", "needs_review", "resolution_note", "review_candidates"):
                        if key in res:
                            comp[key] = res[key]
                    updated_list.append(comp)
                    self.last_run["processed"] += 1
                    if save:
                        self._persist(data, db_path)
        finally:
            self.last_run["queries"] = self.limiter.queries - queries_before

        if save:
            logger.info(f"💾 Catálogo actualizado: {self.last_run['processed']} competidores procesados (prioridad {priority}).")
        return updated_list


if __name__ == "__main__":
    resolver = PageResolver()
    try:
        logger.info("Iniciando resolución de competidores Prioridad 1...")
        results = resolver.resolve_all_priority(priority=1, save=True)
        print("\n" + "=" * 60)
        print("📋 RESUMEN DE RESOLUCIÓN PRIORIDAD 1:")
        print("=" * 60)
        for c in results:
            p_id = c.get("page_ids", ["N/A"])[0] if c.get("page_ids") else "N/A"
            conf = c.get("resolution_confidence", "N/A")
            rev = "⚠️ REQUIERE REVISIÓN" if c.get("needs_review") else "✓ VALIDADO"
            print(f"- {c['name']:<20} | Page ID: {p_id:<18} | Conf: {conf:<6} | {rev}")
    finally:
        resolver.close()
