"""
FastAPI Production Backend & Dashboard for Meta Ads Intelligence
Serves the web dashboard, API endpoints, and streams local media.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sqlalchemy.orm import selectinload

from src import jobs
from src.database import AdRecord, SessionLocal, get_ads_from_db
from src.evaluator import evaluate_ad_cognitively
from src.pillars import PILLARS, RAW_DB
from src.relevance import is_ad_relevant

BASE_DIR = Path(__file__).parent.parent.parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Jinora Meta Ads Intelligence API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount media downloads directory for streaming
app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def to_web_url(path_str: Optional[str]) -> Optional[str]:
    """Safely convert any absolute or relative path to a web accessible /downloads/ URL."""
    if not path_str:
        return None
    try:
        downloads_resolved = DOWNLOADS_DIR.resolve()
        p = Path(path_str)

        if not p.is_absolute():
            # Check if relative to DOWNLOADS_DIR first (standard format)
            candidate = (DOWNLOADS_DIR / p).resolve()
            if candidate.exists() and candidate.is_relative_to(downloads_resolved):
                rel = candidate.relative_to(downloads_resolved)
                return f"/downloads/{rel.as_posix()}"

            # Or if relative to BASE_DIR (e.g. 'downloads/Brand/...')
            candidate_base = (BASE_DIR / p).resolve()
            if candidate_base.exists() and candidate_base.is_relative_to(downloads_resolved):
                rel = candidate_base.relative_to(downloads_resolved)
                return f"/downloads/{rel.as_posix()}"
        else:
            p_resolved = p.resolve()
            if p_resolved.exists() and p_resolved.is_relative_to(downloads_resolved):
                rel = p_resolved.relative_to(downloads_resolved)
                return f"/downloads/{rel.as_posix()}"
    except Exception:
        pass
    return None


class SearchRequest(BaseModel):
    query: str
    country: str = "US"
    limit: int = 5
    forensics: bool = True


@app.get("/", response_class=HTMLResponse)
async def dashboard_view(request: Request):
    """Render the main executive dashboard."""
    db = SessionLocal()
    try:
        raw_ads = (
            db.query(AdRecord)
            .options(selectinload(AdRecord.media), selectinload(AdRecord.analyses), selectinload(AdRecord.media_info))
            .order_by(AdRecord.days_active.desc())
            .all()
        )

        total_ads = len(raw_ads)
        winning_ads_count = sum(1 for a in raw_ads if a.days_active >= 30)
        videos_count = sum(1 for a in raw_ads if a.has_video and a.video_path)
        audited_count = sum(1 for a in raw_ads if a.full_transcript)

        formatted_ads = []
        for ad in raw_ads:
            vid_url = to_web_url(ad.video_path)
            img_url = to_web_url(ad.image_path)

            # Fallback for poster if image is missing but frame exists
            if not img_url and ad.video_path:
                try:
                    v_p = Path(ad.video_path)
                    if not v_p.is_absolute():
                        v_p = (BASE_DIR / v_p).resolve()
                    frame_1 = v_p.parent / "frames" / "frame_0001.jpg"
                    if frame_1.exists():
                        img_url = to_web_url(str(frame_1))
                except Exception:
                    pass

            # Pre-evaluate cognitive score
            eval_data = evaluate_ad_cognitively({
                "days_active": ad.days_active,
                "has_video": ad.has_video,
                "full_transcript": ad.full_transcript,
                "headline": ad.headline,
                "body_text": ad.body_text,
                "link_url": ad.link_url,
            })

            formatted_ads.append({
                "id": ad.id,
                "page_name": ad.page_name,
                "days_active": ad.days_active,
                "headline": ad.headline,
                "body_text": ad.body_text,
                "cta_text": ad.cta_text,
                "link_url": ad.link_url,
                "has_video": ad.has_video,
                "video_url": vid_url,
                "image_url": img_url,
                "search_query": ad.search_query,
                "full_transcript": ad.full_transcript,
                "duration_seconds": ad.duration_seconds,
                "total_frames": ad.total_frames,
                "cognitive_score": eval_data["total_score"],
                "evaluation": eval_data,
            })

        # Calculate Top 10 Champions (Highest longevity & best performing ads)
        # Slices strictly the top 10 winning ads across all tracked competitors
        champions = sorted(
            formatted_ads,
            key=lambda x: (x.get("days_active", 0), 1 if x.get("has_video") else 0),
            reverse=True,
        )[:10]

        # Attach rank 1 to 10
        for rank, c in enumerate(champions, 1):
            c["rank"] = rank

        companies = sorted(list({a["page_name"] for a in formatted_ads if a["page_name"]}))

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "ads": formatted_ads,
                "champions": champions,
                "companies": companies,
                "total_ads": total_ads,
                "winning_ads_count": winning_ads_count,
                "videos_count": videos_count,
                "audited_count": audited_count,
            },
        )
    finally:
        db.close()


@app.get("/api/ads")
async def list_ads(pillar: Optional[str] = None, min_days: int = 0, limit: int = 50):
    """API endpoint to list ads as JSON."""
    ads = get_ads_from_db(pillar=pillar, min_days=min_days, limit=limit)
    return [
        {
            "id": a.id,
            "page_name": a.page_name,
            "days_active": a.days_active,
            "headline": a.headline,
            "body_text": a.body_text,
            "cta_text": a.cta_text,
            "link_url": a.link_url,
            "has_video": a.has_video,
            "video_path": a.video_path,
            "full_transcript": a.full_transcript,
        }
        for a in ads
    ]


@app.get("/api/ads/{ad_id}")
async def get_ad_detail(ad_id: str):
    """API endpoint to get full details and timeline of a single ad."""
    db = SessionLocal()
    try:
        ad = db.query(AdRecord).filter(AdRecord.id == ad_id).first()
        if not ad:
            raise HTTPException(status_code=404, detail="Ad no encontrado")

        eval_data = evaluate_ad_cognitively({
            "days_active": ad.days_active,
            "has_video": ad.has_video,
            "full_transcript": ad.full_transcript,
            "headline": ad.headline,
            "body_text": ad.body_text,
            "link_url": ad.link_url,
        })

        return {
            "id": ad.id,
            "page_name": ad.page_name,
            "days_active": ad.days_active,
            "headline": ad.headline,
            "body_text": ad.body_text,
            "cta_text": ad.cta_text,
            "link_url": ad.link_url,
            "has_video": ad.has_video,
            "video_url": to_web_url(ad.video_path),
            "image_url": to_web_url(ad.image_path),
            "full_transcript": ad.full_transcript,
            "transcript_timeline": ad.transcript_timeline,
            "duration_seconds": ad.duration_seconds,
            "total_frames": ad.total_frames,
            "evaluation": eval_data,
        }
    finally:
        db.close()


@app.get("/api/competitors")
async def get_competitors():
    """Returns the flattened list of 105 global mapped competitors."""
    flat_list = []
    for pkey, pdata in RAW_DB.get("pillars", {}).items():
        for comp in pdata.get("competitors", []):
            flat_list.append({
                "name": comp.get("name"),
                "website": comp.get("website"),
                "region": comp.get("region"),
                "focus": comp.get("focus"),
                "pillar": pkey,
            })
    return flat_list


@app.post("/api/search")
async def execute_search(req: SearchRequest):
    """Queue a pipeline run (discover -> ... -> signals) for a brand/keyword. Returns the job_id immediately."""
    payload = {
        "query": req.query, "countries": [req.country], "limit": req.limit, "forensics": req.forensics,
        "media_filter": "video",
    }
    return {"status": "queued", "job_id": jobs.enqueue("run", payload)}


@app.post("/api/forensics/{ad_id}")
async def run_forensics_on_demand(ad_id: str):
    """Queue local Whisper transcription for an already downloaded ad."""
    db = SessionLocal()
    try:
        ad = db.get(AdRecord, ad_id)
        video_file = DOWNLOADS_DIR / ad.video_path if ad and ad.video_path else None
        if not video_file or not video_file.exists():
            raise HTTPException(status_code=404, detail="Video local no disponible para este anuncio")
    finally:
        db.close()
    return {"status": "queued", "job_id": jobs.enqueue("forensics", ad_id=ad_id)}


class EvaluateRequest(BaseModel):
    ad_ids: Optional[List[str]] = None
    force: bool = False
    prompt_version: Optional[str] = None
    limit: Optional[int] = None


@app.post("/api/evaluate")
async def enqueue_evaluation(req: EvaluateRequest):
    """Queue an evaluation of the pending queue (or of specific ads). The worker respects quota and budget."""
    return {"status": "queued", "job_id": jobs.enqueue("evaluate", req.model_dump(exclude_none=True))}


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
