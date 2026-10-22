# LEGACY: prueba puntual contra Meta (viabilidad de anuncios inactivos, Fase 3); renombrado para que pytest no lo recolecte
"""
Prueba de viabilidad de anuncios inactivos en Meta Ads Library.
Solo lectura, sin guardar en base de datos ni llamar a LLMs.
Prueba ManyChat, Respond.io y HubSpot en PE, MX, CO.
"""

import logging
import random
import sys
import time
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

from meta_ads_collector import MetaAdsCollector

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("InactiveViability")

TARGETS = [
    {"name": "ManyChat", "page_id": "521778124648972"},
    {"name": "Respond.io", "page_id": "633874670104937"},
    {"name": "HubSpot", "page_id": "192993730741131"},
]

COUNTRIES = ["PE", "MX", "CO"]

def main():
    collector = MetaAdsCollector()
    results = {}
    total_inactive = 0

    print("=" * 65)
    print("🔬 PRUEBA DE VIABILIDAD DE ANUNCIOS INACTIVOS (PE, MX, CO)")
    print("=" * 65)

    try:
        for t in TARGETS:
            name = t["name"]
            pid = t["page_id"]
            results[name] = {}
            for country in COUNTRIES:
                time.sleep(random.uniform(1.0, 2.0))
                try:
                    ads = list(collector.search(
                        query="",
                        country=country,
                        search_type="PAGE",
                        page_ids=[pid],
                        status="INACTIVE",
                        max_results=20,
                    ))
                    cnt = len(ads)
                    results[name][country] = cnt
                    total_inactive += cnt
                    print(f"[{name:<12}] {country}: {cnt} inactivos devueltos por Meta")
                except Exception as e:
                    logger.warning(f"Error consultando {name} en {country}: {e}")
                    results[name][country] = f"Error: {e}"

        print("-" * 65)
        print(f"TOTAL ANUNCIOS INACTIVOS ENCONTRADOS: {total_inactive}")
        print("=" * 65)
        if total_inactive <= 5:
            print("Conclusión: Los inactivos comerciales devueltos en LatAm son casi cero. Se mantiene 'active' por defecto.")
        else:
            print(f"Conclusión: Se encontraron {total_inactive} inactivos. Guardar solo como datos sin evaluarlos con IA.")
    finally:
        collector.close()

if __name__ == "__main__":
    main()
