"""
Phase 8 reports: md/html without --summary, patterns computed by code, escaping, no keys / absolute paths, and the optional
executive summary (ONE request, through QuotaGuard) with mocks only.
"""

import re
from unittest.mock import MagicMock

import pytest
from pipeline_helpers import add_evaluated_ad, seed_competitors, video_result

from src import cli, report as report_mod, summary as summary_mod
from src.ai.llm_client import GeminiClient
from src.ai.quota import QuotaExhausted, QuotaGuard
from src.config import settings
from src.database import SessionLocal
from src.db.models import STATUS_REJECTED

FAKE_KEY = "AI" + "zaSyFAKE-SECRET-KEY-0123456789"  # assembled: no key-shaped literal in the repo
FAKE_ANTHROPIC = "sk-" + "ant-FAKE-SECRET"


@pytest.fixture
def world(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.settings.DOWNLOADS_DIR", str(tmp_path))
    comps = seed_competitors()
    alpha = comps["Alpha CRM"]
    add_evaluated_ad("r1", tmp_path, alpha, jinora=90.0, duration=30.0, result=video_result(focus=("automatizacion", "agendamiento"), scores=(22, 20, 20, 18), ai="ninguna"))
    add_evaluated_ad("r2", tmp_path, alpha, jinora=80.0, duration=60.0, group="gX", result=video_result(fmt="demo_pantalla", voice=False, focus=("automatizacion",), ai="central", language="en"))
    add_evaluated_ad("r2b", tmp_path, alpha, jinora=70.0, group="gX")
    add_evaluated_ad("r3", tmp_path, alpha, jinora=60.0, duration=45.0, result=video_result(focus=("agendamiento", "gestion_leads_crm"), ai="mencionada"))
    add_evaluated_ad("other_pillar", tmp_path, comps["Beta Chat"], jinora=99.0, pillar="conversacional", page_name="Beta Chat")
    add_evaluated_ad("hidden", tmp_path, alpha, jinora=95.0, status=STATUS_REJECTED)
    return tmp_path


def build(**kw):
    with SessionLocal() as db:
        return report_mod.build_report(db, **kw)


# ---------------------------------------------------------------- content and patterns

def test_report_ranks_by_stored_jinora_score_one_row_per_creative(world):
    rep = build(pillar="crm", top=10)
    assert [i["ad_id"] for i in rep["top"]] == ["r1", "r2", "r3"]  # r2b is a variant of r2; other pillar and rejected are out
    assert rep["top"][1]["variants"] == 2 and rep["evaluated"] == 3 and rep["unevaluated"] == 0
    assert [i["ad_id"] for i in build(pillar="crm", top=2)["top"]] == ["r1", "r2"]
    assert build(pillar="voz")["top"] == []
    with pytest.raises(ValueError):
        build(pillar="ventas")


def test_patterns_are_computed_by_code_without_llm(world, monkeypatch):
    monkeypatch.setattr(GeminiClient, "generate_text", lambda *a, **k: pytest.fail("patterns must not use an LLM"))
    p = build(pillar="crm")["patterns"]
    assert p["n"] == 3
    assert {x["tag"]: x["count"] for x in p["feature_focus"]} == {"automatizacion": 2, "agendamiento": 2, "gestion_leads_crm": 1}
    assert p["feature_focus"][0]["share"] == 67 and {x["tag"]: x["count"] for x in p["formats"]} == {"ugc": 2, "demo_pantalla": 1}
    assert p["duration"] == {"min": 30.0, "max": 60.0, "median": 45.0, "n": 3}
    assert p["voice"] == {"with_voice": 2, "without_voice": 1, "known": 3, "share_with_voice": 67}
    assert {x["tag"]: x["count"] for x in p["ai_mention"]} == {"ninguna": 1, "central": 1, "mencionada": 1}  # descriptive only
    assert {x["tag"] for x in p["languages"]} == {"es", "en"}
    assert report_mod.compute_patterns([])["duration"] is None and report_mod.compute_patterns([])["voice"]["share_with_voice"] is None


def test_markdown_report_has_every_section_and_no_summary_by_default(world, tmp_path):
    rep = build(pillar="crm", top=10)
    path = report_mod.write_report(rep, "md", output_dir=tmp_path / "reports")
    assert path.name.startswith("jinora_top3_crm_") and path.suffix == ".md"
    text = path.read_text(encoding="utf-8")
    for needle in ("# Top 3 por jinora_score — pilar crm", "## Ranking", "## Patrones comunes", "## Detalle por anuncio", "**Propuesta de valor:**",
                   "**Desglose:**", "Captación 22/25", "**Qué copiar:** copiar esto", "**Qué superar:** superar esto", "**Idea para Cosmiq:** idea de prueba",
                   "no generado (opción --summary apagada)", "Ángulos recurrentes (feature_focus): Automatización (2 de 3)", "Duración: de 30 s a 60 s",
                   "Voz: 2 de 3 con voz (67 %)", "Mención de IA (dato descriptivo, no suma ni resta)", "| 1 | Alpha CRM |"):
        assert needle in text, needle
    assert "## Resumen ejecutivo" not in text  # section only exists when a summary was generated
    assert "hidden" not in text and "other_pillar" not in text  # rejected and other pillars never appear


def test_html_report_is_standalone_and_has_the_same_content(world, tmp_path):
    path = report_mod.write_report(build(pillar="crm"), "html", output_dir=tmp_path)
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "Content-Security-Policy" in page and "<script" not in page.lower()
    for needle in ("Patrones comunes", "Qué copiar", "Qué superar", "Idea para Cosmiq", "no generado (opción --summary apagada)", "Alpha CRM"):
        assert needle in page, needle
    with pytest.raises(ValueError):
        report_mod.write_report(build(pillar="crm"), "pdf", output_dir=tmp_path)


def test_empty_report_is_clear(clean_db, tmp_path):
    rep = build(pillar="crm")
    assert "Todavía no hay anuncios evaluados" in report_mod.format_report(rep)
    assert "Todavía no hay anuncios evaluados" in report_mod.render_markdown(rep) and "No hay anuncios en el top" in report_mod.render_markdown(rep)


# ---------------------------------------------------------------- safety

def test_third_party_text_is_escaped_in_md_and_html(clean_db, tmp_path, monkeypatch):
    monkeypatch.setattr("src.config.settings.DOWNLOADS_DIR", str(tmp_path))
    comps = seed_competitors()
    evil = "<script>alert(1)</script> | <img src=x onerror=alert(2)>"
    add_evaluated_ad("evil", tmp_path, comps["Alpha CRM"], page_name="Alpha CRM",
                     result=video_result(value_proposition=evil, what_to_model=evil, what_to_beat=evil, cosmiq_adaptation_idea=evil))
    rep = build(pillar="crm")
    rep["top"][0]["competitor"] = evil
    md, page = report_mod.render_markdown(rep), report_mod.render_html(rep)
    assert "<script>" not in md and "<img" not in md and "&lt;script&gt;" in md and "\\|" in md
    assert "<script>" not in page and "<img" not in page and "&lt;script&gt;" in page and "onerror=alert(2)" in page  # inert text inside &lt;img ...&gt;
    assert re.search(r"<img", page) is None


def test_reports_never_contain_keys_or_absolute_paths(world, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    add_evaluated_ad("leaky", tmp_path, None, jinora=99.0, pillar="crm",
                     result=video_result(value_proposition=f"clave {FAKE_KEY} y {FAKE_ANTHROPIC} en C:\\Users\\riega\\Documents\\Jinora\\downloads\\x.mp4",
                                         what_to_model="ver /Users/riega/secreto/video.mp4", what_to_beat="ok"))
    rep = build(pillar="crm")
    rep["summary"] = {"requested": True, "generated": True, "model": "m", "reason": None, "text": f"Resumen con {FAKE_KEY} en D:\\Proyectos\\clave.txt"}
    for text in (report_mod.render_markdown(rep), report_mod.render_html(rep)):
        assert FAKE_KEY not in text and FAKE_ANTHROPIC not in text
        assert not re.search(r"[A-Za-z]:[\\/]", text) and "/Users/" not in text and "/home/" not in text and str(tmp_path) not in text
        assert "<redacted>" in text and "<ruta-local>" in text


# ---------------------------------------------------------------- executive summary (opt-in, one request, mocks)

class FakeLLM:
    model = "fake-model"

    def __init__(self, text="Patrones: ...\nHuecos: ...", error=None):
        self.text, self.error, self.calls = text, error, []

    def generate_text(self, prompt, system="", max_attempts=1):
        self.calls.append({"prompt": prompt, "system": system, "max_attempts": max_attempts})
        if self.error:
            raise self.error
        return self.text


def test_summary_prompt_is_built_from_stored_facts_and_stays_neutral(world):
    rep = build(pillar="crm")
    prompt = summary_mod.build_prompt(rep)
    for needle in ("Alpha CRM", "propuesta de prueba", "copiar esto", "superar esto", "automatizacion", "mención de IA (descriptivo)"):
        assert needle in prompt, needle
    assert "No supongas que un anuncio usa o promueve IA" in summary_mod.SYSTEM_PROMPT
    assert not re.search(r"[A-Za-z]:[\\/]", prompt) and "downloads" not in prompt  # no local paths


def test_summary_makes_exactly_one_request_and_the_report_says_so(world, tmp_path):
    rep = build(pillar="crm")
    llm = FakeLLM()
    rep["summary"] = summary_mod.generate_executive_summary(rep, client=llm)
    assert len(llm.calls) == 1 and llm.calls[0]["max_attempts"] == 1  # ONE request, no retries
    assert rep["summary"]["generated"] is True and rep["summary"]["model"] == "fake-model"
    text = report_mod.render_markdown(rep)
    assert "**Resumen ejecutivo con LLM:** generado con fake-model (una sola solicitud)" in text and "## Resumen ejecutivo" in text and "Huecos: ..." in text
    assert "generado con fake-model" in report_mod.render_html(rep)


@pytest.mark.parametrize("error,expected", [
    (QuotaExhausted(model="m", day_pt="d", requests_used=18, limit=18), "cuota diaria agotada"),
    (RuntimeError(f"503 UNAVAILABLE key={FAKE_KEY}"), "error del cliente LLM"),
])
def test_summary_failures_are_reported_not_raised_and_never_leak_the_key(world, monkeypatch, error, expected):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", FAKE_KEY)
    result = summary_mod.generate_executive_summary(build(pillar="crm"), client=FakeLLM(error=error))
    assert result["generated"] is False and result["requested"] is True and expected in result["reason"] and FAKE_KEY not in result["reason"]
    rep = build(pillar="crm")
    rep["summary"] = result
    assert f"no generado: {result['reason']}" in report_mod.render_markdown(rep)


def test_summary_without_key_or_without_ads_does_nothing(world, monkeypatch, clean_db):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    constructed = []
    monkeypatch.setattr("src.ai.llm_client.GeminiClient", lambda *a, **k: constructed.append(1))
    rep = build(pillar="crm")
    assert "GEMINI_API_KEY" in summary_mod.generate_executive_summary(rep)["reason"] and constructed == []
    rep["top"] = []
    assert "no hay anuncios" in summary_mod.generate_executive_summary(rep)["reason"]


def test_gemini_generate_text_is_one_quota_guarded_request(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FREE_TIER_MODE", True)
    monkeypatch.setattr(settings, "GEMINI_RPD_MAIN", None)
    monkeypatch.setattr(settings, "GEMINI_RPD", 6)
    monkeypatch.setattr(settings, "RESERVE_REQUESTS", 2)
    monkeypatch.setattr(settings, "GEMINI_RPM", 1000)
    monkeypatch.setattr(settings, "GEMINI_RETRY_DELAYS", [0.001] * 5)
    monkeypatch.setattr("src.ai.llm_client.time.sleep", lambda s: None)
    guard = QuotaGuard(db_path=str(tmp_path / "q.db"), api_key="fake-key-for-tests")
    client = GeminiClient(api_key="fake-key-for-tests", quota_guard=guard)
    client._client = MagicMock()
    client._client.models.generate_content.side_effect = Exception("503 UNAVAILABLE")

    with pytest.raises(Exception, match="503"):
        client.generate_text("resume esto", system="sé breve")
    assert client._client.models.generate_content.call_count == 1  # a 503 is NOT retried for a summary
    usage = guard.get_daily_usage(settings.LLM_MODEL, key_fingerprint=client.key_fingerprint)
    assert usage["requests"] == 1 and usage["requests_failed"] == 1  # counted by QuotaGuard

    ok = MagicMock(text="  resumen listo  ")
    ok.usage_metadata.prompt_token_count = ok.usage_metadata.candidates_token_count = 5
    client._client.models.generate_content.side_effect = None
    client._client.models.generate_content.return_value = ok
    assert client.generate_text("otra") == "resumen listo"
    guard.mark_exhausted(settings.LLM_MODEL, key_fingerprint=client.key_fingerprint)
    with pytest.raises(QuotaExhausted):
        client.generate_text("sin cuota")  # checked before any request is made
    assert client._client.models.generate_content.call_count == 2


# ---------------------------------------------------------------- CLI

def test_cli_report_files_and_summary_flag(world, tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "out"
    constructed = []
    monkeypatch.setattr("src.ai.llm_client.GeminiClient", lambda *a, **k: constructed.append(1))  # must never be built without --summary

    assert cli.main(["report", "--pillar", "crm", "--top", "5", "--format", "md", "--output-dir", str(out_dir)]) == 0
    out = capsys.readouterr().out
    assert "Reporte guardado en:" in out and "no solicitado (use --summary)" in out and constructed == []
    assert [p.suffix for p in out_dir.iterdir()] == [".md"]

    assert cli.main(["report", "--pillar", "crm", "--format", "html", "--output-dir", str(out_dir)]) == 0
    assert sorted(p.suffix for p in out_dir.iterdir()) == [".html", ".md"] and constructed == []

    # --summary: opt-in, one request through the (mocked) configured client
    llm = FakeLLM("Resumen simulado")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", FAKE_KEY)
    monkeypatch.setattr("src.ai.llm_client.GeminiClient", lambda *a, **k: llm)
    assert cli.main(["report", "--pillar", "crm", "--summary", "--output-dir", str(out_dir)]) == 0
    assert len(llm.calls) == 1 and "Resumen ejecutivo con LLM: generado" in capsys.readouterr().out
    assert "Resumen simulado" in next(out_dir.glob("*.md")).read_text(encoding="utf-8")

    llm.error = QuotaExhausted(model="m", day_pt="d", requests_used=18, limit=18)
    assert cli.main(["report", "--pillar", "crm", "--summary", "--format", "md", "--output-dir", str(out_dir)]) == 0
    assert "NO generado — cuota diaria agotada" in capsys.readouterr().out and len(llm.calls) == 2


def test_cli_report_without_format_still_prints_the_table(world, capsys):
    assert cli.main(["report", "--pillar", "crm", "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "TOP 2 POR jinora_score" in out and "Alpha CRM" in out and "Reporte guardado" not in out
