"""
Optional executive summary for reports (Phase 8). OFF by default: it only runs when `report --summary` is passed.
Exactly ONE request to the configured LLM client (Gemini), through QuotaGuard (daily cap, reserve, key fingerprint).
If it cannot run (no quota, no key, error) the report says so; the rest of the report never depends on it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.ai.quota import QuotaExhausted
from src.config import settings

SYSTEM_PROMPT = (
    "Eres analista de inteligencia competitiva para Cosmiq, una plataforma de automatización de ventas y atención "
    "(WhatsApp, CRM, agendamiento, gestión de leads, mensajes masivos, agentes conversacionales y de voz). "
    "Recibirás los mejores anuncios de competidores ya evaluados. Redacta en español, en 3 secciones breves: "
    "1) Patrones comunes, 2) Huecos que Cosmiq puede ocupar, 3) Recomendaciones concretas de creativos. "
    "Básate SOLO en los datos recibidos. No supongas que un anuncio usa o promueve IA: la mención de IA es un dato "
    "descriptivo, no un mérito ni un defecto. Habla de capacidades y ángulos (feature_focus), no de la tecnología."
)


def build_prompt(report: Dict[str, Any]) -> str:
    """Compact text with the stored facts of the top ads (no file paths, no keys, no raw ad copy beyond the stored analysis)."""
    lines = [f"Pilar: {report.get('pillar') or 'todos'}. Anuncios evaluados en el top: {len(report['top'])}.", ""]
    for i, item in enumerate(report["top"], 1):
        lines += [
            f"[{i}] Competidor: {item['competitor']} | jinora_score {item['jinora_score']} (creativo {item['creative_score']}, mercado {item['market_score']}) "
            f"| formato {item['format']} | idioma {item['language']} | voz {item['has_voiceover']} | {item['days_active']} días | {item['variants']} variantes",
            f"    Ángulos (feature_focus): {', '.join(item['feature_focus']) or '-'} | mención de IA (descriptivo): {item['ai_mention'] or '-'}",
            f"    Propuesta de valor: {item['value_proposition'] or '-'}",
            f"    Qué copiar: {item['what_to_model'] or '-'}",
            f"    Qué superar: {item['what_to_beat'] or '-'}",
            "",
        ]
    patterns = report["patterns"]
    lines.append("Patrones calculados con código: "
                 f"ángulos {[(p['tag'], p['count']) for p in patterns['feature_focus']]}; formatos {[(p['tag'], p['count']) for p in patterns['formats']]}; "
                 f"voz {patterns['voice']}; duración {patterns['duration']}")
    return "\n".join(lines)


def _redact(text: str) -> str:
    for secret in (settings.GEMINI_API_KEY, settings.ANTHROPIC_API_KEY):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def generate_executive_summary(report: Dict[str, Any], client: Optional[Any] = None) -> Dict[str, Any]:
    """Returns {"requested": True, "generated": bool, "text": str|None, "model": str|None, "reason": str|None}."""
    result: Dict[str, Any] = {"requested": True, "generated": False, "text": None, "model": None, "reason": None}
    if not report["top"]:
        result["reason"] = "no hay anuncios evaluados en este alcance"
        return result
    try:
        if client is None:
            if not settings.GEMINI_API_KEY:
                result["reason"] = "sin GEMINI_API_KEY configurada"
                return result
            from src.ai.llm_client import GeminiClient

            client = GeminiClient()
        text = client.generate_text(build_prompt(report), system=SYSTEM_PROMPT, max_attempts=1)
    except QuotaExhausted as exc:
        result["reason"] = f"cuota diaria agotada ({exc.requests_used}/{exc.limit}); no se envió la solicitud"
        return result
    except Exception as exc:  # network / API errors must never break the report
        result["reason"] = _redact(f"error del cliente LLM: {type(exc).__name__}: {str(exc)[:160]}")
        return result
    if not text:
        result["reason"] = "el modelo devolvió una respuesta vacía"
        return result
    result.update(generated=True, text=text, model=getattr(client, "model", None))
    return result
