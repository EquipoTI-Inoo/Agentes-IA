"""
Jinora Media Fingerprinting and Format Classification Module
- Exact file hashing (SHA-256) for downloaded media deduplication
- Perceptual image hashing (pHash via Pillow & imagehash) for visual variant grouping
- Composite creative hash (visual_hash + normalized copy) for ad deduplication
- Phase 5: variant grouping (exact SHA-256, or close pHash + identical normalized copy)
- Format classification (video, imagen, carrusel, dco) and LLM routing
  (video -> Gemini, static -> Claude)
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import imagehash
from PIL import Image

from src.config import settings

logger = logging.getLogger("Jinora.Fingerprint")


def compute_file_sha256(path: Union[str, Path], chunk_size: int = 65536) -> str:
    """Computes exact SHA-256 hex digest of a file on disk."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found for hashing: {file_path}")

    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_image_phash(path: Union[str, Path]) -> Optional[str]:
    """Computes perceptual hash (pHash) string of an image file."""
    image_path = Path(path)
    if not image_path.exists():
        return None

    try:
        with Image.open(image_path) as img:
            # Ensure RGB mode for consistent hash
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            h = imagehash.phash(img)
            return str(h)
    except Exception as e:
        logger.warning(f"Error computing pHash for {image_path}: {e}")
        return None


def compute_creative_hash(visual_hash: Optional[str], copy_text: str = "", title: str = "") -> str:
    """
    Computes composite creative hash combining visual fingerprint and copy.
    Variants with identical visual and copy share this creative_hash.
    """
    clean_copy = re.sub(r"\s+", " ", f"{title} {copy_text}".lower().strip())
    content = f"vis:{visual_hash or 'none'}|copy:{clean_copy}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def normalize_copy(text: Optional[str]) -> str:
    """Lowercase, strip punctuation/emojis and collapse repeated whitespace."""
    if not text:
        return ""
    clean = re.sub(r"[^\w\s]|_", " ", text.lower())
    return re.sub(r"\s+", " ", clean).strip()


def compute_copy_hash(body_text: Optional[str], headline: Optional[str] = None) -> Optional[str]:
    """
    SHA-256 of the normalized primary text (body). The headline is only used when there is
    no body: Meta rotates headlines across variants of the same creative, so it is not identity.
    """
    normalized = normalize_copy(body_text) or normalize_copy(headline)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_creative_hash(phash: Optional[str], media_sha256: Optional[str], copy_hash: Optional[str]) -> str:
    """Phase 5 creative_hash = pHash of thumbnail/image + SHA-256 of media file + normalized copy hash."""
    content = f"phash:{phash or 'none'}|sha:{media_sha256 or 'none'}|copy:{copy_hash or 'none'}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def phash_distance(a: str, b: str) -> int:
    """Hamming distance between two hex pHash strings (0-64 for 8x8 pHash)."""
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def group_creative_variants(ads: List[Dict[str, Any]], phash_max_distance: int) -> Dict[str, Dict[str, Any]]:
    """
    Groups ads that are variants of the same creative. Each ad dict needs 'id' and may carry
    'media_sha256', 'phash' and 'copy_hash'. Two ads are linked when:
      1. they share the exact media SHA-256 (byte-identical video/image), or
      2. both have a pHash within phash_max_distance AND identical normalized copy, or
      3. neither has any visual fingerprint (no media on disk) AND identical normalized copy.
    Links are transitive (union-find). Returns {ad_id: {"creative_group_id", "variant_count"}}.
    """
    parent: Dict[str, str] = {str(a["id"]): str(a["id"]) for a in ads}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    by_sha: Dict[str, List[str]] = {}
    by_copy: Dict[str, List[Dict[str, Any]]] = {}
    for a in ads:
        if a.get("media_sha256"):
            by_sha.setdefault(a["media_sha256"], []).append(str(a["id"]))
        if a.get("copy_hash"):
            by_copy.setdefault(a["copy_hash"], []).append(a)

    for ids in by_sha.values():
        for other in ids[1:]:
            union(ids[0], other)

    # Rules 2 and 3 both require identical copy, so only compare within copy buckets
    for bucket in by_copy.values():
        with_phash = [a for a in bucket if a.get("phash")]
        for i, a in enumerate(with_phash):
            for b in with_phash[i + 1:]:
                if phash_distance(a["phash"], b["phash"]) <= phash_max_distance:
                    union(str(a["id"]), str(b["id"]))
        no_visual = [str(a["id"]) for a in bucket if not a.get("phash") and not a.get("media_sha256")]
        for other in no_visual[1:]:
            union(no_visual[0], other)

    members: Dict[str, List[str]] = {}
    for ad_id in parent:
        members.setdefault(find(ad_id), []).append(ad_id)

    result: Dict[str, Dict[str, Any]] = {}
    for root, ids in members.items():
        group_id = "cg_" + hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        for ad_id in ids:
            result[ad_id] = {"creative_group_id": group_id, "variant_count": len(ids)}
    return result


def detect_ad_format(ad_data: Dict[str, Any]) -> str:
    """
    Classifies ad into one of four standard formats:
    - 'video': if creatives contain video files or video URLs
    - 'carrusel': if 2 or more distinct image cards/creatives with separate titles/links
    - 'imagen': if single image creative without video
    - 'dco': dynamic creative optimization (mixes videos and images or multiple variant headlines)
    """
    creatives = ad_data.get("creatives") or []
    has_video_path = bool(ad_data.get("video_path"))

    video_count = sum(
        1 for c in creatives
        if c.get("has_video") or c.get("video_hd_url") or c.get("video_sd_url") or c.get("video_url")
    )
    if has_video_path and video_count == 0:
        video_count = 1

    image_count = sum(
        1 for c in creatives
        if c.get("has_image") or c.get("image_url") or c.get("original_image_url")
    )
    if bool(ad_data.get("image_path")) and image_count == 0:
        image_count = 1

    total_creatives = len(creatives)

    # 1. DCO: Mixes video and image cards or multiple mixed variations
    if video_count > 0 and image_count > 1 and total_creatives > 2:
        return "dco"

    # 2. Video: Contains video content
    if video_count > 0:
        return "video"

    # 3. Carousel: Multiple image cards
    if total_creatives >= 2 or image_count >= 2:
        return "carrusel"

    # 4. Single static image
    return "imagen"


def route_ad_for_evaluation(format_type: str) -> Dict[str, str]:
    """
    Routes creative to appropriate LLM provider based on architectural decisions:
    - video -> Gemini (LLM_MODEL, multimodal)
    - imagen / carrusel / dco -> Claude (CLAUDE_MODEL_STATIC)
    """
    fmt = format_type.lower()
    if fmt == "video":
        return {
            "provider": "gemini",
            "model": settings.LLM_MODEL,
            "media_resolution": settings.GEMINI_MEDIA_RESOLUTION,
            "routing_reason": "Video multimodal procesado por Gemini (nivel gratuito)",
        }
    else:
        return {
            "provider": "claude",
            "model": settings.CLAUDE_MODEL_STATIC,
            "media_resolution": "high",
            "routing_reason": "Anuncio estático procesado por Claude con crédito prepago",
        }
