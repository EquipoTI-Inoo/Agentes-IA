"""B3/B4: prioritized queue, exclusions, evaluable media, estimated days. Temporary DB, no APIs."""

import pytest
from pipeline_helpers import add_ad, seed_competitors

from src.database import SessionLocal
from src.db.models import STATUS_EVALUATED, STATUS_OUT_OF_CATALOG, STATUS_REJECTED, Ad
from src.work_queue import estimate_days_to_clear, queue_stats, select_queue


@pytest.fixture
def world(clean_db, tmp_path):
    comps = seed_competitors()  # Alpha priority 1, Beta priority 2
    return comps, tmp_path


def _order(downloads, **kw):
    with SessionLocal() as db:
        return [a.id for a in select_queue(db, downloads_dir=downloads, **kw)]


def test_queue_order_priority_1_then_longevity_then_variants(world):
    comps, d = world
    add_ad("beta_old", d, comps["Beta Chat"], days=300)  # priority 2, very long running
    add_ad("alpha_young", d, comps["Alpha CRM"], days=5)
    add_ad("alpha_30_v1", d, comps["Alpha CRM"], days=30, variants=1)
    add_ad("alpha_30_v5", d, comps["Alpha CRM"], days=30, variants=5)  # same days, more variants
    add_ad("alpha_90", d, comps["Alpha CRM"], days=90)
    add_ad("no_competitor", d, None, days=500)
    add_ad("alpha_inactive", d, comps["Alpha CRM"], days=1000, active=False)  # inactive ads go last (Phase 0.1 policy)

    assert _order(d) == [
        "alpha_90",          # (a) priority 1, (b) longest
        "alpha_30_v5",       # priority 1, 30 days, (c) more variants first
        "alpha_30_v1",
        "alpha_young",
        "beta_old",          # priority 2 comes after every priority-1 ad even with 300 days
        "no_competitor",     # no competitor: lowest priority among active ads
        "alpha_inactive",    # inactive: lowest of all
    ]


def test_rejected_and_out_of_catalog_are_never_queued(world):
    comps, d = world
    add_ad("good", d, comps["Alpha CRM"], days=10)
    add_ad("rej", d, comps["Alpha CRM"], status=STATUS_REJECTED, days=999, variants=50)
    add_ad("ooc", d, None, status=STATUS_OUT_OF_CATALOG, days=999, variants=50)
    assert _order(d) == ["good"]
    assert _order(d, include_evaluated=True) == ["good"]  # --force does not resurrect them either
    with SessionLocal() as db:
        stats = queue_stats(db, d)
    assert (stats["rejected"], stats["out_of_catalog"], stats["queue"]) == (1, 1, 1)


def test_ads_without_their_media_file_are_not_evaluable(world):
    comps, d = world
    add_ad("has_file", d, comps["Alpha CRM"])
    add_ad("no_media_row", d, comps["Alpha CRM"], with_media=False)
    add_ad("video_flag_but_only_image", d, comps["Alpha CRM"], video=False, with_media=True)  # static with image: ok
    with SessionLocal() as db:
        db.get(Ad, "has_file").media[0].rel_path = "missing/file.mp4"
        db.commit()
    assert _order(d) == ["video_flag_but_only_image"]
    with SessionLocal() as db:
        stats = queue_stats(db, d)
    assert stats["queue"] == 1 and stats["pending_without_media"] == 2 and stats["queue_static"] == 1 and stats["queue_video"] == 0


def test_force_requeues_evaluated_ads_and_scope_filters(world):
    comps, d = world
    add_ad("pending_a", d, comps["Alpha CRM"], pillar="crm")
    add_ad("done_a", d, comps["Alpha CRM"], status=STATUS_EVALUATED, pillar="crm")
    add_ad("pending_b", d, comps["Beta Chat"], pillar="conversacional", page_name="Beta Chat")
    assert _order(d) == ["pending_a", "pending_b"]
    assert _order(d, include_evaluated=True) == ["done_a", "pending_a", "pending_b"]  # equal keys: ordered by id
    assert _order(d, pillar="conversacional") == ["pending_b"]
    assert _order(d, competitor_id=comps["Alpha CRM"]) == ["pending_a"]
    assert _order(d, limit=1) == ["pending_a"]


@pytest.mark.parametrize("pending,rpd,reserve,expected", [
    (0, 20, 2, 0), (1, 20, 2, 1), (18, 20, 2, 1), (19, 20, 2, 2), (150, 20, 2, 9), (310, 20, 2, 18), (5, 2, 2, 5),
])
def test_estimated_days_to_clear_is_pending_over_rpd_minus_reserve(pending, rpd, reserve, expected):
    assert estimate_days_to_clear(pending, rpd, reserve) == expected
