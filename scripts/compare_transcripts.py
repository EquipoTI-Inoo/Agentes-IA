"""
Independent Audio Transcription Comparison: Gemini Multimodal vs Local Faster-Whisper
Evaluates transcription fidelity across complete ad audio without truncating:
- Runs local Whisper independently directly from media files.
- Extracts complete Gemini transcription from database analyses.
- Computes standard Word Error Rate (WER) normalizing case, whitespace, and punctuation.
- Supports --ids argument.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
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

from src.database import AdRecord, AnalysisRecord, SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("Jinora.CompareTranscripts")
DOWNLOADS_DIR = ROOT_DIR / "downloads"


def normalize_words(text: str) -> List[str]:
    """Normalizes text by lowercasing, stripping punctuation, and splitting into words."""
    if not text:
        return []
    # Replace newlines, commas, periods, quotes, dashes with space
    clean = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    return [w for w in clean.split() if w.strip()]


def compute_word_error_rate(reference: str, hypothesis: str) -> Tuple[float, int, int]:
    """
    Computes exact Word Error Rate (WER) using Levenshtein distance on word tokens.
    Returns: (wer_score, edits, reference_word_count)
    """
    ref_words = normalize_words(reference)
    hyp_words = normalize_words(hypothesis)

    n = len(ref_words)
    m = len(hyp_words)

    if n == 0:
        return (0.0 if m == 0 else 1.0, m, 0)

    # Dynamic programming matrix for Levenshtein distance
    dp = [[0] * (m + 1) for _ in range(n + 1)]

    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref_words[i - 1] == hyp_words[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(
                    dp[i - 1][j],      # deletion
                    dp[i][j - 1],      # insertion
                    dp[i - 1][j - 1],  # substitution
                )

    edits = dp[n][m]
    wer = edits / float(n)
    return (wer, edits, n)


def top_differences(reference: str, hypothesis: str, n: int = 3) -> List[Dict[str, str]]:
    """Largest word-level differences (normalized tokens) between Whisper (ref) and Gemini (hyp)."""
    import difflib

    ref_words = normalize_words(reference)
    hyp_words = normalize_words(hypothesis)
    matcher = difflib.SequenceMatcher(a=ref_words, b=hyp_words, autojunk=False)
    diffs = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        diffs.append({
            "type": {"replace": "sustitución", "delete": "solo Whisper", "insert": "solo Gemini"}[tag],
            "whisper": " ".join(ref_words[i1:i2]),
            "gemini": " ".join(hyp_words[j1:j2]),
            "context": " ".join(ref_words[max(0, i1 - 4):i1]),
        })
    diffs.sort(key=lambda d: max(len(d["whisper"].split()), len(d["gemini"].split())), reverse=True)
    return diffs[:n]


def transcribe_with_local_whisper(
    video_path: Path,
    model_size: str = "base",
    device: str = "cpu",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Runs local Faster-Whisper transcription on the video file."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster_whisper no está instalado. Instálalo con 'pip install faster-whisper'.")
        return ("", [])

    logger.info(f"Cargando modelo local Whisper ({model_size}) en {device}...")
    model = WhisperModel(model_size, device=device, compute_type="int8")

    logger.info(f"Transcribiendo con Whisper local: {video_path.name}...")
    segments, info = model.transcribe(str(video_path), beam_size=5)

    segments_list = []
    full_text_parts = []
    for s in segments:
        text = s.text.strip()
        segments_list.append({
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "text": text,
        })
        full_text_parts.append(text)

    full_text = " ".join(full_text_parts)
    return (full_text, segments_list)


def load_gemini_transcript(ad_id: str, db: Any) -> Optional[str]:
    """Loads Gemini complete transcription from analyses table or analysis.json."""
    record = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.ad_id == ad_id, AnalysisRecord.analysis_type == "video", AnalysisRecord.model.like("gemini%"))
        .order_by(AnalysisRecord.id.desc())
        .first()
    )
    if record and record.result_json:
        try:
            data = json.loads(record.result_json)
            trans_list = data.get("transcript", [])
            if trans_list:
                return " ".join(t.get("text", "").strip() for t in trans_list)
        except Exception:
            pass

    # Check analysis.json in ad folder
    for p in DOWNLOADS_DIR.glob(f"**/{ad_id}/analysis.json"):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                trans_list = data.get("transcript", [])
                if trans_list:
                    return " ".join(t.get("text", "").strip() for t in trans_list)
            except Exception:
                pass
    return None


def run_comparison(
    ad_ids: List[str],
    whisper_model: str = "base",
    device: str = "cpu",
) -> List[Dict[str, Any]]:
    """Runs side-by-side comparison for given ad IDs."""
    db = SessionLocal()
    results = []

    try:
        for aid in ad_ids:
            ad_rec = db.query(AdRecord).filter(AdRecord.id == aid).first()
            if not ad_rec:
                logger.warning(f"Anuncio {aid} no encontrado en BD.")
                continue

            v_path_str = ad_rec.video_path
            v_path = Path(v_path_str) if v_path_str else None
            if v_path and not v_path.is_absolute():
                v_path = DOWNLOADS_DIR / v_path

            if not v_path or not v_path.exists():
                # Search in downloads
                matches = list(DOWNLOADS_DIR.glob(f"**/{aid}/*.mp4"))
                v_path = matches[0] if matches else None

            if not v_path or not v_path.exists():
                logger.warning(f"Video local para {aid} no encontrado en disco.")
                continue

            gemini_full = load_gemini_transcript(aid, db)
            if not gemini_full:
                logger.warning(f"No hay transcripción de Gemini guardada para {aid}. Ejecuta la evaluación primero.")
                continue

            whisper_full, _ = transcribe_with_local_whisper(v_path, model_size=whisper_model, device=device)

            wer, edits, total_words = compute_word_error_rate(
                reference=whisper_full,
                hypothesis=gemini_full,
            )

            results.append({
                "ad_id": aid,
                "page_name": ad_rec.page_name,
                "video_file": v_path.name,
                "whisper_word_count": len(normalize_words(whisper_full)),
                "gemini_word_count": len(normalize_words(gemini_full)),
                "wer_percent": round(wer * 100, 2),
                "edits": edits,
                "whisper_source": "faster-whisper ejecutado sobre el archivo de video en esta corrida (no leído de la BD)",
                "differences": top_differences(whisper_full, gemini_full, 3),
                "whisper_full": whisper_full,
                "gemini_full": gemini_full,
            })

    finally:
        db.close()

    # Print summary
    print("\n" + "=" * 70)
    print("🔬 COMPARACIÓN INDEPENDIENTE DE TRANSCRIPCIÓN: GEMINI vs LOCAL WHISPER")
    print("=" * 70)
    for r in results:
        print(f"\nANUNCIO: {r['ad_id']} ({r['page_name']} | {r['video_file']})")
        print(f"  * Palabras Whisper : {r['whisper_word_count']}")
        print(f"  * Palabras Gemini  : {r['gemini_word_count']}")
        print(f"  * Word Error Rate  : {r['wer_percent']}% ({r['edits']} ediciones / {r['whisper_word_count']} palabras)")
        print(f"\n--- Transcripción Completa Whisper (Local) ---")
        print(r['whisper_full'] if r['whisper_full'] else "[SIN VOZ / VACÍO]")
        print(f"\n--- Transcripción Completa Gemini (Multimodal) ---")
        print(r['gemini_full'] if r['gemini_full'] else "[SIN VOZ / VACÍO]")
        print(f"\n--- 3 diferencias más largas (tokens normalizados) ---")
        for d in r["differences"]:
            print(f"  [{d['type']}] ...{d['context']}... | Whisper: '{d['whisper']}' | Gemini: '{d['gemini']}'")
        print("-" * 70)
    print("=" * 70 + "\n")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Compara transcripciones de Gemini vs Whisper local con WER.")
    parser.add_argument("--ids", nargs="+", help="IDs de anuncios a comparar", default=[])
    parser.add_argument("--model", default="base", help="Tamaño del modelo Whisper (tiny, base, small)")
    parser.add_argument("--device", default="cpu", help="Dispositivo para Whisper (cpu, cuda)")
    parser.add_argument("--out", help="Ruta JSON donde guardar los resultados completos")
    args = parser.parse_args()

    if not args.ids:
        print("Uso: python scripts/compare_transcripts.py --ids <ad_id1> <ad_id2> ...")
        return

    results = run_comparison(args.ids, whisper_model=args.model, device=args.device)
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Resultados guardados en {args.out}")


if __name__ == "__main__":
    main()
