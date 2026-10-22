"""
Unit tests for AI Evaluation Pydantic Schemas (Video and Static).
"""

import pytest
from pydantic import ValidationError

from src.ai.schemas import (
    DimensionScore,
    EvaluationResult,
    Evidence,
    StaticDimensionScore,
    StaticEvaluationResult,
    StaticEvidence,
    TranscriptSegment,
)


def test_evidence_validation():
    ev = Evidence(timestamp="00:01-00:03", source="audio", quote="Hola mundo")
    assert ev.source == "audio"

    # Invalid source
    with pytest.raises(ValidationError):
        Evidence(source="invalid_source", quote="test")


def test_dimension_score_limits_and_evidence():
    ev = Evidence(timestamp="00:01", source="visual", quote="logo")

    # Valid score
    ds = DimensionScore(score=22, anchor_level="20-25", reasoning="Excelente gancho", evidence=[ev])
    assert ds.score == 22

    # Score > 25
    with pytest.raises(ValidationError):
        DimensionScore(score=26, anchor_level="20-25", reasoning="test", evidence=[ev])

    # Score < 0
    with pytest.raises(ValidationError):
        DimensionScore(score=-1, anchor_level="0-6", reasoning="test", evidence=[ev])

    # Empty evidence list (must have at least 1)
    with pytest.raises(ValidationError):
        DimensionScore(score=10, anchor_level="7-12", reasoning="test", evidence=[])


def test_evaluation_result_ai_neutrality_fields():
    ev = Evidence(timestamp="00:01", source="audio", quote="Texto")
    ds = DimensionScore(score=15, anchor_level="13-19", reasoning="Bueno", evidence=[ev])

    # Valid EvaluationResult
    res = EvaluationResult(
        transcript=[TranscriptSegment(start_s=0.0, end_s=2.5, text="Bienvenido a la demo")],
        has_voiceover=True,
        hook=ds,
        mechanism=ds,
        narrative=ds,
        offer=ds,
        hook_description="Pantalla con interfaz",
        value_proposition="Automatiza tus mensajes masivos",
        feature_focus=["mensajes_masivos_campanas", "gestion_leads_crm"],
        ai_mention="ninguna",
        format="demo_pantalla",
        language="es",
        what_to_model="Demostración ágil",
        what_to_beat="CTA poco visible",
        cosmiq_adaptation_idea="Campaña enfocada en WhatsApp",
        confidence=0.9,
    )
    assert res.ai_mention == "ninguna"
    assert len(res.feature_focus) == 2
    assert res.has_voiceover is True


def test_static_evaluation_result_validation():
    ev = StaticEvidence(zone="centro", quote="Prueba gratis 14 días")
    ds = StaticDimensionScore(score=20, anchor_level="20-25", reasoning="Claro", evidence=[ev])

    res = StaticEvaluationResult(
        stop_power=ds,
        message_clarity=ds,
        proof=ds,
        offer_cta=ds,
        text_in_image="Prueba gratis 14 días",
        text_density="baja",
        visual_style="captura_producto",
        has_human_face=False,
        has_product_screenshot=True,
        main_promise="Tu CRM en WhatsApp",
        value_proposition="Conecta tus chats y vende más",
        feature_focus=["gestion_leads_crm"],
        ai_mention="mencionada",
        language="es",
        what_to_model="Tipografía grande",
        what_to_beat="Añadir botón",
        cosmiq_adaptation_idea="Adaptar a agendamiento",
        confidence=0.95,
    )
    assert res.ai_mention == "mencionada"
    assert res.has_product_screenshot is True
