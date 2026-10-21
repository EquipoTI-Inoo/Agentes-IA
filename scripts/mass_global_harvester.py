"""
Jinora Massive Global Harvester
Iterates across the full 105 competitor database (Voice AI, Conversational, Smart CRM)
Scrapes, downloads, transcribes, extracts frames, and populates the database at scale.
"""

import json
import logging
import sys
import time
from pathlib import Path

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JinoraMassHarvester")

COMPETITORS_CONFIG = BASE_DIR / "config" / "competitors_database.json"

MASS_TARGETS = [
    # --- Pilar 1: Voice AI ---
    {"pillar": "voz", "queries": ["Vapi AI", "Bland AI", "ElevenLabs", "Synthflow AI", "Air AI", "Retell AI", "Dialpad", "voice ai agent", "agente de voz ia"]},
    # --- Pilar 2: Conversacional (WhatsApp / Instagram) ---
    {"pillar": "conversacional", "queries": ["ManyChat", "Chatbase", "Botpress", "Voiceflow", "Landbot", "WATI", "Trengo", "Respond.io", "chatbot whatsapp ventas", "agente ia whatsapp"]},
    # --- Pilar 3: CRM Inteligente & Automatización ---
    {"pillar": "crm", "queries": ["Kommo CRM", "HubSpot", "GoHighLevel", "Salesforce", "Pipedrive", "Close CRM", "Zoho CRM", "ActiveCampaign", "crm whatsapp ventas"]},
]

COUNTRIES = ["US", "ES", "MX", "GB", "CO"]


def run_massive_sweep():
    logger.info("=" * 80)
    logger.info("🚀 JINORA: ESCANEO MASIVO GLOBAL A GRAN ESCALA (3 PILARES)")
    logger.info("=" * 80)

    forensics = VideoForensicsEngine(whisper_model_size="base")
    downloads_dir = BASE_DIR / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    total_added = 0

    with MetaAdsHunter(output_dir=str(downloads_dir)) as hunter:
        for t in MASS_TARGETS:
            pillar = t["pillar"]
            queries = t["queries"]

            logger.info(f"\n=======================================================")
            logger.info(f"🏛️ PILAR: {pillar.upper()} ({len(queries)} marcas/términos estratégicos)")
            logger.info(f"=======================================================")

            for q in queries:
                for c_code in COUNTRIES[:3]:  # US, ES, MX
                    logger.info(f"\n🔎 Rastreando: '{q}' en [{c_code}]...")
                    try:
                        ads = hunter.search_ads(
                            query=q,
                            country=c_code,
                            media_filter="video",
                            limit=6,
                        )
                    except Exception as e:
                        logger.warning(f"  [!] Error buscando {q} en {c_code}: {e}")
                        continue

                    if not ads:
                        continue

                    logger.info(f"  [+] {len(ads)} anuncios aprobados para '{q}' en [{c_code}]")

                    for ad in ads:
                        ad_id = str(ad.get("ad_id"))
                        p_name = ad.get("page_name") or q
                        days = ad.get("days_active", 0)

                        logger.info(f"    ⚡ Procesando: {p_name} ({ad_id}) - {days}d activo")

                        # 1. Download
                        try:
                            hunter.download_ad(ad, subfolder=p_name)
                        except Exception as e:
                            logger.warning(f"      [!] Error descarga: {e}")
                            continue

                        # 2. Forensics
                        v_file = next((Path(f) for f in ad.get("downloaded_files", []) if f.endswith(".mp4")), None)
                        if v_file and v_file.exists():
                            try:
                                f_report = forensics.analyze_video(video_path=v_file)
                                ad["forensics"] = f_report
                                logger.info(f"      [✓] Forense OK: {f_report.get('duration_seconds', 0):.1f}s | {f_report.get('total_frames_extracted', 0)} frames")
                            except Exception as e:
                                logger.error(f"      [!] Error en forense: {e}")

                        # 3. Cognitive Evaluation
                        c0 = ad.get("creatives", [{}])[0]
                        eval_data = evaluate_ad_cognitively({
                            "days_active": days,
                            "has_video": bool(v_file),
                            "full_transcript": ad.get("forensics", {}).get("full_transcript", ""),
                            "headline": c0.get("title", ""),
                            "body_text": c0.get("body", ""),
                            "link_url": c0.get("link_url", ""),
                        })
                        ad["evaluation"] = eval_data

                        # 4. Save to DB
                        try:
                            rec = save_or_update_ad_record(ad, pillar=pillar, search_query=q)
                            total_added += 1
                            logger.info(f"      💾 Guardado en DB (#{total_added}): {rec.page_name} | Score: {eval_data['total_score']}/100")
                        except Exception as e:
                            logger.error(f"      [!] Error guardando en DB: {e}")

                    time.sleep(0.4)

    logger.info("\n" + "=" * 80)
    logger.info(f"✅ ESCANEO MASIVO COMPLETADO: {total_added} anuncios procesados y guardados.")
    logger.info("=" * 80)


if __name__ == "__main__":
    run_massive_sweep()
