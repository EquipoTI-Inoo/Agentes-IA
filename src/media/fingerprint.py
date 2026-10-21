"""
Jinora Media Fingerprinting and Format Classification Module
- Exact file hashing (SHA-256) for downloaded media deduplication
- Perceptual image hashing (pHash via Pillow & imagehash) for visual variant grouping
- Composite creative hash (visual_hash + normalized copy) for ad deduplication
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
