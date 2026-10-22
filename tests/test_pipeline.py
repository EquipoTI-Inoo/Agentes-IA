"""
B1/B3: pipeline stages are idempotent, an interrupted run resumes without duplicating downloads or evaluations,
and a daily 429 leaves ads pending without counting failures. Fakes only; temporary DB and downloads dir.
"""

import pytest
from pipeline_helpers import FakeEvaluator, FakeHunter, FakeRelevance, Interrupted, add_ad, scraper_ad, seed_competitors

from src.ai.quota import QuotaExhausted
from src.database import SessionLocal
from src.db.models import (
    STATUS_DISCOVERED, STATUS_DOWNLOADED, STATUS_EVALUATED, STATUS_FILTERED, STATUS_NEEDS_REVIEW, STATUS_OUT_OF_CATALOG,
    STATUS_PENDING, STATUS_REJECTED, STATUS_SCORED, Analysis, Ad, Job, Media, MediaInfo,
)
from src.pipeline import Pipeline, RunOptions, StopRequested


@pytest.fixture
def world(clean_db, tmp_path):
    comps = seed_competitors()
    return comps, tmp_path / "downloads"


def make_pipeline(downloads, hunter=None, relevance=None, evaluator=None, **kw):
    return Pipeline(
        hunter_factory=lambda: hunter, relevance_filter_factory=lambda: relevance or FakeRelevance(),
        evaluate_fn=evaluator or FakeEvaluator(), quota_check=lambda: None, downloads_dir=downloads, **kw,
    )


def four_ads():
    return [scraper_ad(f"ad{i}", days=10 * i) for i in range(1, 5)]


def statuses():
    with SessionLocal() as s:
        return {a.id: a.status for a in s.query(Ad).all()}


def count(model):
    with SessionLocal() as s:
        return s.query(model).count()


def opts(**kw):
    base = dict(competitor="Alpha CRM", countries=["PE"], limit=10)
    base.update(kw)
    return RunOptions(**base)


# ---------------------------------------------------------------- idempotence of each stage

def test_discover_is_idempotent_and_records_country_and_download_jobs(world):
    _, d = world
    hunter = FakeHunter(d, {"Alpha CRM": four_ads()})
    p = make_pipeline(d, hunter)
    first = p.stage_discover(opts(countries=["PE", "MX"]))
    assert (first["queries"], first["seen"], first["new"], first["updated"]) == (2, 8, 4, 4)  # same 4 ads seen in PE and MX
    snapshot = (count(Ad), count(Job))
    assert snapshot == (4, 4) and set(statuses().values()) == {STATUS_DISCOVERED}

    again = p.stage_discover(opts(countries=["PE", "MX"]))
    assert again["new"] == 0 and (count(Ad), count(Job)) == snapshot  # no duplicate ads, one pending download job per ad
    with SessionLocal() as s:
        assert s.get(Ad, "ad1").country_list == '["MX", "PE"]'  # merged across searches
        assert s.get(Ad, "ad1").competitor_id is not None  # resolved by page_id
    assert hunter.search_calls[0]["filter_relevance"] is False  # discovery keeps everything; filtering is its own stage
    assert hunter.search_calls[0]["competitor"] == {"name": "Alpha CRM", "page_ids": ["1"]}  # page_id search first


def test_filter_stage_decides_status_and_is_idempotent(world):
    comps, d = world
    ads = [scraper_ad("ok"), scraper_ad("bad"), scraper_ad("doubt"), scraper_ad("later"),
           scraper_ad("stranger", page_id="999", page_name="Someone Else"),
           scraper_ad("old_inactive", is_active=False, days_active=20)]
    for ad in ads:
        ad.setdefault("is_active", True)
    hunter = FakeHunter(d, {"Alpha CRM": ads})
    relevance = FakeRelevance(irrelevant={"bad"}, review={"doubt"}, pending={"later"})
    p = make_pipeline(d, hunter, relevance)
    p.stage_discover(opts())
    result = p.stage_filter(opts())

    st = statuses()
    assert st["ok"] == STATUS_FILTERED and st["bad"] == STATUS_REJECTED and st["doubt"] == STATUS_NEEDS_REVIEW
    assert st["later"] == STATUS_DISCOVERED  # could not be classified now: decided on a later run
    assert st["stranger"] == STATUS_OUT_OF_CATALOG  # relevant but its page is not a catalogued competitor
    assert st["old_inactive"] == "inactive_data_only"
    assert result["pending_relevance"] == 1

    before = statuses()
    p.stage_filter(opts())
    assert relevance.calls == 2 and statuses() == before  # only 'later' was re-checked; nothing else moved


def test_each_stage_skips_what_is_already_processed(world):
    comps, d = world
    hunter, evaluator = FakeHunter(d, {"Alpha CRM": four_ads()}), FakeEvaluator()
    p = make_pipeline(d, hunter, evaluator=evaluator)
    p.stage_discover(opts()), p.stage_filter(opts())

    assert p.stage_download(opts())["downloaded"] == 4
    fetched = dict(hunter.fetches)
    assert p.stage_download(opts())["candidates"] == 0 and hunter.fetches == fetched  # nothing refetched

    assert p.stage_media(opts())["processed"] == 4
    media_rows, info_rows = count(Media), count(MediaInfo)
    assert p.stage_media(opts())["processed"] == 0 and (count(Media), count(MediaInfo)) == (media_rows, info_rows)
    assert set(statuses().values()) == {STATUS_PENDING}
    with SessionLocal() as s:
        info = s.get(MediaInfo, "ad1")
        assert info.sha256 and info.phash and (info.width, info.height) == (30, 20)

    assert p.stage_evaluate(opts())["evaluated"] == 4
    calls = list(evaluator.calls)
    second = p.stage_evaluate(opts())
    assert second["queue"] == 0 and evaluator.calls == calls and count(Analysis) == 4

    first_signals = p.stage_signals(opts())
    assert set(statuses().values()) == {STATUS_SCORED} and first_signals["marked_scored"] == 4
    assert p.stage_signals(opts())["marked_scored"] == 0 and count(Analysis) == 4


# ---------------------------------------------------------------- B1 acceptance: resume after interruption

def test_run_interrupted_during_download_resumes_without_duplicates(world):
    comps, d = world
    ads = [scraper_ad(f"r{i}", days=100 - i) for i in range(1, 6)]
    hunter = FakeHunter(d, {"Alpha CRM": ads}, interrupt_on_download=3)  # dies right after writing the 3rd ad's files
    evaluator = FakeEvaluator()

    with pytest.raises(Interrupted):
        make_pipeline(d, hunter, evaluator=evaluator).run(opts())
    assert hunter.closed and count(Ad) == 5
    assert sum(1 for s in statuses().values() if s == STATUS_DOWNLOADED) == 2  # the 3rd was never recorded

    hunter.interrupt_on_download = None  # second run: same data, new Pipeline instance (fresh process)
    summary = make_pipeline(d, hunter, evaluator=evaluator).run(opts())

    assert summary["stages"]["download"]["downloaded"] == 3  # only the ads that were not done
    assert all(n == 1 for n in hunter.fetches.values()) and len(hunter.fetches) == 10  # every file fetched exactly once
    assert count(Ad) == 5 and count(Media) == 10  # no duplicate ads or media rows
    assert sorted(evaluator.calls) == [f"r{i}" for i in range(1, 6)] and count(Analysis) == 5  # every ad evaluated once
    assert set(statuses().values()) == {STATUS_SCORED}
    with SessionLocal() as s:
        assert {j.status for j in s.query(Job).filter(Job.type == "download")} == {"done"}  # no orphaned jobs


def test_run_interrupted_during_evaluation_resumes_without_duplicate_evaluations(world):
    comps, d = world
    hunter = FakeHunter(d, {"Alpha CRM": [scraper_ad(f"e{i}", days=100 - i) for i in range(1, 6)]})
    evaluator = FakeEvaluator(interrupt_after_write={"e3"})  # the analysis of e3 is saved, then the process dies

    with pytest.raises(Interrupted):
        make_pipeline(d, hunter, evaluator=evaluator).run(opts())
    assert count(Analysis) == 3 and statuses()["e3"] == STATUS_EVALUATED

    evaluator.interrupt_after_write = set()
    # also simulate that the status update of e3 was lost with the crash
    with SessionLocal() as s:
        s.get(Ad, "e3").status = STATUS_PENDING
        s.commit()
    make_pipeline(d, hunter, evaluator=evaluator).run(opts())

    assert sorted(evaluator.calls) == ["e1", "e2", "e3", "e4", "e5"]  # e3 was NOT evaluated a second time
    assert count(Analysis) == 5 and set(statuses().values()) == {STATUS_SCORED}


def test_stop_request_ends_the_run_cleanly_and_resumes_later(world):
    comps, d = world
    hunter = FakeHunter(d, {"Alpha CRM": four_ads()})
    flag = {"stop": False}
    evaluator = FakeEvaluator()

    class StoppingEvaluator(FakeEvaluator):
        def __call__(self, *a, **kw):
            result = super().__call__(*a, **kw)
            flag["stop"] = True  # the worker was asked to shut down after the first evaluation
            return result

    summary = make_pipeline(d, hunter, evaluator=StoppingEvaluator(), stop_check=lambda: flag["stop"]).run(opts())
    assert summary["interrupted"] is True and count(Analysis) == 1

    flag["stop"] = False
    make_pipeline(d, hunter, evaluator=evaluator).run(opts())
    assert count(Analysis) == 4 and len(evaluator.calls) == 3  # continued where it stopped
    with pytest.raises(StopRequested):
        Pipeline(stop_check=lambda: True)._check_stop()


# ---------------------------------------------------------------- B3: quota and queue behavior

def test_daily_429_stops_the_run_and_leaves_ads_pending_without_failures(world):
    comps, d = world
    for i, days in enumerate((400, 300, 200, 100), 1):
        add_ad(f"q{i}", d, comps["Alpha CRM"], days=days)
    evaluator = FakeEvaluator(quota_ids={"q3"})
    result = make_pipeline(d, evaluator=evaluator).stage_evaluate(RunOptions(stages=("evaluate",)))

    assert result["quota_stopped"] is True and result["failed"] == 0 and result["evaluated"] == 2
    assert evaluator.calls == ["q1", "q2", "q3"]  # q4 was never attempted: the run stopped
    st = statuses()
    assert (st["q1"], st["q2"], st["q3"], st["q4"]) == (STATUS_EVALUATED, STATUS_EVALUATED, STATUS_PENDING, STATUS_PENDING)
    assert count(Job) == 0  # nothing recorded as a failure

    resumed = FakeEvaluator()  # "tomorrow": the next run continues exactly with the pending ones
    make_pipeline(d, evaluator=resumed).stage_evaluate(RunOptions(stages=("evaluate",)))
    assert resumed.calls == ["q3", "q4"]


def test_quota_preflight_stops_before_calling_any_evaluator(world):
    comps, d = world
    add_ad("p1", d, comps["Alpha CRM"])
    evaluator = FakeEvaluator()

    def exhausted():
        raise QuotaExhausted(model="m", day_pt="d", requests_used=18, limit=18)

    p = Pipeline(evaluate_fn=evaluator, quota_check=exhausted, downloads_dir=d)
    result = p.stage_evaluate(RunOptions(stages=("evaluate",)))
    assert result["quota_stopped"] and evaluator.calls == [] and statuses()["p1"] == STATUS_PENDING


def test_real_video_evaluator_leaves_ad_pending_without_failure_record_on_quota_exhaustion(clean_db, tmp_path, monkeypatch):
    """The real evaluate_video_ad: QuotaExhausted is not a failure (no Job error, status untouched)."""
    from unittest.mock import MagicMock

    from src.evaluator import evaluate_video_ad

    comps = seed_competitors()
    add_ad("real1", tmp_path, comps["Alpha CRM"])
    client = MagicMock()
    client.evaluate.side_effect = QuotaExhausted(model="m", day_pt="d", requests_used=18, limit=18)
    monkeypatch.setattr("src.evaluator.get_video_info", lambda p: {"duration_s": 10.0, "has_audio_track": True})
    monkeypatch.setattr("src.evaluator.DOWNLOADS_DIR", tmp_path)

    with pytest.raises(QuotaExhausted):
        evaluate_video_ad({"ad_id": "real1", "video_path": "Alpha CRM/real1/video_0.mp4"}, client=client, force=True)
    assert statuses()["real1"] == STATUS_PENDING and count(Job) == 0 and count(Analysis) == 0

    client.evaluate.side_effect = RuntimeError("503 Service Unavailable")  # a genuine failure IS recorded
    with pytest.raises(RuntimeError):
        evaluate_video_ad({"ad_id": "real1", "video_path": "Alpha CRM/real1/video_0.mp4"}, client=client, force=True)
    with SessionLocal() as s:
        job = s.query(Job).one()
        assert job.type == "evaluate" and "503" in job.error


def test_claude_deferral_skips_remaining_statics_but_videos_continue(world):
    comps, d = world
    add_ad("s1", d, comps["Alpha CRM"], days=500, video=False)
    add_ad("s2", d, comps["Alpha CRM"], days=400, video=False)
    add_ad("v1", d, comps["Alpha CRM"], days=100, video=True)
    evaluator = FakeEvaluator(pending_ids={"s1"})
    result = make_pipeline(d, evaluator=evaluator).stage_evaluate(RunOptions(stages=("evaluate",)))
    assert result["deferred"] == 2 and result["claude_blocked"] and result["failed"] == 0
    assert evaluator.calls == ["s1", "v1"]  # s2 (static) was not even tried; the video still ran
    assert statuses() == {"s1": STATUS_PENDING, "s2": STATUS_PENDING, "v1": STATUS_EVALUATED}


def test_generic_failure_is_counted_and_the_run_continues(world):
    comps, d = world
    for i in (1, 2, 3):
        add_ad(f"f{i}", d, comps["Alpha CRM"], days=100 - i)
    result = make_pipeline(d, evaluator=FakeEvaluator(fail_ids={"f2"})).stage_evaluate(RunOptions(stages=("evaluate",)))
    assert (result["failed"], result["evaluated"], result["quota_stopped"]) == (1, 2, False)


def test_force_prompt_version_and_limit_reach_the_evaluator(world):
    comps, d = world
    add_ad("g1", d, comps["Alpha CRM"], status=STATUS_EVALUATED, days=50)
    add_ad("g2", d, comps["Alpha CRM"], days=40)
    ev = FakeEvaluator()
    make_pipeline(d, evaluator=ev).stage_evaluate(RunOptions(stages=("evaluate",), force=True, prompt_version="v2", eval_limit=1))
    assert ev.calls == ["g1"] and ev.kwargs == [{"force": True, "prompt_version": "v2"}]


def test_download_min_days_and_missing_sources(world):
    comps, d = world
    hunter = FakeHunter(d, {"Alpha CRM": [scraper_ad("young", days=3), scraper_ad("old", days=60)]})
    p = make_pipeline(d, hunter)
    p.stage_discover(opts()), p.stage_filter(opts())
    result = p.stage_download(opts(download_min_days=15))
    assert result["skipped_min_days"] == 1 and result["downloaded"] == 1 and hunter.download_calls == ["old"]
    assert statuses()["young"] == STATUS_FILTERED  # waits for a run without the threshold

    add_ad("orphan", d, comps["Alpha CRM"], status=STATUS_FILTERED, with_media=False)  # no stored media URLs
    assert p.stage_download(opts(download_min_days=0))["no_source"] == 1


def test_options_validation_and_scoping(world):
    with pytest.raises(ValueError, match="Etapas desconocidas"):
        RunOptions(stages=("discover", "magic"))
    comps, d = world
    with pytest.raises(ValueError, match="no está en el catálogo"):
        make_pipeline(d, FakeHunter(d)).stage_discover(RunOptions(competitor="Nope", countries=["PE"]))
    with pytest.raises(ValueError, match="Indica --pillar"):
        make_pipeline(d, FakeHunter(d)).stage_discover(RunOptions(countries=["PE"]))

    hunter = FakeHunter(d, {"Alpha CRM": [scraper_ad("a1")], "Beta Chat": [scraper_ad("b1", page_id="2", page_name="Beta Chat")]})
    p = make_pipeline(d, hunter)
    p.stage_discover(RunOptions(competitor="alpha crm", countries=["PE"]))  # case-insensitive catalog lookup
    assert set(statuses()) == {"a1"}
    p.stage_discover(RunOptions(all_pillars=True, countries=["PE"], max_ads=1))
    assert set(statuses()) == {"a1"}  # max_ads=1: stopped after the first ad seen, Beta Chat was never searched
    assert [c["query"] for c in hunter.search_calls][:2] == ["Alpha CRM", "Alpha CRM"] and len(hunter.search_calls) == 2


def test_meta_block_stops_discovery_but_later_stages_still_run(world):
    from src.collect.ratelimit import MetaBlockedError

    comps, d = world

    class BlockedHunter(FakeHunter):
        def search_ads(self, **kw):
            raise MetaBlockedError("blocked")

    summary = make_pipeline(d, BlockedHunter(d)).run(opts(stages=("discover", "filter", "signals")))
    assert summary["stages"]["discover"]["blocked"] is True and "signals" in summary["stages"]
    assert "cost" in summary and summary["cost"]["gemini_requests"] >= 0


def test_keyword_discovery_adds_pillar_keywords_and_strangers_end_out_of_catalog(world):
    comps, d = world
    from src.pillars import PILLARS

    assert "voice ai agent" in PILLARS["voz"]["keywords"] and "crm whatsapp ventas" in PILLARS["crm"]["keywords"]  # terms of the retired scripts
    hunter = FakeHunter(d, {"crm whatsapp ventas": [scraper_ad("kw1", page_id="777", page_name="Unknown Co")],
                            "Alpha CRM": [scraper_ad("own1")]})
    p = make_pipeline(d, hunter)
    summary = p.stage_discover(RunOptions(pillar="crm", keywords=True, countries=["PE"], limit=5))
    queries = [c["query"] for c in hunter.search_calls]
    assert "Alpha CRM" in queries and "crm whatsapp ventas" in queries and "Beta Chat" not in queries  # crm competitors + crm keywords only
    assert summary["new"] == 2
    p.stage_filter(RunOptions(pillar="crm", keywords=True))
    st = statuses()
    assert st["own1"] == STATUS_FILTERED and st["kw1"] == STATUS_OUT_OF_CATALOG  # keyword hits from non-catalog pages are kept but never queued
