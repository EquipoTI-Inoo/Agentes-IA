# LEGACY: duplica scripts/run_phase4_real_tests.py (que tiene tope de presupuesto por clave) y llama a Gemini con ids fijos
"""
Script to execute Phase 4 live validation tests:
1. Live evaluation of 3 calibration ads (WhatsApp, CRM, Mass campaigns)
2. Consistency test (repeat 1 ad 2 more times, total 3 runs, check delta <= 3 per dim)
3. Transcription comparison (Gemini vs Whisper for 3 ads)
4. Quota check
"""
import sys
import json
import sqlite3
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Ensure UTF-8 output
sys.stdout.reconfigure(encoding="utf-8")

from src.database import SessionLocal, AdRecord, AnalysisRecord
from src.evaluator import evaluate_video_ad
from src.ai.quota import QuotaGuard
from src.config import settings

def main():
    quota = QuotaGuard()
    initial_usage = quota.get_daily_usage(settings.LLM_MODEL)
    print(f"=== INICIO PRUEBA FASE 4 ===", flush=True)
    print(f"Modelo: {settings.LLM_MODEL}", flush=True)
    print(f"Uso inicial hoy: {initial_usage['requests']} requests", flush=True)
    print(f"Disponibles: {quota.get_available_requests(settings.LLM_MODEL)}", flush=True)

    db = SessionLocal()

    # -------------------------------------------------------------
    # 1. EVALUAR 3 VIDEOS DE CALIBRACIÓN
    # -------------------------------------------------------------
    # 1. WhatsApp: Leadsales (1714051753080819) -> Already cached from run 1
    # 2. CRM: Clientify (1041244048736662)
    # 3. Masivos/Campañas: B2Chat (1649292236915392)
    selected_ads = [
        {"id": "1714051753080819", "tipo": "WhatsApp", "nombre": "Leadsales", "force": False},
        {"id": "1041244048736662", "tipo": "CRM", "nombre": "Clientify", "force": True},
        {"id": "1649292236915392", "tipo": "Mensajes Masivos / Campañas", "nombre": "B2Chat", "force": True}
    ]

    eval_results = {}

    for item in selected_ads:
        aid = item["id"]
        print(f"\n---> Evaluando {item['tipo']} ({item['nombre']}, id={aid}, force={item['force']})...", flush=True)
        ad_rec = db.query(AdRecord).filter(AdRecord.id == aid).first()
        if not ad_rec:
            raise RuntimeError(f"Ad {aid} no encontrado en BD")
        
        ad_dict = {
            "id": ad_rec.id,
            "ad_id": ad_rec.id,
            "page_name": ad_rec.page_name,
            "video_path": ad_rec.video_path,
            "headline": ad_rec.headline,
            "body_text": ad_rec.body_text,
            "cta_text": ad_rec.cta_text,
            "link_url": ad_rec.link_url,
        }

        res = evaluate_video_ad(ad_dict, force=item["force"])
        eval_results[aid] = res
        ev = res["evaluation"]
        print(f"OK! Cached={res.get('cached')} | Score: {res['creative_score']} | Hook: {ev['hook']['score']}, Mechanism: {ev['mechanism']['score']}, Narrative: {ev['narrative']['score']}, Offer: {ev['offer']['score']}", flush=True)

    # -------------------------------------------------------------
    # 2. PRUEBA DE CONSISTENCIA (Repetir ad 1714051753080819 2 veces más con force=True)
    # -------------------------------------------------------------
    repeat_aid = "1714051753080819"
    print(f"\n---> Corriendo prueba de consistencia sobre ad {repeat_aid} (2 corridas adicionales)...", flush=True)
    consistency_runs = [eval_results[repeat_aid]]

    ad_rec = db.query(AdRecord).filter(AdRecord.id == repeat_aid).first()
    ad_dict = {
        "id": ad_rec.id,
        "ad_id": ad_rec.id,
        "page_name": ad_rec.page_name,
        "video_path": ad_rec.video_path,
        "headline": ad_rec.headline,
        "body_text": ad_rec.body_text,
        "cta_text": ad_rec.cta_text,
        "link_url": ad_rec.link_url,
    }

    for run_idx in [2, 3]:
        print(f"Corrida {run_idx}...", flush=True)
        res = evaluate_video_ad(ad_dict, force=True)
        consistency_runs.append(res)
        ev = res["evaluation"]
        print(f"Corrida {run_idx} score: {res['creative_score']} | H: {ev['hook']['score']}, M: {ev['mechanism']['score']}, N: {ev['narrative']['score']}, O: {ev['offer']['score']}", flush=True)

    # -------------------------------------------------------------
    # 3. COMPARACIÓN DE TRANSCRIPCIONES (Whisper vs Gemini)
    # -------------------------------------------------------------
    # 3 videos que ya tienen Whisper transcript en BD:
    # 1. 1097388605975667 (WATI)
    # 2. 1666749958340789 (HubSpot)
    # 3. 1574738331364465 (Bland AI)
    whisper_ads = ["1097388605975667", "1666749958340789", "1574738331364465"]
    transcription_comparisons = []

    print(f"\n---> Corriendo comparación de transcripciones (Whisper vs Gemini)...", flush=True)
    for aid in whisper_ads:
        ad_rec = db.query(AdRecord).filter(AdRecord.id == aid).first()
        whisper_text = ad_rec.full_transcript or ""
        
        ad_dict = {
            "id": ad_rec.id,
            "ad_id": ad_rec.id,
            "page_name": ad_rec.page_name,
            "video_path": ad_rec.video_path,
            "headline": ad_rec.headline,
            "body_text": ad_rec.body_text,
            "cta_text": ad_rec.cta_text,
            "link_url": ad_rec.link_url,
        }

        print(f"Transcribiendo con Gemini ad {aid} ({ad_rec.page_name})...", flush=True)
        res = evaluate_video_ad(ad_dict, force=True)
        ev = res["evaluation"]
        gemini_text = " ".join(t.get("text", "") for t in ev.get("transcript", []))

        transcription_comparisons.append({
            "ad_id": aid,
            "page_name": ad_rec.page_name,
            "whisper_sample": whisper_text[:220],
            "gemini_sample": gemini_text[:220],
            "whisper_len": len(whisper_text),
            "gemini_len": len(gemini_text),
        })

    # -------------------------------------------------------------
    # 4. REPORTE FINAL Y MÉTRICAS
    # -------------------------------------------------------------
    final_usage = quota.get_daily_usage(settings.LLM_MODEL)
    used_today = final_usage["requests"]

    output_data = {
        "eval_summaries": {},
        "consistency": {
            "runs": [],
            "max_diff_per_dimension": {},
            "max_diff_total": 0,
        },
        "transcription_comparisons": transcription_comparisons,
        "requests_used_today": used_today,
    }

    for item in selected_ads:
        aid = item["id"]
        ev = eval_results[aid]["evaluation"]
        output_data["eval_summaries"][aid] = {
            "nombre": item["nombre"],
            "tipo": item["tipo"],
            "total": eval_results[aid]["creative_score"],
            "hook": ev["hook"]["score"],
            "mechanism": ev["mechanism"]["score"],
            "narrative": ev["narrative"]["score"],
            "offer": ev["offer"]["score"],
            "value_proposition": ev.get("value_proposition"),
            "feature_focus": ev.get("feature_focus"),
            "ai_mention": ev.get("ai_mention"),
            "target_niche": ev.get("target_niche"),
            "what_to_model": ev.get("what_to_model"),
            "cosmiq_adaptation_idea": ev.get("cosmiq_adaptation_idea"),
        }

    # Consistency metrics
    dim_scores = {"hook": [], "mechanism": [], "narrative": [], "offer": [], "total": []}
    for i, r in enumerate(consistency_runs, 1):
        ev = r["evaluation"]
        scores = {
            "run": i,
            "hook": ev["hook"]["score"],
            "mechanism": ev["mechanism"]["score"],
            "narrative": ev["narrative"]["score"],
            "offer": ev["offer"]["score"],
            "total": r["creative_score"],
        }
        output_data["consistency"]["runs"].append(scores)
        for k in ["hook", "mechanism", "narrative", "offer"]:
            dim_scores[k].append(ev[k]["score"])
        dim_scores["total"].append(r["creative_score"])

    for k in ["hook", "mechanism", "narrative", "offer"]:
        output_data["consistency"]["max_diff_per_dimension"][k] = max(dim_scores[k]) - min(dim_scores[k])
    output_data["consistency"]["max_diff_total"] = max(dim_scores["total"]) - min(dim_scores["total"])

    # Save to json file for reference
    with open("phase4_test_results.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print("\n=== RESULTADOS COMPLETOS GUARDADOS EN phase4_test_results.json ===", flush=True)
    print(f"Total requests gastadas hoy: {used_today}", flush=True)
    db.close()

if __name__ == "__main__":
    main()
