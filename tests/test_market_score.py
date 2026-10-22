"""
Tests for Phase 5 market_score (src/scoring/market.py).
Platform data only: no AI, no creative content, no network.
"""

import math
from datetime import datetime, timezone

import pytest
import yaml

from src.scoring.market import (
    SCORING_CONFIG_PATH,
    compute_active_days,
    compute_market_score,
    load_scoring_config,
)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
CONFIG = load_scoring_config()


def _ad(**overrides):
    ad = {
        "delivery_start_time": "2026-10-20T07:00:00+00:00",  # 1 day before NOW
        "delivery_stop_time": None,
        "days_active": 1,
        "is_active": True,
        "variant_count": 1,
        "countries": '["PE", "MX"]',
        "publisher_platforms": '["FACEBOOK", "INSTAGRAM"]',
    }
    ad.update(overrides)
    return ad


def test_config_weights_sum_to_one():
    weights = CONFIG["market_score"]["weights"]
    assert math.isclose(sum(weights.values()), 1.0)
    assert weights == {"longevity": 0.40, "variants": 0.25, "countries": 0.15, "is_active": 0.10, "platforms": 0.10}


def test_weights_not_summing_one_raise_error(tmp_path):
    cfg = yaml.safe_load(SCORING_CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["market_score"]["weights"]["longevity"] = 0.50  # total 1.10
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="sumar 1.0"):
        load_scoring_config(bad)


def test_missing_weight_raises_error(tmp_path):
    cfg = yaml.safe_load(SCORING_CONFIG_PATH.read_text(encoding="utf-8"))
    del cfg["market_score"]["weights"]["platforms"]
    bad = tmp_path / "scoring.yaml"
    bad.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError, match="faltan pesos"):
        load_scoring_config(bad)


def test_longevity_1_day_vs_180_plus_days():
    short = compute_market_score(_ad(), CONFIG, now=NOW)
    long_ = compute_market_score(_ad(delivery_start_time="2026-03-01T07:00:00+00:00"), CONFIG, now=NOW)
    very_long = compute_market_score(_ad(delivery_start_time="2025-01-01T07:00:00+00:00"), CONFIG, now=NOW)

    short_l = short["market_components"]["used"]["longevity"]
    long_l = long_["market_components"]["used"]["longevity"]
    assert short_l["raw"]["days"] == 1
    assert short_l["value"] == pytest.approx(math.log1p(1) / math.log1p(180), abs=1e-4)
    assert long_l["raw"]["days"] > 180
    assert long_l["value"] == 1.0  # capped at 180 days
    assert long_["market_score"] > short["market_score"]
    # Beyond the 180-day cap, more days add nothing
    assert very_long["market_score"] == long_["market_score"]


def test_1_vs_8_variants():
    one = compute_market_score(_ad(variant_count=1), CONFIG, now=NOW)
    eight = compute_market_score(_ad(variant_count=8), CONFIG, now=NOW)
    assert one["market_components"]["used"]["variants"]["value"] == 0.0
    assert eight["market_components"]["used"]["variants"]["value"] == pytest.approx(math.log(8) / math.log(10), abs=1e-4)
    # Only variants differ: difference = 25 % weight * component value * 100
    assert eight["market_score"] - one["market_score"] == pytest.approx(25 * math.log(8) / math.log(10), abs=0.02)


def test_active_vs_inactive():
    start = "2026-07-01T07:00:00+00:00"
    active = compute_market_score(_ad(delivery_start_time=start, is_active=True), CONFIG, now=NOW)
    inactive = compute_market_score(
        _ad(delivery_start_time=start, is_active=False, delivery_stop_time="2026-08-10T07:00:00+00:00"),
        CONFIG,
        now=NOW,
    )
    assert active["market_components"]["used"]["is_active"]["value"] == 1.0
    assert inactive["market_components"]["used"]["is_active"]["value"] == 0.0
    # Inactive longevity = start -> stop (40 days), not start -> today
    assert inactive["market_components"]["used"]["longevity"]["raw"] == {"days": 40, "source": "start_to_stop"}
    assert active["market_components"]["used"]["longevity"]["raw"]["days"] == 112
    assert active["market_score"] > inactive["market_score"]


def test_inactive_without_stop_date_uses_stored_days():
    info = compute_active_days(_ad(is_active=False, delivery_stop_time=None, days_active=67), now=NOW)
    assert info == {"days": 67, "source": "stored_days_active"}


def test_missing_component_rescales_weights():
    ad = _ad(
        delivery_start_time="2026-03-01T07:00:00+00:00",  # longevity value 1.0
        variant_count=10,  # variants value 1.0
        countries=None,  # unknown -> excluded
        publisher_platforms="[]",  # unknown -> excluded
        is_active=False,
        delivery_stop_time="2026-10-01T07:00:00+00:00",
    )
    res = compute_market_score(ad, CONFIG, now=NOW)
    comps = res["market_components"]
    assert set(comps["missing"]) == {"countries", "platforms"}
    assert set(comps["used"]) == {"longevity", "variants", "is_active"}
    eff = {k: v["effective_weight"] for k, v in comps["used"].items()}
    assert sum(eff.values()) == pytest.approx(1.0, abs=1e-3)
    assert eff["longevity"] == pytest.approx(0.40 / 0.75, abs=1e-4)
    # (0.40 * 1 + 0.25 * 1 + 0.10 * 0) / 0.75 * 100
    assert res["market_score"] == pytest.approx(86.67, abs=0.01)


def test_platforms_count_only_configured_ones():
    res = compute_market_score(
        _ad(publisher_platforms='["FACEBOOK", "INSTAGRAM", "WHATSAPP", "THREADS"]'), CONFIG, now=NOW
    )
    plat = res["market_components"]["used"]["platforms"]
    assert plat["raw"] == ["FACEBOOK", "INSTAGRAM"]
    assert plat["value"] == 0.5


def test_no_data_at_all_gives_null_market_score():
    res = compute_market_score(
        {"delivery_start_time": None, "is_active": None, "variant_count": None,
         "countries": None, "publisher_platforms": None},
        CONFIG,
        now=NOW,
    )
    assert res["market_score"] is None
    assert len(res["market_components"]["missing"]) == 5


def test_market_score_ignores_creative_content_and_ai_fields():
    base = compute_market_score(_ad(), CONFIG, now=NOW)
    noisy = compute_market_score(
        _ad(body_text="Copy brillante", creative_score=99, page_name="Marca X", cognitive_score=100),
        CONFIG,
        now=NOW,
    )
    assert base == noisy


# --- boundary values --------------------------------------------------------------------------

ALL_COUNTRIES = ["PE", "MX", "CO", "AR", "CL", "ES", "US", "BR", "EC"]
ALL_PLATFORMS = ["FACEBOOK", "INSTAGRAM", "MESSENGER", "AUDIENCE_NETWORK"]


def _used(result, name):
    return result["market_components"]["used"][name]["value"]


def test_longevity_boundaries_around_zero_and_the_180_day_cap():
    def at(days):
        start = datetime.fromtimestamp(NOW.timestamp() - days * 86400, tz=timezone.utc).isoformat()
        return compute_market_score(_ad(delivery_start_time=start), CONFIG, now=NOW)

    assert _used(at(0), "longevity") == 0.0  # started today
    assert 0.0 < _used(at(1), "longevity") < _used(at(179), "longevity") < 1.0
    assert _used(at(180), "longevity") == 1.0
    assert _used(at(181), "longevity") == 1.0 and _used(at(5000), "longevity") == 1.0  # beyond the cap adds nothing
    assert at(5000)["market_score"] == at(180)["market_score"]


def test_variant_boundaries_around_the_cap():
    cap = CONFIG["market_score"]["variants_cap"]
    values = {n: _used(compute_market_score(_ad(variant_count=n), CONFIG, now=NOW), "variants") for n in (1, 2, cap - 1, cap, cap + 1, 500)}
    assert values[1] == 0.0 and 0.0 < values[2] < values[cap - 1] < 1.0
    assert values[cap] == values[cap + 1] == values[500] == 1.0


def test_country_and_platform_boundaries():
    cap = CONFIG["market_score"]["countries_cap"]
    countries = lambda n: _used(compute_market_score(_ad(countries=ALL_COUNTRIES[:n]), CONFIG, now=NOW), "countries")
    assert countries(1) == pytest.approx(1 / cap, abs=1e-4) and countries(cap) == 1.0 and countries(len(ALL_COUNTRIES)) == 1.0
    platforms = lambda p: compute_market_score(_ad(publisher_platforms=p), CONFIG, now=NOW)
    assert _used(platforms(ALL_PLATFORMS[:1]), "platforms") == 0.25 and _used(platforms(ALL_PLATFORMS), "platforms") == 1.0
    assert _used(platforms(ALL_PLATFORMS + ["WHATSAPP", "THREADS"]), "platforms") == 1.0  # uncounted platforms add nothing
    assert _used(platforms(["WHATSAPP"]), "platforms") == 0.0  # only uncounted platforms: present but worth nothing


def test_score_extremes_are_exactly_0_and_100_with_no_cap_in_between():
    best = compute_market_score(
        _ad(delivery_start_time="2018-01-01T00:00:00+00:00", variant_count=999, countries=ALL_COUNTRIES, publisher_platforms=ALL_PLATFORMS, is_active=True),
        CONFIG, now=NOW)
    assert best["market_score"] == 100.0
    worst = compute_market_score(
        _ad(delivery_start_time=NOW.isoformat(), variant_count=1, countries=None, publisher_platforms=None, is_active=False,
            delivery_stop_time=NOW.isoformat()),
        CONFIG, now=NOW)
    assert worst["market_score"] == 0.0 and set(worst["market_components"]["missing"]) == {"countries", "platforms"}
    mid = compute_market_score(_ad(variant_count=5, countries=ALL_COUNTRIES[:3]), CONFIG, now=NOW)["market_score"]
    assert 0.0 < mid < 100.0
    for n in (1, 3, 9, 50):  # more of anything never lowers the score and never exceeds 100
        score = compute_market_score(_ad(variant_count=n, countries=ALL_COUNTRIES), CONFIG, now=NOW)["market_score"]
        assert 0.0 <= score <= 100.0
