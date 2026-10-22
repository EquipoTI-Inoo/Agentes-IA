"""
Job queue on the 'jobs' table (Phase 7, B5). No external dependencies: any process can enqueue, one worker
(src/worker.py) consumes.

  type      payload
  run       RunOptions dict (src/pipeline.py)            -> worker runs the pipeline
  evaluate  {ad_ids?, force?, prompt_version?, limit?}   -> worker evaluates (queue order)
  forensics ad_id column                                  -> worker runs local Whisper
  download  per-ad media URLs (pipeline internal; the worker ignores it)

status: pending -> running -> done | failed. A job that was running when the worker died goes back to pending
(requeue_running) and is simply run again: every pipeline stage is idempotent.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import func, update

from src.db.models import Job, from_json, to_json, utcnow

WORKER_TYPES = ("run", "evaluate", "forensics")


def _session_factory():
    from src.database import SessionLocal

    return SessionLocal


def job_to_dict(job: Job) -> Dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "ad_id": job.ad_id,
        "status": job.status,
        "error": job.error,
        "payload": from_json(job.payload, {}),
        "result": from_json(job.result, None),
        "attempts": job.attempts or 0,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def enqueue(job_type: str, payload: Optional[Dict[str, Any]] = None, ad_id: Optional[str] = None) -> int:
    """Adds a pending job and returns its id."""
    with _session_factory()() as s:
        job = Job(type=job_type, ad_id=ad_id, payload=to_json(payload or {}), status="pending", attempts=0)
        s.add(job)
        s.commit()
        return job.id


def upsert_pending(job_type: str, ad_id: str, payload: Dict[str, Any]) -> int:
    """One pending job per (type, ad): refreshes its payload (e.g. newer media URLs) instead of duplicating it."""
    with _session_factory()() as s:
        job = s.query(Job).filter(Job.type == job_type, Job.ad_id == ad_id, Job.status == "pending").first()
        if job is None:
            job = Job(type=job_type, ad_id=ad_id, status="pending", attempts=0)
            s.add(job)
        job.payload = to_json(payload)
        s.commit()
        return job.id


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    with _session_factory()() as s:
        job = s.get(Job, job_id)
        return job_to_dict(job) if job else None


def list_jobs(status: Optional[str] = None, job_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    with _session_factory()() as s:
        q = s.query(Job)
        if status:
            q = q.filter(Job.status == status)
        if job_type:
            q = q.filter(Job.type == job_type)
        return [job_to_dict(j) for j in q.order_by(Job.id.desc()).limit(limit).all()]


def pending_jobs(job_type: str, ad_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    with _session_factory()() as s:
        q = s.query(Job).filter(Job.type == job_type, Job.status == "pending")
        if ad_ids is not None:
            q = q.filter(Job.ad_id.in_(list(ad_ids)))
        return [job_to_dict(j) for j in q.order_by(Job.id).all()]


def claim_next(types: Iterable[str] = WORKER_TYPES) -> Optional[Dict[str, Any]]:
    """Atomically takes the oldest pending job (pending -> running). Safe even with several consumers."""
    types = list(types)
    with _session_factory()() as s:
        while True:
            row = s.query(Job.id).filter(Job.status == "pending", Job.type.in_(types)).order_by(Job.id).first()
            if row is None:
                return None
            claimed = s.execute(
                update(Job)
                .where(Job.id == row[0], Job.status == "pending")
                .values(status="running", started_at=utcnow(), attempts=func.coalesce(Job.attempts, 0) + 1)
            )
            s.commit()
            if claimed.rowcount:
                return job_to_dict(s.get(Job, row[0]))
            # another consumer won the race: look for the next one


def complete(job_id: int, result: Optional[Dict[str, Any]] = None) -> None:
    _finish(job_id, "done", None, result)


def fail(job_id: int, error: str, result: Optional[Dict[str, Any]] = None) -> None:
    _finish(job_id, "failed", error, result)


def note_error(job_id: int, error: str) -> None:
    """Records an error on a job that stays pending (another attempt is still allowed); counts the attempt."""
    with _session_factory()() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.error, job.attempts = error, (job.attempts or 0) + 1
            s.commit()


def release(job_id: int, result: Optional[Dict[str, Any]] = None) -> None:
    """Puts a running job back to pending (worker stopped mid-job); it will resume later."""
    with _session_factory()() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.status, job.result, job.finished_at = "pending", to_json(result) if result is not None else job.result, None
            s.commit()


def _finish(job_id: int, status: str, error: Optional[str], result: Optional[Dict[str, Any]]) -> None:
    with _session_factory()() as s:
        job = s.get(Job, job_id)
        if job is not None:
            job.status, job.error, job.finished_at = status, error, utcnow()
            job.result = json.dumps(result, ensure_ascii=False, default=str) if result is not None else job.result
            s.commit()


def requeue_running(types: Iterable[str] = WORKER_TYPES) -> int:
    """At worker start: jobs left 'running' by a dead worker go back to pending. Returns how many."""
    with _session_factory()() as s:
        res = s.execute(update(Job).where(Job.status == "running", Job.type.in_(list(types))).values(status="pending"))
        s.commit()
        return res.rowcount or 0
