# scripts/legacy — scripts históricos

Esta carpeta guarda scripts de fases anteriores del proyecto. **No tienen mantenimiento**: no se prueban, no se actualizan con el esquema de la base ni con el CLI y pueden fallar tal como están. Se conservan solo como referencia de cómo se hizo algo en su momento. Nada del código activo los importa.

Para operar Jinora usa `python -m src.cli` (ver el README de la raíz). Los scripts que sí se mantienen viven en `scripts/`: `calibrate.py`, `compare_transcripts.py`, `resolve_page_ids.py`, `run_phase4_real_tests.py` y `schedule_windows.ps1`.

| Archivo | Por qué es histórico |
|---|---|
| `cleanup_irrelevant.py` | Roto: consulta la columna `full_transcript`, que ya no existe en `ads` desde la Fase 6. La relevancia la hace la etapa `filter` del pipeline y el cleanup de catálogo es `python -m src.cli cleanup`. |
| `run_phase4_live_evals.py` | Duplica `scripts/run_phase4_real_tests.py` (que sí tiene tope de presupuesto por clave) y llama a Gemini con ids fijos. |
| `audit_media_phase3.py` | Auditoría puntual de la Fase 3; la etapa `media` del pipeline calcula hashes, pHash y metadatos. |
| `build_calibration_sample.py` | Armó una sola vez la muestra de calibración de la Fase 4. |
| `refine_sample.py` | Sustituyó una sola vez videos de esa muestra. |
| `legacy_inactive_viability.py` | Prueba puntual contra Meta (anuncios inactivos, Fase 3). Se renombró (antes `test_inactive_viability.py`) para que pytest no lo recolecte. |
| `legacy_phase2_collection.py` | Corrida de aceptación de la Fase 2 contra Meta. Se renombró (antes `test_phase2_collection.py`) por la misma razón. |
| `smart_search.py` | Expansor de búsquedas anterior al pipeline; nadie lo importaba. Antes vivía en `src/`. |

Cada archivo empieza con una línea `# LEGACY: <motivo>`. Los scripts calculan rutas a partir de su propia ubicación (`scripts/`), así que al estar un nivel más abajo esas rutas pueden ya no apuntar a la raíz del proyecto.

`pytest.ini` ordena a pytest ignorar esta carpeta.
