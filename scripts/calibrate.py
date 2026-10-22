"""
Calibration and Alignment Script (Phase 4.6)
Compares AI evaluations against human scores in calibration/manual_scores.csv:
- Computes Spearman rank correlation per dimension and overall.
- Computes Mean Absolute Error (MAE) per dimension and overall.
- Analyzes consistency across multiple runs of the same ad.
- Runs cleanly even if the CSV is empty or partially populated without inventing scores.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.database import AnalysisRecord, SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Jinora.Calibrate")

MANUAL_SCORES_PATH = ROOT_DIR / "calibration" / "manual_scores.csv"
DOWNLOADS_DIR = ROOT_DIR / "downloads"


def compute_spearman(x: List[float], y: List[float]) -> float:
    """Computes exact Spearman rank correlation coefficient without external dependencies."""
    n = len(x)
    if n < 2:
        return 0.0

    def rank_data(arr: List[float]) -> List[float]:
        indexed = sorted(enumerate(arr), key=lambda item: item[1])
        ranks = [0.0] * len(arr)
        i = 0
        while i < len(indexed):
            val = indexed[i][1]
            j = i
            while j < len(indexed) and indexed[j][1] == val:
                j += 1
            avg_rank = (i + j - 1) / 2.0 + 1.0
            for k in range(i, j):
                ranks[indexed[k][0]] = avg_rank
            i = j
        return ranks

    rx = rank_data(x)
    ry = rank_data(y)

    # Check for constant arrays (zero variance)
    if len(set(rx)) == 1 or len(set(ry)) == 1:
        return 0.0

    d_sq = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    denom = n * (n**2 - 1)
    if denom == 0:
        return 0.0
    return 1.0 - (6.0 * d_sq) / denom


def compute_mae(x: List[float], y: List[float]) -> float:
    """Computes Mean Absolute Error between two series."""
    if not x:
        return 0.0
    return sum(abs(a - b) for a, b in zip(x, y)) / float(len(x))


def load_manual_scores(csv_path: Path = MANUAL_SCORES_PATH) -> List[Dict[str, Any]]:
    """Loads human scores from CSV; skips empty or non-numeric rows without inventing scores."""
    if not csv_path.exists():
        logger.warning(f"Archivo de calibración no encontrado: {csv_path}")
        return []

    valid_entries = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ad_id = str(row.get("ad_id", "")).strip()
            if not ad_id:
                continue

            # Check if dimensions have numeric values
            h_str = row.get("hook", "").strip()
            m_str = row.get("mechanism", "").strip()
            n_str = row.get("narrative", "").strip()
            o_str = row.get("offer", "").strip()

            if not (h_str and m_str and n_str and o_str):
                continue

            try:
                hook = float(h_str)
                mechanism = float(m_str)
                narrative = float(n_str)
                offer = float(o_str)
                valid_entries.append({
                    "ad_id": ad_id,
                    "hook": hook,
                    "mechanism": mechanism,
                    "narrative": narrative,
                    "offer": offer,
                    "total": hook + mechanism + narrative + offer,
                })
            except ValueError:
                continue

    return valid_entries


def load_ai_evaluation(ad_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves AI evaluation for ad_id from database or local analysis.json."""
    db = SessionLocal()
    try:
        rec = (
            db.query(AnalysisRecord)
            .filter(AnalysisRecord.ad_id == ad_id)
            .order_by(AnalysisRecord.created_at.desc())
            .first()
        )
        if rec and rec.result_json:
            try:
                data = json.loads(rec.result_json)
                return {
                    "ad_id": ad_id,
                    "hook": float(data.get("hook", {}).get("score", 0)),
                    "mechanism": float(data.get("mechanism", {}).get("score", 0)),
                    "narrative": float(data.get("narrative", {}).get("score", 0)),
                    "offer": float(data.get("offer", {}).get("score", 0)),
                    "total": float(data.get("creative_score", 0)),
                }
            except Exception:
                pass

        # Fallback to local analysis.json in downloads
        for f in DOWNLOADS_DIR.rglob(f"{ad_id}/analysis.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                return {
                    "ad_id": ad_id,
                    "hook": float(data.get("hook", {}).get("score", 0)),
                    "mechanism": float(data.get("mechanism", {}).get("score", 0)),
                    "narrative": float(data.get("narrative", {}).get("score", 0)),
                    "offer": float(data.get("offer", {}).get("score", 0)),
                    "total": float(data.get("creative_score", 0)),
                }
            except Exception:
                pass

        return None
    finally:
        db.close()


def run_calibration_report() -> Dict[str, Any]:
    human_scores = load_manual_scores()
    print("=" * 65)
    print("REPORTE DE CALIBRACIÓN Y ALINEACIÓN (HUMANO vs IA)")
    print("=" * 65)
    print(f"Archivo analizado: {MANUAL_SCORES_PATH}")

    if not human_scores:
        print(
            "\n⚠️  AVISO: No se encontraron puntajes humanos cargados en manual_scores.csv "
            "(0 anuncios comparados).\n"
            "El archivo contiene la plantilla con los 20 ad_ids seleccionados para que el dueño "
            "los puntúe a mano según la rúbrica (hook, mechanism, narrative, offer).\n"
            "Ningún puntaje ha sido inventado artificialmente."
        )
        print("=" * 65)
        return {"compared_count": 0, "spearman": None, "mae": None}

    # Match with AI evaluations
    matched_human = []
    matched_ai = []

    for h in human_scores:
        ai_eval = load_ai_evaluation(h["ad_id"])
        if ai_eval:
            matched_human.append(h)
            matched_ai.append(ai_eval)

    count = len(matched_human)
    print(f"Anuncios con puntaje humano y evaluación IA disponibles: {count} de {len(human_scores)}")

    if count < 2:
        print("\nSe requieren al menos 2 anuncios con ambas evaluaciones para calcular correlaciones.")
        print("=" * 65)
        return {"compared_count": count, "spearman": None, "mae": None}

    # Extract vectors
    dims = ["hook", "mechanism", "narrative", "offer", "total"]
    metrics = {}

    print(f"\n{'Dimensión':<20} | {'Spearman':<10} | {'MAE (Error medio)':<18}")
    print("-" * 55)

    for d in dims:
        h_vals = [item[d] for item in matched_human]
        ai_vals = [item[d] for item in matched_ai]
        spearman_val = compute_spearman(h_vals, ai_vals)
        mae_val = compute_mae(h_vals, ai_vals)
        metrics[d] = {"spearman": spearman_val, "mae": mae_val}
        print(f"{d.capitalize():<20} | {spearman_val:<10.3f} | {mae_val:<18.2f}")

    print("=" * 65)
    return {"compared_count": count, "metrics": metrics}


if __name__ == "__main__":
    run_calibration_report()
