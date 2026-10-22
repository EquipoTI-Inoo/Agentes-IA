"""
Jinora web dashboard and API (Phase 8).

The dashboard only READS what is stored in the database (creative_score, market_score, jinora_score and the saved
evaluation); it never recalculates a score and never calls an evaluator or an LLM. The only writes are queued jobs
(the worker, a separate process, does the work). Ad text is third-party content: the API returns it as plain JSON data,
the UI renders it with textContent, and every response carries a strict Content-Security-Policy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import dashboard_data, jobs
from src.config import settings
from src.database import SessionLocal
from src.db.models import EXCLUDED_FROM_QUEUE, Ad
from src.queue_status import build_queue_status
from src.work_queue import has_scored_analysis

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="Jinora Dashboard API", version="3.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/", include_in_schema=False)
async def dashboard_page():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


# --------------------------------------------------------------------------------------
# Read-only API
# --------------------------------------------------------------------------------------

@app.get("/api/ads/top")
def api_top_ads(
    pillar: Optional[str] = None,
    country: Optional[str] = None,
    competitor: Optional[str] = None,
    format: Optional[str] = None,
    language: Optional[str] = None,
    has_voiceover: Optional[bool] = None,
    status: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    unevaluated_limit: int = Query(50, ge=0, le=200),
) -> Dict[str, Any]:
    """Ranking by jinora_score, one row per creative group ("N variantes"); unevaluated ads are listed apart."""
    with SessionLocal() as db:
        try:
            filters = dashboard_data.parse_filters(db, pillar, country, competitor, format, language, has_voiceover, status)
        except dashboard_data.FilterError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return dashboard_data.top_ads(db, filters, limit=limit, offset=offset, unevaluated_limit=unevaluated_limit)


@app.get("/api/filters")
def api_filters() -> Dict[str, Any]:
    with SessionLocal() as db:
        return dashboard_data.facets(db)


@app.get("/api/ads/{ad_id}")
def api_ad_detail(ad_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        detail = dashboard_data.ad_detail(db, ad_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Anuncio no encontrado")
    return detail


@app.get("/api/competitors")
def api_competitors(include_empty: bool = False) -> List[Dict[str, Any]]:
    with SessionLocal() as db:
        return dashboard_data.competitors_overview(db, include_empty=include_empty)


@app.get("/api/queue/status")
def api_queue_status() -> Dict[str, Any]:
    """Same numbers as `python main.py queue --status`, plus the recent jobs."""
    return build_queue_status()


# --------------------------------------------------------------------------------------
# Media with HTTP Range support (so the player can jump to a second)
# --------------------------------------------------------------------------------------

MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
CHUNK = 64 * 1024


def safe_media_path(rel_path: str) -> Optional[Path]:
    """Resolves rel_path under DOWNLOADS_DIR; None if it escapes it (path traversal), is missing or not a media file."""
    base = settings.downloads_path.resolve()
    try:
        candidate = (base / rel_path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(base) or not candidate.is_file() or candidate.suffix.lower() not in MEDIA_TYPES:
        return None
    return candidate


def parse_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """'bytes=a-b' | 'bytes=a-' | 'bytes=-n' -> inclusive (start, end); None if invalid or unsatisfiable."""
    if not header.startswith("bytes=") or "," in header:
        return None
    start_s, _, end_s = header[6:].strip().partition("-")
    try:
        if start_s == "":
            n = int(end_s)
            if n <= 0:
                return None
            start, end = max(0, size - n), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    end = min(end, size - 1)
    return (start, end) if 0 <= start <= end < size else None


def _iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/api/media/{ad_id}")
def api_media(ad_id: str, request: Request, kind: Optional[str] = Query(None, pattern="^(video|image)$")):
    with SessionLocal() as db:
        ad = db.get(Ad, ad_id)
        if ad is None:
            raise HTTPException(status_code=404, detail="Anuncio no encontrado")
        wanted = [kind] if kind else ["video", "image"]
        rel = next((m.rel_path for k in wanted for m in ad.media if m.kind == k), None)
    if rel is None:
        raise HTTPException(status_code=404, detail="El anuncio no tiene ese archivo")
    path = safe_media_path(rel)
    if path is None:
        raise HTTPException(status_code=404, detail="Archivo no disponible")

    size = path.stat().st_size
    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"}
    media_type = MEDIA_TYPES[path.suffix.lower()]
    range_header = request.headers.get("range")
    if not range_header:
        return StreamingResponse(_iter_file(path, 0, size - 1), media_type=media_type, headers={**base_headers, "Content-Length": str(size)})
    parsed = parse_range(range_header, size)
    if parsed is None:
        return Response(status_code=416, headers={**base_headers, "Content-Range": f"bytes */{size}"})
    start, end = parsed
    return StreamingResponse(
        _iter_file(path, start, end), status_code=206, media_type=media_type,
        headers={**base_headers, "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)},
    )


# --------------------------------------------------------------------------------------
# Writes: only enqueue jobs (the worker does the work)
# --------------------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    country: str = "US"
    limit: int = 5
    forensics: bool = True


class EvaluateRequest(BaseModel):
    ad_ids: Optional[List[str]] = None
    force: bool = False
    prompt_version: Optional[str] = None
    limit: Optional[int] = None


@app.post("/api/search")
async def execute_search(req: SearchRequest):
    """Queue a pipeline run (discover -> ... -> signals) for a brand/keyword. Returns the job_id immediately."""
    payload = {"query": req.query, "countries": [req.country], "limit": req.limit, "forensics": req.forensics, "media_filter": "video"}
    return {"status": "queued", "job_id": jobs.enqueue("run", payload)}


@app.post("/api/forensics/{ad_id}")
async def run_forensics_on_demand(ad_id: str):
    """Queue local Whisper transcription for an already downloaded ad."""
    with SessionLocal() as db:
        ad = db.get(Ad, ad_id)
        video = settings.downloads_path / ad.video_path if ad and ad.video_path else None
        if not video or not video.exists():
            raise HTTPException(status_code=404, detail="Video local no disponible para este anuncio")
    return {"status": "queued", "job_id": jobs.enqueue("forensics", ad_id=ad_id)}


@app.post("/api/evaluate")
async def enqueue_evaluation(req: EvaluateRequest):
    """Queue an evaluation of the pending queue (or of specific ads). The worker respects quota and budget."""
    return {"status": "queued", "job_id": jobs.enqueue("evaluate", req.model_dump(exclude_none=True))}


@app.post("/api/evaluate/{ad_id}")
async def enqueue_ad_evaluation(ad_id: str):
    """"Encolar análisis" button: queues ONE evaluation job for this ad and returns its job_id. Evaluates nothing now."""
    with SessionLocal() as db:
        ad = db.get(Ad, ad_id)
        if ad is None:
            raise HTTPException(status_code=404, detail="Anuncio no encontrado")
        if ad.status in EXCLUDED_FROM_QUEUE:
            raise HTTPException(status_code=409, detail=f"El anuncio está {ad.status} y no se evalúa")
        if has_scored_analysis(db, ad_id):
            raise HTTPException(status_code=409, detail="El anuncio ya tiene evaluación")
    existing = jobs.pending_jobs("evaluate", [ad_id])
    job_id = existing[0]["id"] if existing else jobs.enqueue("evaluate", {"ad_ids": [ad_id]}, ad_id=ad_id)
    return {"status": "queued", "job_id": job_id, "already_queued": bool(existing)}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: int):
    """State of a queued job: pending | running | done | failed (+ result / error)."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job


@app.get("/api/jobs")
async def list_recent_jobs(status: Optional[str] = None, limit: int = 50):
    return jobs.list_jobs(status=status, limit=min(max(limit, 1), 200))
