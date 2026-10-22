"""
Unit tests for ClaudeClient and Static Evaluator (Anthropic Claude).
All tests run with mocks; zero real calls to Claude API.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.ai.claude_client import BudgetExceededError, ClaudeClient, MissingApiKeyError
from src.ai.schemas import AdCopy, MediaBundle, StaticDimensionScore, StaticEvaluationResult, StaticEvidence
from src.evaluator_static import evaluate_static_ad


@pytest.fixture
def dummy_image(tmp_path):
    img_path = tmp_path / "test_image.jpg"
    from PIL import Image
    im = Image.new("RGB", (800, 600), color="blue")
    im.save(img_path)
    return img_path


def test_claude_client_raises_missing_api_key(dummy_image):
    client = ClaudeClient(api_key="")
    media = MediaBundle(media_path=dummy_image, media_type="image")
    copy = AdCopy(headline="Test", body_text="Test body")

    with pytest.raises(MissingApiKeyError):
        client.evaluate(media, copy)


def test_claude_client_enforces_budget_limit(dummy_image):
    mock_quota = MagicMock()
    # Simulate already spent $8.50 > $8.00 budget limit
    mock_quota.get_total_spend.return_value = 8.50

    client = ClaudeClient(api_key="sk-ant-test", budget_limit=8.0, quota_guard=mock_quota)
    media = MediaBundle(media_path=dummy_image, media_type="image")
    copy = AdCopy(headline="Test", body_text="Test body")

    with pytest.raises(BudgetExceededError):
        client.evaluate(media, copy)


def test_evaluate_static_ad_gracefully_defers_to_pending_evaluation(dummy_image):
    # Empty API key
    client = ClaudeClient(api_key="")
    ad_dict = {
        "ad_id": "static_123",
        "image_path": str(dummy_image),
        "headline": "Oferta CRM",
    }

    result = evaluate_static_ad(ad_dict, client=client, force=True)
    assert result["status"] == "pending_evaluation"
    assert "Sin clave ANTHROPIC_API_KEY" in result["notice"]
