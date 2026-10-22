"""
Ad Evaluation Format Router
Routes ad evaluation according to media format:
- video -> GeminiClient via evaluate_video_ad
- imagen / carrusel / dco -> ClaudeClient via evaluate_static_ad
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.evaluator import evaluate_video_ad
from src.evaluator_static import evaluate_static_ad
from src.media.fingerprint import detect_ad_format

logger = logging.getLogger("Jinora.Router")


def route_and_evaluate_ad(ad_dict: Dict[str, Any], force: bool = False, prompt_version: Optional[str] = None) -> Dict[str, Any]:
    """Inspects ad format and routes to appropriate evaluator (Gemini for video, Claude for static)."""
    format_type = detect_ad_format(ad_dict)
    logger.info(f"Enrutando anuncio {ad_dict.get('ad_id')} (Formato detectado: {format_type})")

    if format_type == "video":
        return evaluate_video_ad(ad_dict, force=force, prompt_version=prompt_version)
    else:
        return evaluate_static_ad(ad_dict, force=force, prompt_version=prompt_version)
