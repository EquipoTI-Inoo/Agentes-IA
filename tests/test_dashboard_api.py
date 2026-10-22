"""
Phase 8: read-only dashboard API. Temporary DB, fakes, 0 external APIs.
Ranking grouped by creative, exclusions, filters, detail, competitors, media (Range + path traversal), enqueue-only POST,
HTML-safety and read-only guarantees.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pipeline_helpers import add_ad, add_evaluated_ad, seed_competitors, video_result

from src import dashboard_data, jobs
from src.database import SessionLocal
from src.db.models import Ad, Analysis, Job, MarketSignal, STATUS_OUT_OF_CATALOG, STATUS_REJECTED
from src.web.app import app, parse_range, safe_media_path

client = TestClient(app)


@pytest.fixture
def world(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.settings.DOWNLOADS_DIR", str(tmp_path))
    return seed_competitors(), tmp_path


def seed_ranking(comps, d):
    alpha, beta = comps["Alpha CRM"], comps["Beta Chat"]
    add_evaluated_ad("g1_best", d, alpha, jinora=82.0, group="grp1", days=60)
    add_evaluated_ad("g1_other", d, alpha, jinora=70.0, group="grp1", days=20)  # variant of the same creative
    add_evaluated_ad("solo_mid", d, alpha, jinora=75.5, result=video_result(fmt="demo_pantalla", language="en", voice=False, focus=("agendamiento",), ai="central"))
    add_evaluated_ad("beta_low", d, beta, jinora=40.0, pillar="conversacional", page_name="Beta Chat", countries=("MX",),
                     result=video_result(fmt="talking_head", focus=("mensajes_masivos_campanas",), ai="mencionada"))
    add_evaluated_ad("no_jinora", d, alpha, jinora=None, market=None)  # has creative_score, no jinora_score yet
    add_evaluated_ad("rej", d, alpha, jinora=99.0, status=STATUS_REJECTED)
    add_evaluated_ad("ooc", d, None, jinora=98.0, status=STATUS_OUT_OF_CATALOG)
    add_ad("pending_ad", d, alpha, days=500)  # not evaluated, file on disk
    add_ad("pending_nofile", d, alpha, with_media=False)


def top(**params):
    r = client.get("/api/ads/top", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- /api/ads/top

def test_top_groups_by_creative_excludes_hidden_and_lists_unevaluated_apart(world):
    seed_ranking(*world)
    data = top()
    ids = [r["ad_id"] for r in data["items"]]
    assert ids == ["g1_best", "solo_mid", "beta_low", "no_jinora"]  # jinora desc; no jinora_score last; rej/ooc/pending absent
    best = data["items"][0]
    assert best["variants"] == 2 and best["group_id"] == "grp1"  # ONE row for the creative, "N variantes"
    assert [r["variants"] for r in data["items"][1:]] == [1, 1, 1]
    assert data["total"] == 4
    assert data["items"][-1]["jinora_score"] is None and data["items"][-1]["creative_score"] is not None  # never computed on the fly

    unevaluated = {r["ad_id"]: r for r in data["unevaluated"]["items"]}
    assert set(unevaluated) == {"pending_ad", "pending_nofile"} and data["unevaluated"]["total"] == 2
    assert unevaluated["pending_ad"]["can_enqueue"] is True
    assert unevaluated["pending_nofile"]["can_enqueue"] is False and "sin archivo" in unevaluated["pending_nofile"]["enqueue_reason"]
    assert all(r["jinora_score"] is None for r in unevaluated.values())


def test_row_carries_stored_scores_and_descriptive_facts(world):
    seed_ranking(*world)
    row = top()["items"][0]
    assert (row["jinora_score"], row["creative_score"], row["market_score"]) == (82.0, 68, 60.0)
    assert row["format"] == "ugc" and row["language"] == "es" and row["has_voiceover"] is True
    assert row["feature_focus"] == ["automatizacion"] and row["ai_mention"] == "ninguna" and row["rubric_outdated"] is False
    assert row["thumbnail_url"] == "/api/media/g1_best?kind=image" and row["competitor"] == "Alpha CRM"


def test_valid_filters(world):
    seed_ranking(*world)
    assert [r["ad_id"] for r in top(pillar="conversacional")["items"]] == ["beta_low"]
    assert [r["ad_id"] for r in top(country="mx")["items"]] == ["beta_low"]  # case-insensitive ISO code
    assert {r["ad_id"] for r in top(competitor="alpha crm")["items"]} == {"g1_best", "solo_mid", "no_jinora"}
    assert [r["ad_id"] for r in top(format="demo_pantalla")["items"]] == ["solo_mid"]
    assert [r["ad_id"] for r in top(language="en")["items"]] == ["solo_mid"]
    assert [r["ad_id"] for r in top(has_voiceover="false")["items"]] == ["solo_mid"]
    assert {r["ad_id"] for r in top(has_voiceover="true")["items"]} == {"g1_best", "beta_low", "no_jinora"}
    assert [r["ad_id"] for r in top(pillar="crm", format="ugc", language="es", has_voiceover="true", country="PE")["items"]][:1] == ["g1_best"]
    assert [r["ad_id"] for r in top(status="evaluated")["items"]][0] == "g1_best"
    only_one = top(limit=1, offset=1)
    assert [r["ad_id"] for r in only_one["items"]] == ["solo_mid"] and only_one["total"] == 4
    # filters describing an evaluation hide the unevaluated list (they cannot match it)
    assert top(format="ugc")["unevaluated"]["total"] == 0 and top(pillar="crm")["unevaluated"]["total"] == 2


@pytest.mark.parametrize("params", [
    {"pillar": "ventas"}, {"country": "PERU"}, {"country": "P1"}, {"format": "gif"}, {"language": "ESPAÑOL!"}, {"language": "e"},
    {"status": "rejected"}, {"status": "out_of_catalog"}, {"status": "'; DROP TABLE ads;--"}, {"competitor": "No Such Co"},
    {"has_voiceover": "maybe"}, {"limit": 0}, {"limit": 5000}, {"offset": -1},
])
def test_invalid_filters_are_rejected_with_422(world, params):
    seed_ranking(*world)
    r = client.get("/api/ads/top", params=params)
    assert r.status_code == 422


def test_empty_database_gives_empty_lists_not_errors(clean_db):
    data = client.get("/api/ads/top").json()
    assert data["items"] == [] and data["total"] == 0 and data["unevaluated"]["total"] == 0
    assert client.get("/api/competitors").json() == [] and client.get("/api/filters").json()["pillars"] == ["voz", "conversacional", "crm"]


def test_filter_whitelists_match_the_evaluation_schemas():
    from typing import get_args

    from src.ai.schemas import EvaluationResult, StaticEvaluationResult

    assert set(get_args(EvaluationResult.model_fields["format"].annotation)) == set(dashboard_data.VIDEO_FORMATS)
    assert set(get_args(StaticEvaluationResult.model_fields["visual_style"].annotation)) == set(dashboard_data.STATIC_STYLES)
    assert set(get_args(EvaluationResult.model_fields["ai_mention"].annotation)) == set(dashboard_data.AI_MENTION)


# ---------------------------------------------------------------- /api/ads/{id}

def test_detail_has_everything_the_panel_needs(world):
    comps, d = world
    add_evaluated_ad("det1", d, comps["Alpha CRM"], group="grpD", jinora=77.0, market=61.5, headline="Titular", body="Cuerpo largo", link="https://ejemplo.test/oferta", duration=33.0)
    add_evaluated_ad("det2", d, comps["Alpha CRM"], group="grpD")
    r = client.get("/api/ads/det1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    ev = body["evaluation"]
    assert [x["key"] for x in ev["dimensions"]] == ["hook", "mechanism", "narrative", "offer"]
    hook = ev["dimensions"][0]
    assert (hook["score"], hook["max"], hook["anchor_level"], hook["reasoning"]) == (20, 25, "13-19", "razón hook")
    assert hook["evidence"][0] == {"timestamp": "00:02-00:05", "seconds": 2, "source": "audio", "quote": "cita hook"}  # clickable: start of the range
    assert ev["value_proposition"] == "propuesta de prueba" and ev["feature_focus"] == ["automatizacion"] and ev["ai_mention"] == "ninguna"
    assert (ev["format"], ev["language"], ev["has_voiceover"]) == ("ugc", "es", True)
    assert ev["transcript"][0]["text"] == "hola equipo" and ev["hook_description"] == "gancho de prueba"
    assert (ev["what_to_model"], ev["what_to_beat"], ev["cosmiq_adaptation_idea"]) == ("copiar esto", "superar esto", "idea de prueba")
    assert (ev["model"], ev["prompt_version"], ev["rubric_version"], ev["rubric_outdated"]) == ("fake-model", "v1", ev["rubric_current"], False)
    assert body["market"]["market_score"] == 61.5 and body["market"]["jinora_score"] == 77.0
    assert body["market"]["market_components"]["used"]["longevity"]["effective_weight"] == 0.4 and body["market"]["market_components"]["missing"] == ["countries"]
    assert {v["ad_id"] for v in body["variants"]} == {"det1", "det2"} and [v["ad_id"] for v in body["variants"] if v["is_current"]] == ["det1"]
    assert body["media"] == {"video_url": "/api/media/det1?kind=video", "image_url": "/api/media/det1?kind=image"}
    assert body["ad"]["duration_s"] == 33.0 and body["ad"]["link_url"] == "https://ejemplo.test/oferta" and body["can_enqueue"] is False


def test_detail_of_unevaluated_ad_and_unknown_ad(world):
    comps, d = world
    add_ad("noeval", d, comps["Alpha CRM"])
    body = client.get("/api/ads/noeval").json()
    assert body["evaluation"] is None and body["can_enqueue"] is True and body["market"]["jinora_score"] is None
    assert client.get("/api/ads/nope").status_code == 404


def test_previous_rubric_is_labelled(world):
    comps, d = world
    add_evaluated_ad("old", d, comps["Alpha CRM"], rubric="0.9")
    add_evaluated_ad("new", d, comps["Alpha CRM"], jinora=10.0)
    rows = {r["ad_id"]: r for r in top()["items"]}
    assert rows["old"]["rubric_outdated"] is True and rows["new"]["rubric_outdated"] is False
    ev = client.get("/api/ads/old").json()["evaluation"]
    assert ev["rubric_outdated"] is True and ev["rubric_version"] == "0.9" and ev["rubric_current"] not in (None, "0.9")


@pytest.mark.parametrize("value,expected", [
    ("00:02-00:05", 2), ("00:02", 2), ("01:05", 65), ("0:07 - 0:09", 7), ("00:02–00:05", 2), ("1:02:03", 3723), ("12:30-13:00", 750),
    (None, None), ("", None), ("sin tiempo", None),
])
def test_timestamp_start_seconds(value, expected):
    assert dashboard_data.parse_timestamp_start(value) == expected


def test_static_ad_detail_uses_static_dimensions(world):
    comps, d = world
    result = {"stop_power": {"score": 15, "anchor_level": "13-19", "reasoning": "r", "evidence": [{"card": None, "zone": "titular", "quote": "q"}]},
              "message_clarity": {"score": 14, "anchor_level": "13-19", "reasoning": "r", "evidence": [{"card": 2, "zone": "cta", "quote": "q2"}]},
              "proof": {"score": 10, "anchor_level": "7-12", "reasoning": "r", "evidence": [{"card": None, "zone": "centro", "quote": "q"}]},
              "offer_cta": {"score": 12, "anchor_level": "7-12", "reasoning": "r", "evidence": [{"card": None, "zone": "cta", "quote": "q"}]},
              "value_proposition": "vp", "feature_focus": ["gestion_leads_crm"], "ai_mention": "ninguna", "visual_style": "meme",
              "text_in_image": "texto", "main_promise": "promesa", "creative_score": 51}
    add_evaluated_ad("stat1", d, comps["Alpha CRM"], result=result, video=False, a_type="static", jinora=50.0)
    ev = client.get("/api/ads/stat1").json()["evaluation"]
    assert ev["type"] == "static" and [x["key"] for x in ev["dimensions"]] == ["stop_power", "message_clarity", "proof", "offer_cta"]
    assert ev["format"] == "meme" and ev["has_voiceover"] is False and ev["dimensions"][1]["evidence"][0]["source"] == "tarjeta 2 · cta"
    assert ev["dimensions"][0]["evidence"][0]["seconds"] is None  # statics have no timeline
    assert [r["format"] for r in top(format="meme")["items"]] == ["meme"]


# ---------------------------------------------------------------- /api/competitors

def test_competitors_overview(world):
    comps, d = world
    alpha = comps["Alpha CRM"]
    add_evaluated_ad("a1", d, alpha, jinora=80.0, start=datetime(2026, 8, 5), result=video_result(scores=(20, 20, 20, 20), focus=("automatizacion", "agendamiento")))
    add_evaluated_ad("a2", d, alpha, jinora=60.0, start=datetime(2026, 8, 20), result=video_result(scores=(10, 10, 10, 10), focus=("automatizacion",), fmt="mixto", ai="central"))
    add_evaluated_ad("a3", d, alpha, jinora=70.0, start=datetime(2026, 9, 2), active=False, result=video_result(scores=(15, 15, 15, 15), focus=("agendamiento",)))
    add_ad("a4", d, alpha, days=5)  # pending
    add_ad("a_rej", d, alpha, status=STATUS_REJECTED)  # excluded from every count
    data = {c["name"]: c for c in client.get("/api/competitors").json()}
    assert set(data) == {"Alpha CRM"}  # Beta Chat has no ads: hidden unless include_empty
    a = data["Alpha CRM"]
    assert (a["ads_total"], a["active_ads"], a["evaluated"], a["pending"]) == (4, 3, 3, 1)
    assert a["avg_creative_score"] == 60.0 and a["avg_jinora_score"] == 70.0  # means of the STORED scores
    assert {x["tag"]: x["count"] for x in a["recurring_angles"]} == {"automatizacion": 2, "agendamiento": 2}
    assert a["creative_formats"] == {"ugc": 2, "mixto": 1} and a["ai_mention"] == {"ninguna": 2, "central": 1}
    assert a["media_formats"]["video"] == 4
    months = {m["month"]: m for m in a["timeline"]}
    assert months["2026-08"]["ads"] == 2 and months["2026-08"]["evaluated"] == 2 and months["2026-08"]["avg_creative_score"] == 60.0  # a1 (80) + a2 (40)
    assert months["2026-09"]["ads"] == 1 and months["sin fecha"]["ads"] == 1  # a3 ; the pending ad has no start date
    assert [m["month"] for m in a["timeline"]] == ["2026-08", "2026-09", "sin fecha"]
    assert [c["name"] for c in client.get("/api/competitors", params={"include_empty": "true"}).json()] == ["Alpha CRM", "Beta Chat"]


# ---------------------------------------------------------------- /api/media (Range, traversal)

def media_url(ad_id, kind="video"):
    return f"/api/media/{ad_id}?kind={kind}"


def test_media_full_and_range_responses(world):
    comps, d = world
    add_evaluated_ad("m1", d, comps["Alpha CRM"])
    full = client.get(media_url("m1"))
    assert full.status_code == 200 and len(full.content) == 100 and full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"

    part = client.get(media_url("m1"), headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == b"0123456789" and part.headers["content-range"] == "bytes 10-19/100" and part.headers["content-length"] == "10"
    assert client.get(media_url("m1"), headers={"Range": "bytes=95-"}).headers["content-range"] == "bytes 95-99/100"
    suffix = client.get(media_url("m1"), headers={"Range": "bytes=-5"})
    assert suffix.status_code == 206 and suffix.headers["content-range"] == "bytes 95-99/100" and len(suffix.content) == 5
    assert client.get(media_url("m1"), headers={"Range": "bytes=0-999"}).headers["content-range"] == "bytes 0-99/100"  # end is clamped

    for bad in ("bytes=200-300", "bytes=abc", "items=0-5", "bytes=5-2", "bytes=0-1,5-6", "bytes=-0"):
        assert client.get(media_url("m1"), headers={"Range": bad}).status_code == 416, bad
    assert client.get(media_url("m1"), headers={"Range": "bytes=200-300"}).headers["content-range"] == "bytes */100"

    img = client.get(media_url("m1", "image"))
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    default = client.get("/api/media/m1")  # no kind: the video when there is one
    assert default.headers["content-type"] == "video/mp4"


def test_media_unknown_ad_kind_or_file(world):
    comps, d = world
    add_evaluated_ad("m2", d, comps["Alpha CRM"], video=False)  # image only
    assert client.get("/api/media/nope").status_code == 404
    assert client.get(media_url("m2", "video")).status_code == 404
    assert client.get("/api/media/m2?kind=audio").status_code == 422
    (d / "Alpha CRM" / "m2" / "image_0.jpg").unlink()
    assert client.get(media_url("m2", "image")).status_code == 404  # row exists, file is gone


def test_media_blocks_path_traversal(world, tmp_path):
    comps, d = world
    secret = d.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    add_evaluated_ad("evil", d, comps["Alpha CRM"])
    with SessionLocal() as s:  # a poisoned Media row must not be able to read outside DOWNLOADS_DIR
        ad = s.get(Ad, "evil")
        ad.media[0].rel_path = "../secret.txt"
        ad.media[1].rel_path = str(secret)  # absolute path
        s.commit()
    for kind in ("video", "image"):
        r = client.get(media_url("evil", kind))
        assert r.status_code == 404 and b"TOP SECRET" not in r.content
    for ad_id in ("..%2F..%2Fsecret.txt", "../secret.txt", "%2e%2e%2fsecret.txt"):
        assert client.get(f"/api/media/{ad_id}").status_code in (404, 422)

    assert safe_media_path("../secret.txt") is None and safe_media_path(str(secret)) is None
    assert safe_media_path("Alpha CRM/evil/video_0.mp4") is not None
    (d / "notes.txt").write_text("x", encoding="utf-8")
    assert safe_media_path("notes.txt") is None  # only video/image extensions are served
    assert parse_range("bytes=0-9", 100) == (0, 9) and parse_range("bytes=-10", 100) == (90, 99) and parse_range("bytes=9-0", 100) is None


# ---------------------------------------------------------------- POST /api/evaluate/{ad_id}: enqueue only

def test_enqueue_evaluation_creates_a_job_and_calls_no_llm(world, monkeypatch):
    comps, d = world
    add_ad("enq1", d, comps["Alpha CRM"])

    def forbidden(*a, **kw):
        raise AssertionError("the dashboard must not call any LLM")

    monkeypatch.setattr("src.ai.llm_client.GeminiClient.evaluate", forbidden)
    monkeypatch.setattr("src.ai.llm_client.GeminiClient.generate_text", forbidden)
    monkeypatch.setattr("src.ai.claude_client.ClaudeClient.evaluate", forbidden)
    monkeypatch.setattr("src.pipeline.Pipeline.run", forbidden)

    r = client.post("/api/evaluate/enq1")
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["already_queued"] is False
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    assert job["type"] == "evaluate" and job["status"] == "pending" and job["ad_id"] == "enq1" and job["payload"] == {"ad_ids": ["enq1"]}

    again = client.post("/api/evaluate/enq1").json()
    assert again["job_id"] == r.json()["job_id"] and again["already_queued"] is True  # no duplicate jobs
    with SessionLocal() as s:
        assert s.query(Job).filter(Job.type == "evaluate").count() == 1 and s.query(Analysis).count() == 0


def test_enqueue_evaluation_validations(world):
    comps, d = world
    add_ad("rej2", d, comps["Alpha CRM"], status=STATUS_REJECTED)
    add_evaluated_ad("done1", d, comps["Alpha CRM"])
    assert client.post("/api/evaluate/ghost").status_code == 404
    assert client.post("/api/evaluate/rej2").status_code == 409
    assert client.post("/api/evaluate/done1").status_code == 409
    with SessionLocal() as s:
        assert s.query(Job).count() == 0


# ---------------------------------------------------------------- /api/queue/status

def test_queue_status_matches_queue_status_command(world, capsys):
    comps, d = world
    seed_ranking(comps, d)
    body = client.get("/api/queue/status").json()
    assert re.fullmatch(r"\S{8}", body["quota"]["key_fingerprint"])
    assert {"requests_today", "success_today", "failed_today", "rpd", "reserve", "usable"} <= set(body["quota"])
    assert body["totals"]["queue"] == 1 and body["totals"]["queue_video"] == 1 and body["totals"]["evaluated"] == 7  # every ad with a stored analysis, hidden ones included
    assert body["totals"]["rejected"] == 1 and body["totals"]["out_of_catalog"] == 1
    assert body["estimate"]["days_to_clear"] == 1 and "spent_usd" in body["claude"]
    from src import cli

    cli.main(["queue", "--status"])
    out = capsys.readouterr().out
    assert f"En cola (pending_evaluation, con media): {body['totals']['queue']}" in out and body["quota"]["key_fingerprint"] in out


# ---------------------------------------------------------------- safety

def test_ad_text_is_returned_as_data_and_dangerous_links_are_dropped(world):
    comps, d = world
    xss = "<script>alert('xss')</script><img src=x onerror=alert(1)>"
    add_evaluated_ad("xss1", d, comps["Alpha CRM"], headline=xss, body=xss, link="javascript:alert(document.cookie)",
                     result=video_result(value_proposition=xss, what_to_model=xss, transcript_text=xss))
    r = client.get("/api/ads/xss1")
    assert r.headers["content-type"].startswith("application/json") and r.headers["x-content-type-options"] == "nosniff"
    body = r.json()
    assert body["ad"]["headline"] == xss and body["evaluation"]["value_proposition"] == xss  # raw data; the UI inserts it with textContent
    assert body["ad"]["link_url"] is None  # javascript: links never reach the UI
    for scheme in ("data:text/html,<b>x</b>", "vbscript:x", "file:///etc/passwd", "//evil.test", ""):
        assert dashboard_data.safe_http_url(scheme) is None
    assert dashboard_data.safe_http_url("http://ok.test/p?q=1") == "http://ok.test/p?q=1"


def test_page_and_script_follow_the_xss_rules():
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["referrer-policy"] == "no-referrer"
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page.text) and 'on' not in re.findall(r'\s(on\w+)=', page.text)  # no inline scripts / handlers
    assert client.get("/api/ads/top").headers["content-security-policy"] == csp  # also on API responses

    source = Path("src/web/static/dashboard.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", source, flags=re.S)  # the header comment mentions the forbidden sinks
    for sink in (r"\.innerHTML\s*[+]?=", r"\.outerHTML\s*=", r"insertAdjacentHTML", r"document\.write", r"\beval\s*\(", r"new Function"):
        assert not re.search(sink, code), sink
    assert "textContent" in code and "setAttribute('style'" not in code
    assert client.get("/static/dashboard.js").status_code == 200 and client.get("/static/dashboard.css").status_code == 200
    assert client.get("/downloads/anything.mp4").status_code == 404  # the raw downloads mount is gone; media goes through /api/media


def test_dashboard_never_calls_an_evaluator_and_never_writes(world):
    """Imports: the read layer does not touch evaluators/LLM clients. Behavior: GET requests change nothing."""
    for module in ("src/dashboard_data.py", "src/web/app.py", "src/queue_status.py"):
        text = Path(module).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+src\.(evaluator|evaluator_static|ai\.router|ai\.llm_client|ai\.claude_client|pipeline)\b", text, re.M), module
    comps, d = world
    seed_ranking(comps, d)

    def snapshot():
        with SessionLocal() as s:
            return (
                [(a.id, a.status, a.updated_at) for a in s.query(Ad).order_by(Ad.id)],
                [(a.id, a.creative_score) for a in s.query(Analysis).order_by(Analysis.id)],
                [(m.ad_id, m.market_score, m.jinora_score, m.updated_at) for m in s.query(MarketSignal).order_by(MarketSignal.ad_id)],
                s.query(Job).count(),
            )

    before = snapshot()
    for path in ("/api/ads/top", "/api/ads/g1_best", "/api/competitors", "/api/filters", "/api/queue/status", "/api/media/g1_best?kind=video", "/api/jobs", "/"):
        assert client.get(path).status_code == 200, path
    assert snapshot() == before


def test_server_listens_on_localhost_by_default():
    text = Path("start_server.py").read_text(encoding="utf-8")
    assert 'os.getenv("HOST", "127.0.0.1")' in text
    assert "HOST=0.0.0.0" in Path("docker-compose.yml").read_text(encoding="utf-8")  # Docker opts in explicitly
