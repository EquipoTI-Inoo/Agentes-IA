"""
Tests for PageResolver (src/collect/page_resolver.py) with mocks.
Verifies resolving official page IDs for competitors without calling Meta live API.
"""

from unittest.mock import MagicMock, patch
import pytest

from src.collect.page_resolver import PageResolver, extract_base_domain
from src.collect.ratelimit import MetaRateLimiter


def test_extract_base_domain():
    assert extract_base_domain("https://www.kommo.com/pricing/") == "kommo.com"
    assert extract_base_domain("http://subdomain.leadsales.io/chat") == "leadsales.io"
    assert extract_base_domain("invalid-url") == "invalid-url"


def test_page_resolver_exact_match_with_domain_verification():
    mock_collector = MagicMock()
    # Mock client.search_pages returning candidate pages
    mock_collector.client.search_pages.return_value = [
        {
            "page_id": "105860241124620",
            "page_name": "Leadsales",
            "category": "Software Company",
            "page_verified": "VERIFIED",
        },
        {
            "page_id": "999999999999999",
            "page_name": "Lead Sales Auto Store",
            "category": "Auto Dealership",
            "page_verified": "NOT_VERIFIED",
        },
    ]

    # Mock collector.search returning an ad with matching domain
    mock_ad = MagicMock()
    mock_creative = MagicMock()
    mock_creative.link_url = "https://leadsales.io/demo"
    mock_ad.creatives = [mock_creative]
    mock_collector.search.return_value = [mock_ad]

    resolver = PageResolver(collector=mock_collector, limiter=MetaRateLimiter(sleep=lambda s: None))
    competitor = {
        "name": "Leadsales",
        "website": "https://leadsales.io",
        "markets": ["MX"],
    }

    result = resolver.resolve_competitor(competitor, country="MX", verify_ads_domain=True)

    assert result["page_ids"] == ["105860241124620"]
    assert result["resolution_confidence"] == "alta"
    assert result["needs_review"] is False


def test_page_resolver_ambiguous_candidate_flags_needs_review():
    mock_collector = MagicMock()
    mock_collector.client.search_pages.return_value = [
        {
            "page_id": "111222333",
            "page_name": "Random Other Service",
            "category": "Local Business",
            "page_verified": "NOT_VERIFIED",
        }
    ]

    resolver = PageResolver(collector=mock_collector, limiter=MetaRateLimiter(sleep=lambda s: None))
    competitor = {
        "name": "B2Chat",
        "website": "https://b2chat.io",
        "markets": ["CO"],
    }

    result = resolver.resolve_competitor(competitor, country="CO", verify_ads_domain=False)

    assert result["page_ids"] == []
    assert result["resolution_confidence"] == "baja"
    assert result["needs_review"] is True


def test_page_resolver_no_candidates_found():
    mock_collector = MagicMock()
    mock_collector.client.search_pages.return_value = []

    resolver = PageResolver(collector=mock_collector, limiter=MetaRateLimiter(sleep=lambda s: None))
    competitor = {
        "name": "NonExistentCRM12345",
        "website": "https://nonexistent.io",
        "markets": ["PE"],
    }

    result = resolver.resolve_competitor(competitor, country="PE", verify_ads_domain=False)

    assert result["page_ids"] == []
    assert result["resolution_confidence"] == "baja"
    assert result["needs_review"] is True
