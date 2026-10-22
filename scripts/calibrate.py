"""
Calibration and Alignment Script (Phase 4.6)
Compares AI evaluations against human scores in calibration/manual_scores.csv:
- Computes Spearman rank correlation per dimension and overall.
- Computes Mean Absolute Error (MAE) and Mean Difference (AI - Human).
- Analyzes results grouped by creative 'format' (animacion, ugc, talking_head, etc.).
- Respects source tags: only includes verified scores (e.g. source='owner'), ignoring source='placeholder'.
- Runs cleanly even if scores are partially populated without inventing scores.
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from collections import defaultdict
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


def compute_mean_diff(ai_vals: List[float], human_vals: List[float]) -> float:
    """Computes Mean Signed Difference (AI - Human). Positive means AI is more generous."""
    if not ai_vals:
        return 0.0
    return sum(a - h for a, h in zip(ai_vals, human_vals)) / float(len(ai_vals))


def load_manual_scores(csv_path: Path = MANUAL_SCORES_PATH) -> List[Dict[str, Any]]:
    """Loads human scores from CSV; ignores placeholder rows and empty entries without inventing scores."""
    if not csv_path.exists():
        logger.warning(f"Archivo de calibracion no encontrado: {csv_path}")
        return []

    valid_entries = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ad_id = str(row.get("ad_id", "")).strip()
            if not ad_id:
                continue

            source = str(row.get("source", "unknown")).strip().lower()
            if source == "placeholder":
                continue

            # Support total_score or sum of 4 dimensions
            tot_str = row.get("total_score", "").strip()
            h_str = row.get("hook", "").strip()
            m_str = row.get("mechanism", "").strip()
            n_str = row.get("narrative", "").strip()
            o_str = row.get("offer", "").strip()

            hook = float(h_str) if h_str else None
            mechanism = float(m_str) if m_str else None
            narrative = float(n_str) if n_str else None
            offer = float(o_str) if o_str else None

            if tot_str:
                total = float(tot_str)
            elif hook is not None and mechanism is not None and narrative is not None and offer is not None:
                total = hook + mechanism + narrative + offer
            else:
                continue

            valid_entries.append({
                "ad_id": ad_id,
                "total": total,
                "hook": hook,
                "mechanism": mechanism,
                "narrative": narrative,
                "offer": offer,
                "source": source,
            })

    return valid_entries


def load_ai_evaluation(ad_id: str, db: Any) -> Optional[Dict[str, Any]]:
    """Fetches the latest AI analysis for an ad from DB or analysis.json."""
    record = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.ad_id == ad_id)
        .order_by(AnalysisRecord.id.desc())
        .first()
    )

    if record and record.result_json:
        try:
            data = json.loads(record.result_json)
            # Ensure total score is extracted
            total_score = record.creative_score or data.get("creative_score") or data.get("total_score")
            hook = data.get("hook", {}).get("score") if isinstance(data.get("hook"), dict) else None
            mechanism = data.get("mechanism", {}).get("score") if isinstance(data.get("mechanism"), dict) else None
            narrative = data.get("narrative", {}).get("score") if isinstance(data.get("narrative"), dict) else None
            offer = data.get("offer", {}).get("score") if isinstance(data.get("offer"), dict) else None
            ad_format = data.get("format", "otro")

            return {
                "total": float(total_score) if total_score is not None else None,
                "hook": float(hook) if hook is not None else None,
                "mechanism": float(mechanism) if mechanism is not None else None,
                "narrative": float(narrative) if narrative is not None else None,
                "offer": float(offer) if offer is not None else None,
                "format": ad_format,
            }
        except Exception as e:
            logger.debug(f"Error parsing result_json for {ad_id}: {e}")

    # Fallback to local analysis.json
    for p in DOWNLOADS_DIR.glob(f"**/{ad_id}/analysis.json"):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return {
                    "total": float(data.get("creative_score", 0)),
                    "hook": float(data.get("hook", {}).get("score", 0)),
                    "mechanism": float(data.get("mechanism", {}).get("score", 0)),
                    "narrative": float(data.get("narrative", {}).get("score", 0)),
                    "offer": float(data.get("offer", {}).get("score", 0)),
                    "format": data.get("format", "otro"),
                }
            except Exception:
                pass

    return None


def run_calibration_report(csv_path: Path = MANUAL_SCORES_PATH) -> Dict[str, Any]:
    """Generates complete Spearman rank correlation, MAE, and per-format breakdown."""
    manual_entries = load_manual_scores(csv_path)

    if not manual_entries:
        msg = (
            f"AVISO: No se encontraron puntajes humanos validos en {csv_path.name} "
            f"(filas con puntaje real o con source != 'placeholder': 0). "
            f"El archivo contiene la plantilla con los 20 ad_ids seleccionados. "
            f"Ningun puntaje ha sido inventado artificialmente."
        )
        print("\n" + "=" * 65)
        print("REPORTE DE CALIBRACION Y ALINEACION (HUMANO vs IA)")
        print("=" * 65)
        print(f"Archivo analizado: {csv_path}")
        print(f"\n[!]  {msg}")
        print("=" * 65 + "\n")
        return {"status": "no_human_scores", "total_evaluated": 0}

    db = SessionLocal()
    matched_data = []

    try:
        for m in manual_entries:
            ai_data = load_ai_evaluation(m["ad_id"], db)
            if ai_data and ai_data["total"] is not None:
                matched_data.append({"human": m, "ai": ai_data})
    finally:
        db.close()

    if not matched_data:
        print("\n" + "=" * 65)
        print("REPORTE DE CALIBRACION Y ALINEACION (HUMANO vs IA)")
        print("=" * 65)
        print(f"Puntajes humanos validos cargados: {len(manual_entries)}")
        print("Anuncios que cuentan con evaluacion IA en la BD: 0")
        print("Ejecuta primero la evaluacion con IA para generar los puntajes a comparar.")
        print("=" * 65 + "\n")
        return {"status": "no_ai_matches", "human_count": len(manual_entries), "ai_count": 0}

    # 1. Total score metrics
    human_totals = [d["human"]["total"] for d in matched_data]
    ai_totals = [d["ai"]["total"] for d in matched_data]

    spearman_total = compute_spearman(ai_totals, human_totals)
    mae_total = compute_mae(ai_totals, human_totals)
    mean_diff_total = compute_mean_diff(ai_totals, human_totals)

    # 2. Dimensional metrics (if dimensions are present)
    dim_metrics = {}
    for dim in ["hook", "mechanism", "narrative", "offer"]:
        h_vals = [d["human"][dim] for d in matched_data if d["human"][dim] is not None and d["ai"][dim] is not None]
        a_vals = [d["ai"][dim] for d in matched_data if d["human"][dim] is not None and d["ai"][dim] is not None]
        if len(h_vals) >= 2:
            dim_metrics[dim] = {
                "count": len(h_vals),
                "spearman": compute_spearman(a_vals, h_vals),
                "mae": compute_mae(a_vals, h_vals),
                "mean_diff": compute_mean_diff(a_vals, h_vals),
            }

    # 3. Format breakdown
    by_format = defaultdict(lambda: {"human": [], "ai": []})
    for d in matched_data:
        fmt = d["ai"]["format"]
        by_format[fmt]["human"].append(d["human"]["total"])
        by_format[fmt]["ai"].append(d["ai"]["total"])

    format_metrics = {}
    for fmt, vals in by_format.items():
        h_f = vals["human"]
        a_f = vals["ai"]
        format_metrics[fmt] = {
            "count": len(h_f),
            "spearman": compute_spearman(a_f, h_f) if len(h_f) >= 2 else 0.0,
            "mae": compute_mae(a_f, h_f),
            "mean_diff": compute_mean_diff(a_f, h_f),
        }

    # Render Report
    print("\n" + "=" * 65)
    print("REPORTE DE CALIBRACION Y ALINEACION (HUMANO vs IA)")
    print("=" * 65)
    print(f"Total de anuncios comparados: {len(matched_data)} (de {len(manual_entries)} puntuados a mano)")
    print("-" * 65)
    print(f"PUNTAJE TOTAL (0-100):")
    print(f"  * Correlacion de Spearman (r): {spearman_total:+.3f}")
    print(f"  * Error Medio Absoluto (MAE) : {mae_total:.1f} pts")
    print(f"  * Diferencia Media (IA - Hum): {mean_diff_total:+.1f} pts ({'IA infla' if mean_diff_total > 0 else 'IA estricta'})")

    if dim_metrics:
        print("-" * 65)
        print("DESGLOSE POR DIMENSION (0-25):")
        for dim, m in dim_metrics.items():
            print(f"  * {dim.capitalize():<12}: r={m['spearman']:+.3f} | MAE={m['mae']:.1f} pts | DifMedia={m['mean_diff']:+.1f} pts (n={m['count']})")

    print("-" * 65)
    print("ANALISIS POR FORMATO CREATIVO (Neutralidad):")
    for fmt, m in sorted(format_metrics.items()):
        print(f"  * {fmt:<14}: n={m['count']:<2} | MAE={m['mae']:.1f} pts | DifMedia={m['mean_diff']:+.1f} pts | r={m['spearman']:+.3f}")

    print("=" * 65 + "\n")

    return {
        "status": "success",
        "count": len(matched_data),
        "total": {
            "spearman": spearman_total,
            "mae": mae_total,
            "mean_diff": mean_diff_total,
        },
        "dimensions": dim_metrics,
        "by_format": format_metrics,
    }


if __name__ == "__main__":
    run_calibration_report()
