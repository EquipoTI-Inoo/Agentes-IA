"""
Pydantic Schemas for AI Ad Evaluation
Strictly enforces typing, evidence requirements, and AI neutrality fields
for both Video (Gemini) and Static Ads (Claude).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field

FeatureFocusType = Literal[
    "automatizacion",
    "agendamiento",
    "gestion_leads_crm",
    "mensajes_masivos_campanas",
    "atencion_omnicanal",
    "agentes_ia",
    "pagos_ventas_en_chat",
    "integraciones_api",
    "otro",
]

AIMentionType = Literal["ninguna", "mencionada", "central"]
AnchorLevelType = Literal["0-6", "7-12", "13-19", "20-25"]


# =====================================================================
# Video Evaluation Schemas (Gemini)
# =====================================================================

class Evidence(BaseModel):
    timestamp: Optional[str] = Field(
        None, description="Intervalo temporal ej '00:02-00:05' o None si proviene del copy"
    )
    source: Literal["audio", "visual", "copy", "cta"] = Field(
        ..., description="Origen de la evidencia observada"
    )
    quote: str = Field(..., description="Cita textual o descripción visual observable y concreta")


class DimensionScore(BaseModel):
    score: int = Field(..., ge=0, le=25, description="Puntaje de 0 a 25 según rúbrica")
    anchor_level: AnchorLevelType = Field(..., description="Nivel del ancla que justifica el puntaje")
    reasoning: str = Field(..., description="Justificación específica a este anuncio (2 a 4 frases)")
    evidence: List[Evidence] = Field(..., min_length=1, description="Al menos una evidencia verificable")


class TranscriptSegment(BaseModel):
    start_s: float = Field(..., description="Segundo de inicio aproximado (±2s)")
    end_s: float = Field(..., description="Segundo de fin aproximado (±2s)")
    text: str = Field(..., description="Texto literal pronunciado en este segmento")


class EvaluationResult(BaseModel):
    transcript: List[TranscriptSegment] = Field(
        default_factory=list, description="Transcripción secuencial. Lista vacía si no hay voz/diálogo"
    )
    has_voiceover: bool = Field(
        ..., description="True si existe locución/voz hablada humana o sintética. False si solo es música o efectos"
    )
    scene_cuts_estimate: Optional[int] = Field(None, description="Estimación de cambios de plano o cortes")
    speech_pace_wpm_estimate: Optional[int] = Field(None, description="Velocidad de habla aproximada en palabras por minuto")
    burned_in_subtitles: Optional[bool] = Field(None, description="True si el video tiene subtítulos incrustados en pantalla")
    hook: DimensionScore = Field(..., description="Dimensión 1: Captación (0–3 s)")
    mechanism: DimensionScore = Field(..., description="Dimensión 2: Mecanismo y prueba")
    narrative: DimensionScore = Field(..., description="Dimensión 3: Narrativa y ritmo")
    offer: DimensionScore = Field(..., description="Dimensión 4: Oferta y llamada a la acción")
    hook_description: str = Field(..., description="Qué ocurre exactamente en los primeros 3 segundos (audio y visual)")
    value_proposition: str = Field(..., description="Qué problema resuelven y qué prometen en una sola frase (sin asumir IA)")
    feature_focus: List[FeatureFocusType] = Field(
        ..., min_length=1, max_length=3, description="1 a 3 capacidades comerciales que destaca el anuncio"
    )
    ai_mention: AIMentionType = Field(
        ..., description="Dato factual: si la IA se menciona o es central. No suma ni resta puntos"
    )
    format: Literal["ugc", "demo_pantalla", "talking_head", "animacion", "testimonial", "mixto", "otro"] = Field(
        ..., description="Formato de producción visual del video"
    )
    language: str = Field(default="es", description="Idioma detectado del anuncio (es, en, pt, etc.)")
    target_niche: Optional[str] = Field(None, description="Nicho o industria objetivo detectada si es específica")
    what_to_model: str = Field(..., description="Principio o técnica de alto impacto a modelar para Cosmiq")
    what_to_beat: str = Field(..., description="Fricción o debilidad en el anuncio que Cosmiq puede superar")
    cosmiq_adaptation_idea: str = Field(
        ..., description="Idea concreta de anuncio adaptada a Cosmiq basada en la capacidad que destaca este anuncio"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Nivel de certidumbre general de la evaluación")


# =====================================================================
# Static Evaluation Schemas (Claude)
# =====================================================================

class StaticEvidence(BaseModel):
    card: Optional[int] = Field(None, description="Número de tarjeta en carrusel (1-indexado) o None")
    zone: Literal["superior", "centro", "inferior", "completa", "copy", "titular", "cta"] = Field(...)
    quote: str = Field(..., description="Texto leído en la imagen o descripción visual concreta")


class StaticDimensionScore(BaseModel):
    score: int = Field(..., ge=0, le=25, description="Puntaje de 0 a 25 según rúbrica estática")
    anchor_level: AnchorLevelType = Field(..., description="Nivel del ancla que justifica el puntaje")
    reasoning: str = Field(..., description="Justificación específica a este creativo")
    evidence: List[StaticEvidence] = Field(..., min_length=1, description="Al menos una evidencia verificable")


class StaticEvaluationResult(BaseModel):
    stop_power: StaticDimensionScore = Field(..., description="Dimensión 1: Poder de detención visual (Stop power)")
    message_clarity: StaticDimensionScore = Field(..., description="Dimensión 2: Claridad del mensaje y propuesta")
    proof: StaticDimensionScore = Field(..., description="Dimensión 3: Prueba visible o demostración")
    offer_cta: StaticDimensionScore = Field(..., description="Dimensión 4: Oferta, beneficio y llamada a la acción")
    text_in_image: str = Field(..., description="Transcripción literal OCR de todo el texto visible en la imagen")
    text_density: Literal["sin_texto", "baja", "media", "alta"] = Field(...)
    visual_style: Literal[
        "captura_producto",
        "chat_whatsapp",
        "persona_rostro",
        "meme",
        "infografia",
        "testimonial",
        "mockup_dispositivo",
        "ilustracion",
        "foto_stock",
        "otro",
    ] = Field(...)
    has_human_face: bool = Field(...)
    has_product_screenshot: bool = Field(...)
    main_promise: str = Field(..., description="Promesa principal expresada en una frase")
    value_proposition: str = Field(..., description="Qué problema resuelven y qué ofrecen (sin asumir IA)")
    feature_focus: List[FeatureFocusType] = Field(..., min_length=1, max_length=3)
    ai_mention: AIMentionType = Field(...)
    target_niche: Optional[str] = Field(None)
    language: str = Field(default="es")
    carousel_narrative: Optional[str] = Field(None, description="Análisis de progresión narrativa entre tarjetas si es carrusel")
    weakest_card: Optional[int] = Field(None, description="Índice de la tarjeta más débil en carrusel")
    what_to_model: str = Field(...)
    what_to_beat: str = Field(...)
    cosmiq_adaptation_idea: str = Field(...)
    confidence: float = Field(..., ge=0.0, le=1.0)


# =====================================================================
# Common Input Bundles for Evaluation Pipeline
# =====================================================================

class MediaBundle(BaseModel):
    media_path: Union[str, Path]
    media_type: Literal["video", "image", "carousel", "dco"] = "video"
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    has_audio_track: bool = True
    carousel_paths: List[Union[str, Path]] = Field(default_factory=list)


class AdCopy(BaseModel):
    headline: str = ""
    body_text: str = ""
    cta_text: str = ""
    link_url: str = ""
