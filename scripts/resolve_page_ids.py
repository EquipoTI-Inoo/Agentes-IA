"""
Script para resolver page_ids de competidores de Prioridad 2 y 3 en Meta Ads Library.
NO EJECUTAR sin confirmación previa del usuario.
Utiliza src.collect.page_resolver.PageResolver con pausas aleatorias de 1 a 3 segundos.
"""

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

from src.collect.page_resolver import PageResolver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ResolvePageIDs")

DB_PATH = ROOT_DIR / "config" / "competitors_database.json"

def estimate_queries():
    with open(DB_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    comps = [c for p in data.get("pillars", {}).values() for c in p.get("competitors", [])]
    p2_unresolved = [c for c in comps if c.get("priority") == 2 and not c.get("page_ids")]
    p3_unresolved = [c for c in comps if c.get("priority") == 3 and not c.get("page_ids")]
    
    total_unresolved = len(p2_unresolved) + len(p3_unresolved)
    # Cada competidor realiza 1 consulta typeahead obligatoria, y hasta 2 consultas de anuncios
    # si hay candidatos con coincidencia inicial para verificar dominio.
    min_queries = total_unresolved * 1
    max_queries = total_unresolved * 3
    avg_queries = int(total_unresolved * 1.5)
    
    return {
        "p2_unresolved": len(p2_unresolved),
        "p3_unresolved": len(p3_unresolved),
        "total_unresolved": total_unresolved,
        "min_queries": min_queries,
        "max_queries": max_queries,
        "avg_queries": avg_queries,
    }

def main():
    est = estimate_queries()
    print("=" * 65)
    print("RESOLUCIÓN DE PAGE_IDS PARA PRIORIDAD 2 Y 3")
    print("=" * 65)
    print(f"Competidores Prioridad 2 sin page_id: {est['p2_unresolved']}")
    print(f"Competidores Prioridad 3 sin page_id: {est['p3_unresolved']}")
    print(f"Total competidores a procesar:        {est['total_unresolved']}")
    print(f"Consultas estimadas a Meta:           {est['min_queries']} a {est['max_queries']} (promedio ~{est['avg_queries']})")
    print("=" * 65)

    if "--dry-run" in sys.argv:
        print("[DRY-RUN] Saliendo sin ejecutar consultas.")
        return

    print("Iniciando resolución (Prioridad 2)...")
    with PageResolver() as resolver:
        resolver.resolve_all_priority(priority=2, save=True, db_path=DB_PATH)
        print("Iniciando resolución (Prioridad 3)...")
        resolver.resolve_all_priority(priority=3, save=True, db_path=DB_PATH)

    print("Resolución completada con éxito.")

if __name__ == "__main__":
    main()
