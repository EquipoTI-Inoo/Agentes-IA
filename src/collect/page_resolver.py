"""
Jinora - Competitor Page Resolver
Resolves official Meta Ad Library page_ids for mapped competitors.
Validates official pages using name matching, category signals, and link_url domain verification.
NO Gemini calls used in this module.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from meta_ads_collector import MetaAdsCollector
from meta_ads_collector.constants import SEARCH_PAGE
from meta_ads_collector.filters import FilterConfig

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


class PageResolver:
    """Discovers and verifies official Meta Ads Library page_ids for competitors."""

    def __init__(self, collector: Optional[MetaAdsCollector] = None) -> None:
        self.collector = collector or MetaAdsCollector()
        self._own_collector = collector is None

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

    def _sleep_with_jitter(self, min_s: float = 1.0, max_s: float = 3.0) -> None:
        """Rate-limiting polite pause between Meta requests."""
        time.sleep(random.uniform(min_s, max_s))

    def resolve_competitor(
        self,
        competitor: Dict[str, Any],
        country: Optional[str] = None,
        verify_ads_domain: bool = True,
    ) -> Dict[str, Any]:
        """
        Search for candidate pages in Meta Ad Library and determine the official page_id.
        Returns a dict with: page_ids, resolved_at, resolution_confidence, needs_review.
        """
        name = competitor.get("name", "").strip()
        website = competitor.get("website", "")
        target_domain = extract_base_domain(website)
        
        # Determine country to search
        markets = competitor.get("markets", [])
        search_country = country or (markets[0] if markets else "US")

        logger.info(f"🔎 Resolviendo page_id para: '{name}' (Dominio esperado: {target_domain or 'N/A'}, país: {search_country})")

        # Queries to try: full name, or stripped name without parens (e.g. 'Sirena (Zenvia)' -> 'Sirena')
        queries = [name]
        name_no_parens = re.sub(r"\(.*?\)", "", name).strip()
        if name_no_parens and name_no_parens.lower() != name.lower():
            queries.append(name_no_parens)

        raw_candidates = []
        for q in queries:
            self._sleep_with_jitter(1.0, 2.0)
            try:
                found = self.collector.client.search_pages(q, country=search_country)
                for f in found:
                    if f.get("page_id") and not any(r.get("page_id") == f.get("page_id") for r in raw_candidates):
                        raw_candidates.append(f)
            except Exception as e:
                logger.warning(f"  [!] Error consultando search_pages para '{q}': {e}")
            if raw_candidates:
                break

        if not raw_candidates:
            logger.warning(f"  [X] No se encontraron páginas en Meta para '{name}'")
            return {
                "page_ids": [],
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "resolution_confidence": "baja",
                "needs_review": True,
            }

        # Step 1: Pre-score candidates strictly by name and category
        name_clean = re.sub(r"[^a-zA-Z0-9\s]", "", name).lower().strip()
        name_parts = set(name_clean.split())
        pre_scored: List[Tuple[float, Dict[str, Any]]] = []

        for cand in raw_candidates:
            cand_name = cand.get("page_name", "").strip()
            cand_name_clean = re.sub(r"[^a-zA-Z0-9\s]", "", cand_name).lower().strip()
            category = (cand.get("category") or "").lower()
            is_verified = cand.get("page_verified") in ("VERIFIED", "BLUE_VERIFIED")

            score = 0.0
            if cand_name_clean == name_clean:
                score += 60.0
            elif cand_name_clean == name_no_parens.lower():
                score += 55.0
            elif name_clean in cand_name_clean or cand_name_clean in name_clean:
                score += 35.0
            else:
                cand_parts = set(cand_name_clean.split())
                if name_parts and name_parts.issubset(cand_parts):
                    score += 25.0

            if score > 0:
                if is_verified:
                    score += 20.0
                if any(tech_cat in category for tech_cat in ["software", "internet", "business", "marketing", "consulting", "technology", "product/service"]):
                    score += 15.0
                pre_scored.append((score, cand))

        if not pre_scored:
            logger.warning(f"  [X] Ningún candidato tuvo coincidencia de nombre aceptable con '{name}'")
            return {
                "page_ids": [],
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "resolution_confidence": "baja",
                "needs_review": True,
            }

        pre_scored.sort(key=lambda x: x[0], reverse=True)
        # Only inspect ads for top 2 candidates
        top_candidates = pre_scored[:2]
        scored_candidates: List[Tuple[float, Dict[str, Any], str]] = []

        for initial_score, cand in top_candidates:
            cand_id = str(cand.get("page_id", ""))
            cand_name = cand.get("page_name", "").strip()
            category = (cand.get("category") or "").lower()
            is_verified = cand.get("page_verified") in ("VERIFIED", "BLUE_VERIFIED")
            current_score = initial_score
            confidence = "media" if initial_score >= 50.0 else "baja"

            if verify_ads_domain and target_domain and cand_id:
                try:
                    self._sleep_with_jitter(1.0, 2.0)
                    sample_ads = list(self.collector.search(
                        query="",
                        country=search_country,
                        search_type=SEARCH_PAGE,
                        page_ids=[cand_id],
                        max_results=3,
                    ))
                    domain_matched = False
                    for ad in sample_ads:
                        for c in (ad.creatives or []):
                            ad_link = c.link_url or ""
                            ad_domain = extract_base_domain(ad_link)
                            if target_domain and (target_domain in ad_domain or ad_domain in target_domain):
                                domain_matched = True
                                break
                        if domain_matched:
                            break

                    if domain_matched:
                        current_score += 100.0
                        confidence = "alta"
                        logger.info(f"  [✓] Dominio confirmado en anuncio para '{cand_name}' (ID: {cand_id}): {target_domain}")
                    elif sample_ads and (initial_score >= 50.0):
                        current_score += 20.0
                        confidence = "alta" if is_verified else "media"
                except Exception as e:
                    logger.debug(f"Error verificando anuncios para {cand_id}: {e}")

            scored_candidates.append((current_score, cand, confidence))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_cand, best_conf = scored_candidates[0]
        best_page_id = str(best_cand.get("page_id"))

        if best_score >= 100.0:
            final_conf = "alta"
            needs_review = False
        elif best_score >= 50.0:
            final_conf = "media"
            needs_review = False
        else:
            final_conf = "baja"
            needs_review = True

        logger.info(
            f"  -> Resultado: {best_cand.get('page_name')} (ID: {best_page_id}) | "
            f"Confianza: {final_conf.upper()} | needs_review={needs_review}"
        )

        return {
            "page_ids": [best_page_id] if best_page_id else [],
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolution_confidence": final_conf,
            "needs_review": needs_review,
        }

    def resolve_all_priority(
        self,
        priority: int = 1,
        save: bool = True,
        db_path: Path = COMPETITORS_FILE,
        max_competitors: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Resolve all competitors matching priority level and optionally update JSON file."""
        if not db_path.exists():
            raise FileNotFoundError(f"Competitors database not found: {db_path}")

        data = json.loads(db_path.read_text(encoding="utf-8"))
        updated_list = []
        processed = 0

        for pillar_key, pillar_data in data.get("pillars", {}).items():
            for comp in pillar_data.get("competitors", []):
                if comp.get("priority") == priority:
                    if max_competitors and processed >= max_competitors:
                        break

                    # If already resolved with alta/media confidence, skip re-resolution
                    if comp.get("page_ids") and comp.get("resolution_confidence") in ("alta", "media"):
                        logger.info(f"⏭️ Saltando '{comp['name']}': ya resuelto previamente ({comp['page_ids'][0]})")
                        updated_list.append(comp)
                        processed += 1
                        continue

                    res = self.resolve_competitor(comp)
                    comp["page_ids"] = res["page_ids"]
                    comp["resolved_at"] = res["resolved_at"]
                    comp["resolution_confidence"] = res["resolution_confidence"]
                    comp["needs_review"] = res["needs_review"]

                    updated_list.append(comp)
                    processed += 1

        if save:
            db_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            logger.info(f"💾 Base de competidores actualizada con {processed} registros resueltos.")

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
