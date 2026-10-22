"""
Script para scraping dirigido y generación de la muestra de calibración manual (20 videos P1).
Cumple estrictamente con las reglas de Jinora:
- 0 llamadas a Gemini o Claude
- Pausas aleatorias de 1 a 3 segundos
- Búsqueda por page_id, solo activos, países DEFAULT_COUNTRIES
- No borra originales de downloads/
- Deduplicación por sha256 y creative_hash, tope 120s, máx 3 por competidor
- Semilla fija 2026, carpetas y manifiesto lista.csv
"""

from __future__ import annotations

import csv
import json
import logging
import os
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.config import settings
from src.collect.relevance import TwoLayerRelevanceFilter
from src.database import save_or_update_ad_record
from src.media.fingerprint import compute_creative_hash, compute_file_sha256, compute_image_phash
from src.media.media_info import get_video_info
from src.scraper import MetaAdsHunter

# Strict guard against any LLM API calls
def _strictly_no_llm(*args, **kwargs):
    raise RuntimeError("LLM call intercepted! Zero LLM calls are permitted in this calibration task.")

TwoLayerRelevanceFilter._call_gemini_batch = _strictly_no_llm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("CalibrationSample")

OUTPUT_SAMPLE_DIR = ROOT_DIR / "calibration" / "videos_scrape"
MANIFEST_PATH = OUTPUT_SAMPLE_DIR / "lista.csv"
COMPETITORS_PATH = ROOT_DIR / "config" / "competitors_database.json"
DOWNLOADS_DIR = ROOT_DIR / "downloads"


def retry_failed_downloads(hunter: MetaAdsHunter) -> int:
    """Finds folders in downloads/ where metadata indicates a video exists but video_*.mp4 is missing."""
    logger.info("Verificando descargas previas incompletas en downloads/...")
    re_downloaded_count = 0
    for meta_file in DOWNLOADS_DIR.rglob("metadata.json"):
        folder = meta_file.parent
        v_files = list(folder.glob("*.mp4"))
        if v_files:
            continue
        try:
            ad_data = json.loads(meta_file.read_text(encoding="utf-8"))
            creatives = ad_data.get("creatives", [])
            has_vid = any(c.get("has_video") or c.get("video_hd_url") or c.get("video_sd_url") for c in creatives)
            if has_vid:
                logger.info(f"Reintentando descarga de video para ad {ad_data.get('ad_id')} en {folder.name}...")
                subfolder = folder.parent.name
                downloaded = hunter.download_ad(ad_data, subfolder=subfolder)
                if any(f.endswith(".mp4") for f in downloaded.get("downloaded_files", [])):
                    re_downloaded_count += 1
        except Exception as e:
            logger.warning(f"Error procesando {meta_file}: {e}")
    logger.info(f"Reintentos completados: {re_downloaded_count} videos descargados exitosamente.")
    return re_downloaded_count


def load_priority1_competitors() -> List[Dict[str, Any]]:
    if not COMPETITORS_PATH.exists():
        raise FileNotFoundError(f"Archivo no encontrado: {COMPETITORS_PATH}")

    with open(COMPETITORS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    p1_comps = []
    for pillar_name, pillar_data in data.get("pillars", {}).items():
        for comp in pillar_data.get("competitors", []):
            if comp.get("priority") == 1:
                comp_copy = dict(comp)
                comp_copy["pillar"] = pillar_name
                p1_comps.append(comp_copy)

    return p1_comps


def run_targeted_scraping() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Scrapes active ads by page_id for all Priority 1 competitors across DEFAULT_COUNTRIES.
    Saves to DB and downloads video media.
    """
    p1_competitors = load_priority1_competitors()
    logger.info(f"Iniciando scraping dirigido para {len(p1_competitors)} competidores P1.")

    countries = settings.DEFAULT_COUNTRIES
    logger.info(f"Países a consultar: {countries}")

    hunter = MetaAdsHunter(output_dir=DOWNLOADS_DIR)

    stats_by_competitor: Dict[str, Dict[str, Any]] = {}
    ad_countries_map: Dict[str, Set[str]] = {}
    all_seen_ads: Dict[str, Dict[str, Any]] = {}

    for comp in p1_competitors:
        comp_name = comp["name"]
        pillar = comp["pillar"]
        page_ids = [str(pid) for pid in comp.get("page_ids", []) if pid]

        stats_by_competitor[comp_name] = {
            "pillar": pillar,
            "page_ids": page_ids,
            "total_active_ads": 0,
            "unique_active_ads": 0,
            "valid_videos_downloaded": 0,
            "ad_ids": [],
        }

        if not page_ids:
            logger.warning(f"⚠️ Competidor '{comp_name}' no tiene page_id. Omitiendo.")
            continue

        logger.info(f"\n=======================================================")
        logger.info(f"🔍 Competidor: {comp_name} ({pillar.upper()}) | Page ID: {page_ids}")
        logger.info(f"=======================================================")

        competitor_ads: Dict[str, Dict[str, Any]] = {}

        for country in countries:
            logger.info(f"Consultando {comp_name} en {country}...")
            # Query active ads with polite delay
            time.sleep(random.uniform(1.0, 2.5))
            try:
                found_ads = hunter.search_ads(
                    competitor=comp,
                    country=country,
                    status="ACTIVE",
                    media_filter="all",
                    limit=20,
                    use_two_layer_relevance=True,
                )
            except Exception as e:
                logger.error(f"Error consultando {comp_name} en {country}: {e}")
                found_ads = []

            for ad in found_ads:
                ad_id = str(ad.get("ad_id"))
                ad_countries_map.setdefault(ad_id, set()).add(country)
                ad["_pillar"] = pillar
                ad["_comp_name"] = comp_name

                if ad_id not in competitor_ads:
                    competitor_ads[ad_id] = ad

        # If 0 video ads were found in media_filter='all', do a targeted media_filter='video' search
        has_any_video = any(
            any(c.get("has_video") or c.get("video_hd_url") or c.get("video_sd_url") for c in ad.get("creatives", []))
            for ad in competitor_ads.values()
        )
        if not has_any_video:
            logger.info(f"No se detectaron videos en filtro general para {comp_name}. Intentando filtro 'video' en países clave...")
            for country in ["MX", "PE", "CO", "US"]:
                time.sleep(random.uniform(1.0, 2.0))
                try:
                    v_ads = hunter.search_ads(
                        competitor=comp,
                        country=country,
                        status="ACTIVE",
                        media_filter="video",
                        limit=10,
                        use_two_layer_relevance=True,
                    )
                    for ad in v_ads:
                        ad_id = str(ad.get("ad_id"))
                        ad_countries_map.setdefault(ad_id, set()).add(country)
                        ad["_pillar"] = pillar
                        ad["_comp_name"] = comp_name
                        if ad_id not in competitor_ads:
                            competitor_ads[ad_id] = ad
                except Exception as e:
                    logger.debug(f"Targeted video search error for {comp_name} in {country}: {e}")

        # Now download media and save to DB
        stats_by_competitor[comp_name]["total_active_ads"] = len(competitor_ads)
        stats_by_competitor[comp_name]["unique_active_ads"] = len(competitor_ads)

        for ad_id, ad_dict in competitor_ads.items():
            creatives = ad_dict.get("creatives", [])
            has_vid = any(c.get("has_video") or c.get("video_hd_url") or c.get("video_sd_url") for c in creatives)

            try:
                if has_vid:
                    # Download video & thumbnail
                    downloaded_ad = hunter.download_ad(ad_dict, subfolder=comp_name)
                    save_or_update_ad_record(downloaded_ad, pillar=pillar, search_query=comp_name)

                    dl_files = downloaded_ad.get("downloaded_files", [])
                    v_rel = next((f for f in dl_files if f.endswith(".mp4")), None)
                    if v_rel:
                        v_abs = DOWNLOADS_DIR / v_rel
                        if v_abs.exists() and v_abs.stat().st_size > 1000:
                            stats_by_competitor[comp_name]["valid_videos_downloaded"] += 1
                            stats_by_competitor[comp_name]["ad_ids"].append(ad_id)
                            all_seen_ads[ad_id] = downloaded_ad
                else:
                    # Save ad record metadata to DB without downloading image assets
                    save_or_update_ad_record(ad_dict, pillar=pillar, search_query=comp_name)
            except Exception as e:
                logger.error(f"Error descargando/guardando anuncio {ad_id}: {e}")

        logger.info(
            f"✅ {comp_name}: {stats_by_competitor[comp_name]['total_active_ads']} anuncios activos, "
            f"{stats_by_competitor[comp_name]['valid_videos_downloaded']} videos válidos descargados."
        )

    # Attach countries map into all_seen_ads
    for ad_id, ad_data in all_seen_ads.items():
        ad_data["_countries_str"] = ", ".join(sorted(ad_countries_map.get(ad_id, set())))

    return stats_by_competitor, list(all_seen_ads.values())


def select_calibration_sample(
    ads_with_video: List[Dict[str, Any]],
    stats: Dict[str, Any],
    seed: int = 2026,
) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Applies selection criteria:
    a) Only ads with video file validly on disk.
    b) Discard duplicates by creative_hash / sha256.
    c) Discard videos > 120 seconds.
    d) Max 3 videos per competitor.
    e) Pick 20 at random (seed 2026), balancing conversacional and crm.
    Returns: (selected_20, discarded_duration_count, discarded_duplicate_count)
    """
    discarded_duration = 0
    discarded_duplicate = 0

    seen_sha: Set[str] = set()
    seen_creative_hash: Set[str] = set()

    # Pre-process candidate ads
    processed_candidates: List[Dict[str, Any]] = []

    for ad in ads_with_video:
        ad_id = str(ad.get("ad_id"))
        comp_name = ad.get("_comp_name") or ad.get("page_name") or "Desconocido"
        pillar = ad.get("_pillar") or "conversacional"

        dl_files = ad.get("downloaded_files", [])
        v_rel = next((f for f in dl_files if f.endswith(".mp4")), None)
        if not v_rel:
            continue
        v_abs = DOWNLOADS_DIR / v_rel
        if not v_abs.exists() or v_abs.stat().st_size < 1000:
            continue

        # Extract video info via pymediainfo
        try:
            v_info = get_video_info(v_abs)
            dur_s = float(v_info.get("duration_s") or 0.0)
            width = int(v_info.get("width") or 0)
            height = int(v_info.get("height") or 0)
            aspect_ratio = v_info.get("aspect_ratio") or "custom"
        except Exception as e:
            logger.warning(f"No se pudo extraer metadata de {v_abs}: {e}")
            dur_s = 0.0
            width, height, aspect_ratio = 0, 0, "unknown"

        # Check duration <= 120s
        if dur_s > 120.0 or dur_s <= 0.0:
            logger.info(f"  [X] Descartado por duración ({dur_s:.1f}s > 120s): {comp_name} ({ad_id})")
            discarded_duration += 1
            continue

        # Compute SHA-256
        file_sha = compute_file_sha256(v_abs)
        if file_sha in seen_sha:
            logger.info(f"  [X] Descartado por SHA256 duplicado: {comp_name} ({ad_id})")
            discarded_duplicate += 1
            continue

        # Compute Creative Hash
        creatives = ad.get("creatives", [])
        c0 = creatives[0] if creatives else {}
        headline = c0.get("title") or ""
        body = c0.get("body") or ""
        c_hash = compute_creative_hash(file_sha, copy_text=body, title=headline)

        if c_hash in seen_creative_hash:
            logger.info(f"  [X] Descartado por Creative Hash duplicado: {comp_name} ({ad_id})")
            discarded_duplicate += 1
            continue

        seen_sha.add(file_sha)
        seen_creative_hash.add(c_hash)

        candidate_record = {
            "ad_id": ad_id,
            "competidor": comp_name,
            "pilar": pillar,
            "video_abs_path": v_abs,
            "duration_s": dur_s,
            "resolution": f"{width}x{height} ({aspect_ratio})" if width and height else "N/A",
            "headline": headline.strip(),
            "body_text": body.strip(),
            "cta": c0.get("cta_text") or c0.get("cta_type") or "N/A",
            "link_url": c0.get("link_url") or "",
            "countries": ad.get("_countries_str") or "US",
            "days_active": ad.get("days_active", 0),
            "file_size_mb": v_abs.stat().st_size / (1024 * 1024),
        }
        processed_candidates.append(candidate_record)

    logger.info(f"\nCandidatos únicos válidos (<= 120s): {len(processed_candidates)}")
    logger.info(f"Descartados por duración (> 120s): {discarded_duration}")
    logger.info(f"Descartados por duplicado: {discarded_duplicate}")

    # Enforce maximum 3 videos per competitor
    comps_dict: Dict[str, List[Dict[str, Any]]] = {}
    for cand in processed_candidates:
        comps_dict.setdefault(cand["competidor"], []).append(cand)

    capped_candidates: List[Dict[str, Any]] = []
    rng = random.Random(seed)
    for c_name, c_list in comps_dict.items():
        rng.shuffle(c_list)
        kept = c_list[:3]
        capped_candidates.extend(kept)
        if len(c_list) > 3:
            logger.info(f"Competidor {c_name}: limitando de {len(c_list)} a 3 videos.")

    logger.info(f"Candidatos tras tope de 3 por competidor: {len(capped_candidates)}")

    # Balance conversacional and CRM
    conversacional = [c for c in capped_candidates if c["pilar"] == "conversacional"]
    crm = [c for c in capped_candidates if c["pilar"] == "crm"]

    rng.shuffle(conversacional)
    rng.shuffle(crm)

    selected: List[Dict[str, Any]] = []

    # If total <= 20, take all
    if len(capped_candidates) <= 20:
        selected = list(capped_candidates)
    else:
        # Prioritize having representatives of both
        # Take up to 8 CRM and up to 12 Conversacional, or balance according to availability
        crm_target = min(len(crm), 8)
        conv_target = 20 - crm_target
        if len(conversacional) < conv_target:
            conv_target = len(conversacional)
            crm_target = min(len(crm), 20 - conv_target)

        selected = crm[:crm_target] + conversacional[:conv_target]
        # If still < 20, fill from remainder
        if len(selected) < 20:
            rem = [c for c in capped_candidates if c not in selected]
            rng.shuffle(rem)
            selected.extend(rem[:(20 - len(selected))])

    # Final shuffle with seed
    rng.shuffle(selected)
    selected = selected[:20]

    return selected, discarded_duration, discarded_duplicate


def export_calibration_sample(selected_ads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Copies and renames files to calibration/videos_scrape/NN_Competidor_adid.mp4
    Generates calibration/videos_scrape/lista.csv
    """
    OUTPUT_SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    csv_rows = []
    large_files = []

    for idx, item in enumerate(selected_ads, 1):
        nn = f"{idx:02d}"
        comp_clean = "".join(c for c in item["competidor"] if c.isalnum() or c in ("_", "-"))
        ad_id = item["ad_id"]
        out_filename = f"{nn}_{comp_clean}_{ad_id}.mp4"
        dest_path = OUTPUT_SAMPLE_DIR / out_filename

        # Copy file without deleting original
        src_path = item["video_abs_path"]
        shutil.copy2(src_path, dest_path)

        size_mb = dest_path.stat().st_size / (1024 * 1024)
        if size_mb > 30.0:
            large_files.append((out_filename, size_mb))

        fb_library_url = f"https://www.facebook.com/ads/library/?id={ad_id}"

        row = {
            "n": idx,
            "archivo": out_filename,
            "competidor": item["competidor"],
            "pilar": item["pilar"],
            "ad_id": ad_id,
            "paises": item["countries"],
            "dias_activo": item["days_active"],
            "duracion_s": round(item["duration_s"], 1),
            "resolucion": item["resolution"],
            "titular": item["headline"].replace("\n", " "),
            "texto_del_anuncio": item["body_text"].replace("\n", " "),
            "cta": item["cta"],
            "link_url": item["link_url"],
            "link_a_biblioteca_de_anuncios": fb_library_url,
        }
        csv_rows.append(row)

    # Write lista.csv
    fieldnames = [
        "n",
        "archivo",
        "competidor",
        "pilar",
        "ad_id",
        "paises",
        "dias_activo",
        "duracion_s",
        "resolucion",
        "titular",
        "texto_del_anuncio",
        "cta",
        "link_url",
        "link_a_biblioteca_de_anuncios",
    ]

    with open(MANIFEST_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    logger.info(f"✅ Manifiesto generado en: {MANIFEST_PATH}")
    if large_files:
        logger.warning("Archivos mayores a 30 MB detectados:")
        for fn, sz in large_files:
            logger.warning(f"  - {fn}: {sz:.2f} MB")

    return csv_rows


def main():
    logger.info("=== INICIANDO PROCESO DE MUESTRA DE CALIBRACIÓN ===")
    hunter = MetaAdsHunter(output_dir=DOWNLOADS_DIR)
    retried_vids = retry_failed_downloads(hunter)

    p1_comps = load_priority1_competitors()
    stats_by_comp, all_downloaded = run_targeted_scraping()
    selected, disc_dur, disc_dup = select_calibration_sample(all_downloaded, stats_by_comp, seed=2026)
    manifest_rows = export_calibration_sample(selected)

    summary_data = {
        "competitors_used": [
            {"name": c["name"], "pillar": c["pillar"], "page_ids": c.get("page_ids")}
            for c in p1_comps
        ],
        "stats_by_competitor": stats_by_comp,
        "retried_videos_downloaded": retried_vids,
        "total_videos_downloaded_in_run": retried_vids + sum(s["valid_videos_downloaded"] for s in stats_by_comp.values()),
        "discarded_duration": disc_dur,
        "discarded_duplicate": disc_dup,
        "selected_20": selected,
    }

    summary_file = ROOT_DIR / "calibration" / "summary.json"
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.write_text(json.dumps(summary_data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("\n" + "=" * 70)
    print("PROCESO DE CALIBRACIÓN COMPLETADO")
    print(f"Total videos seleccionados: {len(selected)}")
    print(f"Manifiesto: {MANIFEST_PATH}")
    print(f"Resumen guardado en: {summary_file}")
    print("=" * 70)


if __name__ == "__main__":
    main()
