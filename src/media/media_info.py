"""
Jinora Media Inspection Utility
Extracts video and image metadata locally without FFmpeg or external heavy binaries.
Powered by pymediainfo and Pillow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from PIL import Image
from pymediainfo import MediaInfo

from src.config import settings


def _calculate_aspect_ratio(width: int, height: int) -> str:
    """Classify aspect ratio into standard ad formats (9:16, 1:1, 4:5, 16:9) or custom."""
    if height <= 0 or width <= 0:
        return "unknown"
    ratio = width / height
    if 0.50 <= ratio <= 0.62:
        return "9:16"
    if 0.75 <= ratio <= 0.85:
        return "4:5"
    if 0.95 <= ratio <= 1.05:
        return "1:1"
    if 1.70 <= ratio <= 1.85:
        return "16:9"
    return f"{ratio:.2f}"


def get_video_info(path: Union[str, Path]) -> Dict[str, Any]:
    """
    Extracts fundamental video container metadata without FFmpeg.
    Returns:
        duration_s: Video duration in seconds (float).
        width: Video frame width in pixels (int).
        height: Video frame height in pixels (int).
        aspect_ratio: Classified aspect ratio (e.g. '9:16', '16:9', '1:1').
        has_audio_track: True if at least one audio track exists.
    """
    video_path = Path(path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    media_info = MediaInfo.parse(str(video_path))

    duration_ms: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_audio_track = False

    for track in media_info.tracks:
        if track.track_type == "General" and duration_ms is None:
            if track.duration is not None:
                duration_ms = float(track.duration)
        elif track.track_type == "Video":
            if track.duration is not None and duration_ms is None:
                duration_ms = float(track.duration)
            if track.width is not None:
                width = int(track.width)
            if track.height is not None:
                height = int(track.height)
        elif track.track_type == "Audio":
            has_audio_track = True

    duration_s = round((duration_ms / 1000.0), 2) if duration_ms is not None else 0.0
    w = width or 0
    h = height or 0
    aspect_ratio = _calculate_aspect_ratio(w, h)

    return {
        "duration_s": duration_s,
        "width": w,
        "height": h,
        "aspect_ratio": aspect_ratio,
        "has_audio_track": has_audio_track,
    }


def get_image_info(path: Union[str, Path]) -> Dict[str, Any]:
    """
    Extracts image metadata using Pillow.
    Returns width, height, aspect ratio, image format, and color mode.
    """
    image_path = Path(path)
    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    with Image.open(image_path) as img:
        width, height = img.size
        img_format = img.format or "UNKNOWN"
        img_mode = img.mode

    aspect_ratio = _calculate_aspect_ratio(width, height)

    return {
        "width": width,
        "height": height,
        "aspect_ratio": aspect_ratio,
        "format": img_format,
        "mode": img_mode,
    }


def estimate_video_tokens(duration_s: float, resolution: str = "medium") -> int:
    """
    Estimates token consumption for Gemini multimodal video processing.
    - low resolution: ~100 tokens/second
    - medium resolution: ~280 tokens/second
    - high resolution: ~560 tokens/second
    Plus 1,000 baseline tokens for system prompt, rubric, and JSON response schema.
    """
    if duration_s <= 0:
        return 1000

    res = resolution.lower()
    if res == "high":
        tokens_per_sec = 560
    elif res == "low":
        tokens_per_sec = 100
    else:  # medium (default)
        tokens_per_sec = 280

    media_tokens = int(duration_s * tokens_per_sec)
    return media_tokens + 1000


def check_video_context_limit(
    duration_s: float,
    resolution: str = "medium",
    max_context_tokens: int = 1_000_000,
    max_tpm_tokens: Optional[int] = None,
) -> Tuple[bool, int, str]:
    """
    Verifies if a video fits into the model context window and free tier TPM without clipping.
    Returns:
        (is_too_long, est_tokens, reason)
    """
    est_tokens = estimate_video_tokens(duration_s, resolution=resolution)
    tpm_limit = max_tpm_tokens if max_tpm_tokens is not None else settings.GEMINI_TPM

    # 1. TPM limit check: in free tier, a single request cannot exceed GEMINI_TPM (default 250,000 tokens)
    if tpm_limit and est_tokens > tpm_limit:
        return (
            True,
            est_tokens,
            f"demasiado_largo: {est_tokens:,} tokens estimados ({duration_s:.1f}s) excede el límite TPM de {tpm_limit:,} tokens/minuto",
        )

    # 2. Context window cap (1M tokens) or absolute hard duration limit (1 hour)
    if est_tokens > max_context_tokens or duration_s > 3600.0:
        return (
            True,
            est_tokens,
            f"demasiado_largo: {est_tokens:,} tokens estimados ({duration_s:.1f}s) excede contexto de {max_context_tokens:,}",
        )

    return False, est_tokens, "ok"
