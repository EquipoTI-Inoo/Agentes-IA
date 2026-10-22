"""A4 (out_of_catalog cleanup) and B2/B4 (CLI commands, queue --status, main.py compatibility). Temporary DB."""

import json
import re
import sys

import pytest
from pipeline_helpers import RUBRIC, add_ad, seed_competitors

from src import cli
from src.database import SessionLocal
from src.db.models import (
    STATUS_EVALUATED, STATUS_OUT_OF_CATALOG, STATUS_PENDING, STATUS_REJECTED, Ad, Analysis, MarketSignal,
)
from src.work_queue import select_queue


@pytest.fixture
def world(clean_db, tmp_path):
    comps = seed_competitors()
    d = tmp_path
    add_ad("c1", d, comps["Alpha CRM"])
    add_ad("c2", d, comps["Beta Chat"], page_name="Beta Chat")
    add_ad("x1", d, None, page_name="AssignX", pillar="crm")
    add_ad("x2", d, None, page_name="AssignX", pillar="crm")
    add_ad("x3", d, None, page_name="Deepgram", pillar="voz")
    add_ad("x4", d, None, page_name="Unfold Tales", pillar="conversacional")
    add_ad("rej", d, None, page_name="Air Wick", status=STATUS_REJECTED)
    add_ad("ooc", d, None, page_name="Old Mark", status=STATUS_OUT_OF_CATALOG)
    with SessionLocal() as s:
        s.get(Ad, "x1").search_query = "ManyChat"
        s.add(Analysis(ad_id="x3", model="m", rubric_version=RUBRIC["video"], analysis_type="video", creative_score=50, result_json="{}"))
        s.commit()
    return d


def statuses():
    with SessionLocal() as s:
        return {a.id: a.status for a in s.query(Ad).all()}


# ---------------------------------------------------------------- A4

def test_cleanup_dry_run_only_lists(world, capsys):
    before = statuses()
    assert cli.main(["cleanup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "Serían marcados como out_of_catalog: 4 anuncios de 3 páginas" in out
    for page in ("AssignX", "Deepgram", "Unfold Tales"):
        assert page in out
    assert "Air Wick" not in out and "Alpha CRM" not in out  # rejected / catalogued ads are not candidates
    assert "con evaluación guardada entre ellos: 1" in out and "ManyChat" in out  # the search that brought them in
    assert statuses() == before  # nothing changed


def test_cleanup_apply_marks_without_deleting_and_is_idempotent(world, capsys):
    total = len(statuses())
    assert cli.main(["cleanup", "--apply"]) == 0
    assert "APLICADO" in capsys.readouterr().out
    st = statuses()
    assert len(st) == total  # no ad deleted
    assert {i for i, s in st.items() if s == STATUS_OUT_OF_CATALOG} == {"x1", "x2", "x3", "x4", "ooc"}
    assert st["rej"] == STATUS_REJECTED and st["c1"] == STATUS_PENDING  # rejected and catalogued ads untouched
    with SessionLocal() as s:
        assert s.query(Analysis).filter(Analysis.ad_id == "x3").count() == 1  # evaluations are kept too

    cli.main(["cleanup", "--apply"])
    assert "0 anuncios de 0 páginas" in capsys.readouterr().out and statuses() == st


def test_queue_excludes_rejected_and_out_of_catalog_after_cleanup(world):
    with SessionLocal() as db:
        assert {a.id for a in select_queue(db, downloads_dir=world)} == {"c1", "c2", "x1", "x2", "x3", "x4"}
    cli.main(["cleanup", "--apply"])
    with SessionLocal() as db:
        assert {a.id for a in select_queue(db, downloads_dir=world)} == {"c1", "c2"}


def test_cleanup_requires_dry_run_or_apply(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["cleanup"])
    assert exc.value.code == 2


# ---------------------------------------------------------------- B2: commands build the right RunOptions

@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake_run(self, opts):
        seen["opts"] = opts
        return {"options": opts.to_dict(), "stages": {}, "interrupted": False, "quota_stopped": False}

    monkeypatch.setattr("src.pipeline.Pipeline.run", fake_run)
    return seen


def test_run_command_maps_flags(captured, capsys):
    assert cli.main(["run", "--pillar", "conversacional", "--countries", "PE,MX,CO", "--limit", "20"]) == 0
    o = captured["opts"]
    assert (o.pillar, o.countries, o.limit) == ("conversacional", ["PE", "MX", "CO"], 20)
    assert tuple(o.stages) == ("discover", "filter", "download", "media", "evaluate", "signals")

    cli.main(["run", "--competitor", "Kommo", "--stages", "download,media,evaluate"])
    o = captured["opts"]
    assert o.competitor == "Kommo" and tuple(o.stages) == ("download", "media", "evaluate")

    cli.main(["run", "--all", "--keywords", "--forensics", "--download-min-days", "15", "--max-ads", "1000", "--media", "video"])
    o = captured["opts"]
    assert (o.all_pillars, o.keywords, o.forensics, o.download_min_days, o.max_ads, o.media_filter) == (True, True, True, 15, 1000, "video")


def test_run_rejects_bad_stage_and_missing_selector(captured, capsys):
    assert cli.main(["run", "--pillar", "crm", "--stages", "download,magic"]) == 2
    assert "Etapas desconocidas" in capsys.readouterr().out


def test_run_enqueue_creates_a_job_instead_of_running(captured, capsys):
    from src import jobs

    assert cli.main(["run", "--pillar", "crm", "--enqueue"]) == 0
    assert "opts" not in captured
    queued = jobs.list_jobs(job_type="run")
    assert len(queued) == 1 and queued[0]["payload"]["pillar"] == "crm" and queued[0]["status"] == "pending"


def test_evaluate_commands(captured, tmp_path, monkeypatch, capsys):
    assert cli.main(["evaluate", "--pending"]) == 0
    o = captured["opts"]
    assert tuple(o.stages) == ("evaluate", "signals") and o.force is False and o.prompt_version is None

    # an unknown prompt version fails early with a clear message, before touching anything
    assert cli.main(["evaluate", "--force", "--prompt-version", "v2"]) == 2
    assert "prompts/evaluator_v2.md" in capsys.readouterr().out

    monkeypatch.setattr("src.ai.prompts.PROMPTS_DIR", tmp_path)
    (tmp_path / "evaluator_v2.md").write_text("p", encoding="utf-8")
    (tmp_path / "evaluator_static_v2.md").write_text("p", encoding="utf-8")
    assert cli.main(["evaluate", "--force", "--prompt-version", "v2", "--limit", "5"]) == 0
    o = captured["opts"]
    assert (o.force, o.prompt_version, o.eval_limit) == (True, "v2", 5)


def test_queue_run_respects_the_preferred_window(captured, monkeypatch, capsys):
    monkeypatch.setattr("src.config.is_within_preferred_hours", lambda *a, **k: False)
    assert cli.main(["queue", "--run", "--respect-hours"]) == 0
    assert "Fuera de la ventana preferida" in capsys.readouterr().out and "opts" not in captured
    cli.main(["queue", "--run", "--respect-hours", "--now"])  # --now overrides the window
    assert tuple(captured["opts"].stages) == ("evaluate", "signals")


# ---------------------------------------------------------------- report

def test_report_ranks_by_jinora_score_and_never_ranks_unevaluated_ads(clean_db, tmp_path, capsys):
    comps = seed_competitors()
    for ad_id, comp, creative, jinora, days in (("top", "Alpha CRM", 90, 85.0, 10), ("mid", "Alpha CRM", 60, 70.5, 50),
                                                 ("low", "Beta Chat", 40, 50.0, 5)):
        add_ad(ad_id, tmp_path, comps[comp], days=days, page_name=comp, status=STATUS_EVALUATED, pillar="crm" if comp == "Alpha CRM" else "conversacional")
        with SessionLocal() as s:
            s.add(Analysis(ad_id=ad_id, model="m", rubric_version=RUBRIC["video"], analysis_type="video", creative_score=creative,
                           result_json=json.dumps({"format": "ugc"})))
            s.add(MarketSignal(ad_id=ad_id, market_score=jinora, jinora_score=jinora))
            s.commit()
    add_ad("noscore", tmp_path, comps["Alpha CRM"], days=999, variants=99)  # no creative_score: never ranked
    add_ad("hidden", tmp_path, comps["Alpha CRM"], status=STATUS_REJECTED)

    assert cli.main(["report", "--top", "2"]) == 0
    out = capsys.readouterr().out
    rows = re.findall(r"^\s*(\d+)\s+(\S.*?)\s{2,}", out, re.M)
    assert [r[1].strip() for r in rows] == ["Alpha CRM", "Alpha CRM"] and out.index("top") < out.index("mid")
    assert "low" not in out.split("Evaluados")[0]  # --top 2 leaves the third out
    assert "Evaluados con jinora_score: 3" in out and "sin evaluar" in out

    assert cli.main(["report", "--pillar", "conversacional", "--top", "5"]) == 0
    only = capsys.readouterr().out
    assert "low" in only and "top" not in only.split("Evaluados")[0]


# ---------------------------------------------------------------- B4 + main.py compatibility

def test_queue_status_shows_fingerprint_requests_and_estimated_days(world, capsys):
    from src.config import settings

    assert cli.main(["queue", "--status"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"fingerprint\):\s+\S{8}\s", out)  # exactly the 8-character fingerprint, never the key
    for text in ("En cola", "Videos → Gemini", "Estáticos → Claude", "Anuncios evaluados", "Solicitudes de esta clave hoy",
                 "éxitos", "fallos", "DÍAS ESTIMADOS", "Excluidos de la cola: rejected 1 | out_of_catalog 1"):
        assert text in out
    usable = settings.GEMINI_RPD - settings.RESERVE_REQUESTS
    assert f"/ {usable}" in out  # pending videos / (RPD - RESERVE_REQUESTS)


def run_main(monkeypatch, *argv):
    import main

    monkeypatch.setattr(sys, "argv", ["main.py", *argv])
    with pytest.raises(SystemExit) as exc:
        main.main()
    return exc.value.code


def test_main_py_keeps_queue_status_run_and_rescore(world, monkeypatch, capsys, captured):
    assert run_main(monkeypatch, "queue", "--status") == 0
    assert "ESTADO DE LA COLA" in capsys.readouterr().out

    assert run_main(monkeypatch, "rescore") == 0
    assert "RESCORE LOCAL" in capsys.readouterr().out

    assert run_main(monkeypatch, "queue", "--run", "--now") == 0
    assert tuple(captured["opts"].stages) == ("evaluate", "signals")

    monkeypatch.setattr("src.config.is_within_preferred_hours", lambda *a, **k: False)
    assert run_main(monkeypatch, "queue", "--run", "--respect-hours") == 0
    assert "Fuera de la ventana preferida" in capsys.readouterr().out
