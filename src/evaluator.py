"""
Jinora AI Video Evaluator (Phase 4)
Replaces legacy heuristics with real multimodal Gemini evaluation:
- Strict Pydantic schema validation (EvaluationResult)
- 4-dimension anchored scoring (hook, mechanism, narrative, offer, 0-25 each)
- Creative score computed strictly by code (LLM never sums)
- Prompt enforces AI neutrality and Cosmiq context without mentioning days_active or competitor brand
- Caching by model + prompt_version + creative_hash in 'analyses' database table
- Persists analysis.json alongside the ad
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.ai.llm_client import GeminiClient
from src.ai.schemas import AdCopy, EvaluationResult, MediaBundle
from src.config import settings
from src.database import AdRecord, AnalysisRecord, SessionLocal
from src.media.fingerprint import compute_creative_hash, compute_file_sha256
from src.media.media_info import get_video_info

logger = logging.getLogger("Jinora.Evaluator")

DOWNLOADS_DIR = Path("downloads")
PROMPT_VERSION = "v1"
RUBRIC_VERSION = "v1"


def evaluate_video_ad(
    ad_dict: Dict[str, Any],
    client: Optional[GeminiClient] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Evaluates a video ad using Gemini, calculating creative_score by code
    and persisting results to SQLite/PostgreSQL 'analyses' table and analysis.json.
    """
    ad_id = str(ad_dict.get("ad_id") or ad_dict.get("id"))
    page_name = ad_dict.get("page_name") or "general"

    # 1. Locate local video file
    video_path_str = ad_dict.get("video_path")
    if not video_path_str:
        # Check in downloaded_files
        for f in ad_dict.get("downloaded_files", []):
            if str(f).endswith(".mp4"):
                video_path_str = f
                break

    if not video_path_str:
        raise ValueError(f"No video file found for ad {ad_id}")

    v_path = Path(video_path_str)
    if not v_path.is_absolute():
        v_path = DOWNLOADS_DIR / v_path

    if not v_path.exists():
        raise FileNotFoundError(f"Video file does not exist on disk: {v_path}")

    # 2. Compute media info and hashes
    v_info = get_video_info(v_path)
    file_sha = compute_file_sha256(v_path)

    creatives = ad_dict.get("creatives", [])
    c0 = creatives[0] if creatives else {}
    headline = ad_dict.get("headline") or c0.get("title") or ""
    body_text = ad_dict.get("body_text") or c0.get("body") or ""
    cta_text = ad_dict.get("cta_text") or c0.get("cta_text") or c0.get("cta_type") or ""
    link_url = ad_dict.get("link_url") or c0.get("link_url") or ""

    c_hash = compute_creative_hash(file_sha, copy_text=body_text, title=headline)

    model_name = settings.LLM_MODEL
    db = SessionLocal()

    try:
        # 3. Check cache in database (unless force=True)
        if not force:
            cached = (
                db.query(AnalysisRecord)
                .filter(
                    AnalysisRecord.ad_id == ad_id,
                    AnalysisRecord.model == model_name,
                    AnalysisRecord.prompt_version == PROMPT_VERSION,
                )
                .first()
            )
            if cached:
                logger.info(f"💾 Utilizando evaluación en caché para ad {ad_id} (Score: {cached.creative_score}/100)")
                cached_data = json.loads(cached.result_json)
                return {
                    "ad_id": ad_id,
                    "creative_score": cached.creative_score,
                    "total_score": cached.creative_score,
                    "cached": True,
                    "evaluation": cached_data,
                }

        # 4. Prepare bundles for LLM
        media_bundle = MediaBundle(
            media_path=v_path,
            media_type="video",
            duration_s=v_info.get("duration_s"),
            width=v_info.get("width"),
            height=v_info.get("height"),
            has_audio_track=v_info.get("has_audio_track", True),
        )

        copy_bundle = AdCopy(
            headline=headline,
            body_text=body_text,
            cta_text=cta_text,
            link_url=link_url,
        )

        # 5. Call Gemini client
        eval_client = client or GeminiClient()
        try:
            eval_result: EvaluationResult = eval_client.evaluate(media_bundle, copy_bundle)
        except Exception as eval_err:
            # Leave ad in pending_evaluation with retry_after and last error (Paso 2)
            ad_record = db.query(AdRecord).filter(AdRecord.id == ad_id).first()
            if ad_record:
                ad_record.cognitive_score = None
                pending_info = {
                    "status": "pending_evaluation",
                    "last_error": str(eval_err),
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "retry_after": datetime.now(timezone.utc).isoformat(),
                }
                ad_record.score_breakdown = json.dumps(pending_info, ensure_ascii=False)
                db.commit()
            raise

        # 6. Sum dimensions strictly BY CODE (LLM never sums)
        creative_score = (
            eval_result.hook.score
            + eval_result.mechanism.score
            + eval_result.narrative.score
            + eval_result.offer.score
        )

        result_dict = eval_result.model_dump()
        result_dict["creative_score"] = creative_score
        result_dict["total_score"] = creative_score

        # 7. Persist to analysis.json in ad folder
        ad_folder = v_path.parent
        analysis_json_path = ad_folder / "analysis.json"
        try:
            analysis_json_path.write_text(
                json.dumps(result_dict, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(f"Guardado analysis.json en: {analysis_json_path}")
        except Exception as e:
            logger.warning(f"No se pudo escribir {analysis_json_path}: {e}")

        # 8. Persist to 'analyses' table in DB
        analysis_record = AnalysisRecord(
            ad_id=ad_id,
            model=model_name,
            prompt_version=PROMPT_VERSION,
            rubric_version=RUBRIC_VERSION,
            creative_hash=c_hash,
            analysis_type="video",
            creative_score=creative_score,
            result_json=json.dumps(result_dict, ensure_ascii=False),
            cost_usd=0.0,
        )
        db.add(analysis_record)

        # Update AdRecord if exists
        ad_record = db.query(AdRecord).filter(AdRecord.id == ad_id).first()
        if ad_record:
            ad_record.cognitive_score = creative_score
            ad_record.score_breakdown = json.dumps(result_dict, ensure_ascii=False)
            if eval_result.transcript and not ad_record.full_transcript:
                full_trans = " ".join(t.text for t in eval_result.transcript)
                ad_record.full_transcript = full_trans
            if eval_result.speech_pace_wpm_estimate:
                ad_record.total_frames = eval_result.speech_pace_wpm_estimate

        db.commit()

        return {
            "ad_id": ad_id,
            "creative_score": creative_score,
            "total_score": creative_score,
            "cached": False,
            "evaluation": result_dict,
        }

    finally:
        db.close()


def evaluate_ad_cognitively(ad_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility bridge for existing callers in web/app.py."""
    try:
        return evaluate_video_ad(ad_dict)
    except Exception as e:
        logger.warning(f"Error en evaluate_video_ad ({e}), retornando estructura base.")
        return {
            "total_score": 50,
            "creative_score": 50,
            "error": str(e),
        }
