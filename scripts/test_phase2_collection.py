"""
Test verification script for Phase 2 acceptance.
Runs collection over 10 Priority 1 competitors across LatAm (PE, MX, CO).
Generates the acceptance summary table and relevance audit.
"""

import json
import logging
import random
import sys
import time
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from typing import Any, Dict, List

from src.ai.quota import get_quota_guard
from src.collect.relevance import TwoLayerRelevanceFilter
from src.config import settings
from src.scraper import MetaAdsHunter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Phase2Verification")

def run_acceptance_run():
    config_path = Path("config/competitors_database.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    all_competitors: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if "pillars" in data:
            for pillar_name, pdata in data["pillars"].items():
                for comp in pdata.get("competitors", []):
                    comp_copy = dict(comp)
                    comp_copy.setdefault("pillar", pillar_name)
                    all_competitors.append(comp_copy)
        elif "competitors" in data:
            all_competitors = data["competitors"]
    elif isinstance(data, list):
        all_competitors = data

    # Pick 10 Priority 1 competitors that have page_ids
    priority_1 = [c for c in all_competitors if c.get("priority") == 1 and c.get("page_ids")][:10]

    test_countries = ["PE", "MX", "CO"]
    summary_rows = []
    all_approved_ads = []

    quota_guard = get_quota_guard()
    start_usage = quota_guard.get_daily_usage(settings.LLM_MODEL_CHEAP)["requests"]

    with MetaAdsHunter() as hunter:
        for comp in priority_1:
            comp_name = comp["name"]
            page_ids = comp.get("page_ids", [])
            for country in test_countries:
                # Polite pause
                time.sleep(random.uniform(1.0, 2.0))
                
                try:
                    ads = hunter.search_ads(
                        query="",
                        country=country,
                        limit=3,
                        page_ids=page_ids,
                        competitor=comp,
                        use_two_layer_relevance=True,
                    )
                    found = len(ads)
                    approved = sum(1 for a in ads if a.get("relevance_result", {}).get("is_relevant") or a.get("status") == "pending_relevance")
                    rejected = found - approved

                    summary_rows.append({
                        "competitor": comp_name,
                        "country": country,
                        "found": found,
                        "approved": approved,
                        "rejected": rejected,
                    })

                    for a in ads:
                        if a.get("relevance_result", {}).get("is_relevant"):
                            c0 = (a.get("creatives") or [{}])[0]
                            all_approved_ads.append({
                                "competitor": comp_name,
                                "ad_id": a.get("ad_id"),
                                "page_name": a.get("page_name"),
                                "headline": c0.get("title") or "N/A",
                                "body": (c0.get("body") or "N/A")[:100],
                                "reason": a.get("relevance_result", {}).get("reason"),
                            })
                except Exception as e:
                    logger.warning(f"Error consultando {comp_name} en {country}: {e}")
                    summary_rows.append({
                        "competitor": comp_name,
                        "country": country,
                        "found": 0,
                        "approved": 0,
                        "rejected": 0,
                    })

    end_usage = quota_guard.get_daily_usage(settings.LLM_MODEL_CHEAP)["requests"]
    gemini_calls_used = end_usage - start_usage

    # Print Summary Table
    print("\n" + "=" * 70)
    print("📊 TABLA DE RESUMEN - FASE 2 (10 Competidores Prioridad 1 en PE, MX, CO)")
    print("=" * 70)
    print(f"{'Competidor':<16} | {'País':<4} | {'Encontrados':<11} | {'Aprobados':<10} | {'Descartados':<11}")
    print("-" * 70)
    total_found = sum(r["found"] for r in summary_rows)
    total_approved = sum(r["approved"] for r in summary_rows)
    total_rejected = sum(r["rejected"] for r in summary_rows)
    for r in summary_rows:
        print(f"{r['competitor']:<16} | {r['country']:<4} | {r['found']:<11} | {r['approved']:<10} | {r['rejected']:<11}")
    print("-" * 70)
    print(f"{'TOTAL':<16} | {'ALL':<4} | {total_found:<11} | {total_approved:<10} | {total_rejected:<11}")
    print("=" * 70)

    # Print Sample of 20 Approved Ads
    sample_size = min(20, len(all_approved_ads))
    print(f"\n🔍 MUESTRA DE VERIFICACIÓN MANUAL ({sample_size} Anuncios Aprobados):")
    print("=" * 70)
    for idx, ad in enumerate(all_approved_ads[:20], 1):
        print(f"[{idx}] Competidor: {ad['competitor']} | Page: {ad['page_name']} | ID: {ad['ad_id']}")
        print(f"    Titular: {ad['headline']}")
        print(f"    Copy: {ad['body']}...")
        print(f"    Motivo: {ad['reason']}\n")

    print("=" * 70)
    print(f"Llamadas reales a Gemini utilizadas en esta corrida: {gemini_calls_used} (Límite: <= 5)")
    print("=" * 70)

if __name__ == "__main__":
    run_acceptance_run()
