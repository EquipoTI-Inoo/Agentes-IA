"""
Tests for Phase 5 jinora_score, local rescore and evaluator isolation.
Uses a temporary SQLite database; no Gemini, no Claude, no network.
"""

import json
import socket
import sys
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import Settings
from src.scoring.jinora import compute_jinora_score, get_score_weights, rescore_all

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
BRAND = "MarcaCompetidoraZeta"
DAYS = 4321


def _settings_from_env_file(tmp_path, monkeypatch, creative: str, market: str) -> Settings:
    monkeypatch.delenv("SCORE_WEIGHT_CREATIVE", raising=False)
    monkeypatch.delenv("SCORE_WEIGHT_MARKET", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"SCORE_WEIGHT_CREATIVE={creative}\nSCORE_WEIGHT_MARKET={market}\n", encoding="utf-8")
    return Settings(_env_file=str(env))


def test_jinora_null_without_creative_score():
    assert compute_jinora_score(None, 80.0, (0.7, 0.3)) is None
    assert compute_jinora_score(None, 100.0, (0.7, 0.3)) is None


def test_jinora_null_without_market_score():
    assert compute_jinora_score(60, None, (0.7, 0.3)) is None


def test_jinora_formula_default_weights():
    defaults = Settings(_env_file=None)
    assert get_score_weights(defaults) == (0.7, 0.3)
    assert compute_jinora_score(60, 80.0, get_score_weights(defaults)) == pytest.approx(66.0)
    assert compute_jinora_score(83, 41.5, (0.7, 0.3)) == pytest.approx(0.7 * 83 + 0.3 * 41.5, abs=0.01)


def test_jinora_has_no_caps():
    assert compute_jinora_score(100, 100.0, (0.7, 0.3)) == pytest.approx(100.0)
    assert compute_jinora_score(0, 0.0, (0.7, 0.3)) == 0.0


def test_weights_read_from_env_file(tmp_path, monkeypatch):
    cfg = _settings_from_env_file(tmp_path, monkeypatch, "0.6", "0.4")
    weights = get_score_weights(cfg)
    assert weights == (0.6, 0.4)
    assert compute_jinora_score(50, 100.0, weights) == pytest.approx(70.0)


def test_env_weights_not_summing_one_raise(tmp_path, monkeypatch):
    cfg = _settings_from_env_file(tmp_path, monkeypatch, "0.7", "0.5")
    with pytest.raises(ValueError, match="sumar 1.0"):
        get_score_weights(cfg)


@pytest.fixture
def temp_db(tmp_path):
    from src.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'test_phase5.db'}")
    Base.metadata.create_all(bind=engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def no_network_no_ai(monkeypatch):
    """Any socket connection or LLM evaluation during the test fails loudly."""
    def _blocked(*args, **kwargs):
        raise AssertionError("Llamada externa prohibida en rescore")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    from src.ai.claude_client import ClaudeClient
    from src.ai.llm_client import GeminiClient

    monkeypatch.setattr(GeminiClient, "evaluate", _blocked)
    monkeypatch.setattr(ClaudeClient, "evaluate", _blocked)


def test_rescore_persists_scores_locally(temp_db, tmp_path, no_network_no_ai):
    from src.database import AdRecord, AnalysisRecord
    from src.db.models import Ad, Analysis

    body = '"They turned off all eight of my alarms so I\'d miss the SAT."'
    db = temp_db()
    for i in range(8):
        db.add(AdRecord(
            id=f"alarm_{i}", page_name="Unfold Tales", body_text=body, headline="Read more",
            start_date=datetime(2026, 10, 11, 7), days_active=9, is_active=True,
            platforms='["FACEBOOK", "INSTAGRAM"]',
        ))
    db.add(AdRecord(
        id="evaluated_1", page_name="Clientify", body_text="Tu CRM con WhatsApp", headline="Prueba gratis",
        start_date=datetime(2026, 5, 1, 7), days_active=170, is_active=True,
        platforms='["FACEBOOK", "INSTAGRAM", "MESSENGER", "AUDIENCE_NETWORK"]',
    ))
    db.add(AdRecord(
        id="no_analysis", page_name="Kommo", body_text="Otro copy", headline="",
        start_date=datetime(2026, 9, 1, 7), is_active=True, platforms='["FACEBOOK"]',
    ))
    db.flush()
    db.add(AnalysisRecord(ad_id="evaluated_1", model="m", creative_score=50, result_json="{}"))
    db.add(AnalysisRecord(ad_id="evaluated_1", model="m", creative_score=60, result_json="{}"))  # latest
    # A forensic transcript row has no creative_score and must never feed jinora_score
    db.add(AnalysisRecord(ad_id="no_analysis", model="faster-whisper", analysis_type="forensics",
                          creative_score=None, result_json='{"full_transcript": "hola"}'))
    db.commit()
    db.close()

    summary = rescore_all(session_factory=temp_db, now=NOW, downloads_dir=tmp_path)
    assert summary["ads"] == 10
    assert summary["groups"] == 3
    assert summary["jinora_scored"] == 1
    assert summary["jinora_null"] == 9

    db = temp_db()
    try:
        alarms = db.query(Ad).filter(Ad.id.like("alarm_%")).all()
        assert len({a.creative_group_id for a in alarms}) == 1
        assert {a.variant_count for a in alarms} == {8}
        assert all(a.market_signal.jinora_score is None and a.market_signal.market_score is not None for a in alarms)

        ev = db.query(Ad).filter(Ad.id == "evaluated_1").one()
        comps = json.loads(ev.market_signal.market_components)
        assert set(comps["missing"]) == {"countries"}
        assert ev.market_signal.jinora_score == pytest.approx(0.7 * 60 + 0.3 * ev.market_signal.market_score, abs=0.01)

        no_analysis = db.query(Ad).filter(Ad.id == "no_analysis").one()
        assert no_analysis.market_signal.jinora_score is None
        assert no_analysis.creative_hash and no_analysis.copy_hash
        assert db.query(Analysis).count() == 3  # rescore never creates or removes analyses
    finally:
        db.close()

    # Rescoring is idempotent
    assert rescore_all(session_factory=temp_db, now=NOW, downloads_dir=tmp_path) == summary


def _forbidden_terms():
    return [BRAND, str(DAYS), "days_active", "variant", "días activo"]


def test_gemini_prompt_excludes_days_variants_and_brand(tmp_path, monkeypatch):
    """The evaluator only receives media + copy: never days_active, variants or brand name."""
    from src.ai.llm_client import GeminiClient
    from src.ai.schemas import AdCopy, MediaBundle
    from src.evaluator import evaluate_video_ad

    assert set(AdCopy.model_fields) == {"headline", "body_text", "cta_text", "link_url"}
    for field in ("page_name", "days_active", "variant_count", "market_score", "brand"):
        assert field not in MediaBundle.model_fields

    video = tmp_path / "video_0.mp4"
    video.write_bytes(b"x" * 100)
    captured = {}

    # 1. evaluate_video_ad builds bundles from an ad dict that carries the forbidden data
    class Capture(Exception):
        pass

    capture_client = MagicMock()

    def _capture_evaluate(media, copy):
        captured["bundles"] = media.model_dump_json() + copy.model_dump_json()
        raise Capture()

    capture_client.evaluate.side_effect = _capture_evaluate
    monkeypatch.setattr("src.evaluator.get_video_info", lambda p: {"duration_s": 10.0, "has_audio_track": True})
    monkeypatch.setattr("src.evaluator.SessionLocal", MagicMock())
    with pytest.raises(Capture):
        evaluate_video_ad(
            {"ad_id": "a1", "video_path": str(video), "page_name": BRAND, "days_active": DAYS,
             "variant_count": 8, "market_score": 77.7, "headline": "Vende más", "body_text": "Automatiza chats"},
            client=capture_client, force=True,
        )

    # 2. GeminiClient composes the actual prompt sent to the API
    client = GeminiClient(api_key="mock_key", quota_guard=MagicMock())
    client._client = MagicMock()
    client._client.files.upload.return_value = types.SimpleNamespace(
        name="files/x", state=types.SimpleNamespace(name="ACTIVE")
    )

    def _capture_call(cnts, config=None):
        captured["gemini"] = str(cnts[1]) + str(config.system_instruction)
        raise Capture()

    monkeypatch.setattr(client, "_call_with_transient_retry", _capture_call)
    with pytest.raises(Capture):
        client.evaluate(
            MediaBundle(media_path=video, duration_s=10.0),
            AdCopy(headline="Vende más", body_text="Automatiza chats"),
        )

    for text in (captured["bundles"], captured["gemini"]):
        for term in _forbidden_terms():
            assert term not in text


def test_claude_prompt_excludes_days_variants_and_brand(tmp_path, monkeypatch):
    from PIL import Image

    from src.ai.claude_client import ClaudeClient
    from src.ai.schemas import AdCopy, MediaBundle

    img = tmp_path / "image_0.jpg"
    Image.new("RGB", (50, 50), "white").save(img)
    captured = {}

    class Capture(Exception):
        pass

    def _create(**kwargs):
        captured["claude"] = json.dumps(kwargs, ensure_ascii=False, default=str)
        raise Capture()

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = lambda api_key=None: types.SimpleNamespace(
        messages=types.SimpleNamespace(create=_create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    guard = MagicMock()
    guard.get_total_spend.return_value = 0.0
    client = ClaudeClient(api_key="mock_key", quota_guard=guard)
    with pytest.raises(Capture):
        client.evaluate(
            MediaBundle(media_path=img, media_type="image"),
            AdCopy(headline="Agenda citas", body_text="Automatiza tu agenda"),
        )

    text = captured["claude"]
    assert "Agenda citas" in text  # the copy does reach the model
    for term in _forbidden_terms():
        assert term not in text
