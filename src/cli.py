"""
Jinora command line (Phase 7, B2). Single entry point for humans, schedulers and agents:

    python -m src.cli run --pillar conversacional --countries PE,MX,CO --limit 20
    python -m src.cli run --competitor "Kommo" --stages download,media,evaluate
    python -m src.cli evaluate --pending            (or: evaluate --force --prompt-version v2)
    python -m src.cli report --pillar crm --top 10
    python -m src.cli cleanup --dry-run | --apply
    python -m src.cli queue --status | --run [--respect-hours] [--now]
    python -m src.cli rescore
    python -m src.cli worker [--once]

main.py delegates here for queue / rescore / run / evaluate / report / cleanup / worker.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

COMMANDS = ("run", "evaluate", "report", "cleanup", "queue", "rescore", "worker")


def _split(csv_text: Optional[str]) -> List[str]:
    return [p.strip() for p in (csv_text or "").split(",") if p.strip()]


# --------------------------------------------------------------------------------------
# queue --status (B4)
# --------------------------------------------------------------------------------------

def show_queue_status() -> int:
    from src.ai.quota import get_quota_guard
    from src.config import is_within_preferred_hours, settings
    from src.cost import CostSnapshot
    from src.database import SessionLocal, engine
    from src.db.engine import is_legacy_schema
    from src.work_queue import estimate_days_to_clear, queue_stats

    if is_legacy_schema(engine):
        print("❌ La base tiene el esquema anterior a la Fase 6. Ejecuta: python -m src.db.migrate")
        return 1

    with SessionLocal() as db:
        s = queue_stats(db)
    guard = get_quota_guard()
    snap = CostSnapshot.take(guard)
    main_usage = guard.get_daily_usage(settings.LLM_MODEL, key_fingerprint=snap.key_fingerprint)
    cheap_usage = guard.get_daily_usage(settings.LLM_MODEL_CHEAP, key_fingerprint=snap.key_fingerprint)
    legacy_today = guard.get_key_usage_today("")
    usable_rpd = max(1, settings.GEMINI_RPD - settings.RESERVE_REQUESTS)
    days = estimate_days_to_clear(s["queue_video"], settings.GEMINI_RPD, settings.RESERVE_REQUESTS)

    print("=" * 70)
    print("📊 ESTADO DE LA COLA DE EVALUACIÓN")
    print("=" * 70)
    print(f"Total anuncios en base de datos:             {s['total_ads']}")
    print(f"  - ⏳ En cola (pending_evaluation, con media): {s['queue']}")
    print(f"      🎥 Videos → Gemini:                      {s['queue_video']}")
    print(f"      🖼️ Estáticos → Claude:                   {s['queue_static']}")
    print(f"  - Pendientes sin archivo local (no evaluables): {s['pending_without_media']}")
    print(f"  - Excluidos de la cola: rejected {s['rejected']} | out_of_catalog {s['out_of_catalog']}")
    print(f"Anuncios evaluados con rúbrica IA:           {s['evaluated']}")
    print(f"Anuncios sin evaluar (sin creative_score):   {s['unevaluated']}")
    if s["grouped_ads"]:
        print(f"Grupos de creativos (variantes agrupadas):   {s['creative_groups']} grupos para {s['grouped_ads']} anuncios")
    else:
        print("Grupos de creativos:                         sin calcular (ejecuta: python main.py rescore)")
    print("-" * 70)
    print("📈 CONSUMO Y CUOTA DE HOY (GEMINI - America/Los_Angeles):")
    print(f"  - Clave Gemini activa (fingerprint):       {snap.key_fingerprint}")
    print(f"  - Solicitudes de esta clave hoy:           {snap.gemini_requests} (éxitos {snap.gemini_success}, fallos {snap.gemini_failed})")
    if legacy_today:
        print(f"  - Solicitudes sin fingerprint hoy:         {legacy_today} (registradas antes del conteo por clave)")
    exhausted = main_usage.get("is_exhausted")
    print(f"  - Estado de la cuota:                      {'🛑 AGOTADA (429 RPD - reanuda a medianoche PT)' if exhausted else '🟢 ACTIVA'}")
    print(f"  - Límite diario / reserva / útiles:        {settings.GEMINI_RPD} / {settings.RESERVE_REQUESTS} / {usable_rpd}")
    print(f"    * Modelo principal ({settings.LLM_MODEL}): {main_usage['requests']} req ({main_usage.get('requests_success', 0)} ok, {main_usage.get('requests_failed', 0)} fail)")
    print(f"    * Modelo barato ({settings.LLM_MODEL_CHEAP}): {cheap_usage['requests']} req ({cheap_usage.get('requests_success', 0)} ok, {cheap_usage.get('requests_failed', 0)} fail)")
    print(f"  - Ventana preferida ({settings.EVAL_PREFERRED_HOURS} Lima): {'🟢 DENTRO' if is_within_preferred_hours() else '⏳ FUERA'}")
    print("-" * 70)
    print("⏱️ DÍAS ESTIMADOS PARA VACIAR LA COLA:")
    print(f"  - Videos en cola / (RPD − RESERVE) = {s['queue_video']} / {usable_rpd}  →  ~{days} día(s)")
    print(f"  - Estáticos ({s['queue_static']}): usan Claude; gasto ${snap.claude_usd:.4f} de tope ${settings.CLAUDE_BUDGET_USD:.2f}")
    print("=" * 70)
    return 0


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    from src import jobs
    from src.config import settings
    from src.pipeline import STAGES, Pipeline, RunOptions, format_summary

    opts = RunOptions(
        pillar=args.pillar, competitor=args.competitor, query=args.query, all_pillars=args.all,
        countries=_split(args.countries) or list(settings.DEFAULT_COUNTRIES), limit=args.limit,
        stages=_split(args.stages) or STAGES, media_filter=args.media, keywords=args.keywords,
        forensics=args.forensics, download_min_days=args.download_min_days, max_ads=args.max_ads,
        force=args.force, prompt_version=args.prompt_version,
    )
    if args.enqueue:
        print(f"Job encolado: {jobs.enqueue('run', opts.to_dict())} (lo procesa: python -m src.worker)")
        return 0
    summary = Pipeline().run(opts)
    print(format_summary(summary))
    return 0


def _check_prompt_version(version: Optional[str]) -> None:
    if version:
        from src.ai.prompts import prompt_path

        prompt_path("video", version)
        prompt_path("static", version)


def cmd_evaluate(args: argparse.Namespace, respect_hours: bool = False, now: bool = False) -> int:
    from src.config import is_within_preferred_hours, settings
    from src.pipeline import Pipeline, RunOptions, format_summary

    if respect_hours and not now and not is_within_preferred_hours():
        print(
            f"[INFO] Fuera de la ventana preferida ({settings.EVAL_PREFERRED_HOURS} America/Lima). "
            "Evaluación pospuesta para horario de baja demanda. Usa --now para procesar de inmediato."
        )
        return 0
    _check_prompt_version(args.prompt_version)
    opts = RunOptions(
        stages=("evaluate", "signals"), force=args.force, prompt_version=args.prompt_version,
        eval_limit=args.limit, pillar=args.pillar, competitor=args.competitor,
    )
    print(format_summary(Pipeline().run(opts)))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from src.database import SessionLocal
    from src.report import build_report, format_report

    with SessionLocal() as db:
        print(format_report(build_report(db, pillar=args.pillar, top=args.top)))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from src.catalog import cleanup_report, format_cleanup_report, mark_out_of_catalog
    from src.database import SessionLocal

    with SessionLocal() as db:
        report = cleanup_report(db)
        if args.apply:
            mark_out_of_catalog(db)
        print(format_cleanup_report(report, applied=args.apply))
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    if args.run:
        return cmd_evaluate(
            argparse.Namespace(force=False, prompt_version=None, limit=None, pillar=None, competitor=None),
            respect_hours=args.respect_hours, now=args.now,
        )
    return show_queue_status()


def cmd_rescore(args: argparse.Namespace) -> int:
    from src.scoring.jinora import rescore_all

    summary = rescore_all(refingerprint=args.refingerprint)
    print("=" * 70)
    print("🧮 RESCORE LOCAL — sin llamadas a Gemini ni a Claude")
    print("=" * 70)
    print(f"Anuncios recalculados:                       {summary['ads']}")
    print(f"Grupos de creativos:                         {summary['groups']} ({summary['multi_variant_groups']} con 2+ variantes)")
    if summary["market_median"] is not None:
        print(f"market_score (mín / mediana / máx):          {summary['market_min']:.2f} / {summary['market_median']:.2f} / {summary['market_max']:.2f}")
    print(f"jinora_score calculado:                      {summary['jinora_scored']}")
    print(f"jinora_score NULL (sin creative_score):      {summary['jinora_null']}")
    print("=" * 70)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    from src.worker import main as worker_main

    return worker_main((["--once"] if args.once else []) + ["--poll", str(args.poll)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.cli", description="Jinora: pipeline, cola de evaluación y reportes")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Ejecuta etapas del pipeline (discover→filter→download→media→evaluate→signals)")
    run.add_argument("--pillar", choices=["voz", "conversacional", "crm"])
    run.add_argument("--competitor", help="Nombre de un competidor del catálogo")
    run.add_argument("--query", help="Búsqueda libre (marca o palabra clave) fuera del catálogo")
    run.add_argument("--all", action="store_true", help="Todos los competidores del catálogo")
    run.add_argument("--countries", help="Códigos ISO separados por coma (defecto: DEFAULT_COUNTRIES)")
    run.add_argument("--limit", type=int, default=20, help="Anuncios por consulta")
    run.add_argument("--stages", help="Etapas separadas por coma (defecto: todas)")
    run.add_argument("--media", choices=["video", "image", "all"], default="all")
    run.add_argument("--keywords", action="store_true", help="Descubrir también con las palabras clave del pilar")
    run.add_argument("--forensics", action="store_true", help="Transcripción Whisper local en la etapa media")
    run.add_argument("--download-min-days", type=int, default=0, help="Solo descargar anuncios con al menos N días activos")
    run.add_argument("--max-ads", type=int, help="Detener el descubrimiento tras N anuncios")
    run.add_argument("--force", action="store_true", help="Reevaluar aunque ya estén evaluados")
    run.add_argument("--prompt-version")
    run.add_argument("--enqueue", action="store_true", help="Encolar como job en lugar de ejecutar aquí")
    run.set_defaults(func=cmd_run)

    ev = sub.add_parser("evaluate", help="Evalúa la cola (videos→Gemini, estáticos→Claude)")
    ev.add_argument("--pending", action="store_true", help="Solo pendientes (comportamiento por defecto)")
    ev.add_argument("--force", action="store_true", help="Reevaluar también los ya evaluados")
    ev.add_argument("--prompt-version", help="Versión del prompt (prompts/evaluator_<v>.md)")
    ev.add_argument("--limit", type=int)
    ev.add_argument("--pillar", choices=["voz", "conversacional", "crm"])
    ev.add_argument("--competitor")
    ev.set_defaults(func=cmd_evaluate)

    rep = sub.add_parser("report", help="Top de anuncios por jinora_score")
    rep.add_argument("--pillar", choices=["voz", "conversacional", "crm"])
    rep.add_argument("--top", type=int, default=10)
    rep.set_defaults(func=cmd_report)

    cl = sub.add_parser("cleanup", help="Marca como out_of_catalog los anuncios de páginas que no son competidores")
    group = cl.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Solo lista lo que marcaría")
    group.add_argument("--apply", action="store_true", help="Marca (no borra)")
    cl.set_defaults(func=cmd_cleanup)

    qu = sub.add_parser("queue", help="Estado y ejecución de la cola de evaluación")
    qu.add_argument("--status", action="store_true")
    qu.add_argument("--run", action="store_true")
    qu.add_argument("--respect-hours", action="store_true", help="Respeta la ventana horaria preferida (01:00-06:00 Lima)")
    qu.add_argument("--now", action="store_true", help="Ignora la ventana horaria")
    qu.set_defaults(func=cmd_queue)

    rs = sub.add_parser("rescore", help="Recalcula market_score y jinora_score sin APIs")
    rs.add_argument("--refingerprint", action="store_true")
    rs.set_defaults(func=cmd_rescore)

    wk = sub.add_parser("worker", help="Procesa la cola de jobs (proceso separado)")
    wk.add_argument("--once", action="store_true")
    wk.add_argument("--poll", type=float, default=2.0)
    wk.set_defaults(func=cmd_worker)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    from src.logging_setup import setup_logging

    setup_logging(level=logging.WARNING if args.command in ("queue", "report", "cleanup", "rescore") else logging.INFO)
    try:
        return int(args.func(args) or 0)
    except (ValueError, FileNotFoundError) as exc:  # user-fixable problems: clear message, no traceback
        print(f"❌ {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
