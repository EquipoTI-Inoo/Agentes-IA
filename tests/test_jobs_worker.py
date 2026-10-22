"""B5: job queue on the Job table, separate worker, API that only enqueues. Stop/resume without losing jobs."""

import pytest
from fastapi.testclient import TestClient
from pathlib import Path

from pipeline_helpers import add_ad, seed_competitors

from src import jobs
from src.database import SessionLocal
from src.db.models import Job
from src.web.app import app
from src.worker import Worker


class FakePipe:
    """pipeline_factory product: records what it was asked to run."""

    def __init__(self, stop_check, result=None, error=None, log=None):
        self.stop_check, self.result, self.error, self.log = stop_check, result or {"stages": {}, "interrupted": False}, error, log

    def run(self, opts):
        self.log.append(opts.to_dict())
        if self.error:
            raise self.error
        return self.result


def worker_with(result=None, error=None, log=None):
    log = log if log is not None else []
    return Worker(sleep=lambda s: None, pipeline_factory=lambda stop_check: FakePipe(stop_check, result, error, log)), log


@pytest.fixture(autouse=True)
def _clean(clean_db):
    yield


def test_enqueue_consume_and_status_flow():
    job_id = jobs.enqueue("run", {"pillar": "crm", "countries": ["PE"], "limit": 5})
    assert jobs.get_job(job_id)["status"] == "pending" and jobs.get_job(job_id)["payload"]["pillar"] == "crm"

    worker, log = worker_with(result={"stages": {"discover": {"new": 3}}, "interrupted": False})
    assert worker.run_once() is True and worker.run_once() is False  # second call: queue is empty
    job = jobs.get_job(job_id)
    assert job["status"] == "done" and job["error"] is None and job["attempts"] == 1
    assert job["result"]["stages"]["discover"]["new"] == 3 and job["started_at"] and job["finished_at"]
    assert log[0]["pillar"] == "crm" and log[0]["limit"] == 5  # payload became RunOptions


def test_jobs_run_in_fifo_order_and_the_worker_ignores_internal_download_jobs():
    add_ad("ad1", Path("."), None, with_media=False)
    jobs.upsert_pending("download", "ad1", {"ad": {}})  # pipeline-internal: never consumed by the worker
    first, second = jobs.enqueue("run", {"pillar": "voz"}), jobs.enqueue("run", {"pillar": "crm"})
    worker, log = worker_with()
    worker.run_forever(drain_and_exit=True)
    assert [c["pillar"] for c in log] == ["voz", "crm"]
    assert jobs.get_job(first)["status"] == jobs.get_job(second)["status"] == "done"
    assert [j["status"] for j in jobs.list_jobs(job_type="download")] == ["pending"]


def test_failed_job_records_the_error_and_the_worker_keeps_going():
    bad = jobs.enqueue("run", {"pillar": "voz"})
    good = jobs.enqueue("run", {"pillar": "crm"})
    worker, _ = worker_with(error=ValueError("catálogo vacío"))
    worker.run_once()
    assert jobs.get_job(bad)["status"] == "failed" and "catálogo vacío" in jobs.get_job(bad)["error"]
    worker.error = None
    worker2, _ = worker_with()
    worker2.run_once()
    assert jobs.get_job(good)["status"] == "done"


def test_worker_stopped_mid_job_returns_it_to_the_queue_and_it_resumes():
    job_id = jobs.enqueue("run", {"pillar": "crm"})
    worker, _ = worker_with(result={"stages": {"download": {"downloaded": 2}}, "interrupted": True})
    worker.run_once()
    stopped = jobs.get_job(job_id)
    assert stopped["status"] == "pending" and stopped["finished_at"] is None  # nothing lost

    resumed, log = worker_with(result={"stages": {"download": {"downloaded": 3}}, "interrupted": False})
    resumed.run_once()
    final = jobs.get_job(job_id)
    assert final["status"] == "done" and final["attempts"] == 2 and final["result"]["stages"]["download"]["downloaded"] == 3
    assert log and log[0]["pillar"] == "crm"


def test_job_left_running_by_a_dead_worker_is_requeued_at_startup():
    job_id = jobs.enqueue("evaluate", {"limit": 3})
    claimed = jobs.claim_next()  # a worker took it...
    assert claimed["id"] == job_id and jobs.get_job(job_id)["status"] == "running"
    # ...and was killed (no complete/fail/release). A new worker starts:
    worker, log = worker_with()
    worker.run_forever(drain_and_exit=True)
    assert jobs.get_job(job_id)["status"] == "done" and jobs.get_job(job_id)["attempts"] == 2
    assert log[0]["stages"] == ["evaluate", "signals"] and log[0]["eval_limit"] == 3


def test_stop_request_before_the_loop_leaves_pending_jobs_untouched():
    job_id = jobs.enqueue("run", {"pillar": "crm"})
    worker, log = worker_with()
    worker.request_stop()
    worker.run_forever()
    assert log == [] and jobs.get_job(job_id)["status"] == "pending"


def test_claim_is_atomic_two_consumers_never_get_the_same_job():
    for _ in range(3):
        jobs.enqueue("run", {})
    taken = [jobs.claim_next() for _ in range(5)]
    ids = [t["id"] for t in taken if t]
    assert len(ids) == 3 and len(set(ids)) == 3 and taken[3] is None


def test_forensics_job_uses_the_injected_engine(tmp_path, monkeypatch):
    comps = seed_competitors()
    add_ad("fx1", tmp_path, comps["Alpha CRM"])
    monkeypatch.setattr("src.config.settings.DOWNLOADS_DIR", str(tmp_path))
    worker = Worker(sleep=lambda s: None, forensics_fn=lambda path: {"full_transcript": "hola mundo", "duration_seconds": 12.5, "total_frames_extracted": 12})
    job_id = jobs.enqueue("forensics", ad_id="fx1")
    worker.run_once()
    assert jobs.get_job(job_id)["status"] == "done" and jobs.get_job(job_id)["result"]["transcript_chars"] == 10
    with SessionLocal() as s:
        from src.db.models import Ad

        assert s.get(Ad, "fx1").full_transcript == "hola mundo"

    add_ad("no_video", tmp_path, comps["Alpha CRM"], with_media=False)
    missing = jobs.enqueue("forensics", ad_id="no_video")
    worker.run_once()
    assert jobs.get_job(missing)["status"] == "failed" and "Video local no disponible" in jobs.get_job(missing)["error"]


# ---------------------------------------------------------------- API: enqueue only, status by job_id

client = TestClient(app)


def test_api_search_only_enqueues_and_returns_a_job_id():
    r = client.post("/api/search", json={"query": "Kommo", "country": "PE", "limit": 7, "forensics": False})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    job_id = r.json()["job_id"]

    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["status"] == "pending" and status["type"] == "run"
    assert status["payload"]["query"] == "Kommo" and status["payload"]["countries"] == ["PE"] and status["payload"]["limit"] == 7

    worker, _ = worker_with(result={"stages": {"discover": {"new": 2}}, "interrupted": False})
    worker.run_once()
    done = client.get(f"/api/jobs/{job_id}").json()
    assert done["status"] == "done" and done["result"]["stages"]["discover"]["new"] == 2


def test_api_evaluate_and_forensics_enqueue_and_unknown_job_is_404(tmp_path):
    r = client.post("/api/evaluate", json={"ad_ids": ["a1", "a2"], "force": True, "limit": 5})
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["type"] == "evaluate" and job["payload"] == {"ad_ids": ["a1", "a2"], "force": True, "limit": 5}

    assert client.post("/api/forensics/no_such_ad").status_code == 404  # validated before queuing
    assert client.get("/api/jobs/999999").status_code == 404
    listing = client.get("/api/jobs?status=pending").json()
    assert [j["type"] for j in listing] == ["evaluate"]


def test_api_module_does_not_hold_the_scraper_or_whisper():
    import src.web.app as web

    assert not hasattr(web, "MetaAdsHunter") and not hasattr(web, "VideoForensicsEngine")  # the API never runs the work itself
