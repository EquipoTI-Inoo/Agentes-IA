"""
Phase 4 real validation runner (budget-capped Gemini session + Claude statics).

Usage: python scripts/run_phase4_real_tests.py <p1|p2|p3|p4>

- Gemini budget: SESSION_BUDGET requests TOTAL (retries and failures included), counted per API key
  with QuotaGuard (key_fingerprint). The baseline is stored in calibration/results/_session_budget.json
  the first time the script runs; if the key or the day changes the script refuses to continue.
- A 429 RPD (QuotaExhausted) stops the run, partial results stay saved. The key is never changed.
- The API key is never read from .env by this script nor printed: only its 8-char fingerprint.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
sys.stdout.reconfigure(encoding="utf-8")

from src.ai.quota import QuotaExhausted, get_current_day_pt, get_quota_guard
from src.config import settings
from src.database import AdRecord, AnalysisRecord, SessionLocal
from src.evaluator import PROMPT_VERSION, RUBRIC_VERSION, evaluate_video_ad

SESSION_BUDGET = 18  # raised from 12 by the owner after the 503s; 18 = RPD 20 - RESERVE_REQUESTS 2
RESULTS_DIR = ROOT_DIR / "calibration" / "results"
STATE_PATH = RESULTS_DIR / "_session_budget.json"
DIMENSIONS = ["hook", "mechanism", "narrative", "offer"]

P1_AD = "931108833186595"  # video 6, Respond.io, motion graphics
P2_ADS = {
    "video2": "1605980027649675",
    "video3": "3172443476285076",
    "video4": "1748258599843733",
    "video5": "2027131318676452",
}
P3_NEW_SPANISH = ["1137563185289120", "2140721320183206"]  # B2Chat, Clientify (Spanish voice per local Whisper)
P3_ADS = ["3172443476285076"] + P3_NEW_SPANISH + [P1_AD]  # video 3 reused from P2, video 6 reused from P1
P4_STATICS = {
    "voz_deepgram": "2252659475484926",
    "voz_agentes_zernio": "852138031319943",
    "agente_ia_elise": "1097832299273239",
}

GUARD = get_quota_guard()


class BudgetStop(Exception):
    """Raised when the session budget or the daily quota stops the run."""


def init_budget() -> Dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fp = GUARD.key_fingerprint
    day = get_current_day_pt()
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state["key_fingerprint"] != fp or state["day_pt"] != day:
            raise SystemExit(
                f"La clave o el día cambiaron desde el inicio de la sesión "
                f"({state['key_fingerprint']}/{state['day_pt']} -> {fp}/{day}). No continúo; pide instrucciones."
            )
    else:
        state = {
            "key_fingerprint": fp,
            "day_pt": day,
            "budget": SESSION_BUDGET,
            "baseline_key_requests": GUARD.get_key_usage_today(fp),
            "baseline_main_model": GUARD.get_daily_usage(settings.LLM_MODEL)["requests"],
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # Hard cap at the QuotaGuard level: acquire() refuses once the session budget is consumed
    cap = state["baseline_main_model"] + state["budget"] + settings.RESERVE_REQUESTS
    current = settings.GEMINI_RPD_MAIN if settings.GEMINI_RPD_MAIN is not None else settings.GEMINI_RPD
    settings.GEMINI_RPD_MAIN = min(current, cap)
    return state


STATE = init_budget()


def used() -> int:
    return GUARD.get_key_usage_today(STATE["key_fingerprint"]) - STATE["baseline_key_requests"]


def print_plan(name: str, planned: int) -> None:
    print("\n" + "=" * 72)
    print(f"{name}")
    print(f"  Plan: {planned} solicitudes Gemini | acumulado antes: {used()}/{STATE['budget']} "
          f"| clave {STATE['key_fingerprint']} | modelo {settings.LLM_MODEL}")
    print("=" * 72, flush=True)
    if used() + planned > STATE["budget"]:
        raise BudgetStop(f"El plan excede el presupuesto de sesión ({used()}+{planned} > {STATE['budget']})")


def ad_dict_from_db(ad_id: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        r = db.query(AdRecord).filter(AdRecord.id == ad_id).one()
        return {
            "id": r.id, "ad_id": r.id, "page_name": r.page_name, "video_path": r.video_path,
            "image_path": r.image_path, "headline": r.headline, "body_text": r.body_text,
            "cta_text": r.cta_text, "link_url": r.link_url,
        }
    finally:
        db.close()


def save(name: str, payload: Dict[str, Any]) -> Path:
    payload["session"] = {"key_fingerprint": STATE["key_fingerprint"], "requests_used_total": used(), "budget": STATE["budget"]}
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def evaluate_once(ad_id: str, client: Any, label: str) -> Dict[str, Any]:
    if used() >= STATE["budget"]:
        raise BudgetStop("Presupuesto de sesión agotado")
    before = used()
    ad = ad_dict_from_db(ad_id)
    try:
        res = evaluate_video_ad(ad, client=client, force=True)
    except QuotaExhausted as qe:
        raise BudgetStop(f"429 de cuota diaria / tope alcanzado: {qe}") from qe
    except Exception as err:  # transient errors already went through the client's progressive retries
        raise BudgetStop(
            f"{label}: falló tras los reintentos integrados ({type(err).__name__}: {str(err)[:200]}); "
            f"solicitudes gastadas en esta corrida: {used() - before}"
        ) from err
    spent = used() - before
    ev = res["evaluation"]
    record = {
        "label": label,
        "ad_id": ad_id,
        "page_name": ad["page_name"],
        "model": settings.LLM_MODEL,
        "prompt_version": PROMPT_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "requests_used": spent,
        "creative_score": res["creative_score"],
        "dimensions": {d: ev[d]["score"] for d in DIMENSIONS},
        "format": ev.get("format"),
        "has_voiceover": ev.get("has_voiceover"),
        "language": ev.get("language"),
        "transcript": ev.get("transcript", []),
        "evaluation": ev,
    }
    print(f"[{label}] ad {ad_id} ({ad['page_name']}): total={record['creative_score']} "
          f"dims={record['dimensions']} format={record['format']} voz={record['has_voiceover']} "
          f"| solicitudes de esta corrida={spent} | acumulado={used()}/{STATE['budget']}", flush=True)
    return record


def show_dimension(ev: Dict[str, Any], dim: str) -> None:
    d = ev[dim]
    print(f"    {dim} = {d['score']} ({d['anchor_level']}): {d['reasoning']}")
    for e in d["evidence"]:
        print(f"       - [{e.get('timestamp')}] ({e['source']}) {e['quote']}")


def run_p1() -> None:
    from src.ai.llm_client import GeminiClient

    print_plan("PRUEBA 1 — Video 6 (motion graphics), 3 corridas con 1 sola subida a Files API", 3)
    client = GeminiClient()
    payload: Dict[str, Any] = {"test": "prueba1_video6", "ad_id": P1_AD, "runs": []}
    try:
        for i in (1, 2, 3):
            run = evaluate_once(P1_AD, client, f"P1-corrida{i}")
            payload["runs"].append(run)
            payload["uploads_to_files_api"] = len(client._file_cache)
            save("prueba1_video6", payload)
    except BudgetStop as stop:
        payload["stopped"] = str(stop)
        print(f"DETENIDO: {stop}")
    finally:
        client.cleanup_file_cache()

    runs = payload["runs"]
    for r in runs:
        print(f"\n--- {r['label']}: total={r['creative_score']} dims={r['dimensions']} format={r['format']}")
        show_dimension(r["evaluation"], "hook")
        show_dimension(r["evaluation"], "offer")
    if len(runs) > 1:
        payload["max_diff_per_dimension"] = {
            d: max(r["dimensions"][d] for r in runs) - min(r["dimensions"][d] for r in runs) for d in DIMENSIONS
        }
        payload["max_diff_total"] = max(r["creative_score"] for r in runs) - min(r["creative_score"] for r in runs)
        print(f"\nDiferencia máxima por dimensión: {payload['max_diff_per_dimension']} | total: {payload['max_diff_total']} (meta <= 3 por dimensión)")
    save("prueba1_video6", payload)


def run_p2() -> None:
    from src.ai.llm_client import GeminiClient

    print_plan("PRUEBA 2 — Videos 2, 3, 4 y 5 con la rúbrica nueva (1 corrida por video)", 4)
    client = GeminiClient()
    payload: Dict[str, Any] = {"test": "prueba2_videos_2a5", "runs": []}
    try:
        for label, ad_id in P2_ADS.items():
            payload["runs"].append(evaluate_once(ad_id, client, f"P2-{label}"))
            save("prueba2_videos_2a5", payload)
    except BudgetStop as stop:
        payload["stopped"] = str(stop)
        print(f"DETENIDO: {stop}")
    finally:
        client.cleanup_file_cache()
        save("prueba2_videos_2a5", payload)


def run_p3_gemini() -> None:
    from src.ai.llm_client import GeminiClient

    print_plan("PRUEBA 3 (parte Gemini) — 2 anuncios en español nuevos; video 3 (P2) y video 6 (P1) se reutilizan", 2)
    client = GeminiClient()
    payload: Dict[str, Any] = {"test": "prueba3_gemini_transcripciones", "runs": []}
    try:
        for ad_id in P3_NEW_SPANISH:
            payload["runs"].append(evaluate_once(ad_id, client, f"P3-{ad_id}"))
            save("prueba3_gemini_transcripciones", payload)
    except BudgetStop as stop:
        payload["stopped"] = str(stop)
        print(f"DETENIDO: {stop}")
    finally:
        client.cleanup_file_cache()
        save("prueba3_gemini_transcripciones", payload)


def run_p4() -> None:
    from src.ai.claude_client import ClaudeClient
    from src.evaluator_static import RUBRIC_VERSION as STATIC_RUBRIC, evaluate_static_ad

    print("\n" + "=" * 72)
    print(f"PRUEBA 4 — 3 estáticos con Claude ({settings.CLAUDE_MODEL_STATIC}); 0 solicitudes Gemini; "
          f"acumulado Gemini={used()}/{STATE['budget']}")
    print("=" * 72, flush=True)
    client = ClaudeClient()
    payload: Dict[str, Any] = {"test": "prueba4_estaticos_claude", "model": client.model, "runs": []}
    for label, ad_id in P4_STATICS.items():
        ad = ad_dict_from_db(ad_id)
        spend_before = GUARD.get_total_spend(provider="claude")
        try:
            res = evaluate_static_ad(ad, client=client, force=True)
        except Exception as err:  # report without ever echoing the key
            msg = str(err).replace(settings.ANTHROPIC_API_KEY or "<none>", "<redacted>")
            print(f"[{label}] ERROR {type(err).__name__}: {msg[:300]}")
            payload["runs"].append({"label": label, "ad_id": ad_id, "error": f"{type(err).__name__}: {msg[:300]}"})
            continue
        if res.get("status") == "pending_evaluation":
            print(f"[{label}] pendiente: {res.get('notice')}")
            payload["runs"].append({"label": label, "ad_id": ad_id, "pending": res.get("notice")})
            continue
        ev = res["evaluation"]
        usage = dict(client.last_usage)
        run = {
            "label": label, "ad_id": ad_id, "page_name": ad["page_name"], "model": client.model,
            "prompt_version": PROMPT_VERSION, "rubric_version": STATIC_RUBRIC,
            "creative_score": res["creative_score"],
            "dimensions": {d: ev[d]["score"] for d in ["stop_power", "message_clarity", "proof", "offer_cta"]},
            "value_proposition": ev.get("value_proposition"), "feature_focus": ev.get("feature_focus"),
            "ai_mention": ev.get("ai_mention"), "visual_style": ev.get("visual_style"),
            "tokens_in": usage.get("tokens_in"), "tokens_out": usage.get("tokens_out"),
            "cost_usd": round(usage.get("cost_usd", 0.0), 5),
            "cumulative_spend_usd": round(GUARD.get_total_spend(provider="claude"), 5),
            "budget_usd": settings.CLAUDE_BUDGET_USD,
            "evaluation": ev,
        }
        payload["runs"].append(run)
        print(f"[{label}] {ad['page_name']} ({ad_id}): total={run['creative_score']} dims={run['dimensions']}\n"
              f"    propuesta: {run['value_proposition']}\n"
              f"    feature_focus={run['feature_focus']} ai_mention={run['ai_mention']}\n"
              f"    tokens in/out={run['tokens_in']}/{run['tokens_out']} costo=${run['cost_usd']} "
              f"acumulado=${run['cumulative_spend_usd']} / tope ${run['budget_usd']}", flush=True)
        save("prueba4_estaticos_claude", payload)
        _ = spend_before
    save("prueba4_estaticos_claude", payload)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        {"p1": run_p1, "p2": run_p2, "p3": run_p3_gemini, "p4": run_p4}[cmd]()
    except KeyError:
        raise SystemExit("Uso: python scripts/run_phase4_real_tests.py <p1|p2|p3|p4>")
    except BudgetStop as stop:
        print(f"DETENIDO antes de empezar: {stop}")
    print(f"\nSolicitudes Gemini usadas en la sesión: {used()}/{STATE['budget']} (clave {STATE['key_fingerprint']})")
