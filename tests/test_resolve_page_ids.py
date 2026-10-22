"""
Guarantees of the page_id resolution (scripts/resolve_page_ids.py + src/collect/page_resolver.py), all with mocks:
0 real Meta calls and 0 real sleeps.
"""

import importlib.util
import json
import random
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.collect.page_resolver import (
    PageResolver, backup_competitors_file, classify_competitor, plan_resolution,
)
from src.collect.ratelimit import MetaBlockedError, MetaRateLimiter

ROOT = Path(__file__).resolve().parent.parent


def make_limiter(sleeps=None, **kw):
    sleeps = sleeps if sleeps is not None else []
    return MetaRateLimiter(sleep=sleeps.append, uniform=lambda a, b: b, **kw), sleeps


def comp(name, priority=2, **extra):
    return {"name": name, "priority": priority, "website": "", "markets": ["MX"], **extra}


def write_catalog(path, comps):
    path.write_text(json.dumps({"pillars": {"p": {"competitors": comps}}}), encoding="utf-8")
    return path


def load(path):
    return {c["name"]: c for c in json.loads(path.read_text(encoding="utf-8"))["pillars"]["p"]["competitors"]}


def collector_with(pages_by_query):
    col = MagicMock()
    col.client.search_pages.side_effect = lambda q, country=None: pages_by_query.get(q, [])
    col.search.return_value = []
    return col


def page(pid, name, verified="NOT_VERIFIED"):
    return {"page_id": pid, "page_name": name, "page_verified": verified}


# --- no guessing ---------------------------------------------------------------------------------------

def test_similar_name_is_never_assigned_even_if_verified():
    col = collector_with({"Acme CRM": [page("1", "Acme CRM Store", "VERIFIED"), page("2", "Acme", "VERIFIED")]})
    res = PageResolver(collector=col, limiter=make_limiter()[0]).resolve_competitor(comp("Acme CRM"))
    assert res["page_ids"] == [] and res["needs_review"] is True and res["resolution_confidence"] == "baja"
    assert {c["page_id"] for c in res["review_candidates"]} == {"1", "2"}  # only offered to a person


def test_normalized_name_matches_and_parenthesis_variant_is_tried():
    col = collector_with({"Bird (MessageBird)": [], "Bird": [page("7", "BIRD!")]})
    res = PageResolver(collector=col, limiter=make_limiter()[0]).resolve_competitor(comp("Bird (MessageBird)"))
    assert res["page_ids"] == ["7"] and res["resolution_confidence"] == "media" and res["needs_review"] is False


def test_two_exact_names_without_domain_confirmation_are_ambiguous_not_guessed():
    col = collector_with({"Dup": [page("1", "Dup"), page("2", "Dup")]})
    res = PageResolver(collector=col, limiter=make_limiter()[0]).resolve_competitor(comp("Dup"))
    assert res["page_ids"] == [] and res["needs_review"] is True


def test_exact_name_with_ads_pointing_elsewhere_is_flagged_for_review():
    col = collector_with({"Acme": [page("1", "Acme")]})
    ad = MagicMock()
    ad.creatives = [MagicMock(link_url="https://other-site.com/x")]
    col.search.return_value = [ad]
    res = PageResolver(collector=col, limiter=make_limiter()[0]).resolve_competitor(comp("Acme", website="https://acme.io"))
    assert res["page_ids"] == ["1"] and res["resolution_confidence"] == "media" and res["needs_review"] is True


def test_result_always_carries_confidence_review_flag_and_timestamp():
    col = collector_with({})
    res = PageResolver(collector=col, limiter=make_limiter()[0]).resolve_competitor(comp("Nada"))
    assert {"resolution_confidence", "needs_review", "resolved_at", "page_ids"} <= set(res)


# --- pause and backoff ---------------------------------------------------------------------------------

def test_every_meta_query_is_preceded_by_a_pause_between_1_and_3_seconds():
    seen = []
    limiter = MetaRateLimiter(sleep=seen.append, uniform=lambda a, b: random.uniform(a, b))
    col = collector_with({"Acme": [page("1", "Acme")]})
    PageResolver(collector=col, limiter=limiter).resolve_competitor(comp("Acme", website="https://acme.io"))
    assert limiter.queries == 2  # typeahead + one ads check
    assert len(seen) == 2 and all(1.0 <= s <= 3.0 for s in seen)


def test_block_triggers_exponential_backoff_then_succeeds():
    sleeps = []
    limiter, _ = make_limiter(sleeps)
    col = MagicMock()
    col.client.search_pages.side_effect = [RuntimeError("429 Too Many Requests"), RuntimeError("rate limit"), [page("1", "Acme")]]
    res = PageResolver(collector=col, limiter=limiter).resolve_competitor(comp("Acme"))
    assert res["page_ids"] == ["1"]
    backoffs = [s for s in sleeps if s > 3.0]
    assert len(backoffs) == 2 and backoffs[1] > backoffs[0]  # 30 s then 60 s (plus jitter)


def test_persistent_block_stops_the_run_and_keeps_what_was_resolved(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno"), comp("Dos"), comp("Tres")])
    col = MagicMock()

    def search(q, country=None):
        if q == "Uno":
            return [page("1", "Uno")]
        raise RuntimeError("403 blocked")

    col.client.search_pages.side_effect = search
    resolver = PageResolver(collector=col, limiter=make_limiter(max_retries=2)[0])
    resolver.resolve_all_priority(2, db_path=db, backup=False)
    assert resolver.last_run["blocked"] is True and resolver.last_run["processed"] == 1
    saved = load(db)
    assert saved["Uno"]["page_ids"] == ["1"]
    assert "resolved_at" not in saved["Dos"] and "resolved_at" not in saved["Tres"]  # untouched: retried next run
    assert col.client.search_pages.call_count == 1 + 3  # Uno, then Dos tried 1 + 2 retries and no further competitor


def test_non_block_error_does_not_abort_the_batch(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno"), comp("Dos")])
    col = MagicMock()
    col.client.search_pages.side_effect = lambda q, country=None: (_ for _ in ()).throw(ValueError("boom")) if q == "Uno" else [page("2", "Dos")]
    resolver = PageResolver(collector=col, limiter=make_limiter()[0])
    resolver.resolve_all_priority(2, db_path=db, backup=False)
    saved = load(db)
    assert saved["Dos"]["page_ids"] == ["2"] and "resolved_at" not in saved["Uno"]
    assert resolver.last_run["errors"][0]["name"] == "Uno" and not resolver.last_run["blocked"]


# --- resumable, never overwrites, backup ------------------------------------------------------------

def test_resumable_second_run_repeats_no_query(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno"), comp("Nada")])
    col = collector_with({"Uno": [page("1", "Uno")]})
    resolver = PageResolver(collector=col, limiter=make_limiter()[0])
    resolver.resolve_all_priority(2, db_path=db, backup=False)
    assert resolver.last_run["processed"] == 2 and col.client.search_pages.call_count == 2
    col.client.search_pages.reset_mock()
    resolver.resolve_all_priority(2, db_path=db, backup=False)
    assert col.client.search_pages.call_count == 0
    assert resolver.last_run["skipped_existing"] == 1 and resolver.last_run["skipped_attempted"] == 1


def test_progress_is_saved_after_each_competitor(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno"), comp("Dos")])
    states = []

    def search(q, country=None):
        states.append(set(k for k, v in load(db).items() if v.get("resolved_at")))
        return [page("1" if q == "Uno" else "2", q)]

    col = MagicMock()
    col.client.search_pages.side_effect = search
    PageResolver(collector=col, limiter=make_limiter()[0]).resolve_all_priority(2, db_path=db, backup=False)
    assert states == [set(), {"Uno"}]  # Uno already on disk when Dos is queried


def test_existing_page_ids_are_never_overwritten(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno", page_ids=["999"], resolved_at="2026-01-01", resolution_confidence="alta")])
    col = collector_with({"Uno": [page("1", "Uno")]})
    PageResolver(collector=col, limiter=make_limiter()[0]).resolve_all_priority(2, db_path=db, retry_unresolved=True, backup=False)
    assert load(db)["Uno"]["page_ids"] == ["999"] and col.client.search_pages.call_count == 0


def test_retry_unresolved_reprocesses_tried_ones_and_withdraws_low_confidence_guesses(tmp_path):
    db = write_catalog(tmp_path / "c.json", [
        comp("Intentado", resolved_at="2026-10-21", resolution_confidence="baja", page_ids=[], needs_review=True),
        comp("Adivinado", resolved_at="2026-10-21", resolution_confidence="baja", page_ids=["555"], needs_review=True),
    ])
    col = collector_with({"Intentado": [page("10", "Intentado")], "Adivinado": [page("11", "Otra cosa")]})
    PageResolver(collector=col, limiter=make_limiter()[0]).resolve_all_priority(2, db_path=db, retry_unresolved=True, backup=False)
    saved = load(db)
    assert saved["Intentado"]["page_ids"] == ["10"]
    assert saved["Adivinado"]["page_ids"] == [] and saved["Adivinado"]["needs_review"] is True
    assert "555" in {c["page_id"] for c in saved["Adivinado"]["review_candidates"]}  # nothing is lost


def test_backup_is_a_dated_copy_next_to_the_catalog(tmp_path):
    db = write_catalog(tmp_path / "c.json", [comp("Uno")])
    bak = backup_competitors_file(db)
    assert bak.parent == db.parent and "backup_resolve_" in bak.name and bak.read_text() == db.read_text()
    col = collector_with({})
    resolver = PageResolver(collector=col, limiter=make_limiter()[0])
    resolver.resolve_all_priority(2, db_path=db)  # backup=True by default
    assert resolver.last_run["backup"] and (tmp_path / resolver.last_run["backup"]).exists()


def test_classify_competitor_rules():
    assert classify_competitor({}) == "process_new"
    assert classify_competitor({"page_ids": ["1"], "resolved_at": "x", "resolution_confidence": "alta"}, True) == "skip_existing"
    assert classify_competitor({"resolved_at": "x"}) == "skip_attempted"
    assert classify_competitor({"resolved_at": "x"}, True) == "process_retry"


# --- the script: plan, dry-run, execute guard ---------------------------------------------------------------

def load_script():
    spec = importlib.util.spec_from_file_location("resolve_page_ids_script", ROOT / "scripts" / "resolve_page_ids.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plan_counts_queries_and_time(tmp_path):
    data = {"pillars": {"p": {"competitors": [
        comp("A", website="https://a.io"),                      # 1..3 queries
        comp("B (Alias)", 3, website="https://b.io"),           # 1..4 queries
        comp("C", 3, page_ids=["1"], resolved_at="x", resolution_confidence="alta"),  # skipped
        comp("D", 1),                                           # other priority
    ]}}}
    plan = plan_resolution(data, (2, 3))
    assert plan["to_process"] == 2 and plan["queries_min"] == 2 and plan["queries_max"] == 7
    assert plan["seconds_min"] == 2.0 and plan["seconds_max"] == 21.0
    assert plan["by_priority"][3]["total"] == 2 and plan["by_priority"][3]["to_process"] == 1


def test_script_dry_run_makes_no_meta_calls_and_no_changes(monkeypatch, capsys, tmp_path):
    mod = load_script()
    db = write_catalog(tmp_path / "c.json", [comp("Nuevo")])
    before = db.read_text()
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "PageResolver", MagicMock(side_effect=AssertionError("dry-run must not build a resolver")))
    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Consultas a Meta" in out and "Regla de confianza" in out and "1-3 s" in out
    assert db.read_text() == before and not list(tmp_path.glob("*.backup*"))


def test_script_without_mode_flag_does_nothing(monkeypatch, tmp_path):
    mod = load_script()
    db = write_catalog(tmp_path / "c.json", [comp("Nuevo")])
    monkeypatch.setattr(mod, "DB_PATH", db)
    monkeypatch.setattr(mod, "PageResolver", MagicMock(side_effect=AssertionError("must not run")))
    assert mod.main([]) == 2


def test_script_execute_uses_resolver_and_exits_3_on_block(monkeypatch, tmp_path):
    mod = load_script()
    db = write_catalog(tmp_path / "c.json", [comp("Nuevo")])
    monkeypatch.setattr(mod, "DB_PATH", db)
    fake = MagicMock()
    fake.__enter__.return_value = fake
    fake.last_run = {"processed": 0, "skipped_existing": 0, "skipped_attempted": 0, "errors": [], "queries": 4, "blocked": True}
    monkeypatch.setattr(mod, "PageResolver", MagicMock(return_value=fake))
    assert mod.main(["--execute"]) == 3
    assert fake.resolve_all_priority.call_count == 1  # stopped at the first priority
    assert list(tmp_path.glob("c.backup_resolve_*"))  # single script-level backup


def test_blocked_error_type_is_exported_for_callers():
    assert issubclass(MetaBlockedError, Exception)
