"""
Jinora AI Static Ad Evaluator (Phase S3 / S4)
Evaluates static images, carousels, and DCO ads using Anthropic Claude:
- Enforces ANTHROPIC_API_KEY check and CLAUDE_BUDGET_USD limit.
- If key is missing or budget exceeded, gracefully defers ad to 'pending_evaluation'.
- Computes creative_score strictly by code (stop_power + message_clarity + proof + offer_cta).
- Persists to 'analyses' table and analysis_static.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.ai.prompts import prompt_path
from src.ai.claude_client import BudgetExceededError, ClaudeClient, MissingApiKeyError, WorkspaceRequiredError
from src.ai.schemas import AdCopy, MediaBundle, StaticEvaluationResult
from src.config import settings
from src.database import AnalysisRecord, SessionLocal, ensure_ad, mark_evaluated
from src.media.fingerprint import compute_creative_hash, compute_image_phash

logger = logging.getLogger("Jinora.EvaluatorStatic")

DOWNLOADS_DIR = Path("downloads")
PROMPT_VERSION = "v1"
RUBRIC_PATH = Path(__file__).resolve().parent.parent / "config" / "rubric_static.yaml"


def _load_rubric_version() -> str:
    """Version declared in config/rubric_static.yaml, so analyses rows show which anchors were used."""
    import yaml

    try:
        return str(yaml.safe_load(RUBRIC_PATH.read_text(encoding="utf-8")).get("version", "v1"))
    except Exception:
        return "v1"


RUBRIC_VERSION = _load_rubric_version()


def evaluate_static_ad(
    ad_dict: Dict[str, Any],
    client: Optional[ClaudeClient] = None,
    force: bool = False,
    prompt_version: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluates a static image, carousel, or DCO ad using Claude.
    Gracefully handles missing API keys by marking status as pending_evaluation.
    """
    ad_id = str(ad_dict.get("ad_id") or ad_dict.get("id"))
    version = prompt_version or PROMPT_VERSION
    if client is None and version != PROMPT_VERSION:
        client = ClaudeClient(prompt_path=prompt_path("static", version))  # fails early if the prompt does not exist

    # 1. Locate local image or carousel files
    image_path_str = ad_dict.get("image_path")
    carousel_paths: list[Path] = []

    if not image_path_str:
        for f in ad_dict.get("downloaded_files", []):
            f_str = str(f)
            if f_str.endswith(".jpg") or f_str.endswith(".png") or f_str.endswith(".webp"):
                if not image_path_str:
                    image_path_str = f_str
                carousel_paths.append(DOWNLOADS_DIR / f_str if not Path(f_str).is_absolute() else Path(f_str))

    if not image_path_str:
        raise ValueError(f"No image files found for ad {ad_id}")

    img_path = Path(image_path_str)
    if not img_path.is_absolute():
        img_path = DOWNLOADS_DIR / img_path

    if not img_path.exists():
        raise FileNotFoundError(f"Image file does not exist on disk: {img_path}")

    # 2. Compute pHash and creative hash
    img_phash = compute_image_phash(img_path) or "0000000000000000"

    creatives = ad_dict.get("creatives", [])
    c0 = creatives[0] if creatives else {}
    headline = ad_dict.get("headline") or c0.get("title") or ""
    body_text = ad_dict.get("body_text") or c0.get("body") or ""
    cta_text = ad_dict.get("cta_text") or c0.get("cta_text") or c0.get("cta_type") or ""
    link_url = ad_dict.get("link_url") or c0.get("link_url") or ""

    c_hash = compute_creative_hash(img_phash, copy_text=body_text, title=headline)
    model_name = settings.CLAUDE_MODEL_STATIC or "claude-sonnet-5-5"
    db = SessionLocal()

    try:
        # 3. Check cache in database
        if not force:
            cached = (
                db.query(AnalysisRecord)
                .filter(
                    AnalysisRecord.ad_id == ad_id,
                    AnalysisRecord.model == model_name,
                    AnalysisRecord.prompt_version == version,
                )
                .first()
            )
            if cached:
                logger.info(f"💾 Utilizando evaluación estática en caché para ad {ad_id} (Score: {cached.creative_score}/100)")
                return {
                    "ad_id": ad_id,
                    "creative_score": cached.creative_score,
                    "total_score": cached.creative_score,
                    "cached": True,
                    "evaluation": json.loads(cached.result_json),
                }

        # 4. Prepare bundles for Claude
        media_type = "carousel" if len(carousel_paths) > 1 else "image"
        media_bundle = MediaBundle(
            media_path=img_path,
            media_type=media_type,
            carousel_paths=[str(p) for p in carousel_paths],
        )

        copy_bundle = AdCopy(
            headline=headline,
            body_text=body_text,
            cta_text=cta_text,
            link_url=link_url,
        )

        # 5. Call Claude client with key and budget safeguards
        eval_client = client or ClaudeClient()

        try:
            eval_result: StaticEvaluationResult = eval_client.evaluate(media_bundle, copy_bundle)
        except (MissingApiKeyError, BudgetExceededError, WorkspaceRequiredError) as safe_err:
            logger.warning(f"⚠️ {safe_err}. El anuncio {ad_id} queda en pending_evaluation.")
            return {
                "ad_id": ad_id,
                "status": "pending_evaluation",
                "notice": str(safe_err),
                "cached": False,
            }

        # 6. Sum dimensions strictly BY CODE
        creative_score = (
            eval_result.stop_power.score
            + eval_result.message_clarity.score
            + eval_result.proof.score
            + eval_result.offer_cta.score
        )

        result_dict = eval_result.model_dump()
        result_dict["creative_score"] = creative_score
        result_dict["total_score"] = creative_score

        # Real cost of this call (ClaudeClient.last_usage); 0.0 for clients that do not report it
        last_usage = getattr(eval_client, "last_usage", None)
        real_cost_usd = float(last_usage.get("cost_usd", 0.0)) if isinstance(last_usage, dict) else 0.0

        # 7. Persist to analysis_static.json
        ad_folder = img_path.parent
        analysis_json_path = ad_folder / "analysis_static.json"
        try:
            analysis_json_path.write_text(
                json.dumps(result_dict, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(f"Guardado analysis_static.json en: {analysis_json_path}")
        except Exception as e:
            logger.warning(f"No se pudo escribir {analysis_json_path}: {e}")

        # 8. Persist to 'analyses' table in DB
        analysis_record = AnalysisRecord(
            ad_id=ad_id,
            model=model_name,
            prompt_version=version,
            rubric_version=RUBRIC_VERSION,
            creative_hash=c_hash,
            analysis_type="static",
            creative_score=creative_score,
            result_json=json.dumps(result_dict, ensure_ascii=False),
            cost_usd=real_cost_usd,
        )
        db.add(analysis_record)

        mark_evaluated(ensure_ad(db, {**ad_dict, "ad_id": ad_id}))
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
