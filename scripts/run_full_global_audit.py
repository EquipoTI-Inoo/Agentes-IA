"""
Jinora Global Intelligence Engine
Executes complete worldwide scanning, filtering, full Whisper audio transcription,
full FFmpeg frame extraction, and cognitive scoring across all 3 commercial pillars.
"""

import json
import logging
import sys
import time
from pathlib import Path

# UTF-8 on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.database import save_or_update_ad_record
from src.evaluator import evaluate_ad_cognitively
from src.scraper import MetaAdsHunter
from src.video_forensics import VideoForensicsEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("JinoraGlobalAudit")

# Selected high-signal competitors across the 3 pillars and global regions
GLOBAL_TARGETS = [
    # --- Pilar 1: Voice AI ---
    {"pillar": "voz", "query": "Vapi AI", "countries": ["US", "GB"]},
    {"pillar": "voz", "query": "Bland AI", "countries": ["US", "MX"]},
    {"pillar": "voz", "query": "ElevenLabs", "countries": ["US", "ES"]},
    {"pillar": "voz", "query": "Synthflow AI", "countries": ["US", "DE"]},
    {"pillar": "voz", "query": "Air AI", "countries": ["US"]},

    # --- Pilar 2: Conversacional (WhatsApp / Instagram) ---
    {"pillar": "conversacional", "query": "ManyChat", "countries": ["US", "ES", "MX"]},
    {"pillar": "conversacional", "query": "Chatbase", "countries": ["US", "ES"]},
    {"pillar": "conversacional", "query": "Botpress", "countries": ["US", "GB"]},
    {"pillar": "conversacional", "query": "Landbot", "countries": ["ES", "US"]},
    {"pillar": "conversacional", "query": "WATI", "countries": ["US", "BR"]},

    # --- Pilar 3: CRM Inteligente & Automatización ---
    {"pillar": "crm", "query": "Kommo CRM", "countries": ["US", "MX", "ES"]},
    {"pillar": "crm", "query": "HubSpot", "countries": ["US", "ES", "CO"]},
    {"pillar": "crm", "query": "GoHighLevel", "countries": ["US", "GB"]},
    {"pillar": "crm", "query": "Close CRM", "countries": ["US"]},
    {"pillar": "crm", "query": "Pipedrive", "countries": ["ES", "MX", "US"]},
]


def run_global_intelligence():
    logger.info("=" * 75)
    logger.info("🌍 JINORA: RASTREO Y ANÁLISIS FORENSE GLOBAL DE LOS 3 PILARES")
    logger.info("=" * 75)

    downloads_dir = BASE_DIR / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    logger.info("📦 Inicializando Faster-Whisper (int8) y FFmpeg...")
    forensics = VideoForensicsEngine(whisper_model_size="base")

    total_discovered = 0
    total_audited = 0

    with MetaAdsHunter(output_dir=str(downloads_dir)) as hunter:
        for target in GLOBAL_TARGETS:
            pillar = target["pillar"]
            query = target["query"]
            countries = target["countries"]

            logger.info(f"\n=======================================================")
            logger.info(f"🏛️ PILAR: {pillar.upper()} | OBJETIVO: '{query}' en {countries}")
            logger.info(f"=======================================================")

            for country in countries:
                logger.info(f"  🔎 Escaneando Meta Ads Library: '{query}' en [{country}]...")
                try:
                    ads = hunter.search_ads(
                        query=query,
                        country=country,
                        media_filter="video",
                        limit=3,
                        min_active_days=0,
                    )
                except Exception as e:
                    logger.warning(f"  [!] Error buscando '{query}' en {country}: {e}")
                    continue

                logger.info(f"  [+] Encontrados {len(ads)} anuncios relevantes tras filtro antirruido.")
                total_discovered += len(ads)

                for ad in ads:
                    ad_id = str(ad.get("ad_id"))
                    page_name = ad.get("page_name") or query
                    days = ad.get("days_active", 0)

                    logger.info(f"\n  ⚡ PROCESANDO: {page_name} (ID: {ad_id} | {days} días activo)")

                    # 1. Download Video and Images
                    try:
                        hunter.download_ad(ad, subfolder=page_name)
                    except Exception as e:
                        logger.warning(f"    [!] Error descargando media para {ad_id}: {e}")
                        continue

                    # Find video file
                    video_file = next(
                        (Path(f) for f in ad.get("downloaded_files", []) if f.endswith(".mp4")),
                        None,
                    )

                    # 2. Complete Multimodal Forensics (100% Transcription + 100% Visuals)
                    if video_file and video_file.exists():
                        logger.info(f"    🎙️ Transcribiendo audio completo con Whisper + Extrayendo fotogramas...")
                        try:
                            f_report = forensics.analyze_video(video_file)
                            ad["forensics"] = f_report
                            chars = len(f_report.get("full_transcript", ""))
                            frames = f_report.get("total_frames_extracted", 0)
                            dur = f_report.get("duration_seconds", 0)
                            logger.info(f"    ✅ Forense completado: {dur:.1f}s | {chars} caracteres | {frames} fotogramas.")
                        except Exception as e:
                            logger.error(f"    [!] Error en forense de video: {e}")

                    # 3. Cognitive Evaluation
                    first_c = ad.get("creatives", [{}])[0]
                    eval_data = evaluate_ad_cognitively({
                        "days_active": days,
                        "has_video": bool(video_file),
                        "full_transcript": ad.get("forensics", {}).get("full_transcript", ""),
                        "headline": first_c.get("title", ""),
                        "body_text": first_c.get("body", ""),
                        "link_url": first_c.get("link_url", ""),
                        "pacing_words_per_minute": ad.get("forensics", {}).get("pacing_words_per_minute", 0),
                    })
                    ad["evaluation"] = eval_data

                    # 4. Save to Database
                    try:
                        rec = save_or_update_ad_record(ad, pillar=pillar, search_query=query)
                        logger.info(f"    💾 Guardado en Base de Datos: Score {eval_data.get('total_score')}/100")
                        total_audited += 1
                    except Exception as e:
                        logger.error(f"    [!] Error guardando en DB: {e}")

                # Polite delay between queries to avoid Meta rate limits
                time.sleep(1)

    logger.info("\n" + "=" * 75)
    logger.info("🏆 RASTREO GLOBAL JINORA FINALIZADO EXITOSAMENTE")
    logger.info(f"📊 Anuncios globales recolectados: {total_discovered}")
    logger.info(f"🔬 Anuncios auditados con IA y guardados: {total_audited}")
    logger.info("=" * 75)


if __name__ == "__main__":
    run_global_intelligence()
