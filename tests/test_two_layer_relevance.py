"""
Tests for Two-Layer Relevance System (src/collect/relevance.py) and scraper integration.
- Layer 1: Direct pass for mapped page_id, fast negative rule rejection
- Layer 2: LLM batching with Pydantic validation
- QuotaExhausted: fallback to pending_relevance (never silently dropped)
- Candidate Competitors: threshold tracking (>= 3 relevant ads)
- Scraper: verifies SEARCH_PAGE vs SEARCH_KEYWORD dispatch
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.ai.quota import QuotaExhausted
from src.collect.relevance import (
    RelevanceBatchResult,
    RelevanceResult,
    TwoLayerRelevanceFilter,
)
from src.scraper import MetaAdsHunter


@pytest.fixture
def temp_filter(tmp_path):
    """Fixture creating isolated competitors DB and candidate JSON."""
    comp_file = tmp_path / "competitors.json"
    comp_file.write_text(
        json.dumps([
            {
                "name": "Kommo",
                "pillar": "crm",
                "page_ids": ["103444988081696"],
                "website": "https://kommo.com",
            },
            {
                "name": "ManyChat",
                "pillar": "conversacional",
                "page_ids": ["521778124648972"],
                "website": "https://manychat.com",
            },
        ]),
        encoding="utf-8",
    )
    cand_file = tmp_path / "candidates.json"
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("Dummy prompt", encoding="utf-8")

    filt = TwoLayerRelevanceFilter(
        competitors_path=comp_file,
        candidates_path=cand_file,
        prompt_path=prompt_file,
    )
    return filt, cand_file


def test_layer1_direct_approval_for_mapped_page_id(temp_filter):
    filt, _ = temp_filter
    ad = {
        "ad_id": "ad_123",
        "page_id": "103444988081696",
        "page_name": "Kommo Official",
        "creatives": [{"body": "Get the best CRM for WhatsApp", "title": "Kommo CRM"}],
    }
    result, needs_l2 = filt.classify_layer1(ad)

    assert needs_l2 is False
    assert result is not None
    assert result.is_relevant is True
    assert result.pillar == "crm"
    assert result.confidence == 1.0
    assert "Página oficial mapeada" in result.reason


def test_layer1_negative_rule_rejection(temp_filter):
    filt, _ = temp_filter
    # Negative match: Air Wick, Rom-Com, shoes, etc.
    bad_ads = [
        {"ad_id": "b1", "page_name": "Air Wick Freshness", "page_id": "999", "creatives": [{"body": "Fresh scent for your home"}]},
        {"ad_id": "b2", "page_name": "Rom-Com Reads", "page_id": "998", "creatives": [{"body": "Read romance novels free"}]},
        {"ad_id": "b3", "page_name": "Airmoto Pump", "page_id": "997", "creatives": [{"body": "Compact tire inflator"}]},
        {"ad_id": "b4", "page_name": "Chair Co", "page_id": "996", "creatives": [{"body": "Ergonomic desk chairs"}]},
        {"ad_id": "b5", "page_name": "Tienda Moda", "page_id": "995", "creatives": [{"body": "Zapatos y vestidos de fiesta"}]},
    ]

    for ad in bad_ads:
        result, needs_l2 = filt.classify_layer1(ad)
        assert needs_l2 is False, f"Expected {ad['page_name']} to be rejected in Layer 1"
        assert result is not None
        assert result.is_relevant is False
        assert result.pillar == "ninguno"
        assert "Descartado por coincidencia negativa en Capa 1" in result.reason


def test_layer2_llm_classification_mock(temp_filter):
    filt, _ = temp_filter
    discovery_ad = {
        "ad_id": "disc_01",
        "page_id": "777888",
        "page_name": "BotSmart Automation",
        "creatives": [{"body": "Chatbot con IA para automatizar ventas en WhatsApp", "title": "BotSmart IA"}],
    }

    # Verify Layer 1 sends to Layer 2
    res1, needs_l2 = filt.classify_layer1(discovery_ad)
    assert needs_l2 is True
    assert res1 is None

    # Simulate Layer 2 LLM response
    mock_batch = RelevanceBatchResult(
        results=[
            RelevanceResult(
                ad_id="disc_01",
                is_relevant=True,
                pillar="conversacional",
                is_competitor_of_cosmiq=True,
                confidence=0.92,
                reason="Software de automatización de WhatsApp",
            )
        ]
    )

    with patch.object(filt.quota_guard, "execute_with_retry", return_value=mock_batch):
        results = filt.classify_layer2_batch([discovery_ad])

    assert len(results) == 1
    assert results[0].is_relevant is True
    assert results[0].pillar == "conversacional"
    assert results[0].confidence == 0.92


def test_layer2_quota_exhausted_fallback_to_pending(temp_filter):
    filt, _ = temp_filter
    discovery_ads = [
        {"ad_id": "q1", "page_id": "111", "page_name": "CRM X", "creatives": [{"body": "CRM cloud"}]},
        {"ad_id": "q2", "page_id": "222", "page_name": "VoiceBot Y", "creatives": [{"body": "AI Phone"}]},
    ]

    # Simulate QuotaExhausted from QuotaGuard
    with patch.object(
        filt.quota_guard,
        "execute_with_retry",
        side_effect=QuotaExhausted(
            model="gemini-2.5-flash-lite",
            day_pt="2026-10-21",
            requests_used=20,
            limit=20,
            reason="Daily limit reached",
        ),
    ):
        results = filt.classify_layer2_batch(discovery_ads)

    assert len(results) == 2
    for r in results:
        assert r.pending_relevance is True
        assert "pending_relevance: QuotaExhausted" in r.reason
        # Must not be discarded as false permanently, marked pending
        assert r.confidence == 0.0


def test_candidate_competitors_threshold(temp_filter):
    filt, cand_file = temp_filter

    page_id = "candidate_page_999"
    page_name = "NewAI Startup"

    # Feed 1st and 2nd relevant ad
    filt.record_candidate_page(page_id, page_name, "ad_1", "conversacional")
    filt.record_candidate_page(page_id, page_name, "ad_2", "conversacional")

    with open(cand_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1
    assert data[0]["relevant_ads_count"] == 2
    assert data[0]["meets_threshold"] is False

    # Feed 3rd ad -> meets threshold (>= 3)
    filt.record_candidate_page(page_id, page_name, "ad_3", "conversacional")
    with open(cand_file, "r", encoding="utf-8") as f:
        data_after = json.load(f)
    assert data_after[0]["relevant_ads_count"] == 3
    assert data_after[0]["meets_threshold"] is True


def test_scraper_search_page_vs_keyword_dispatch():
    mock_collector = MagicMock()
    mock_collector.collect.return_value = []

    hunter = MetaAdsHunter(output_dir="downloads")
    hunter.collector = mock_collector

    # Case 1: Competitor with page_ids -> uses SEARCH_PAGE
    hunter.search_ads(
        query="HubSpot",
        country="PE",
        competitor={"name": "HubSpot", "page_ids": ["192993730741131"]},
    )
    mock_collector.collect.assert_called_with(
        page_ids=["192993730741131"],
        search_type="PAGE",
        country="PE",
        status="ACTIVE",
        max_results=10,
        filter_config=mock_collector.collect.call_args[1]["filter_config"],
    )

    # Case 2: Keyword discovery without page_ids -> uses KEYWORD_UNORDERED
    mock_collector.reset_mock()
    hunter.search_ads(
        query="agente de voz ia",
        country="MX",
        competitor=None,
    )
    mock_collector.collect.assert_called_with(
        query="agente de voz ia",
        search_type="KEYWORD_UNORDERED",
        country="MX",
        status="ACTIVE",
        max_results=10,
        filter_config=mock_collector.collect.call_args[1]["filter_config"],
    )
