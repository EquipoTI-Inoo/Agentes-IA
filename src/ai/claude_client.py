"""
Claude Client for Static Ad Evaluation (Images, Carousels, DCO)
Uses Anthropic Messages API with strict budget capping, image dimension resizing,
and zero-cost fallback to pending_evaluation when ANTHROPIC_API_KEY is not configured.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from PIL import Image

from src.ai.quota import QuotaGuard
from src.ai.schemas import AdCopy, MediaBundle, StaticEvaluationResult
from src.config import settings

logger = logging.getLogger("Jinora.ClaudeClient")


class MissingApiKeyError(Exception):
    """Raised when ANTHROPIC_API_KEY is absent from environment."""
    pass


class WorkspaceRequiredError(Exception):
    """Raised when Anthropic rejects the request because the API key is not scoped to a workspace."""
    pass


def _redact(text: str, secret: Optional[str]) -> str:
    """Never let the API key reach logs or error messages."""
    return text.replace(secret, "<redacted>") if secret else text


class BudgetExceededError(Exception):
    """Raised when cumulative Claude spending reaches or exceeds CLAUDE_BUDGET_USD."""
    pass


class ClaudeClient:
    """Evaluates static image, carousel, and DCO advertisements using Anthropic Claude."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        budget_limit: Optional[float] = None,
        max_image_px: Optional[int] = None,
        prompt_path: Optional[Path] = None,
        pricing_path: Optional[Path] = None,
        quota_guard: Optional[QuotaGuard] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.ANTHROPIC_API_KEY
        self.model = model or settings.CLAUDE_MODEL_STATIC or "claude-sonnet-5-5"
        self.budget_limit = budget_limit if budget_limit is not None else settings.CLAUDE_BUDGET_USD
        self.max_image_px = max_image_px or settings.CLAUDE_MAX_IMAGE_PX or 1568
        self.quota_guard = quota_guard or QuotaGuard()

        root_dir = Path(__file__).resolve().parent.parent.parent
        self.prompt_path = prompt_path or (root_dir / "prompts" / "evaluator_static_v1.md")
        self.pricing_path = pricing_path or (root_dir / "config" / "pricing.yaml")

        self.prompt_template = ""
        if self.prompt_path.exists():
            self.prompt_template = self.prompt_path.read_text(encoding="utf-8")

        self.pricing = self._load_pricing()
        self.last_usage: Dict[str, Any] = {}

    def _load_pricing(self) -> Dict[str, Any]:
        if not self.pricing_path.exists():
            return {
                "input_per_million": 3.00,
                "output_per_million": 15.00,
            }
        try:
            with open(self.pricing_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            anthropic_cfg = data.get("anthropic", {}).get("models", {})
            return anthropic_cfg.get(self.model, {"input_per_million": 3.00, "output_per_million": 15.00})
        except Exception as e:
            logger.warning(f"Error cargando pricing.yaml ({e}), usando valores default.")
            return {"input_per_million": 3.00, "output_per_million": 15.00}

    def _calculate_cost(self, tokens_in: int, tokens_out: int) -> float:
        in_cost = (tokens_in / 1_000_000.0) * self.pricing.get("input_per_million", 3.00)
        out_cost = (tokens_out / 1_000_000.0) * self.pricing.get("output_per_million", 15.00)
        return in_cost + out_cost

    def _prepare_image_b64(self, path: Union[str, Path]) -> Tuple[str, str]:
        """Loads image, resizes if longest edge exceeds max_image_px, returns (base64_str, media_type)."""
        img_path = Path(path)
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {img_path}")

        with Image.open(img_path) as img:
            format_name = img.format or "JPEG"
            media_type = f"image/{format_name.lower().replace('jpg', 'jpeg')}"
            if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                media_type = "image/jpeg"
                img = img.convert("RGB")

            w, h = img.size
            max_edge = max(w, h)
            if max_edge > self.max_image_px:
                scale = self.max_image_px / float(max_edge)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                logger.info(f"Redimensionada imagen {img_path.name}: {w}x{h} -> {new_w}x{new_h}")

            buffer = io.BytesIO()
            save_format = "PNG" if media_type == "image/png" else "JPEG"
            if save_format == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buffer, format=save_format, quality=90)
            b64_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
            return b64_data, media_type

    def evaluate(self, media: MediaBundle, copy: AdCopy) -> StaticEvaluationResult:
        """
        Evaluates a static ad, carousel, or DCO using Anthropic Claude.
        Enforces API key presence and budget limits.
        """
        if not self.api_key:
            raise MissingApiKeyError(
                "Sin clave ANTHROPIC_API_KEY en .env: anuncio estático encolado en pending_evaluation"
            )

        current_spend = self.quota_guard.get_total_spend(provider="claude")
        if current_spend >= self.budget_limit:
            raise BudgetExceededError(
                f"Tope de gasto alcanzado para Claude (${current_spend:.2f} >= ${self.budget_limit:.2f} USD). "
                f"Anuncio estático encolado en pending_evaluation."
            )

        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)

        # Build message contents
        content_blocks: List[Dict[str, Any]] = []

        # 1. Main or primary image
        b64_main, media_type_main = self._prepare_image_b64(media.media_path)
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type_main,
                "data": b64_main,
            },
        })

        # 2. Carousel cards if present (up to 10 cards)
        if media.carousel_paths:
            for idx, c_path in enumerate(media.carousel_paths[:10], 1):
                try:
                    c_b64, c_mtype = self._prepare_image_b64(c_path)
                    content_blocks.append({
                        "type": "text",
                        "text": f"--- TARJETA {idx} DEL CARRUSEL ---",
                    })
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": c_mtype,
                            "data": c_b64,
                        },
                    })
                except Exception as e:
                    logger.warning(f"Error procesando tarjeta {idx} ({c_path}): {e}")

        # 3. Text prompt with copy (no brand, no days_active)
        user_prompt = (
            f"=== INFORMACIÓN DEL COPY Y TEXTO DEL ANUNCIO ESTÁTICO ===\n"
            f"Titular: {copy.headline or 'N/A'}\n"
            f"Texto del anuncio (copy): {copy.body_text or 'N/A'}\n"
            f"Llamada a la acción (CTA): {copy.cta_text or 'N/A'}\n"
            f"URL de destino: {copy.link_url or 'N/A'}\n\n"
            f"Formato detectado: {media.media_type}\n\n"
            f"IMPORTANTE: Evalúa con base estricta en las anclas de la rúbrica (0-25). "
            f"Sin cupos ni topes numéricos. No sumes el puntaje. "
            f"Responde estrictamente con un objeto JSON válido conforme al esquema StaticEvaluationResult."
        )
        content_blocks.append({"type": "text", "text": user_prompt})

        # Call Claude
        logger.info(f"Enviando solicitud estática a Claude ({self.model})...")
        # Keys not scoped to a workspace must name the workspace explicitly
        extra_args: Dict[str, Any] = {}
        if settings.ANTHROPIC_WORKSPACE_ID:
            extra_args["workspace_id"] = settings.ANTHROPIC_WORKSPACE_ID

        try:
            response = client.messages.create(
                **extra_args,
                model=self.model,
                max_tokens=2500,
                system=self.prompt_template,
                messages=[{"role": "user", "content": content_blocks}],
            )
        except Exception as exc:
            detail = _redact(str(exc), self.api_key)
            if getattr(exc, "status_code", None) == 400 and "workspace" in detail.lower():
                raise WorkspaceRequiredError(
                    "Anthropic rechazó la solicitud (400): la clave ANTHROPIC_API_KEY no está asociada a un workspace. "
                    "Acción: copia el ID de tu workspace desde la consola de Anthropic y ponlo en .env como "
                    "ANTHROPIC_WORKSPACE_ID=<id> (o usa una clave creada dentro de un workspace). "
                    "El anuncio queda en pending_evaluation y no cuenta como fallo."
                ) from None
            raise

        # Extract text response
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        # Validate JSON with Pydantic
        clean_text = raw_text.strip()
        if clean_text.startswith("```json"):
            clean_text = clean_text[len("```json"):].strip()
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3].strip()

        eval_result = StaticEvaluationResult.model_validate_json(clean_text)

        # Token & cost recording
        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost_usd = self._calculate_cost(tokens_in, tokens_out)
        self.last_usage = {"tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": cost_usd}

        self.quota_guard.record_usage(
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requests=1,
            provider="claude",
            cost_usd=cost_usd,
        )

        return eval_result
