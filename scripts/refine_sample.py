"""
Script para filtrar y reemplazar los videos no relevantes en la muestra de calibración.
Sustituye los anuncios de Kommo Home y Yalo Perú por anuncios genuinos de CRM y WhatsApp
de Clientify, ManyChat, Leadsales, Respond.io y B2Chat (4 de cada uno = 20 videos en total).
"""

import csv
import json
import random
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.media.fingerprint import compute_file_sha256
from src.media.media_info import get_video_info
DOWNLOADS_DIR = ROOT_DIR / "downloads"
CALIB_DIR = ROOT_DIR / "calibration" / "videos_scrape"

VALID_COMPS = ["Clientify", "ManyChat", "Leadsales", "Respond.io", "B2Chat"]


def main():
    candidates_by_comp = {c: [] for c in VALID_COMPS}
    seen_sha = set()

    for c_name in VALID_COMPS:
        comp_dir = DOWNLOADS_DIR / c_name
        for meta in comp_dir.glob("*/metadata.json"):
            try:
                d = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            ad_id = str(d.get("ad_id"))
            folder = meta.parent
            v_files = list(folder.glob("video_0.mp4"))
            if not v_files:
                continue
            v_file = v_files[0]
            if v_file.stat().st_size < 1000:
                continue
            sha = compute_file_sha256(v_file)
            if sha in seen_sha:
                continue
            seen_sha.add(sha)
            try:
                v_info = get_video_info(v_file)
            except Exception:
                continue
            dur = float(v_info.get("duration_s") or 0.0)
            if dur > 120.0 or dur <= 0.0:
                continue

            creatives = d.get("creatives", [])
            c0 = creatives[0] if creatives else {}
            title = (c0.get("title") or "").strip()
            body = (c0.get("body") or "").strip()
            pillar = "crm" if c_name == "Clientify" else "conversacional"

            candidates_by_comp[c_name].append({
                "competidor": c_name,
                "pillar": pillar,
                "ad_id": ad_id,
                "video_abs_path": v_file,
                "duration_s": dur,
                "width": v_info.get("width"),
                "height": v_info.get("height"),
                "aspect_ratio": v_info.get("aspect_ratio"),
                "headline": title,
                "body_text": body,
                "cta": c0.get("cta_text") or c0.get("cta_type") or "N/A",
                "link_url": c0.get("link_url") or "",
                "days_active": d.get("days_active", 0),
            })

    # Pick 4 from each of the 5 competitors = 20 videos
    rng = random.Random(2026)
    selected_20 = []
    for c_name in VALID_COMPS:
        c_list = candidates_by_comp[c_name]
        rng.shuffle(c_list)
        selected_20.extend(c_list[:4])

    rng.shuffle(selected_20)

    CALIB_DIR.mkdir(parents=True, exist_ok=True)
    # Remove existing files
    for old in CALIB_DIR.glob("*.*"):
        old.unlink()

    csv_rows = []
    for idx, item in enumerate(selected_20, 1):
        nn = f"{idx:02d}"
        c_clean = "".join(c for c in item["competidor"] if c.isalnum() or c in ("_", "-"))
        ad_id = item["ad_id"]
        out_filename = f"{nn}_{c_clean}_{ad_id}.mp4"
        dest_path = CALIB_DIR / out_filename
        shutil.copy2(item["video_abs_path"], dest_path)

        w = item.get("width") or 0
        h = item.get("height") or 0
        ar = item.get("aspect_ratio") or "custom"
        res_str = f"{w}x{h} ({ar})" if w and h else "N/A"

        csv_rows.append({
            "n": idx,
            "archivo": out_filename,
            "competidor": item["competidor"],
            "pilar": item["pillar"],
            "ad_id": ad_id,
            "paises": "LatAm / US",
            "dias_activo": item["days_active"],
            "duracion_s": round(item["duration_s"], 1),
            "resolucion": res_str,
            "titular": item["headline"].replace("\n", " "),
            "texto_del_anuncio": item["body_text"].replace("\n", " "),
            "cta": item["cta"],
            "link_url": item["link_url"],
            "link_a_biblioteca_de_anuncios": f"https://www.facebook.com/ads/library/?id={ad_id}",
        })

    manifest_file = CALIB_DIR / "lista.csv"
    fieldnames = [
        "n", "archivo", "competidor", "pilar", "ad_id", "paises", "dias_activo",
        "duracion_s", "resolucion", "titular", "texto_del_anuncio", "cta",
        "link_url", "link_a_biblioteca_de_anuncios"
    ]
    with open(manifest_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"Muestra final consolidada con {len(selected_20)} videos en {CALIB_DIR}")


if __name__ == "__main__":
    main()
