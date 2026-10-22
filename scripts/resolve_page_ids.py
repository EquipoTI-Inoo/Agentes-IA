"""
Resolves the official Meta page_ids of competitors of priority 2 and 3 (config/competitors_database.json).

    python scripts/resolve_page_ids.py --dry-run                         # plan only: counts, queries, time (no Meta, no writes)
    python scripts/resolve_page_ids.py --execute --retry-unresolved      # real run (the owner runs this)

Safe by design (see src/collect/page_resolver.py):
- Without --dry-run or --execute it only prints the plan and exits.
- Never guesses: assigns a page only when its name equals the competitor's name exactly or normalized.
- 1-3 s random pause before every Meta query and exponential backoff on blocks; a persistent block stops the run.
- Backs the catalog up first and saves after EACH competitor: if it is cut, run the same command again and it continues
  without repeating queries. Competitors that already have page_ids are never overwritten.
- --retry-unresolved also retries the ones already tried and left without a page (and withdraws low-confidence guesses
  that cannot be confirmed, keeping their ids under review_candidates).
"""

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.collect.page_resolver import PageResolver, backup_competitors_file, plan_resolution

DB_PATH = ROOT_DIR / "config" / "competitors_database.json"
EXECUTE_COMMAND = "python scripts/resolve_page_ids.py --execute --retry-unresolved"

CONFIDENCE_RULES = """\
Regla de confianza que aplica (no adivina):
  - Se asigna una página SOLO si su nombre es igual al del competidor (exacto o normalizado: minúsculas, solo letras y
    dígitos, con o sin el texto entre paréntesis). Nombres parecidos, insignia de verificada o categoría NO bastan.
  - alta : nombre exacto Y los anuncios de la página apuntan al sitio web del competidor (needs_review = false).
  - media: un único nombre exacto sin dominio verificable (needs_review = false); si sus anuncios apuntan a otro dominio,
           needs_review = true.
  - baja : sin páginas, sin nombre exacto, o varios nombres exactos sin dominio confirmado -> NO se asigna page_id,
           needs_review = true y se guardan hasta 3 review_candidates para decidir a mano."""


def _fmt_minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def print_plan(plan: dict, retry_unresolved: bool) -> None:
    print("=" * 72)
    print("RESOLUCIÓN DE PAGE_IDS — PLAN (no se hace ninguna consulta)")
    print("=" * 72)
    for pr, s in plan["by_priority"].items():
        print(f"Prioridad {pr}: {s['total']} competidores | sin page_id: {s['without_page_id']} "
              f"(nunca intentados: {s['never_attempted']}, ya intentados sin resolver: {s['unresolved_already_tried']}) "
              f"| con page_id de confianza baja: {s['low_confidence_with_id']} | se procesarían: {s['to_process']}")
    print("-" * 72)
    print(f"Modo: {'--retry-unresolved (reintenta los ya intentados)' if retry_unresolved else 'solo nunca intentados'}")
    print(f"Competidores a procesar:   {plan['to_process']}")
    if plan["names"]:
        print("  " + ", ".join(plan["names"][:25]) + (" ..." if len(plan["names"]) > 25 else ""))
    print(f"Consultas a Meta:          {plan['queries_min']} a {plan['queries_max']} (esperado ~{plan['queries_expected']})")
    print(f"Tiempo con pausa de 1-3 s: {_fmt_minutes(plan['seconds_min'])} a {_fmt_minutes(plan['seconds_max'])} "
          f"(esperado ~{_fmt_minutes(plan['seconds_expected'])}; sin contar lo que tarde Meta en responder)")
    print("-" * 72)
    print(CONFIDENCE_RULES)
    print("=" * 72)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Resuelve page_ids de competidores de prioridad 2 y 3")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Solo imprime el plan: sin consultas a Meta ni cambios")
    mode.add_argument("--execute", action="store_true", help="Ejecuta la resolución real (consulta Meta)")
    parser.add_argument("--retry-unresolved", action="store_true", help="Reintenta los que ya se intentaron sin resolver")
    parser.add_argument("--priorities", default="2,3", help="Prioridades a procesar (defecto: 2,3)")
    parser.add_argument("--max", type=int, default=None, help="Máximo de competidores por prioridad (para pruebas)")
    args = parser.parse_args(argv)

    priorities = tuple(int(p) for p in args.priorities.split(",") if p.strip())
    data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    plan = plan_resolution(data, priorities, args.retry_unresolved)
    print_plan(plan, args.retry_unresolved)

    if args.dry_run:
        print("[DRY-RUN] Terminado sin consultar Meta ni modificar el catálogo.")
        print(f"Para ejecutar la resolución real:  {EXECUTE_COMMAND}")
        return 0
    if not args.execute:
        print("No se ejecutó nada. Usa --dry-run para simular o --execute para la resolución real.")
        return 2
    if not plan["to_process"]:
        print("No hay competidores pendientes con estas opciones. (Usa --retry-unresolved para reintentar los ya intentados.)")
        return 0

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    backup = backup_competitors_file(DB_PATH)
    print(f"Backup del catálogo: config/{backup.name}")
    exit_code = 0
    with PageResolver() as resolver:
        for pr in priorities:
            print(f"Iniciando resolución (prioridad {pr})...")
            resolver.resolve_all_priority(priority=pr, save=True, db_path=DB_PATH, max_competitors=args.max,
                                          retry_unresolved=args.retry_unresolved, backup=False)
            run = resolver.last_run
            print(f"  procesados {run['processed']} | omitidos (ya tienen page_id) {run['skipped_existing']} | "
                  f"omitidos (ya intentados) {run['skipped_attempted']} | errores {len(run['errors'])} | consultas a Meta {run['queries']}")
            if run["blocked"]:
                print("Meta bloqueó las consultas. Lo resuelto quedó guardado; espera un rato y ejecuta el MISMO comando para continuar.")
                exit_code = 3
                break
    if exit_code == 0:
        print("Resolución completada. Revisa los competidores con needs_review = true en config/competitors_database.json.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
