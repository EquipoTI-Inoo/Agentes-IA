"""
Unified LLM Client Layer for Ad Evaluation
Implements common evaluate() interface:
- GeminiClient: Video analysis with Files API, medium resolution, QuotaGuard, and progressive retries.
- ClaudeClient: Imported from claude_client.py for static images, carousels, and DCO ads.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import ValidationError

from src.ai.claude_client import ClaudeClient
from src.ai.quota import (
    QuotaExhausted,
    QuotaGuard,
    compute_key_fingerprint,
    get_current_day_pt,
    get_quota_guard,
)
from src.ai.schemas import AdCopy, EvaluationResult, MediaBundle, StaticEvaluationResult
from src.config import settings
from src.media.media_info import check_video_context_limit, estimate_video_tokens

logger = logging.getLogger("Jinora.LLMClient")


def extract_retry_after(err: Exception) -> Optional[float]:
    """Extract retry delay in seconds from HTTP headers or Google RPC error details."""
    resp = getattr(err, "response", None)
    if resp and hasattr(resp, "headers"):
        ra = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass

    err_str = str(err)
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+(?:\.\d+)?)s['\"]", err_str)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


class BaseLLMClient(ABC):
    """Abstract base class for all LLM evaluation clients."""

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model

    @abstractmethod
    def evaluate(
        self, media: MediaBundle, copy: AdCopy
    ) -> Union[EvaluationResult, StaticEvaluationResult]:
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
        self.quota_guard = quota_guard or get_quota_guard()
        # Quota is counted per key; only the fingerprint is ever stored or logged
        self.key_fingerprint = compute_key_fingerprint(self.api_key)

        root_dir = Path(__file__).resolve().parent.parent.parent
        self.prompt_path = prompt_path or (root_dir / "prompts" / "evaluator_v1.md")

        self.prompt_template = ""
        if self.prompt_path.exists():
            self.prompt_template = self.prompt_path.read_text(encoding="utf-8")
        else:
            logger.warning(f"Prompt template no encontrado en {self.prompt_path}")

        # Lazy client and session-level upload cache (reuses file across consistency runs)
        self._client = None
        self._file_cache: Dict[str, Any] = {}

    @property
    def client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def cleanup_file_cache(self) -> None:
        """Deletes all session cached uploaded files from Gemini Files API."""
        for v_key, uploaded_file in list(self._file_cache.items()):
            if uploaded_file and hasattr(uploaded_file, "name"):
                try:
                    logger.info(f"Limpiando archivo temporal de Files API: {uploaded_file.name}")
                    self.client.files.delete(name=uploaded_file.name)
                except Exception as del_err:
                    logger.debug(f"Error eliminando archivo remoto {uploaded_file.name}: {del_err}")
        self._file_cache.clear()

    def evaluate(self, media: MediaBundle, copy: AdCopy) -> EvaluationResult:
        """
        Uploads video via Files API (or reuses existing upload), executes evaluation
        against Gemini with response_schema, records all attempts in QuotaGuard,
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
        self.quota_guard.acquire(self.model, est_tokens=est_tokens, key_fingerprint=self.key_fingerprint)

        from google.genai import types

        v_key = str(video_path.resolve())
        uploaded_file = self._file_cache.get(v_key)

        try:
            # 3. Upload video using Google GenAI Files API (reuse if already cached in session)
            if not uploaded_file:
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

                self._file_cache[v_key] = uploaded_file
                logger.info(f"Video activo en Files API: {uploaded_file.name}")
            else:
                logger.info(f"Reusando video en Files API (ahorro de subida): {uploaded_file.name}")

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
                f"Neutralidad de formato: persona, UGC, demo o motion graphics no suman ni restan. "
                f"Si una dimensión recibe menos de 8 puntos (< 8), DEBES justificar explícitamente qué buscaste y no encontraste con tiempo y canal. "
                f"No sumes los puntajes en el JSON. "
                f"Responde estrictamente con un objeto JSON válido conforme al esquema EvaluationResult."
            )

            config = types.GenerateContentConfig(
                temperature=0.0,
                system_instruction=self.prompt_template,
                response_mime_type="application/json",
                response_schema=EvaluationResult,
            )

            # 5. Execute generation with progressive backoff and attempt tracking
            logger.info(f"Generando evaluación con {self.model} (temperatura=0.0)...")
            response = self._call_with_transient_retry([uploaded_file, user_content], config=config)
            raw_text = response.text or ""

            # 6. Validate with Pydantic with 1 retry on ValidationError
            try:
                eval_result = EvaluationResult.model_validate_json(raw_text)
            except ValidationError as val_err:
                logger.warning(f"Error de validación Pydantic en primera respuesta: {val_err}. Iniciando único reintento...")
                self.quota_guard.acquire(self.model, est_tokens=500, key_fingerprint=self.key_fingerprint)
                retry_prompt = (
                    f"{user_content}\n\n"
                    f"ATENCIÓN: Tu respuesta anterior generó el siguiente error de validación:\n{val_err}\n"
                    f"Recuerda que si una dimensión recibe menos de 8 puntos (< 8), debes justificar explícitamente qué buscaste y no encontraste con tiempo y canal.\n"
                    f"Corrige los campos y responde estrictamente con el JSON corregido."
                )
                retry_resp = self._call_with_transient_retry([uploaded_file, retry_prompt], config=config)
                eval_result = EvaluationResult.model_validate_json(retry_resp.text or "")
                response = retry_resp

            return eval_result

        except Exception as e:
            logger.error(f"Fallo durante la evaluación del anuncio en Gemini: {e}")
            raise

    def _call_with_transient_retry(self, cnts: list, config: Any = None):
        """Execute Gemini API call with progressive backoff and attempt tracking."""
        delays = settings.GEMINI_RETRY_DELAYS or [5.0, 15.0, 45.0, 120.0]
        max_attempts = len(delays)

        for attempt_idx, base_delay in enumerate(delays, 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=cnts,
                    config=config,
                )
                tokens_in = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
                self.quota_guard.record_attempt(
                    model=self.model,
                    provider="gemini",
                    status="success",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    key_fingerprint=self.key_fingerprint,
                )
                return resp
            except Exception as err:
                err_str = str(err)
                is_rpd = "429" in err_str and (
                    "per day" in err_str.lower()
                    or "generaterequestsperday" in err_str.lower()
                    or "free_tier_requests" in err_str.lower()
                    or "limit: 20" in err_str.lower()
                )
                status_code = "429_rpd" if is_rpd else (
                    "503" if "503" in err_str else (
                        "429" if "429" in err_str else (
                            "500" if "500" in err_str else "error"
                        )
                    )
                )

                self.quota_guard.record_attempt(
                    model=self.model,
                    provider="gemini",
                    status=status_code,
                    error_message=err_str[:200],
                    key_fingerprint=self.key_fingerprint,
                )

                if is_rpd:
                    self.quota_guard.mark_exhausted(self.model, key_fingerprint=self.key_fingerprint)
                    raise QuotaExhausted(
                        model=self.model,
                        day_pt=get_current_day_pt(),
                        requests_used=settings.get_rpd_for_model(self.model),
                        limit=settings.get_rpd_for_model(self.model),
                        reason="429 RESOURCE_EXHAUSTED: límite diario RPD alcanzado. Cola detenida hasta medianoche PT.",
                    ) from err

                retry_after_s = extract_retry_after(err)
                wait_time = max(retry_after_s or 0.0, base_delay) + random.uniform(0.1, 1.0)

                if attempt_idx < max_attempts and (
                    "503" in err_str or "500" in err_str or "UNAVAILABLE" in err_str
                    or "429" in err_str or "high demand" in err_str or "timeout" in err_str.lower()
                ):
                    logger.warning(
                        f"Error transitorio de Gemini ({status_code}). Intento {attempt_idx}/{max_attempts}. "
                        f"Esperando {wait_time:.1f}s (base={base_delay}s, retry_after={retry_after_s})..."
                    )
                    time.sleep(wait_time)
                    continue

                raise


def get_llm_client(ad_type: str = "video") -> BaseLLMClient:
    """Factory returning the appropriate LLM client based on ad format."""
    if ad_type in ("video", "ugc"):
        return GeminiClient()
    return ClaudeClient()
