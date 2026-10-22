"""
Unit tests for AI Evaluation Pydantic Schemas (Video and Static).
"""

import json

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


def test_low_score_requires_absence_and_channel_citation():
    # Score < 8 with missing absence / channel justification should fail
    ev_generic = Evidence(timestamp="00:01-00:05", source="visual", quote="Solo un fondo azul")
    with pytest.raises(ValidationError) as exc:
        DimensionScore(
            score=5,
            anchor_level="0-6",
            reasoning="Poco atractivo y muy aburrido.",
            evidence=[ev_generic]
        )
    assert "Puntaje bajo (< 8) requiere justificar explícitamente" in str(exc.value)

    # Score < 8 with proper citation of searched element not found, channel, and time range passes
    ev_proper = Evidence(
        timestamp="00:00-00:05",
        source="audio",
        quote="Se buscó locución o gancho de voz en los primeros 5s y no se observó ningún audio ni titular en pantalla."
    )
    ds_valid = DimensionScore(
        score=4,
        anchor_level="0-6",
        reasoning="En el intervalo de 0 a 5 segundos se buscó un gancho verbal o visual en pantalla y no se encontró ninguna promesa ni problema presentado.",
        evidence=[ev_proper]
    )
    assert ds_valid.score == 4


def test_static_low_score_validation():
    # Score < 8 without absence justification fails
    ev_bad = StaticEvidence(zone="centro", quote="Fondo plano")
    with pytest.raises(ValidationError):
        StaticDimensionScore(
            score=5,
            anchor_level="0-6",
            reasoning="Diseño simple.",
            evidence=[ev_bad]
        )

    # Score < 8 with absence justification passes
    ev_ok = StaticEvidence(zone="inferior", quote="No se observa ningún botón o enlace de CTA visible")
    ds_ok = StaticDimensionScore(
        score=4,
        anchor_level="0-6",
        reasoning="En la zona inferior se buscó llamada a la acción y carece de CTA claro.",
        evidence=[ev_ok]
    )
    assert ds_ok.score == 4


# --- invalid JSON is rejected (what a model reply that breaks the contract looks like) ---------

@pytest.mark.parametrize("raw", [
    "esto no es JSON",
    "",
    "{",
    "[]",
    "null",
    '{"hook": {"score": 10}}',  # valid JSON, required fields missing
])
def test_invalid_json_is_rejected_by_both_schemas(raw):
    from src.ai.schemas import EvaluationResult, StaticEvaluationResult

    with pytest.raises(ValidationError):
        EvaluationResult.model_validate_json(raw)
    with pytest.raises(ValidationError):
        StaticEvaluationResult.model_validate_json(raw)


def test_valid_structure_with_a_forbidden_value_is_rejected():
    """Right shape, wrong values: score above 25 and an unknown anchor level must not pass."""
    ev = {"timestamp": "00:01", "source": "visual", "quote": "demo"}
    for bad in ({"score": 26, "anchor_level": "20-25"}, {"score": 10, "anchor_level": "50-99"}):
        with pytest.raises(ValidationError):
            DimensionScore.model_validate_json(json.dumps({**bad, "reasoning": "r", "evidence": [ev]}))
