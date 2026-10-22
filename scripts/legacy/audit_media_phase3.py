# LEGACY: auditoria puntual de la Fase 3; la etapa media del pipeline calcula hash, pHash y metadatos de cada anuncio
"""
Audit and fingerprint all ads and media in Jinora according to Fase 3 requirements:
- Computes SHA-256 for exact video deduplication
- Computes pHash on thumbnails/images to group visual variants
- Computes creative_hash (visual + copy)
- Detects ad format (video, imagen, carrusel, dco) and LLM routing
- Calculates duration, aspect ratio, est_tokens, and flags 'demasiado_largo'
"""

import json
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

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
from src.media.fingerprint import (
    compute_creative_hash,
    compute_file_sha256,
    compute_image_phash,
    detect_ad_format,
    route_ad_for_evaluation,
)
from src.media.media_info import check_video_context_limit, get_video_info

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Phase3Audit")

def audit_media():
    downloads_dir = Path("downloads")
    db_path = Path("data/meta_ads.db")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Phase 6 schema: media paths live in the media table (rel_path, relative to downloads/)
    cur.execute(
        "SELECT a.id, a.page_name, a.headline, a.body_text, "
        "(SELECT rel_path FROM media WHERE ad_id = a.id AND kind = 'video' LIMIT 1) AS video_path, "
        "(SELECT rel_path FROM media WHERE ad_id = a.id AND kind = 'image' LIMIT 1) AS image_path "
        "FROM ads a"
    )  # noqa: E501
    rows = cur.fetchall()

    print("=" * 70)
    print("🎬 AUDITORÍA DE MEDIA Y FINGERPRINTING (FASE 3)")
    print("=" * 70)
    print(f"Total registros en base de datos: {len(rows)}")

    # Grouping trackers
    sha256_groups = defaultdict(list)
    phash_groups = defaultdict(list)
    creative_hash_groups = defaultdict(list)
    format_counts = defaultdict(int)

    too_long_videos = []
    processed_videos = []
    processed_statics = []

    for row in rows:
        ad_id = str(row["id"])
        page_name = row["page_name"] or "Unknown"
        video_rel = row["video_path"]
        image_rel = row["image_path"]
        headline = row["headline"] or ""
        body = row["body_text"] or ""

        # Resolve paths
        video_full = (downloads_dir / video_rel).resolve() if video_rel else None
        image_full = (downloads_dir / image_rel).resolve() if image_rel else None

        # Format detection
        ad_dict_mock = {
            "ad_id": ad_id,
            "video_path": video_rel,
            "image_path": image_rel,
            "creatives": [
                {
                    "has_video": bool(video_rel),
                    "has_image": bool(image_rel),
                    "title": headline,
                    "body": body,
                }
            ],
        }
        fmt = detect_ad_format(ad_dict_mock)
        format_counts[fmt] += 1
        routing = route_ad_for_evaluation(fmt)

        v_sha256 = None
        i_phash = None
        c_hash = None
        video_meta = None
        is_too_long = False
        est_tokens = 0

        # Video analysis
        if video_full and video_full.exists():
            try:
                v_sha256 = compute_file_sha256(video_full)
                sha256_groups[v_sha256].append(ad_id)

                video_meta = get_video_info(video_full)
                dur = video_meta["duration_s"]
                is_too_long, est_tokens, reason = check_video_context_limit(
                    dur, resolution=settings.GEMINI_MEDIA_RESOLUTION
                )
                if is_too_long:
                    too_long_videos.append({
                        "ad_id": ad_id,
                        "page": page_name,
                        "duration_s": dur,
                        "tokens": est_tokens,
                        "reason": reason,
                    })

                processed_videos.append({
                    "ad_id": ad_id,
                    "page": page_name,
                    "duration_s": dur,
                    "tokens": est_tokens,
                    "is_too_long": is_too_long,
                })
            except Exception as e:
                logger.warning(f"Error procesando video {video_full}: {e}")

        # Image analysis
        if image_full and image_full.exists():
            try:
                i_phash = compute_image_phash(image_full)
                if i_phash:
                    phash_groups[i_phash].append(ad_id)
            except Exception as e:
                logger.warning(f"Error procesando imagen {image_full}: {e}")

        c_hash = compute_creative_hash(i_phash or v_sha256, copy_text=body, title=headline)
        creative_hash_groups[c_hash].append(ad_id)

    # Calculate grouping stats
    exact_duplicates = {k: v for k, v in sha256_groups.items() if len(v) > 1}
    visual_variants = {k: v for k, v in phash_groups.items() if len(v) > 1}
    creative_duplicates = {k: v for k, v in creative_hash_groups.items() if len(v) > 1}

    print("\n📊 DISTRIBUCIÓN POR FORMATO:")
    print("-" * 50)
    for fmt, count in sorted(format_counts.items()):
        route = route_ad_for_evaluation(fmt)
        print(f"  - {fmt:<10}: {count} anuncios -> Enrutado a: {route['provider'].upper()} ({route['model']})")

    print("\n🔍 FINGERPRINTING Y AGRUPAMIENTO:")
    print("-" * 50)
    print(f"Total videos con archivo analizado:    {len(processed_videos)}")
    print(f"Grupos de videos con SHA-256 idéntico: {len(exact_duplicates)} ({sum(len(v) for v in exact_duplicates.values())} videos agrupados)")
    print(f"Grupos de variantes visuales (pHash):  {len(visual_variants)} ({sum(len(v) for v in visual_variants.values())} anuncios agrupados)")
    print(f"Grupos de creativos idénticos (hash):  {len(creative_duplicates)} ({sum(len(v) for v in creative_duplicates.values())} anuncios agrupados)")

    print("\n⏱️ LÍMITES DE CONTEXTO Y DURACIÓN (GEMINI):")
    print("-" * 50)
    print(f"Resolución configurada:               {settings.GEMINI_MEDIA_RESOLUTION}")
    print(f"Videos con estado 'demasiado_largo':  {len(too_long_videos)}")
    if too_long_videos:
        for tv in too_long_videos:
            print(f"  [!] Ad {tv['ad_id']} ({tv['page']}): {tv['duration_s']}s -> {tv['tokens']:,} tokens")
    else:
        print("  ✓ Todos los videos caben en el contexto completo de Gemini 2.5 Flash sin recorte.")

    conn.close()
    return {
        "total_ads": len(rows),
        "format_counts": dict(format_counts),
        "videos_analyzed": len(processed_videos),
        "exact_duplicates_count": len(exact_duplicates),
        "visual_variants_count": len(visual_variants),
        "creative_duplicates_count": len(creative_duplicates),
        "too_long_videos_count": len(too_long_videos),
    }

if __name__ == "__main__":
    audit_media()
