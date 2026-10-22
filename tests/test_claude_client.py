"""
Unit tests for ClaudeClient and Static Evaluator (Anthropic Claude).
All tests run with mocks; zero real calls to Claude API.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import sys
import types

from src.ai.claude_client import BudgetExceededError, ClaudeClient, MissingApiKeyError, WorkspaceRequiredError
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


# --- A3: key not scoped to a workspace -------------------------------------------------------

FAKE_KEY = "sk-ant-api03-FAKE-SECRET-VALUE"
WORKSPACE_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': "
    "'This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header "
    f"with the ID of the workspace to use. (key {FAKE_KEY})'}}}}"
)


class _FakeBadRequest(Exception):
    """Stands in for anthropic.BadRequestError: carries status_code like the real one."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture
def fake_anthropic(monkeypatch):
    """A fake 'anthropic' module whose messages.create raises the configured error and records kwargs."""
    state = {"error": _FakeBadRequest(WORKSPACE_400), "kwargs": None}

    def create(**kwargs):
        state["kwargs"] = kwargs
        raise state["error"]

    module = types.ModuleType("anthropic")
    module.Anthropic = lambda api_key=None: types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return state


def _client_with_key():
    quota = MagicMock()
    quota.get_total_spend.return_value = 0.0
    return ClaudeClient(api_key=FAKE_KEY, quota_guard=quota)


def test_workspace_400_gives_a_clear_actionable_error_without_the_key(dummy_image, fake_anthropic):
    media = MediaBundle(media_path=dummy_image, media_type="image")
    with pytest.raises(WorkspaceRequiredError) as exc:
        _client_with_key().evaluate(media, AdCopy(headline="Test"))
    message = str(exc.value)
    assert "ANTHROPIC_WORKSPACE_ID" in message and "workspace" in message.lower()
    assert FAKE_KEY not in message and FAKE_KEY not in repr(exc.value)
    assert exc.value.__cause__ is None  # the original text (which could echo the key) is not chained


def test_workspace_id_is_sent_when_configured(dummy_image, fake_anthropic, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    media = MediaBundle(media_path=dummy_image, media_type="image")
    with pytest.raises(WorkspaceRequiredError):
        _client_with_key().evaluate(media, AdCopy(headline="Test"))
    assert fake_anthropic["kwargs"]["workspace_id"] == "wrkspc_123"

    monkeypatch.setattr(settings, "ANTHROPIC_WORKSPACE_ID", None)
    with pytest.raises(WorkspaceRequiredError):
        _client_with_key().evaluate(media, AdCopy(headline="Test"))
    assert "workspace_id" not in fake_anthropic["kwargs"]


def test_other_400_errors_are_not_mistaken_for_the_workspace_problem(dummy_image, fake_anthropic):
    fake_anthropic["error"] = _FakeBadRequest("Error code: 400 - prompt is too long", 400)
    media = MediaBundle(media_path=dummy_image, media_type="image")
    with pytest.raises(_FakeBadRequest):
        _client_with_key().evaluate(media, AdCopy(headline="Test"))


def test_static_evaluator_defers_instead_of_failing_on_workspace_error(dummy_image, fake_anthropic):
    result = evaluate_static_ad(
        {"ad_id": "static_ws_1", "image_path": str(dummy_image), "headline": "Oferta CRM"},
        client=_client_with_key(), force=True,
    )
    assert result["status"] == "pending_evaluation"
    assert "ANTHROPIC_WORKSPACE_ID" in result["notice"] and FAKE_KEY not in result["notice"]
