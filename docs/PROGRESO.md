# Jinora — Progreso de implementación

> Este archivo lo **actualiza el agente** (Antigravity). El plan (`docs/PLAN_IMPLEMENTACION.md` y `docs/PLAN_ADS_ESTATICOS.md`) es solo lectura: no lo edites. Aquí marca lo hecho, anota decisiones y bloqueos, y haz commit de este archivo junto con el código.

## Fases
| Fase | Estado | Notas |
|---|---|---|
| 0 — Preparación | ✅ Hecha | rama `feat/mejoras-ia`, backup de la DB, `config.py`, `.env.example`, `requirements.txt`, `.gitignore` |
| 0.1 — Modo Gemini gratuito + corrida cada 15 días | ✅ Hecha | `src/config.py` extendido con cuotas y lotes. Implementado `src/ai/quota.py` (`QuotaGuard`, tabla `llm_usage` en hora PT, 429 backoff, `QuotaExhausted`), cola priorizada y filtro heurístico. Creado `scripts/schedule_windows.ps1` (dry-run verificado, sin ejecutar). 9 tests pasando. |
| 0.2 — Gemini reemplaza Whisper y FFmpeg | ✅ Hecha | Limpieza de Fase 0 (removidos faster-whisper, anthropic, openai). Fijado google-genai>=2.32.0 (con video_metadata y Files API). Agregado pymediainfo>=7.0.0. Dockerfile actualizado con libmediainfo0v5. Creado src/media/media_info.py y marcado video_forensics.py como DEPRECATED. 12 tests pasando. Servidor y tests corren sin faster-whisper ni ffmpeg. |
| 1 — Correcciones rápidas | ✅ Hecha | Quitado fps_sample en mass_global_harvester.py. Rutas normalizadas a relativas con fix_paths.py (75 filas actualizadas en la DB). Regex de palabra completa en relevance.py (tests pasando para Air Wick, Airmoto, Chair Co, Rom-Com Reads vs Bland AI, Kommo, Vapi AI, HubSpot). Descarga en streaming a .tmp con rename, retry tenacity y tope de 200MB en scraper.py. start_server.py lee HOST/PORT/ENV. main.py usa datetime.now(). Generado data/rejected_ads.csv (14 anuncios detectados sin tocar la BD). 20 tests pasando. |
| 2 — Datos limpios por `page_id` + LatAm | ⬜ Pendiente | |
| 3 — Preparación de media (Gemini) | ⬜ Pendiente | |
| 4 — Evaluador Gemini + calibración | ⬜ Pendiente | Requiere 20 anuncios puntuados a mano por el usuario |
| 5 — Señal de mercado y puntaje final | ⬜ Pendiente | |
| 6 — Modelo de datos y migración | ⬜ Pendiente | |
| 7 — Pipeline unificado y cola | ⬜ Pendiente | |
| 8 — Dashboard y reportes | ⬜ Pendiente | |
| 9 — Agente, pruebas y despliegue | ⬜ Pendiente | |
| S1–S7 — Anuncios estáticos | ⬜ Pendiente | `docs/PLAN_ADS_ESTATICOS.md` |

## Decisiones vigentes
- Solo Gemini (nivel gratuito, 20 solicitudes/día). Sin Claude ni OpenAI.
- Sin Whisper ni FFmpeg: Gemini transcribe y analiza el video. Se conservan `phash` y lectura de metadata (`pymediainfo`).
- `src/video_forensics.py` queda como DEPRECATED hasta verificar la transcripción de Gemini (Fase 0.2.2).

## Bitácora
- 2026-10-21: Fase 0 terminada (backup de BD, config base, git init). Fase 0.1 completada: implementación de `src/ai/quota.py` (`QuotaGuard`, tabla `llm_usage` con día en hora del Pacífico, soporte de backoff 429, excepción `QuotaExhausted`), cola priorizada (`sort_ads_for_evaluation`), filtro de descarte de bajo valor (`should_evaluate_ad`), script de automatización `scripts/schedule_windows.ps1` (validado en seco sin registrar), suite de 9 tests unitarios pasando al 100%.
- 2026-10-21: Fase 0.2 completada: se retiraron dependencias y claves de Whisper, Claude y OpenAI de `src/config.py`, `.env.example`, `.env` y `requirements.txt`. Se fijó `google-genai>=2.32.0` (verificada compatibilidad con video_metadata y Files API). Se añadió `pymediainfo>=7.0.0` y se actualizó `Dockerfile` con `libmediainfo0v5`. Se implementó `src/media/media_info.py` (`get_video_info`, `get_image_info`). Se marcó `src/video_forensics.py` como `# DEPRECATED` con guard de importación. Suite de 12 tests pasando y servidor verificado sin faster-whisper ni ffmpeg.
- 2026-10-21: Fase 1 completada: backup de la base de datos realizado (`meta_ads_before_phase1_20261021.db`). Corregido argumento inválido `fps_sample` en `scripts/mass_global_harvester.py`. Rutas absolutas convertidas a relativas en `src/database.py`, `src/scraper.py`, `src/web/app.py` y aplicadas a la DB con `scripts/fix_paths.py` (75 filas actualizadas). Regex de límites de palabra en `src/relevance.py` con test de rechazos y aprobaciones. Descarga en streaming a `.tmp` con límite de 200MB, timeout configurable (120s video) y reintentos tenacity en `src/scraper.py`. Variables `HOST/PORT/ENV` en `start_server.py` y `docker-compose.yml`. `datetime.now()` en `main.py`. Generado `data/rejected_ads.csv` con 14 anuncios irrelevantes sin modificar la base ni borrar archivos. 20 tests unitarios pasando al 100%.
