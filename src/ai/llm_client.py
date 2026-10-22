"""
Unified LLM Client Layer for Ad Evaluation
Implements common evaluate() interface:
- GeminiClient: Video analysis with Files API, medium resolution, QuotaGuard, and 1 retry on JSON validation error.
- ClaudeClient: Imported from claude_client.py for static images, carousels, and DCO ads.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Optional, Union

from pydantic import ValidationError

from src.ai.claude_client import ClaudeClient
from src.ai.quota import QuotaGuard
from src.ai.schemas import AdCopy, EvaluationResult, MediaBundle, StaticEvaluationResult
from src.config import settings
from src.media.media_info import check_video_context_limit, estimate_video_tokens

logger = logging.getLogger("Jinora.LLMClient")


class BaseLLMClient(ABC):
    """Abstract interface for all multi-modal ad evaluation LLM providers."""

    @abstractmethod
    def evaluate(self, media: MediaBundle, copy: AdCopy) -> Union[EvaluationResult, StaticEvaluationResult]:
        """Evaluate ad creatives and copy, returning validated structured score."""
        pass


class GeminiClient(BaseLLMClient):
    """Evaluates video ads using Google Gemini API (Free Tier with QuotaGuard)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        quota_guard: Optional[QuotaGuard] = None,
        prompt_path: Optional[Path] = None,
    ) -> None:
        self.api_key = api_key or settings.GEMINI_API_KEY
        self.model = model or settings.LLM_MODEL
        self.quota_guard = quota_guard or QuotaGuard()

        root_dir = Path(__file__).resolve().parent.parent.parent
        self.prompt_path = prompt_path or (root_dir / "prompts" / "evaluator_v1.md")

        self.prompt_template = ""
        if self.prompt_path.exists():
            self.prompt_template = self.prompt_path.read_text(encoding="utf-8")
        else:
            logger.warning(f"Prompt template no encontrado en {self.prompt_path}")

        # Lazy init client
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def evaluate(self, media: MediaBundle, copy: AdCopy) -> EvaluationResult:
        """
        Uploads video via Files API, executes evaluation against Gemini with response_schema,
        and enforces Pydantic validation with 1 retry on error.
        """
        video_path = Path(media.media_path)
        if not video_path.exists():
            raise FileNotFoundError(f"Video file not found for evaluation: {video_path}")

        # 1. Estimate token consumption and check TPM / context limit
        duration_s = media.duration_s or 0.0
        is_too_long, est_tokens, reason = check_video_context_limit(
            duration_s, resolution=settings.GEMINI_MEDIA_RESOLUTION
        )
        if is_too_long:
            raise ValueError(f"No se puede evaluar el video: {reason}")

        # 2. Acquire QuotaGuard slot (enforces Pacific Time daily RPD & RPM/TPM sliding windows)
        self.quota_guard.acquire(self.model, est_tokens=est_tokens)

        from google.genai import types

        uploaded_file = None
        try:
            # 3. Upload video using Google GenAI Files API
            logger.info(f"Subiendo video a Gemini Files API ({video_path.name}, {video_path.stat().st_size / (1024*1024):.2f} MB)...")
            uploaded_file = self.client.files.upload(file=str(video_path))

            # Wait for file processing to complete
            max_wait_s = 60
            waited = 0
            while uploaded_file.state.name == "PROCESSING" and waited < max_wait_s:
                time.sleep(2)
                waited += 2
                uploaded_file = self.client.files.get(name=uploaded_file.name)

            if uploaded_file.state.name != "ACTIVE":
                raise RuntimeError(f"Video upload ended in non-active state: {uploaded_file.state.name}")

            logger.info(f"Video activo en Files API: {uploaded_file.name}")

            # 4. Compose prompt content (no brand name, no days_active)
            audio_instruction = ""
            if not media.has_audio_track:
                audio_instruction = "\nNOTA TÉCNICA: Este archivo de video NO cuenta con pista de audio. Marca has_voiceover=false y transcript=[]."

            user_content = (
                f"=== INFORMACIÓN DEL COPY Y TEXTO DEL ANUNCIO ===\n"
                f"Titular: {copy.headline or 'N/A'}\n"
                f"Texto del anuncio: {copy.body_text or 'N/A'}\n"
                f"Llamada a la acción (CTA): {copy.cta_text or 'N/A'}\n"
                f"URL de destino: {copy.link_url or 'N/A'}\n"
                f"{audio_instruction}\n\n"
                f"IMPORTANTE: Evalúa con base en las 4 dimensiones (0-25) sin cupos ni topes numéricos. "
                f"No sumes los puntajes en el JSON. "
                f"Responde estrictamente con un objeto JSON válido conforme al esquema EvaluationResult."
            )

            config = types.GenerateContentConfig(
                temperature=0.0,
                system_instruction=self.prompt_template,
                response_mime_type="application/json",
                response_schema=EvaluationResult,
            )

            # 5. Execute generation
            logger.info(f"Generando evaluación con {self.model} (temperatura=0.0)...")
            def _call_with_transient_retry(cnts):
                max_retries = 4
                for attempt in range(max_retries):
                    try:
                        return self.client.models.generate_content(
                            model=self.model,
                            contents=cnts,
                            config=config,
                        )
                    except Exception as err:
                        err_str = str(err)
                        if attempt < max_retries - 1 and ("503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str):
                            wait_time = 5.0 * (attempt + 1)
                            logger.warning(f"Error transitorio de Gemini ({err_str[:60]}), esperando {wait_time}s para reintentar ({attempt+1}/{max_retries-1})...")
                            time.sleep(wait_time)
                            continue
                        raise

            response = _call_with_transient_retry([uploaded_file, user_content])

            raw_text = response.text or ""

            # 6. Validate with Pydantic with 1 retry on ValidationError
            try:
                eval_result = EvaluationResult.model_validate_json(raw_text)
            except ValidationError as val_err:
                logger.warning(f"Error de validación Pydantic en primera respuesta: {val_err}. Iniciando único reintento...")
                # Retry counts quota
                self.quota_guard.acquire(self.model, est_tokens=500)
                retry_prompt = (
                    f"{user_content}\n\n"
                    f"ATENCIÓN: Tu respuesta anterior generó el siguiente error de validación:\n{val_err}\n"
                    f"Corrige los campos y responde estrictamente con el JSON corregido."
                )
                retry_resp = _call_with_transient_retry([uploaded_file, retry_prompt])
                eval_result = EvaluationResult.model_validate_json(retry_resp.text or "")
                response = retry_resp

            # 7. Record usage
            tokens_in = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            tokens_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            self.quota_guard.record_usage(
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                requests=1,
                provider="gemini",
                cost_usd=0.0,
            )

            return eval_result

        finally:
            # 8. Clean up uploaded file from Gemini Files API
            if uploaded_file and hasattr(uploaded_file, "name"):
                try:
                    logger.info(f"Limpiando archivo temporal de Files API: {uploaded_file.name}")
                    self.client.files.delete(name=uploaded_file.name)
                except Exception as del_err:
                    logger.debug(f"Error eliminando archivo remoto {uploaded_file.name}: {del_err}")


def get_llm_client(ad_type: str = "video") -> BaseLLMClient:
    """Factory returning the appropriate LLM client based on ad format."""
    if ad_type in ("video", "ugc"):
        return GeminiClient()
    return ClaudeClient()
