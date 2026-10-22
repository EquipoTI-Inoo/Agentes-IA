"""--priority N: a run restricted to one catalog tier searches only those competitors (by page_id) and never touches other tiers' ads."""

import pytest
from pipeline_helpers import FakeHunter, FakeRelevance, scraper_ad, seed_competitors

from src.cli import build_parser
from src.database import SessionLocal
from src.db.models import STATUS_DISCOVERED, STATUS_FILTERED, Ad
from src.pipeline import Pipeline, RunOptions


@pytest.fixture
def world(clean_db, tmp_path):
    seed_competitors()
    return tmp_path / "downloads"


def make(downloads, hunter):
    return Pipeline(hunter_factory=lambda: hunter, relevance_filter_factory=lambda: FakeRelevance(),
                    evaluate_fn=lambda *a, **k: pytest.fail("evaluate must not run"), quota_check=lambda: None, downloads_dir=downloads)


def test_priority_discovers_only_that_tier_by_page_id(world):
    hunter = FakeHunter(world, {"Alpha CRM": [scraper_ad("a1", video=False)], "Beta Chat": [scraper_ad("b1", page_id="2", page_name="Beta Chat")]})
    stats = make(world, hunter).stage_discover(RunOptions(priority=1, countries=["PE", "MX"], limit=5, media_filter="image"))
    assert stats["queries"] == 2 and {c["query"] for c in hunter.search_calls} == {"Alpha CRM"}
    assert all(c["competitor"] == {"name": "Alpha CRM", "page_ids": ["1"]} for c in hunter.search_calls)  # never a free-name search
    with SessionLocal() as s:
        assert [a.id for a in s.query(Ad).all()] == ["a1"]


def test_priority_scopes_filter_stage_to_the_tier(world):
    hunter = FakeHunter(world, {"Alpha CRM": [scraper_ad("a1")], "Beta Chat": [scraper_ad("b1", page_id="2", page_name="Beta Chat")]})
    p = make(world, hunter)
    p.stage_discover(RunOptions(competitor="Beta Chat", countries=["PE"]))  # an older run left a tier-2 ad as 'discovered'
    p.stage_discover(RunOptions(priority=1, countries=["PE"]))
    p.stage_filter(RunOptions(priority=1))
    with SessionLocal() as s:
        assert s.get(Ad, "a1").status == STATUS_FILTERED
        assert s.get(Ad, "b1").status == STATUS_DISCOVERED  # untouched


def test_cli_accepts_priority_and_rejects_other_values():
    parser = build_parser()
    args = parser.parse_args(["run", "--priority", "1", "--media", "image", "--stages", "discover,filter,download,media"])
    assert args.priority == 1 and args.media == "image"
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--priority", "4"])


def test_image_only_run_skips_video_ads_and_never_advances_them(world):
    ads = [scraper_ad("img1", video=False), scraper_ad("vid1", video=True)]
    hunter = FakeHunter(world, {"Alpha CRM": ads})
    p = make(world, hunter)
    stats = p.stage_discover(RunOptions(priority=1, countries=["PE"], media_filter="image"))
    assert stats["skipped_video"] == 1 and stats["new"] == 1
    p.stage_discover(RunOptions(competitor="Alpha CRM", countries=["PE"], media_filter="all"))  # a normal run does store the video
    p.stage_filter(RunOptions(priority=1, media_filter="image"))
    with SessionLocal() as s:
        assert s.get(Ad, "img1").status == STATUS_FILTERED
        assert s.get(Ad, "vid1").status == STATUS_DISCOVERED  # outside the static-only scope
