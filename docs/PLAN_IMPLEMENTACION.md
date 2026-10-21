# Jinora — Plan de Implementación de Mejoras

> **Para el agente de Antigravity:** ejecuta este plan **una fase a la vez**, en orden. Al terminar cada fase: marca las casillas `[x]`, corre los criterios de aceptación y detente para revisión antes de pasar a la siguiente. No borres datos sin hacer backup (Fase 0). Mantén el código en Python 3.11, comentarios en español y nombres de variables en inglés.
>
> **Importante sobre este archivo:** antes de tildar casillas, **relee la versión actual** de `docs/PLAN_IMPLEMENTACION.md` y modifica solo las líneas de esa casilla (edición puntual). **No reescribas el archivo completo ni restaures una copia anterior**: el usuario y su asistente lo actualizan por fuera y se perderían cambios. Después de cada fase, haz commit de `docs/` junto con el código.

### Estado y decisiones vigentes
| Decisión | Valor |
|---|---|
| Proveedor de IA | **Solo Gemini** (nivel gratuito, 20 solicitudes/día). Sin Claude ni OpenAI. |
| Audio | **Sin Whisper**: Gemini transcribe el audio dentro de la misma solicitud de evaluación. |
| Video | **Sin FFmpeg**: Gemini recibe el video completo (con rango de tiempo si hace falta). |
| Se conserva (local, gratis) | `phash`/`imagehash` + Pillow para duplicados; `pymediainfo` para duración y proporción. |
| Frecuencia | Barrido cada 15 días + recuperación diaria de pendientes. |
| Fase 0 | ✅ **Hecha** (config.py, .env.example, backup, rama, requirements). |
| Siguiente | Fase 0.1 → Fase 0.2 → Fase 1 → Fase 2 → Fase 4 (ver “Orden recomendado” al final). |

---

## 0. Contexto y objetivo

Jinora rastrea la Meta Ads Library para obtener anuncios de competidores de **Cosmiq** (agentes IA conversacionales por WhatsApp/Instagram, agentes de voz y CRM inteligente, foco LatAm), analiza audio y video con IA y asigna un **puntaje 1–100**.

### Diagnóstico actual (resumen)
| # | Problema | Evidencia |
|---|----------|-----------|
| P1 | El puntaje **no es IA**: `src/evaluator.py` usa valores fijos y textos plantilla; depende casi solo de `days_active`. | 92 ads, todos entre 77 y 95; 37 ads con exactamente 77 (todos < 30 días). |
| P2 | El video **no se analiza visualmente**: se extraen frames a 1 fps y nadie los mira. Whisper alucina en videos sin voz. | Attio: video con música → transcripción inventada en turco. |
| P3 | **Datos contaminados**: búsqueda por palabra clave trae marcas ajenas. Solo ~35 de 92 ads son de competidores mapeados. | "Air AI" → Air Wick, Airmoto, deshumidificador (1589 días = “campeón #1”). "Bland AI" → novelas románticas. |
| P4 | Bug en `relevance.py`: `"ai" in page_name` aprueba "Air Wick", "Chair", etc. | Lista negra parcheada a mano. |
| P5 | Sesgo de mercado: 73 voz / 8 conversacional / 11 CRM; país por defecto US; cero competidores LatAm. | Sin ads de Leadsales, Clientify, Botmaker, B2Chat, Kommo LatAm. |
| P6 | Bugs técnicos: `fps_sample` inválido en `mass_global_harvester.py`, rutas absolutas `C:\Users\Gabo\...` en DB, servidor en `127.0.0.1` dentro de Docker, descarga en memoria, Whisper recargado por request, 4 scripts duplicados, fecha del reporte = `Path.cwd()`. | — |

### Resultado esperado al final
1. Cada anuncio tiene **dos puntajes separados y explicables**:
   - `creative_score` (0–100) — calidad creativa evaluada por LLM multimodal con evidencia citada (timestamps).
   - `market_score` (0–100) — señal de mercado calculada por código (longevidad, variantes, países, activo).
   - `jinora_score` = combinación ponderada configurable (por defecto 0.7 creativo + 0.3 mercado).
2. Solo se guardan anuncios de **competidores reales** (búsqueda por `page_id`) o descubiertos y validados por un clasificador LLM.
3. Un **pipeline único**, idempotente, con etapas reanudables y cola de trabajos.
4. Dashboard que muestra puntajes guardados (no recalculados), desglose por dimensión y evidencia.

---

## Arquitectura objetivo

```
                ┌──────────────┐
 competitors ──►│ 1. DISCOVER  │  búsqueda por page_id (+ keyword discovery opcional)
  (page_ids)    └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 2. FILTER    │  reglas duras + clasificador LLM de relevancia
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 3. DOWNLOAD  │  streaming, dedupe por ad_id y por hash de creativo
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 4. MEDIA     │  duración, proporción, phash (sin Whisper ni FFmpeg)
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 5. EVALUATE  │  LLM multimodal → JSON validado (Pydantic)
                └──────┬───────┘
                       ▼
                ┌──────────────┐
                │ 6. SIGNALS   │  market_score + jinora_score
                └──────┬───────┘
                       ▼
                  DB + Dashboard + Reportes
```

Estructura de carpetas objetivo:
```
src/
  config.py              # settings desde .env (pydantic-settings)
  db/
    models.py            # SQLAlchemy: Competitor, Ad, Creative, Analysis, Job
    migrate.py           # migración desde la tabla `ads` actual
  collect/
    scraper.py           # MetaAdsHunter (refactor)
    page_resolver.py     # nombre de marca → page_id
    relevance.py         # reglas + clasificador LLM
  media/
    downloader.py        # descarga en streaming
    media_info.py        # duración, proporción (pymediainfo); sin Whisper ni FFmpeg
    fingerprint.py       # hash de creativo para variantes
  ai/
    llm_client.py        # cliente Gemini (google-genai)
    evaluator.py         # evaluación creativa (reemplaza el actual)
    schemas.py           # Pydantic: EvaluationResult, RelevanceResult
  scoring/
    market.py            # market_score
  pipeline.py            # orquestador por etapas
  cli.py                 # python -m src.cli ...
  web/app.py
prompts/
  evaluator_v1.md
  relevance_v1.md
config/
  competitors_database.json   # + page_ids
  rubric.yaml                 # rúbrica con anclas
  scoring.yaml                # pesos market/creative
calibration/
  manual_scores.csv           # puntajes humanos para calibrar
tests/
```

---

## FASE 0 — Preparación (½ día)

**Objetivo:** trabajar seguro y con configuración centralizada.

- [x] Inicializar git si no existe; crear rama `feat/mejoras-ia`.
- [x] Backup: copiar `data/meta_ads.db` → `data/backup/meta_ads_YYYYMMDD.db`.
- [x] Crear `.env.example` y `src/config.py` con `pydantic-settings`:
  - `DATABASE_URL`, `DOWNLOADS_DIR`
  - `LLM_PROVIDER` (`gemini` | `anthropic` | `openai`), `LLM_MODEL`, `LLM_MODEL_CHEAP` (para relevancia), `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
  - `WHISPER_MODEL` (default `small`), `WHISPER_DEVICE` (`cpu`/`cuda`)
  - `DEFAULT_COUNTRIES=PE,MX,CO,AR,CL,ES,US`
  - `SCORE_WEIGHT_CREATIVE=0.7`, `SCORE_WEIGHT_MARKET=0.3`
- [x] Añadir `.env` a `.gitignore`. Nunca escribir API keys en código.
- [x] Actualizar `requirements.txt`: `pydantic>=2`, `pydantic-settings`, `google-genai`, `anthropic`, `tenacity`, `pyyaml`, `pytest`, `imagehash` (quitar duplicados del Dockerfile).

**Criterio de aceptación:** `python -c "from src.config import settings; print(settings.LLM_PROVIDER)"` funciona leyendo `.env`.

> **Prompt para Antigravity:** “Ejecuta la FASE 0 de docs/PLAN_IMPLEMENTACION.md. Crea src/config.py con pydantic-settings y todas las variables listadas, .env.example, actualiza requirements.txt y haz backup de la base de datos. No modifiques otra lógica.”

---

## FASE 0.1 — Modo nivel gratuito de Gemini + corrida cada 15 días (½ día) 🆕

> **Nota:** esta fase se agregó **después** de ejecutar la Fase 0. No rehagas la Fase 0: solo **extiende** el `src/config.py` y el `.env.example` que ya existen.

**Decisión del usuario:** usar **solo la API gratuita de Gemini** (para video, estáticos y relevancia) y lanzar el barrido **una vez cada 15 días**.

> **Alcance Gemini-only:** `LLM_PROVIDER=gemini` es el único proveedor a implementar. **No** crear `AnthropicClient` ni `OpenAIClient`; las variables `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` y el paquete `anthropic` del `requirements.txt` quedan sin usar (se pueden retirar). Si en el futuro se quisiera otro proveedor, la interfaz de `llm_client.py` lo permite, pero hoy está fuera de alcance. Si la cuota se agota, los anuncios quedan **pendientes** (no hay proveedor de respaldo).

**Contexto de límites:** el nivel gratuito tiene límites por minuto (RPM), por día (RPD, se reinicia a medianoche hora del Pacífico) y por tokens (TPM), **por proyecto**. Google ya no publica una tabla fija: el usuario debe revisar los valores reales en el panel de *Rate limits* de AI Studio y copiarlos al `.env`. Solo usar modelos **Flash / Flash-Lite** (los Pro recientes no tienen nivel gratuito). En el nivel gratuito Google puede usar el contenido para mejorar sus productos → **nunca enviar datos de clientes**, solo anuncios públicos.

**Límite confirmado por el usuario: 20 solicitudes diarias (RPD)** en su proyecto. El cuello de botella son las **solicitudes**, no los tokens → la estrategia central es **evaluar varios anuncios por solicitud (lotes)**.

**Volumen esperado (estimado, se medirá con `llm_usage`):** primera corrida ~300–500 anuncios; corridas siguientes ~50–150 anuncios nuevos (lo ya evaluado queda en caché). Supuesto de mezcla: ~60–65 % videos, ~35–40 % estáticos/carruseles.

**Capacidad con 20 RPD, todo en Gemini** (videos + estáticos + relevancia comparten la cuota; se reservan 2 solicitudes → ~18 útiles/día):
| Concepto | Primera corrida (~400 anuncios) | Cada 15 días (~100 anuncios) |
|---|---|---|
| Videos (3 por solicitud) | ~250 videos → ~84 solicitudes | ~65 videos → ~22 solicitudes |
| Estáticos (6 por solicitud) | ~150 → ~25 solicitudes | ~35 → ~6 solicitudes |
| Relevancia (20 por solicitud) | ~10–15 solicitudes | ~3–5 solicitudes |
| **Total** | **~120 solicitudes ≈ 7 días** | **~32 solicitudes ≈ 2 días** |
| Con `FIRST_RUN_PRIORITY_ONLY` y filtro “vale la pena evaluar” | ~3–4 días | ~1–2 días |

Si Flash-Lite tiene cuota propia (verificar en AI Studio), relevancia y estáticos pasan a Flash-Lite y los videos quedan solos en Flash: ~1–2 días por corrida quincenal.

Tokens estimados por llamada (verificar con la documentación vigente): video 90 s en resolución baja ≈ 13.500; imagen ≈ 5.500; carrusel 10 tarjetas ≈ 17.000. Un lote de 3 videos ≈ 40.000 tokens, muy por debajo del TPM.

### 0.1.1 Configuración (extender `src/config.py` y `.env.example`)
- [x] `GEMINI_RPM` (default 5), `GEMINI_RPD` (default **20**, valor real del usuario), `GEMINI_TPM` (default 250000) — el usuario los ajusta según AI Studio.
- [x] **Cuota por modelo:** en el nivel gratuito cada modelo suele tener su propio límite. Permitir límites separados: `GEMINI_RPD_MAIN` (para `LLM_MODEL`) y `GEMINI_RPD_CHEAP` (para `LLM_MODEL_CHEAP`). El usuario verifica en AI Studio si Flash-Lite tiene cuota propia; si es así, relevancia y estáticos simples van por Flash-Lite y los videos por Flash.
- [x] `EVAL_BATCH_VIDEOS` (default 3) y `EVAL_BATCH_STATICS` (default 6): anuncios por solicitud.
- [x] `RESERVE_REQUESTS` (default 2): solicitudes diarias que no se usan en el barrido (para pruebas manuales o reintentos).
- [x] `FIRST_RUN_PRIORITY_ONLY` (default `true`): la primera corrida solo evalúa competidores `priority=1`.
- [x] `MAX_EVALS_PER_RUN` (default 0 = sin tope; útil para pruebas).
- [x] `VIDEO_MAX_SECONDS_FOR_LLM` (default 90) y `GEMINI_MEDIA_RESOLUTION` (`low` | `medium`, default `low`).
- [x] `RELEVANCE_BATCH_SIZE` (default 20).
- [x] `FREE_TIER_MODE` (bool, default `true`).
- [x] Verificar que `DEFAULT_COUNTRIES=PE,MX,CO` (texto separado por comas) en `.env` se lea bien: pydantic-settings intenta decodificar como JSON los campos tipo lista; si falla, anotar el campo con `NoDecode` o declararlo como `str` y exponer una propiedad que devuelva la lista. Agregar un test.

### 0.1.2 Control de cuota — `src/ai/quota.py`
- [x] Tabla `llm_usage(day_pt, model, requests, tokens_in, tokens_out)` en la DB (día calculado en hora del Pacífico, que es cuando Google reinicia el RPD).
- [x] `QuotaGuard.acquire(model, est_tokens)`: espera lo necesario para respetar RPM/TPM; si se alcanzó `GEMINI_RPD` del día lanza `QuotaExhausted`.
- [x] Ante error **429** de la API: backoff exponencial (30 s, 60 s, 120 s); si persiste o el mensaje indica cuota diaria agotada → `QuotaExhausted`.
- [x] Registrar uso real (tokens de la respuesta) tras cada llamada.

### 0.1.3 Comportamiento del pipeline con cuota
- [ ] Al capturar `QuotaExhausted`: guardar estado, dejar los anuncios restantes como `pending_evaluation`, imprimir resumen (“evaluados X, pendientes Y, se retoma en la próxima corrida”) y salir con código 0 (no es error).
- [ ] **Cola priorizada** de evaluación: 1) competidores `priority=1` (WhatsApp/CRM LatAm), 2) `is_active=true`, 3) mayor `market_score`, 4) más reciente. Así, si la cuota se acaba, lo importante ya quedó evaluado.
- [ ] Las etapas sin LLM (scraping, descarga, metadata del video, `phash`, `market_score`) **siempre** se completan; solo la evaluación queda pendiente.

### 0.1.4 Ahorro de cuota y tokens
- [ ] **Evaluación en lote (palanca principal):** una solicitud evalúa hasta `EVAL_BATCH_VIDEOS` videos o `EVAL_BATCH_STATICS` estáticos.
  - Cada anuncio va precedido por un separador con su `ad_id` (`=== ANUNCIO <ad_id> ===`) y sus propios medios y textos.
  - La respuesta es `{"results": [{"ad_id": "...", ...EvaluationResult}]}`; se valida **por anuncio**: los válidos se guardan, los inválidos vuelven a la cola (no se pierde el lote completo).
  - Instrucción explícita en el prompt: evaluar cada anuncio de forma independiente, sin comparar entre ellos ni mezclar evidencias.
  - Lotes homogéneos: videos con videos, estáticos con estáticos; los carruseles van solos o máximo 2 por lote.
  - Si el lote supera ~150.000 tokens estimados, partirlo.
  - **Calibración obligatoria:** evaluar 10 anuncios en lote y también individualmente; si la diferencia media por anuncio es > 5 puntos, bajar el tamaño del lote (3 → 2 → 1 en videos).
- [ ] **Filtro “vale la pena evaluar”** (por código, sin LLM): no evaluar anuncios con < 3 días activos, ni creativos cuyo competidor ya tenga ≥ 10 creativos evaluados en los últimos 90 días con el mismo `visual_style`/formato, salvo que su `market_score` esté en el top 20 % del competidor. Quedan como `skipped_low_value` y pueden evaluarse luego si sobra cuota.
- [ ] **Relevancia en lote:** el clasificador recibe `RELEVANCE_BATCH_SIZE` anuncios (solo texto: página, copy, titular, dominio) en **una** llamada y devuelve una lista JSON. Los anuncios de competidores mapeados por `page_id` no gastan llamada.
- [ ] **Video liviano:** enviar a Gemini solo los primeros `VIDEO_MAX_SECONDS_FOR_LLM` segundos (con el `video_metadata` de `google-genai` —`start_offset`/`end_offset`—; verificar en la documentación vigente; si el rango no es aceptado, enviar el video completo con `media_resolution=low`) y con `media_resolution=low` (≈100 tokens/s en lugar de ≈300 tokens/s). Videos pequeños van inline; los grandes por la Files API.
- [ ] Borrar los archivos subidos a la Files API de Gemini después de evaluarlos.
- [ ] Caché estricta por `creative_hash`: variantes del mismo creativo **heredan** la evaluación (1 llamada por creativo, no por anuncio).
- [ ] Una sola llamada por anuncio (evaluación completa); sin llamadas de “resumen” extra durante el barrido. El resumen ejecutivo por pilar (Fase 8) se genera al final con 1 llamada por pilar, solo si queda cuota.

### 0.1.5 Programación cada 15 días (Windows, sin servidor)
- [ ] Comandos del CLI (Fase 7):
  - `python -m src.cli run --all` → barrido completo (scraping + preparación + evaluación hasta agotar cuota).
  - `python -m src.cli evaluate --pending` → solo evalúa lo pendiente (no hace scraping).
- [ ] `scripts/schedule_windows.ps1` que registre con el Programador de tareas:
  - **Barrido:** días **1 y 15** de cada mes, 09:00 → `run --all`.
  - **Recuperación de pendientes: TODOS los días**, 09:30 → `evaluate --pending`. Si no hay pendientes termina en segundos sin consumir cuota. (Con 20 RPD los días fijos no alcanzan: la primera corrida puede tomar ~1 semana.)
  - Cuando `FIRST_RUN_PRIORITY_ONLY=true` y ya no quedan pendientes de prioridad 1, la recuperación diaria sigue con prioridad 2 y luego con `skipped_low_value`, hasta agotar la cuota de cada día.
  - Opción “ejecutar lo antes posible si se omitió el inicio programado” activada (por si la PC estaba apagada).
  - Usar la ruta absoluta del Python del entorno virtual y el directorio del proyecto como carpeta de inicio.
- [ ] Log por corrida en `logs/run_YYYYMMDD.log` + línea resumen en `logs/history.csv` (fecha, anuncios nuevos, evaluados, pendientes, requests usadas).

**Criterio de aceptación:**
- Con `GEMINI_RPD=3`, `EVAL_BATCH_VIDEOS=3` y 12 videos, el pipeline evalúa 9 en 3 solicitudes, deja 3 pendientes y sale sin error; `evaluate --pending` al día siguiente continúa sin repetir los ya evaluados.
- Un lote donde 1 de 3 resultados tiene JSON inválido guarda los 2 válidos y devuelve 1 a la cola.
- Un 429 simulado activa el backoff y no rompe la corrida.
- Las tareas aparecen en el Programador de tareas de Windows.

> **Prompt para Antigravity:** “Ejecuta la FASE 0.1 de docs/PLAN_IMPLEMENTACION.md. La Fase 0 ya está hecha: extiende el src/config.py y el .env.example existentes con las nuevas variables (no los reescribas). Crea src/ai/quota.py y deja preparada la cola priorizada y el manejo de QuotaExhausted para conectarlos en las Fases 4 y 7. Crea scripts/schedule_windows.ps1 pero NO lo ejecutes hasta que el pipeline (Fase 7) esté listo.”

---

## FASE 0.2 — Gemini reemplaza a Whisper y FFmpeg (½ día) 🆕

> **Decisión del usuario:** simplificar. Gemini transcribe el audio y analiza el video completo, así que se **eliminan Whisper y FFmpeg** del flujo. Se conservan solo dos utilidades locales, gratis y sin equivalente en Gemini: `phash` (duplicados) y lectura de metadata (duración/proporción).

**Por qué:** menos dependencias frágiles en Windows, cero solicitudes extra (la transcripción viaja en el mismo JSON de evaluación), y se evita que Whisper invente texto en videos de solo música (caso Attio).

### 0.2.1 Qué se quita y qué se agrega
- [ ] `src/config.py` y `.env.example`: quitar `WHISPER_MODEL` y `WHISPER_DEVICE` (extender el archivo existente; no reescribirlo).
- [ ] `requirements.txt`: quitar `faster-whisper`; agregar `pymediainfo`. Mantener `Pillow` e `imagehash`.
- [ ] `Dockerfile`: quitar la instalación de `ffmpeg`. Si `pymediainfo` no trae `libmediainfo` en la imagen, instalar `libmediainfo0v5` (verificar).
- [ ] Crear `src/media/media_info.py` con `get_video_info(path) -> {duration_s, width, height, aspect_ratio, has_audio_track}` y `get_image_info(path)`.
- [ ] `src/video_forensics.py`: **no borrarlo todavía** (ver 0.2.2); marcarlo como obsoleto (`# DEPRECATED`) y no usarlo en código nuevo.

### 0.2.2 Verificación antes de borrar Whisper (se hace en la calibración de la Fase 4.6)
- [ ] Elegir 3 videos: uno con voz clara en inglés, uno con solo música (p. ej. Attio) y uno en español con acento.
- [ ] Comparar la transcripción de Gemini contra la de Whisper (`video_forensics.py` actual). Criterio: Gemini igual o mejor en los 3 casos, y marca `has_voiceover=false` en el de solo música.
- [ ] Si se cumple: borrar `src/video_forensics.py` y las carpetas `frames/` y `full_forensic_analysis.json` de `downloads/` (con backup previo). Si no se cumple: reabrir esta decisión con el usuario.

**Criterio de aceptación:** el proyecto instala y corre (`python -m pytest`, arranque del servidor) **sin** `faster-whisper` ni FFmpeg instalados; `get_video_info` devuelve duración y proporción correctas para `downloads/Bland AI/1763243594393368/video_0.mp4`.

> **Prompt para Antigravity:** “Ejecuta la FASE 0.2 de docs/PLAN_IMPLEMENTACION.md. Relee el archivo antes de tildar casillas y edita solo esas líneas. Extiende (no reescribas) config.py y .env.example para quitar WHISPER_*, actualiza requirements.txt y Dockerfile, crea src/media/media_info.py con pymediainfo y marca video_forensics.py como DEPRECATED sin borrarlo.”

---

## FASE 1 — Correcciones rápidas (1 día)

**Objetivo:** eliminar bugs que corrompen datos o rompen la ejecución.

- [ ] `scripts/mass_global_harvester.py`: quitar `fps_sample=1.0` de `analyze_video()` (hoy lanza `TypeError` silenciado y nunca transcribe).
- [ ] **Rutas relativas:** guardar `video_path`/`image_path` relativos a `DOWNLOADS_DIR`. Script `scripts/fix_paths.py` que convierta las rutas `C:\Users\Gabo\Documents\Jinora\downloads\...` existentes a relativas.
- [ ] `src/relevance.py`: reemplazar `any(k in page_name for k in ["ai", ...])` por coincidencia de **palabra completa** con regex (`\bai\b`, `\bcrm\b`…). Agregar tests con "Air Wick", "Airmoto", "Chair Co" (deben rechazarse) y "Bland AI", "Kommo" (aprobarse).
- [ ] `src/scraper.py` → descarga en **streaming** a archivo temporal y `rename` al terminar; timeout configurable (120 s para video); reintentos con `tenacity`; límite de tamaño (ej. 200 MB).
- [ ] `start_server.py`: host desde env (`0.0.0.0` en Docker), `reload` solo si `ENV=dev`.
- [ ] `main.py` → `generate_markdown_report`: usar `datetime.now()` en lugar de `Path.cwd()`.
- [ ] ~~Whisper como singleton~~ → no aplica: Whisper se elimina en la Fase 0.2. Si `video_forensics.py` se usa antes, cargar el modelo una sola vez por proceso.
- [ ] Script `scripts/cleanup_irrelevant.py`: re-evalúa todas las filas con el filtro corregido y mueve las irrelevantes a una tabla/archivo `rejected_ads` (no borrar archivos aún; listar para revisión).

**Criterio de aceptación:**
- `pytest tests/test_relevance.py` pasa.
- El dashboard muestra los videos existentes tras `fix_paths.py`.
- `docker compose up` responde en `http://localhost:8000`.

> **Prompt para Antigravity:** “Ejecuta la FASE 1 del plan. Corrige cada bug listado, crea tests para relevance.py con los casos indicados y los scripts fix_paths.py y cleanup_irrelevant.py. Muéstrame la lista de anuncios que cleanup_irrelevant.py marcaría como irrelevantes antes de moverlos.”

---

## FASE 2 — Calidad de datos: búsqueda por competidor real (2 días)

**Objetivo:** que >90 % de los anuncios guardados sean de competidores relevantes.

### 2.1 Resolver page_id de cada competidor
- [ ] `src/collect/page_resolver.py`: para cada competidor de `competitors_database.json`, buscar en la Ads Library y elegir la página oficial (coincidencia de nombre + dominio del `link_url` igual al `website` del competidor).
- [ ] Guardar en el JSON: `"page_ids": ["..."]`, `"resolved_at"`, `"resolution_confidence"`. Los de baja confianza se marcan `"needs_review": true` para validación manual.
- [ ] **Verificar** si `meta-ads-collector` soporta búsqueda por página (`page_id` / `view_all_page_id`). Si no, implementar la consulta por página con la misma sesión `curl_cffi` del collector.

### 2.2 Ampliar competidores LatAm y priorizar
- [ ] Añadir a `competitors_database.json` el campo `priority` (1–3) y `markets` (ej. `["PE","MX","CO"]`).
- [ ] Prioridad 1 (conversacional/CRM LatAm): Kommo, Leadsales, Clientify, Botmaker, B2Chat, Sirena (Zenvia), Yalo, Aivo, ManyChat, Wati, Respond.io, GoHighLevel, HubSpot.
- [ ] Prioridad 2: voz (Vapi, Bland, Retell, Synthflow, ElevenLabs, Air AI) y el resto de CRM.
- [ ] Países por defecto desde `DEFAULT_COUNTRIES` (incluye PE).

### 2.3 Filtro de relevancia en dos capas
- [ ] **Capa 1 (reglas, gratis):** si el `page_id` pertenece a un competidor mapeado → aprobado directo. Si viene de búsqueda por keyword → aplicar lista negra corregida.
- [ ] **Capa 2 (LLM barato):** para anuncios de keyword discovery, clasificar con `LLM_MODEL_CHEAP` usando `prompts/relevance_v1.md`, **en lotes de `RELEVANCE_BATCH_SIZE`** y pasando por `QuotaGuard` (Fase 0.1). Salida JSON (una lista con un objeto por anuncio):
  ```json
  {"is_relevant": true, "pillar": "conversacional|voz|crm|ninguno",
   "is_competitor_of_cosmiq": true, "confidence": 0.0, "reason": "..."}
  ```
- [ ] Páginas nuevas con ≥3 anuncios relevantes → se proponen como competidores nuevos (`config/candidate_competitors.json`).

**Criterio de aceptación:** correr el pipeline sobre 10 competidores × 3 países y verificar manualmente que ≥90 % de los anuncios son relevantes. Ningún anuncio de Air Wick / novelas / gadgets.

> **Prompt para Antigravity:** “Ejecuta la FASE 2. Primero verifica en el código de meta-ads-collector si se puede buscar por page_id y dime cómo. Luego implementa page_resolver.py, el filtro de dos capas y agrega los competidores LatAm con prioridad y mercados.”

---

## FASE 3 — Preparación de media y comprensión multimodal con Gemini (1 día)

**Objetivo:** que Gemini reciba el video completo y devuelva, en **una sola solicitud**, transcripción, detección de voz y análisis visual, sin Whisper ni FFmpeg.

### 3.1 Preparación local (sin LLM, gratis)
- [ ] Por cada video: `get_video_info()` (Fase 0.2): duración, ancho/alto, proporción (9:16 / 1:1 / 16:9), si tiene pista de audio.
- [ ] Hash exacto del archivo (`sha256`) para detectar el mismo video descargado dos veces; `phash` de la miniatura `image_0.jpg` para agrupar variantes.
- [ ] Si el video **no tiene pista de audio** (`has_audio_track=false`), se marca `has_voiceover=false` por código y se le indica a Gemini que no busque diálogo.
- [ ] Videos > 20 MB se suben por la **Files API** de Gemini y se borran tras evaluar; los pequeños van inline.

### 3.2 Qué devuelve Gemini (todo en el mismo JSON de la Fase 4)
- [ ] `transcript` con segmentos y tiempos aproximados (±2 s), `language`, `has_voiceover` (false si solo hay música/efectos).
- [ ] Estimaciones: `scene_cuts_estimate`, `speech_pace_wpm_estimate`, `burned_in_subtitles` (sí/no), `format` (ugc, demo_pantalla, talking_head, animación…).
- [ ] Observación explícita de **0–3 s** (gancho): qué se ve y qué se oye.
- [ ] Reglas del prompt: transcribir solo lo que se oye de verdad; si no hay habla, `transcript=[]`; no inventar diálogo.

### 3.3 Qué se guarda
- [ ] La salida se guarda en la tabla `Analysis` (Fase 6) y como `analysis.json` junto al anuncio (reemplaza a `full_forensic_analysis.json`).
- [ ] Para el dashboard (Fase 8): usar la miniatura que ya descarga Meta (`image_0.jpg`); no se generan fotogramas.
- [ ] Los anuncios aún no evaluados (cuota agotada) muestran “Pendiente de análisis” en lugar de transcripción.

**Criterio de aceptación:** el video de Attio (solo música) sale con `has_voiceover=false` y `transcript=[]`; un video de Bland AI sale con transcripción y tiempos coherentes; ningún proceso local de Whisper/FFmpeg se ejecuta.

> **Prompt para Antigravity:** “Ejecuta la FASE 3 (versión Gemini-only). No uses Whisper ni FFmpeg. Implementa la preparación local (media_info, sha256, phash, subida por Files API) y deja que el evaluador de la Fase 4 devuelva transcripción, has_voiceover y análisis visual en el mismo JSON. Pruébalo con el video de Attio y uno de Bland AI.”

---

## FASE 4 — Evaluador con IA real (3 días) ⭐ fase más importante

**Objetivo:** reemplazar `src/evaluator.py` por una evaluación LLM con rúbrica anclada, evidencia y salida validada.

### 4.1 Rúbrica con anclas — `config/rubric.yaml`
Mantener las 4 dimensiones del `ad-hunter.md` (0–25 c/u) pero con **anclas** para que el modelo puntúe consistente:

| Dimensión | 0–6 | 7–12 | 13–19 | 20–25 |
|-----------|-----|------|-------|-------|
| **1. Captación (0–3 s)** | Arranca con logo/intro genérica; sin texto ni voz que enganche. | Gancho presente pero predecible (“¿Quieres más ventas?”). | Gancho específico con dolor o curiosidad claros; visual acompaña. | Interrupción de patrón fuerte + dolor visceral o afirmación contraintuitiva; audio y visual sincronizados. |
| **2. Mecanismo y prueba** | Solo afirmaciones (“la mejor IA”). | Explica qué hace sin mostrarlo. | Muestra el producto (pantallas, demo parcial) o un dato concreto. | Demo en vivo verificable, caso real con métricas o contraste explícito con la alternativa. |
| **3. Narrativa y ritmo** | Monótono, sin estructura, se cae a los pocos segundos. | Estructura básica, ritmo irregular. | Problema → solución claro, ritmo sostenido. | Arco de tensión completo (dolor → giro → alivio), cortes y locución que sostienen hasta el CTA. |
| **4. Oferta y CTA** | Sin CTA o CTA confuso. | CTA genérico (“Más información”). | CTA claro con beneficio. | CTA de baja fricción + reversión de riesgo (prueba gratis, garantía, demo inmediata) + urgencia/valor evidente. |

### 4.2 Esquema de salida — `src/ai/schemas.py` (Pydantic)
```python
class Evidence(BaseModel):
    timestamp: str | None   # "00:02-00:05" o None si es del copy
    source: Literal["audio", "visual", "copy", "cta"]
    quote: str              # cita textual o descripción visual concreta

class DimensionScore(BaseModel):
    score: int = Field(ge=0, le=25)
    anchor_level: Literal["0-6", "7-12", "13-19", "20-25"]
    reasoning: str          # 2–4 frases, específicas a ESTE anuncio
    evidence: list[Evidence] = Field(min_length=1)

class TranscriptSegment(BaseModel):
    start_s: float          # tiempo aproximado (±2 s)
    end_s: float
    text: str

class EvaluationResult(BaseModel):
    transcript: list[TranscriptSegment] = []   # vacío si no hay habla
    has_voiceover: bool
    scene_cuts_estimate: int | None
    speech_pace_wpm_estimate: int | None
    burned_in_subtitles: bool | None
    hook: DimensionScore
    mechanism: DimensionScore
    narrative: DimensionScore
    offer: DimensionScore
    hook_description: str           # qué pasa en 0–3 s (audio + visual)
    ai_mechanism_framing: str       # cómo presentan la IA (“empleado que no duerme”, etc.)
    format: Literal["ugc", "demo_pantalla", "talking_head", "animacion", "testimonial", "mixto", "otro"]
    language: str
    target_niche: str | None        # inmobiliarias, clínicas, ecommerce…
    what_to_model: str              # principio a copiar para Cosmiq
    what_to_beat: str               # debilidad que Cosmiq puede superar
    cosmiq_adaptation_idea: str     # idea concreta de anuncio para Cosmiq (WhatsApp/voz/CRM)
    confidence: float = Field(ge=0, le=1)
```
- `creative_score = hook + mechanism + narrative + offer` → **lo suma el código**, nunca el LLM.

### 4.3 Prompt — `prompts/evaluator_v1.md`
- [ ] Rol: director creativo de respuesta directa especializado en SaaS B2B LatAm.
- [ ] Incluir la rúbrica anclada completa, el contexto de Cosmiq (WhatsApp, voz, CRM; LatAm) y reglas:
  - Puntuar **solo con lo observable**; cada puntaje requiere ≥1 evidencia con timestamp.
  - Si no hay voz, evaluar texto en pantalla y visual; no inventar diálogo.
  - **Prohibido** usar los días activos para puntuar (esa señal va en `market_score`). No enviar `days_active` al modelo.
  - Usar todo el rango 0–25; un anuncio promedio debería caer en 10–15.
- [ ] Temperatura 0, salida JSON con schema (`response_schema` de Gemini).

### 4.4 Cliente LLM — `src/ai/llm_client.py`
- [ ] Interfaz única: `evaluate(media: MediaBundle, copy: AdCopy) -> EvaluationResult` (`MediaBundle` = ruta del video/imágenes + metadata local de la Fase 3.1).
- [ ] Implementación única `GeminiClient` (video nativo, imágenes y texto). Sin otros proveedores (decisión del usuario, ver Fase 0.1).
- [ ] **Toda** llamada pasa por `QuotaGuard.acquire()` (Fase 0.1.2); el 429 se maneja ahí, no con reintentos ciegos.
- [ ] Si el JSON no valida, 1 reintento con el error de validación en el prompt (también consume cuota: contarlo).
- [ ] Registro de tokens por llamada en `llm_usage`.
- [ ] Modelo por defecto: un **Flash** con nivel gratuito (`LLM_MODEL`); el usuario confirma en AI Studio qué Flash tiene disponible.

### 4.5 Persistencia y caché
- [ ] Tabla `analyses`: `ad_id`, `model`, `prompt_version`, `rubric_version`, `result_json`, `creative_score`, `created_at`, `cost_usd`.
- [ ] No re-evaluar si ya existe análisis con el mismo `model + prompt_version + creative_hash` (salvo `--force`).

### 4.6 Calibración (obligatoria antes de escalar)
- [ ] Elegir 20 anuncios variados (buenos, malos, sin voz, imagen estática).
- [ ] El usuario los puntúa a mano en `calibration/manual_scores.csv` (columnas: ad_id, hook, mechanism, narrative, offer).
- [ ] `scripts/calibrate.py`: corre el evaluador, calcula **correlación de Spearman** y **error absoluto medio** por dimensión, y prueba **consistencia** (evaluar 5 anuncios 3 veces; desviación ≤ 3 puntos).
- [ ] Meta: Spearman ≥ 0.6 en total; si no se cumple, ajustar anclas/prompt y subir `prompt_version`.

**Criterio de aceptación:**
- La distribución de `creative_score` sobre los anuncios existentes usa un rango amplio (desviación estándar > 10) y no correlaciona con `days_active` por diseño.
- Cada evaluación tiene evidencia con timestamps y razonamiento distinto por anuncio.
- `calibrate.py` reporta las métricas.

> **Prompt para Antigravity:** “Ejecuta la FASE 4. Crea rubric.yaml, schemas.py, prompts/evaluator_v1.md y llm_client.py con soporte solo para Gemini (no crees clientes de Claude ni OpenAI), y reemplaza evaluator.py. El LLM nunca debe recibir days_active ni sumar el puntaje. Luego crea scripts/calibrate.py y prepara calibration/manual_scores.csv con 20 ad_ids variados para que yo los puntúe.”

---

## FASE 5 — Señal de mercado y puntaje final (1 día)

**Objetivo:** separar “qué tan bueno es el creativo” de “qué tanto le está funcionando al competidor”.

- [ ] `src/media/fingerprint.py`: `creative_hash` = `phash` de la miniatura/imagen (+ `sha256` del archivo de video para duplicados exactos) + hash del copy normalizado. Agrupa variantes del mismo creativo sin extraer fotogramas.
- [ ] `src/scoring/market.py` → `market_score` 0–100 con pesos en `config/scoring.yaml`:
  - Longevidad (log de días activo, tope 180 días) — 40 %
  - Nº de variantes/duplicados del creativo (escalado) — 25 %
  - Nº de países donde corre — 15 %
  - Sigue activo hoy — 10 %
  - Nº de plataformas (FB, IG, Messenger, Audience Network) — 10 %
- [ ] `jinora_score = w_c * creative_score + w_m * market_score` (pesos desde `.env`).
- [ ] Los duplicados se agrupan: el dashboard muestra 1 tarjeta por creativo con “N variantes”.

**Criterio de aceptación:** los 8 anuncios con el mismo copy (“They turned off all eight of my alarms…”) aparecen como 1 creativo con 8 variantes.

> **Prompt para Antigravity:** “Ejecuta la FASE 5: fingerprint de creativos, market_score con scoring.yaml y jinora_score. Agrupa variantes en la base de datos.”

---

## FASE 6 — Modelo de datos y migración (1 día)

- [ ] `src/db/models.py`:
  - `Competitor(id, name, website, pillar, priority, markets, page_ids)`
  - `Ad(id, competitor_id, page_id, page_name, country_list, start_date, stop_date, is_active, platforms, copy fields, creative_hash, relevance_json, status)`
  - `Media(ad_id, kind, rel_path, size, sha256)`
  - `MediaInfo(ad_id, duration_s, width, height, aspect_ratio, has_audio_track, sha256, phash)` (local); la transcripción, `has_voiceover`, `language` y `scene_cuts_estimate` viven en `Analysis.result_json` (Gemini)
  - `Analysis(...)` (Fase 4), `MarketSignal(ad_id, market_score, breakdown_json)`
  - `Job(id, type, payload, status, error, created_at, finished_at)`
- [ ] `src/db/migrate.py`: migra la tabla `ads` actual → nuevo esquema (las evaluaciones viejas NO se migran; se re-evalúan).
- [ ] Mantener SQLite local; PostgreSQL opcional vía `DATABASE_URL`.

**Criterio de aceptación:** migración corre sin pérdida de anuncios relevantes ni de rutas de media.

---

## FASE 7 — Pipeline unificado y cola de trabajos (2 días)

- [ ] `src/pipeline.py` con etapas `discover → filter → download → media → evaluate → signals`. Cada etapa es **idempotente** (se salta lo ya procesado) y registra estado en `Ad.status`.
- [ ] `src/cli.py`:
  ```bash
  python -m src.cli run --pillar conversacional --countries PE,MX,CO --limit 20
  python -m src.cli run --competitor "Kommo" --stages download,media,evaluate
  python -m src.cli evaluate --force --prompt-version v2
  python -m src.cli report --pillar crm --top 10
  ```
- [ ] **Eliminar** `scripts/ingest_1000_ads.py`, `mass_global_harvester.py`, `run_full_global_audit.py`, `run_global_intelligence.py`, `scrape_conversational_crm.py` (su lógica queda en el pipeline). `main.py` pasa a llamar al CLI.
- [ ] Comando `python -m src.cli evaluate --pending` y la **cola priorizada** + manejo de `QuotaExhausted` de la Fase 0.1.3.
- [ ] Cola de trabajos: worker simple en proceso separado que consume la tabla `Job` (sin dependencias externas). La API solo encola y devuelve `job_id`; endpoint `GET /api/jobs/{id}`.
- [ ] Al terminar esta fase: ejecutar `scripts/schedule_windows.ps1` (Fase 0.1.5) y verificar las tareas.
- [ ] Rate limiting hacia Meta: pausa aleatoria 1–3 s entre consultas, backoff si hay bloqueo.
- [ ] Logs a archivo `logs/jinora.log` + resumen de costo LLM por corrida.

**Criterio de aceptación:** una corrida interrumpida a mitad se reanuda sin duplicar descargas ni evaluaciones.

---

## FASE 8 — Dashboard y reportes (2 días)

- [ ] El dashboard **lee** `creative_score`, `market_score`, `jinora_score` de la DB (no llama al evaluador).
- [ ] Ranking “Top 10” por `jinora_score`, con filtros: pilar, país, competidor, formato, idioma, con/sin voz.
- [ ] Vista de detalle: video + desglose de las 4 dimensiones + evidencias con **timestamps clicables** que saltan el video a ese segundo + `what_to_model` / `what_to_beat` / `cosmiq_adaptation_idea`.
- [ ] Vista por competidor: nº de anuncios activos, formatos usados, ángulos de IA recurrentes, evolución en el tiempo.
- [ ] Botón “Encolar análisis” en anuncios sin evaluar.
- [ ] Reporte Markdown/HTML por pilar con resumen ejecutivo generado por LLM a partir de los top 10 (patrones comunes, huecos que Cosmiq puede ocupar).

---

## FASE 9 — Agente, pruebas y despliegue (1 día)

- [ ] Actualizar `.agents/agents/ad-hunter.md`: que use el CLI nuevo, lea puntajes de la DB y nunca invente puntajes; incluir comandos de ejemplo.
- [ ] Tests (`pytest`):
  - `test_relevance.py` (casos Air Wick, novelas, Kommo vs “cómo”).
  - `test_schemas.py` (JSON inválido se rechaza).
  - `test_market_score.py` (valores límite).
  - `test_media_info.py` (duración/proporción correctas; video sin pista de audio → `has_audio_track=false`).
  - `test_pipeline_idempotent.py`.
- [ ] Dockerfile: sin ffmpeg (Fase 0.2), usuario no-root, `HOST=0.0.0.0`, healthcheck; servicio `worker` en `docker-compose.yml`.
- [ ] README actualizado.

---

## Cronograma sugerido

| Semana | Fases | Entregable |
|--------|-------|------------|
| 1 | 0, 0.1, 1, 2 | Config de nivel gratuito, datos limpios, solo competidores reales, LatAm incluido |
| 2 | 3, 4 | Análisis multimodal + evaluador IA calibrado |
| 3 | 5, 6, 7 | Puntaje final, nuevo esquema, pipeline único con cola |
| 4 | 8, 9 | Dashboard nuevo, tests, Docker listo |

**Atajo si hay prisa:** Fase 1 → Fase 4 (sobre los datos actuales filtrados) → Fase 2. Así ves puntajes reales en ~3 días.

---

## Costos del LLM
- **Modo elegido: solo Gemini, nivel gratuito (costo $0), 20 solicitudes diarias**, con corrida cada 15 días. El límite real es la **cuota diaria**, no el dinero (ver Fase 0.1).
- Primera corrida: ~3–7 días en evaluarse completa con lotes (se retoma sola con la tarea diaria de recuperación). Corridas quincenales: ~1–2 días.
- Si la cuota se queda corta: activar facturación en el **mismo** proyecto de Google (solo cambian los valores del `.env`) o usar la Batch API de Gemini. No crear varios proyectos para multiplicar la cuota gratuita (riesgo con los términos de Google).
- Corridas siguientes: solo anuncios/creativos nuevos; la caché por `creative_hash` evita gastar cuota en lo ya evaluado.
- Si en el futuro se necesita más volumen: activar facturación en el mismo proyecto de Google (sin cambiar código) y subir `GEMINI_RPM/RPD`; en el nivel de pago Google no usa el contenido para mejorar sus productos.

## Riesgos
| Riesgo | Mitigación |
|--------|-----------|
| Meta cambia la estructura o bloquea el scraping | Mantener `meta-ads-collector` actualizado, backoff, aislar el scraper en `collect/` para poder cambiarlo por la API oficial de Ad Library. |
| URLs de video de Meta expiran | Descargar en la misma corrida en que se descubre el anuncio. |
| El LLM puntúa inconsistente | Rúbrica anclada, temperatura 0, calibración, versionado de prompt. |
| Se agota la cuota gratuita | `QuotaGuard`, cola priorizada (lo importante primero), relevancia en lote, video recortado/baja resolución, tareas de recuperación los días siguientes. |
| Google cambia los límites del nivel gratuito | Límites en `.env` (no en código); revisar el panel de AI Studio antes de cada mes. |
| Términos de uso de Meta | Uso interno de inteligencia competitiva, ritmo moderado de consultas, no redistribuir creativos. |

---

## Definición de “terminado”
- [ ] ≥90 % de anuncios en DB son de competidores relevantes.
- [ ] Pilar conversacional y CRM tienen al menos tantos anuncios como voz; PE/MX/CO cubiertos.
- [ ] 100 % de anuncios con video evaluados tienen transcripción de Gemini (o `has_voiceover=false`) y evaluación con evidencia; el proyecto corre sin Whisper ni FFmpeg.
- [ ] Calibración con Spearman ≥ 0.6 frente a puntajes humanos.
- [ ] Dashboard muestra creative / market / jinora score y evidencias clicables.
- [ ] Un solo comando corre todo el pipeline de forma reanudable.
- [ ] Funciona dentro del nivel gratuito de Gemini y se ejecuta solo los días 1 y 15 (más recuperación de pendientes).

---

## Orden recomendado desde el estado actual (Fase 0 hecha)
1. **Fase 0.1** — modo Gemini gratuito, `QuotaGuard`, lotes, programación.
2. **Fase 0.2** — quitar Whisper/FFmpeg (deja `video_forensics.py` como DEPRECATED hasta la verificación).
3. **Fase 1** — correcciones rápidas (relevance, rutas, streaming, Docker).
4. **Fase 2** — datos limpios por `page_id` y competidores LatAm.
5. **Fase 3** — preparación de media (versión Gemini).
6. **Fase 4** — evaluador Gemini + **calibración** (incluye la verificación de transcripción de la Fase 0.2.2).
7. Después: Fases 5, 6, 7, 8, 9 y el plan de estáticos (`PLAN_ADS_ESTATICOS.md`).

**Atajo para ver puntajes reales pronto:** 0.1 → 0.2 → 1 → 4 sobre los anuncios ya filtrados, y luego la Fase 2.
