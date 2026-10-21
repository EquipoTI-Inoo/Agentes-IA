"""
Jinora - Irrelevant Ads Auditor & Cleaner
Audits all existing ad records in data/meta_ads.db using the updated relevance filter.
Writes all rejected ads to data/rejected_ads.csv (ad_id, page_name, motivo).
Does NOT delete any database records or media files.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.relevance import is_ad_relevant

DB_PATH = BASE_DIR / "data" / "meta_ads.db"
OUTPUT_CSV = BASE_DIR / "data" / "rejected_ads.csv"


def audit_irrelevant_ads(db_path: Path = DB_PATH, csv_out: Path = OUTPUT_CSV) -> int:
    if not db_path.exists():
        print(f"❌ Error: Database not found at {db_path}")
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT id, page_name, headline, body_text, link_url, full_transcript, search_query FROM ads")
    rows = cur.fetchall()

    print(f"🔍 Evaluando {len(rows)} anuncios almacenados con el filtro de relevancia corregido...")

    rejected_records = []

    for r in rows:
        ad_id = str(r["id"])
        page_name = str(r["page_name"] or "")
        headline = str(r["headline"] or "")
        body_text = str(r["body_text"] or "")
        link_url = str(r["link_url"] or "")
        full_transcript = str(r["full_transcript"] or "")
        query = str(r["search_query"] or "")

        ad_dict = {
            "page_name": page_name,
            "headline": headline,
            "body_text": body_text,
            "link_url": link_url,
            "full_transcript": full_transcript,
        }

        is_rel, reason = is_ad_relevant(ad_dict, target_query=query)
        if not is_rel:
            rejected_records.append({
                "ad_id": ad_id,
                "page_name": page_name,
                "motivo": reason,
            })

    # Ensure output directory exists
    csv_out.parent.mkdir(parents=True, exist_ok=True)

    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["ad_id", "page_name", "motivo"])
        writer.writeheader()
        writer.writerows(rejected_records)

    print("\n" + "=" * 70)
    print(f"📊 RESUMEN DE AUDITORÍA: {len(rejected_records)} anuncios identificados como IRRELEVANTES (de {len(rows)})")
    print(f"💾 Archivo generado: {csv_out.relative_to(BASE_DIR)}")
    print("=" * 70)

    for idx, item in enumerate(rejected_records, 1):
        print(f"{idx:02d}. [{item['ad_id']}] {item['page_name']} -> {item['motivo']}")

    print("\n⚠️ IMPORTANTE: No se ha modificado ni borrado ningún registro en la base de datos ni archivos en downloads/.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(audit_irrelevant_ads())
