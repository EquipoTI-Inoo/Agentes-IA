"""
Job worker (Phase 7, B5): a separate process that consumes the 'jobs' table. No external dependencies.

    python -m src.worker            # run until Ctrl+C / SIGTERM
    python -m src.worker --once     # drain the queue and exit

Stopping is safe: the current job is released back to 'pending' (the pipeline stops between units of work) and
a job left 'running' by a killed worker is re-queued at the next start. Nothing is lost because every pipeline
stage is idempotent.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from typing import Any, Callable, Dict, Optional

from src import jobs
from src.database import SessionLocal, save_forensics
from src.db.models import Ad
from src.pipeline import Pipeline, RunOptions

logger = logging.getLogger("Jinora.Worker")


class Worker:
    def __init__(
        self,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        pipeline_factory: Optional[Callable[..., Pipeline]] = None,
        forensics_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
    ) -> None:
        self.poll_seconds = poll_seconds
        self._sleep = sleep
        self._pipeline_factory = pipeline_factory or (lambda stop_check: Pipeline(stop_check=stop_check))
        self._forensics_fn = forensics_fn
        self._stop = False

    def request_stop(self) -> None:
        self._stop = True

    def should_stop(self) -> bool:
        return self._stop

    # ---- handlers ----

    def handle(self, job: Dict[str, Any]) -> Dict[str, Any]:
        payload = job["payload"] or {}
        if job["type"] == "run":
            return self._pipeline_factory(self.should_stop).run(RunOptions.from_dict(payload))
        if job["type"] == "evaluate":
            opts = RunOptions.from_dict({**payload, "stages": ["evaluate", "signals"], "eval_limit": payload.get("limit")})
            return self._pipeline_factory(self.should_stop).run(opts)
        if job["type"] == "forensics":
            return self._forensics(job["ad_id"])
        raise ValueError(f"Tipo de job desconocido: {job['type']}")

    def _forensics(self, ad_id: str) -> Dict[str, Any]:
        from src.config import settings

        with SessionLocal() as db:
            ad = db.get(Ad, ad_id)
            video = settings.downloads_path / ad.video_path if ad and ad.video_path else None
            if video is None or not video.exists():
                raise FileNotFoundError("Video local no disponible para este anuncio")
            fn = self._forensics_fn
            if fn is None:
                from src.video_forensics import VideoForensicsEngine

                fn = VideoForensicsEngine("base").analyze_video
            report = fn(video)
            save_forensics(db, ad, report)
            db.commit()
        return {"ad_id": ad_id, "duration_seconds": report.get("duration_seconds"), "transcript_chars": len(report.get("full_transcript") or "")}

    # ---- loop ----

    def run_once(self) -> bool:
        """Processes the oldest pending job. Returns False when there was nothing to do."""
        job = jobs.claim_next()
        if job is None:
            return False
        logger.info(f"Job {job['id']} ({job['type']}) iniciado")
        try:
            result = self.handle(job)
        except Exception as exc:
            logger.error(f"Job {job['id']} falló: {exc}")
            jobs.fail(job["id"], str(exc))
        else:
            if result.get("interrupted"):
                logger.warning(f"Job {job['id']} detenido a mitad: vuelve a la cola para reanudarse")
                jobs.release(job["id"], result)
            else:
                jobs.complete(job["id"], result)
        return True

    def run_forever(self, drain_and_exit: bool = False) -> None:
        requeued = jobs.requeue_running()
        if requeued:
            logger.warning(f"{requeued} job(s) quedaron 'running' de una ejecución anterior y vuelven a la cola")
        while not self._stop:
            if not self.run_once():
                if drain_and_exit:
                    break
                self._sleep(self.poll_seconds)


def main(argv: Optional[list] = None) -> int:
    from src.logging_setup import setup_logging

    parser = argparse.ArgumentParser(description="Worker de la cola de trabajos de Jinora")
    parser.add_argument("--once", action="store_true", help="Procesa los jobs pendientes y termina")
    parser.add_argument("--poll", type=float, default=2.0, help="Segundos entre consultas a la cola cuando está vacía")
    args = parser.parse_args(argv)

    setup_logging()
    worker = Worker(poll_seconds=args.poll)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.request_stop())
    logger.info("Worker iniciado (Ctrl+C para detener sin perder jobs)")
    worker.run_forever(drain_and_exit=args.once)
    logger.info("Worker detenido")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
