"""
Tests for Phase 3: Media Fingerprinting, Format Detection, LLM Routing, Token Estimation,
Context/TPM Limits, and Inactive Ad Handling.
"""

from pathlib import Path
from PIL import Image
import pytest

from src.ai.quota import should_evaluate_ad, sort_ads_for_evaluation
from src.media.fingerprint import (
    compute_creative_hash,
    compute_file_sha256,
    compute_image_phash,
    detect_ad_format,
    route_ad_for_evaluation,
)
from src.media.media_info import check_video_context_limit, estimate_video_tokens


def test_compute_file_sha256(tmp_path):
    f = tmp_path / "test.mp4"
    f.write_bytes(b"dummy video data for hash test")
    h1 = compute_file_sha256(f)
    h2 = compute_file_sha256(f)
    assert len(h1) == 64
    assert h1 == h2


def test_compute_image_phash(tmp_path):
    img_path = tmp_path / "test_thumb.jpg"
    img = Image.new("RGB", (100, 100), color="blue")
    img.save(img_path)

    phash = compute_image_phash(img_path)
    assert phash is not None
    assert isinstance(phash, str)


def test_creative_hash_variant_grouping():
    # Same visual hash + normalized copy should yield identical creative_hash
    ch1 = compute_creative_hash("d8d8d8d8d8d8d8d8", copy_text="Mejor CRM para WhatsApp", title="Prueba Gratis")
    ch2 = compute_creative_hash("d8d8d8d8d8d8d8d8", copy_text="mejor  crm  para whatsapp", title="Prueba Gratis")
    assert ch1 == ch2

    # Different copy yields different hash
    ch3 = compute_creative_hash("d8d8d8d8d8d8d8d8", copy_text="Completamente distinto", title="Otro Titulo")
    assert ch1 != ch3


# --- Task 5: 5 Tests for detect_ad_format covering carousel vs. DCO cases ---

def test_detect_ad_format_case1_standard_carousel():
    """Case 1: Standard multi-card image carousel (3 distinct image cards with steps/features)."""
    ad_carousel = {
        "creatives": [
            {"has_video": False, "has_image": True, "title": "Paso 1: Conecta WhatsApp", "body": "En 2 minutos"},
            {"has_video": False, "has_image": True, "title": "Paso 2: Automatiza Respuestas", "body": "Con IA"},
            {"has_video": False, "has_image": True, "title": "Paso 3: Cierra Ventas", "body": "Sin esfuerzo"},
        ]
    }
    assert detect_ad_format(ad_carousel) == "carrusel"


def test_detect_ad_format_case2_carousel_multiple_links():
    """Case 2: E-commerce or catalog carousel with 4 image cards and individual destination URLs."""
    ad_catalog = {
        "creatives": [
            {"has_image": True, "has_video": False, "image_url": "https://img1.jpg", "link_url": "https://site.com/p1"},
            {"has_image": True, "has_video": False, "image_url": "https://img2.jpg", "link_url": "https://site.com/p2"},
            {"has_image": True, "has_video": False, "image_url": "https://img3.jpg", "link_url": "https://site.com/p3"},
            {"has_image": True, "has_video": False, "image_url": "https://img4.jpg", "link_url": "https://site.com/p4"},
        ]
    }
    assert detect_ad_format(ad_catalog) == "carrusel"


def test_detect_ad_format_case3_dco_mixed_video_and_images():
    """Case 3: Dynamic Creative Optimization (DCO) mixing 1 video variant with multiple image variants."""
    ad_dco = {
        "creatives": [
            {"has_video": True, "has_image": False, "title": "Video Demo Titular", "video_url": "https://v1.mp4"},
            {"has_video": False, "has_image": True, "title": "Imagen Variante A", "image_url": "https://i1.jpg"},
            {"has_video": False, "has_image": True, "title": "Imagen Variante B", "image_url": "https://i2.jpg"},
        ]
    }
    assert detect_ad_format(ad_dco) == "dco"


def test_detect_ad_format_case4_dco_multi_video_multi_image():
    """Case 4: DCO campaign with 2 video variants and 2 image cards tested simultaneously."""
    ad_dco_multi = {
        "creatives": [
            {"has_video": True, "title": "Hook 1", "video_hd_url": "https://vid1.mp4"},
            {"has_video": True, "title": "Hook 2", "video_hd_url": "https://vid2.mp4"},
            {"has_image": True, "has_video": False, "title": "Static 1", "image_url": "https://img1.jpg"},
            {"has_image": True, "has_video": False, "title": "Static 2", "image_url": "https://img2.jpg"},
        ]
    }
    assert detect_ad_format(ad_dco_multi) == "dco"


def test_detect_ad_format_case5_single_image_vs_pure_video():
    """Case 5: Boundaries of single static image vs pure single video ad."""
    single_img = {
        "image_path": "comp/123/image_0.jpg",
        "creatives": [{"has_image": True, "has_video": False, "title": "Single Promo", "body": "Discount code"}],
    }
    assert detect_ad_format(single_img) == "imagen"

    single_vid = {
        "video_path": "comp/123/video_0.mp4",
        "image_path": "comp/123/image_0.jpg",  # Thumbnail must not make it DCO or carousel
        "creatives": [{"has_video": True, "has_image": True, "title": "Single Video", "body": "Watch demo"}],
    }
    assert detect_ad_format(single_vid) == "video"


# --- Task 1 & 2: Routing and Resolution Tokens ---

def test_route_ad_for_evaluation():
    # Video routes to Gemini
    route_v = route_ad_for_evaluation("video")
    assert route_v["provider"] == "gemini"
    assert "gemini" in route_v["model"]

    # Statics route to Claude
    for fmt in ("imagen", "carrusel", "dco"):
        route_s = route_ad_for_evaluation(fmt)
        assert route_s["provider"] == "claude"
        assert route_s["model"] == "claude-sonnet-5-5"


def test_video_token_estimation_resolutions():
    # Low resolution (~100 tokens/s)
    tokens_low = estimate_video_tokens(30.0, resolution="low")
    assert tokens_low == int(30 * 100) + 1000

    # Medium resolution (~280 tokens/s)
    tokens_med = estimate_video_tokens(30.0, resolution="medium")
    assert tokens_med == int(30 * 280) + 1000

    # High resolution (~560 tokens/s)
    tokens_high = estimate_video_tokens(30.0, resolution="high")
    assert tokens_high == int(30 * 560) + 1000


# --- Task 3: 20-minute video triggers demasiado_largo via TPM limit ---

def test_video_context_limit_20_minutes_triggers_demasiado_largo():
    """
    A 20-minute video (1,200 seconds) at medium resolution generates ~337,000 tokens,
    which exceeds the 250,000 GEMINI_TPM rate limit of the free tier.
    Must be marked as 'demasiado_largo'.
    """
    duration_20min = 1200.0  # 20 minutes
    too_long, est_tokens, reason = check_video_context_limit(
        duration_s=duration_20min,
        resolution="medium",
        max_tpm_tokens=250_000,
    )
    assert too_long is True
    assert est_tokens == int(1200 * 280) + 1000  # 337,000 tokens
    assert "demasiado_largo" in reason
    assert "TPM" in reason

    # Contrast with a standard 60-second video: well within TPM and context
    short_too_long, short_tokens, short_reason = check_video_context_limit(
        duration_s=60.0,
        resolution="medium",
        max_tpm_tokens=250_000,
    )
    assert short_too_long is False
    assert short_reason == "ok"


# --- Task 4: Inactive ads handling (data only vs > 60 days threshold and lowest priority) ---

def test_inactive_ads_data_only_and_lowest_priority():
    # 1. Inactive ad with <= 60 days duration -> data only, not queued for evaluation
    inactive_short = {
        "ad_id": "inact_short",
        "is_active": False,
        "days_active": 45,
        "priority": 1,
    }
    should_eval, reason = should_evaluate_ad(inactive_short)
    assert should_eval is False
    assert "skipped_data_only" in reason

    # 2. Inactive ad with > 60 days duration -> eligible for evaluation at lowest priority
    inactive_long = {
        "ad_id": "inact_long",
        "is_active": False,
        "days_active": 75,
        "priority": 1,
        "market_score": 90.0,
    }
    should_eval_long, reason_long = should_evaluate_ad(inactive_long)
    assert should_eval_long is True
    assert reason_long == "eligible"

    # 3. Active ad with 10 days duration -> eligible
    active_ad = {
        "ad_id": "act_norm",
        "is_active": True,
        "days_active": 10,
        "priority": 2,
        "market_score": 50.0,
    }
    should_eval_act, _ = should_evaluate_ad(active_ad)
    assert should_eval_act is True

    # 4. Sorting queue: active ads strictly precede inactive ads (even if inactive has higher priority/market_score)
    queue = [inactive_long, active_ad]
    sorted_queue = sort_ads_for_evaluation(queue)
    assert sorted_queue[0]["ad_id"] == "act_norm"
    assert sorted_queue[1]["ad_id"] == "inact_long"
