"""
Jinora Industrial Harvester - Massive Multi-Competitor Sweep (Target: 1,000 Ads)
Extracts ads across all 105 mapped competitors and 3 commercial pillars.
Saves creative metadata, streaming video URLs, evaluates cognitive score, and queues forensics.
"""

import json
import logging
import sys
import time
from pathlib import Path

# UTF-8 encoding on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.database import save_or_update_ad_record, SessionLocal, AdRecord
from src.evaluator import evaluate_ad_cognitively
from src.scraper import MetaAdsHunter
from src.video_forensics import VideoForensicsEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Jinora1000Harvester")

COMPETITORS_CONFIG = BASE_DIR / "config" / "competitors_database.json"
TARGET_TOTAL_ADS = 1000

COUNTRIES = ["US", "ES", "MX", "GB", "CO", "BR"]


def run_massive_ingestion():
    logger.info("=" * 80)
    logger.info(f"🚀 INICIANDO COSECHA MASIVA JINORA (OBJETIVO: HASTA {TARGET_TOTAL_ADS} ADS)")
    logger.info("=" * 80)

    with open(COMPETITORS_CONFIG, "r", encoding="utf-8") as f:
        comp_db = json.load(f)

    pillars = comp_db.get("pillars", {})
    downloads_dir = BASE_DIR / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    initial_count = db.query(AdRecord).count()
    db.close()
    logger.info(f"📊 Anuncios actuales en Base de Datos: {initial_count}")

    total_added = initial_count

    with MetaAdsHunter(output_dir=str(downloads_dir)) as hunter:
        # Loop through pillars
        for p_key, p_data in pillars.items():
            competitors = p_data.get("competitors", [])
            logger.info(f"\n=======================================================")
            logger.info(f"🏛️ PILAR: {p_key.upper()} ({len(competitors)} competidores mapeados)")
            logger.info(f"=======================================================")

            for comp in competitors:
                if total_added >= TARGET_TOTAL_ADS:
                    logger.info(f"🎯 Meta alcanzada: {total_added} anuncios en base de datos.")
                    break

                c_name = comp["name"]

                # Scan primary markets
                for country in ["US", "ES", "MX"]:
                    if total_added >= TARGET_TOTAL_ADS:
                        break

                    logger.info(f"🔎 Rastreando '{c_name}' en [{country}]...")
                    try:
                        ads = hunter.search_ads(
                            query=c_name,
                            country=country,
                            media_filter="all",  # Video and images
                            limit=15,
                        )
                    except Exception as e:
                        logger.warning(f"  [!] Error buscando {c_name} en {country}: {e}")
                        continue

                    if not ads:
                        continue

                    logger.info(f"  [+] {len(ads)} anuncios aprobados para {c_name} tras filtro antirruido")

                    for ad in ads:
                        ad_id = str(ad.get("ad_id"))
                        p_name = ad.get("page_name") or c_name
                        days = ad.get("days_active", 0)

                        # Cognitive evaluation of the copy & offer
                        c0 = ad.get("creatives", [{}])[0] if ad.get("creatives") else {}
                        eval_data = evaluate_ad_cognitively({
                            "days_active": days,
                            "has_video": any(c.get("has_video") for c in ad.get("creatives", [])),
                            "full_transcript": "",
                            "headline": c0.get("title", ""),
                            "body_text": c0.get("body", ""),
                            "link_url": c0.get("link_url", ""),
                        })
                        ad["evaluation"] = eval_data

                        # Download media for top active ads (>= 15 days active) to optimize disk & network
                        if days >= 15:
                            try:
                                hunter.download_ad(ad, subfolder=p_name)
                            except Exception as e:
                                pass

                        try:
                            rec = save_or_update_ad_record(ad, pillar=p_key, search_query=c_name)
                            total_added += 1
                            if total_added % 10 == 0 or total_added <= 50:
                                logger.info(f"    💾 Guardado (#{total_added}): {p_name} | {days}d activo | Score: {eval_data['total_score']}/100")
                        except Exception as e:
                            logger.error(f"    [!] Error guardando ad {ad_id}: {e}")

                    time.sleep(0.3)

    logger.info("\n" + "=" * 80)
    logger.info(f"🏆 COSECHA MASIVA FINALIZADA. TOTAL ANUNCIOS EN DB: {total_added}")
    logger.info("=" * 80)


if __name__ == "__main__":
    run_massive_ingestion()
