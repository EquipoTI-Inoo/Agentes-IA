"""
Global Intelligence Pipeline - Jinora
Executes worldwide automated scraping, relevance filtering, media downloading,
full Whisper audio transcription, FFmpeg visual frame extraction, and cognitive evaluation
across all 3 core pillars:
1. Voice AI & Phone Agents
2. Conversational AI (WhatsApp & Instagram)
3. Smart CRM & Sales Automation
"""

import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root in sys.path
BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.database import save_or_update_ad_record, SessionLocal
from src.evaluator import evaluate_ad_cognitively
from src.pillars import PILLARS
from src.relevance import is_ad_relevant
from src.scraper import MetaAdsHunter
from src.video_forensics import VideoForensicsEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("JinoraGlobalPipeline")

# Targeted search targets across the 3 pillars for maximum high-converting ad yield
CAMPAIGN_TARGETS = [
    # --- Pillar 1: Voice AI ---
    {
        "pillar": "voz",
        "queries": ["Vapi", "Bland AI", "Retell AI", "voice ai agent", "agente de voz ia"],
        "countries": ["US", "GB", "MX", "ES"],
        "limit_per_query": 3,
    },
    # --- Pillar 2: Conversational AI (WhatsApp / Instagram) ---
    {
        "pillar": "conversacional",
        "queries": ["ManyChat", "chatbot whatsapp ventas", "WATI whatsapp", "agente ia whatsapp"],
        "countries": ["US", "MX", "CO", "ES"],
        "limit_per_query": 3,
    },
    # --- Pillar 3: Smart CRM & Sales Automation ---
    {
        "pillar": "crm",
        "queries": ["Kommo CRM", "HubSpot", "GoHighLevel", "crm whatsapp ventas"],
        "countries": ["US", "MX", "ES", "BR"],
        "limit_per_query": 3,
    },
]


def run_pipeline():
    logger.info("=" * 70)
    logger.info("🚀 INICIANDO RASTREO GLOBAL JINORA: LOS 3 PILARES A NIVEL MUNDIAL")
    logger.info("=" * 70)

    downloads_dir = BASE_DIR / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    # Initialize Forensics Engine (Whisper + FFmpeg)
    logger.info("📦 Cargando motor forense (faster-whisper int8 + FFmpeg)...")
    forensics = VideoForensicsEngine(whisper_model_size="base")

    total_scraped = 0
    total_processed = 0

    with MetaAdsHunter(output_dir=str(downloads_dir)) as hunter:
        for target in CAMPAIGN_TARGETS:
            pillar_key = target["pillar"]
            queries = target["queries"]
            countries = target["countries"]
            limit = target["limit_per_query"]

            logger.info(f"\n=======================================================")
            logger.info(f"🏛️ PILAR: {pillar_key.upper()} ({len(queries)} búsquedas estratégicas)")
            logger.info(f"=======================================================")

            for q in queries:
                for country in countries[:2]:  # Sample top 2 countries per query
                    logger.info(f"\n🔎 Rastreo: '{q}' en [{country}]...")
                    try:
                        ads = hunter.search_ads(
                            query=q,
                            country=country,
                            media_filter="video",
                            limit=limit,
                            min_active_days=0,
                        )
                    except Exception as e:
                        logger.error(f"  [!] Error buscando '{q}' en {country}: {e}")
                        continue

                    total_scraped += len(ads)

                    for ad in ads:
                        ad_id = str(ad.get("ad_id"))
                        page_name = ad.get("page_name")
                        days = ad.get("days_active", 0)

                        logger.info(f"\n⚡ Procesando Anuncio: {page_name} (ID: {ad_id}, {days} días activo)")

                        # 1. Download media files
                        try:
                            hunter.download_ad(ad, subfolder=page_name or q)
                        except Exception as e:
                            logger.warning(f"  [!] Error descargando media: {e}")
                            continue

                        # Find video file
                        video_file = None
                        for f in ad.get("downloaded_files", []):
                            if f.endswith(".mp4"):
                                video_file = Path(f)
                                break

                        # 2. Forensics: 100% Audio Transcription + Frame Extraction
                        if video_file and video_file.exists():
                            logger.info(f"  🔬 Ejecutando auditoría forense completa...")
                            try:
                                f_report = forensics.analyze_video(video_file)
                                ad["forensics"] = f_report
                                ad["full_transcript"] = f_report.get("full_transcript", "")
                                ad["transcript_timeline"] = f_report.get("transcript_timeline", [])
                                ad["duration_seconds"] = f_report.get("duration_seconds", 0)
                                ad["total_frames"] = f_report.get("total_frames_extracted", 0)
                                logger.info(
                                    f"  [+] Transcripción completada: {len(ad['full_transcript'])} caracteres, "
                                    f"{ad['total_frames']} fotogramas extraídos."
                                )
                            except Exception as e:
                                logger.error(f"  [!] Error en forense de video: {e}")

                        # 3. Cognitive Evaluation
                        logger.info(f"  🧠 Aplicando Rúbrica Cognitiva Jinora (100 pts)...")
                        eval_data = evaluate_ad_cognitively({
                            "days_active": ad.get("days_active", 0),
                            "has_video": bool(video_file),
                            "full_transcript": ad.get("full_transcript", ""),
                            "headline": ad.get("creatives", [{}])[0].get("title", ""),
                            "body_text": ad.get("creatives", [{}])[0].get("body", ""),
                            "link_url": ad.get("creatives", [{}])[0].get("link_url", ""),
                            "pacing_words_per_minute": ad.get("forensics", {}).get("pacing_words_per_minute", 0),
                        })
                        ad["evaluation"] = eval_data

                        # 4. Save into SQLite database
                        rec = save_or_update_ad_record(ad, pillar=pillar_key, search_query=q)
                        logger.info(f"  💾 Guardado en Base de Datos: {rec.page_name} -> Score: {eval_data.get('total_score')}/100")
                        total_processed += 1

    logger.info("\n" + "=" * 70)
    logger.info(f"✅ PIPELINE COMPLETADO CON ÉXITO")
    logger.info(f"📊 Total anuncios recolectados: {total_scraped}")
    logger.info(f"🏆 Total auditados y guardados: {total_processed}")
    logger.info("=" * 70)


if __name__ == "__main__":
    run_pipeline()
