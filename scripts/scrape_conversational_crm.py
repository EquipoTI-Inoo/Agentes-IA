"""
Focused scraper for Conversational AI & CRM Pillars
"""

import logging
import sys
from pathlib import Path

# Ensure UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.database import save_or_update_ad_record
from src.evaluator import evaluate_ad_cognitively
from src.scraper import MetaAdsHunter
from src.video_forensics import VideoForensicsEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

forensics = VideoForensicsEngine(whisper_model_size="base")

TASKS = [
    ("conversacional", "ManyChat", "US", 2),
    ("conversacional", "Manychat", "ES", 2),
    ("crm", "HubSpot", "US", 2),
    ("crm", "Kommo CRM", "US", 2),
    ("crm", "GoHighLevel", "US", 2),
]


def run():
    with MetaAdsHunter(output_dir=str(BASE_DIR / "downloads")) as hunter:
        for pillar, q, country, limit in TASKS:
            print(f"\n=== RASTREO {pillar.upper()}: {q} ({country}) ===")
            try:
                ads = hunter.search_ads(query=q, country=country, media_filter="video", limit=limit)
            except Exception as e:
                print(f"Error en busqueda {q}: {e}")
                continue

            for ad in ads:
                page_name = ad.get("page_name") or q
                ad_id = ad.get("ad_id")
                print(f"Procesando: {page_name} (ID: {ad_id})")

                hunter.download_ad(ad, subfolder=page_name)

                v_file = next((Path(f) for f in ad.get("downloaded_files", []) if f.endswith(".mp4")), None)
                if v_file and v_file.exists():
                    print("  -> Ejecutando Whisper y extraccion de fotogramas...")
                    try:
                        f_report = forensics.analyze_video(v_file)
                        ad["forensics"] = f_report
                        print(f"  -> Transcrito: {len(f_report.get('full_transcript', ''))} caracteres.")
                    except Exception as e:
                        print(f"  -> Error forensics: {e}")

                eval_data = evaluate_ad_cognitively({
                    "days_active": ad.get("days_active", 0),
                    "has_video": bool(v_file),
                    "full_transcript": ad.get("forensics", {}).get("full_transcript", ""),
                    "headline": ad.get("creatives", [{}])[0].get("title", ""),
                    "body_text": ad.get("creatives", [{}])[0].get("body", ""),
                    "link_url": ad.get("creatives", [{}])[0].get("link_url", ""),
                })
                ad["evaluation"] = eval_data

                rec = save_or_update_ad_record(ad, pillar=pillar, search_query=q)
                print(f"  -> Guardado en DB: {rec.page_name} | Score: {eval_data['total_score']}/100")


if __name__ == "__main__":
    run()
