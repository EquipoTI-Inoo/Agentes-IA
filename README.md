# Jinora — inteligencia sobre anuncios de la competencia

Jinora vigila los anuncios de los competidores de **Cosmiq** en la **Meta Ads Library** (Facebook e Instagram), descarga sus creativos, los evalúa con modelos de IA con una rúbrica anclada y los ordena por un puntaje final. Cosmiq es una plataforma de automatización de ventas y atención para empresas de LatAm (WhatsApp, CRM, agendamiento, gestión de leads, mensajes masivos, agentes conversacionales y agentes de voz); por eso **mencionar IA o usar un formato (persona, UGC, motion graphics) no suma ni resta puntos**.

## Cómo funciona

```
discover → filter → download → media → evaluate → signals
```

| Etapa | Qué hace | `Ad.status` al terminar |
|---|---|---|
| discover | Busca anuncios por competidor y país (primero por `page_id`) | `discovered` |
| filter | Relevancia en dos capas: `rejected`, `out_of_catalog` (página que no es un competidor del catálogo), `needs_manual_review` | `filtered` |
| download | Baja video e imagen (streaming, sin repetir archivos) | `downloaded` |
| media | Hashes, pHash, duración, proporción, pista de audio (opcional: Whisper local) | `pending_evaluation` |
| evaluate | Cola priorizada: videos → **Gemini**, estáticos → **Claude** | `evaluated` |
| signals | `market_score` y `jinora_score`, solo con datos locales | `scored` |

Cada etapa es **idempotente**: una corrida interrumpida se reanuda ejecutando el mismo comando, sin repetir descargas ni evaluaciones.

**Los tres puntajes** (todos guardados en la base; el dashboard y los reportes solo los leen):

- `creative_score` (0-100): 4 dimensiones de 0 a 25 con anclas. Lo suma el código, nunca el modelo. El evaluador nunca recibe días activos, variantes ni nombre de marca.
- `market_score` (0-100): solo datos de la plataforma (longevidad, variantes del creativo, países, sigue activo, plataformas). Sin IA.
- `jinora_score` = `SCORE_WEIGHT_CREATIVE` × creativo + `SCORE_WEIGHT_MARKET` × mercado (0.7 / 0.3 por defecto). Es `NULL` mientras el anuncio no tenga `creative_score`. No hay topes ni cupos numéricos.

## Requisitos

- **Python 3.11** (probado en Windows 10; también corre en Linux y en Docker).
- **libmediainfo** para leer metadatos de video (`pymediainfo`): en Windows viene con el paquete; en Linux `apt install libmediainfo0v5`. **No hace falta ffmpeg.**
- Una clave de **Gemini** (Google AI Studio, nivel gratuito: 20 solicitudes por día) y, para estáticos, una clave de **Anthropic** con crédito prepago.

## Instalación

```powershell
git clone <url-del-repositorio> Jinora
cd Jinora
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # dependencias de ejecución, con versiones fijas
pip install -r requirements-dev.txt      # opcional: pytest, TestClient, Whisper local (comparar transcripciones)
copy .env.example .env                   # y rellena tus claves (ver la sección siguiente)
python -m src.db.migrate                 # crea la base y carga el catálogo de 105 competidores
```

`python -m src.db.migrate` también migra una base del esquema anterior (haz antes `--dry-run`; crea un backup verificado en `data/backup/`).

**Multiempresa (empresas y proyectos).** Cada competidor pertenece a un proyecto de una empresa; el catálogo de Cosmiq es el proyecto por defecto (ver `docs/PLAN_MULTIEMPRESA.md`). En una base existente:

```bash
python -m src.db.tenancy --dry-run      # qué cambiaría
python -m src.db.tenancy                # backup verificado, migra y verifica (también lo hace src.db.migrate)
```

No toca anuncios, medios ni evaluaciones; cambia `UNIQUE(name)` de competidores por `UNIQUE(project_id, name)`.

## Configuración (`.env`)

El archivo `.env` **no se versiona** (está en `.gitignore`); `.env.example` lista las variables con los secretos vacíos. Los valores por defecto de las no secretas están en `src/config.py`.

| Grupo | Variables | Para qué sirven |
|---|---|---|
| Claves | `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID` | Acceso a Gemini y Claude. El workspace solo hace falta si tu clave de Anthropic no está asociada a uno |
| Modelos | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_MODEL_CHEAP`, `CLAUDE_MODEL_STATIC`, `CLAUDE_MAX_IMAGE_PX` | Modelo de video, modelo barato (relevancia) y modelo de estáticos |
| Cuota de Gemini | `FREE_TIER_MODE`, `GEMINI_RPM`, `GEMINI_RPD`, `GEMINI_TPM`, `GEMINI_RPD_MAIN`, `GEMINI_RPD_CHEAP`, `RESERVE_REQUESTS`, `GEMINI_RETRY_DELAYS` | Límites diarios y por minuto, reserva que nunca se gasta y esperas ante 503/429 |
| Presupuesto de Claude | `CLAUDE_BUDGET_USD` | Tope de gasto en estáticos; al alcanzarlo quedan en `pending_evaluation` |
| Evaluación | `EVAL_BATCH_VIDEOS`, `EVAL_BATCH_STATICS`, `RELEVANCE_BATCH_SIZE`, `MAX_EVALS_PER_RUN`, `VIDEO_MAX_SECONDS_FOR_LLM`, `GEMINI_MEDIA_RESOLUTION`, `EVAL_PREFERRED_HOURS` | Tamaño de lotes, límites por corrida y ventana de baja demanda (hora de Lima) |
| Recolección | `DEFAULT_COUNTRIES`, `FIRST_RUN_PRIORITY_ONLY`, `COLLECT_INACTIVE` | Países por defecto, solo prioridad 1 en la primera corrida, guardar anuncios inactivos (solo datos) |
| Puntaje final | `SCORE_WEIGHT_CREATIVE`, `SCORE_WEIGHT_MARKET` | Pesos de `jinora_score` (deben sumar 1.0; se valida al cargar) |
| Datos | `DATABASE_URL`, `DOWNLOADS_DIR` | SQLite local por defecto; PostgreSQL con una URL de SQLAlchemy |
| Servidor | `HOST`, `PORT`, `ENV` | Solo para `start_server.py`; escucha en `127.0.0.1` salvo que lo cambies (la imagen Docker usa `0.0.0.0`) |

## Uso: el pipeline

```bash
# Un pilar, en tus mercados (voz, conversacional o crm)
python -m src.cli run --pillar conversacional --countries PE,MX,CO --limit 20

# Un competidor del catálogo, solo algunas etapas
python -m src.cli run --competitor "Kommo" --stages download,media,evaluate

# Barrido completo del catálogo (el de la tarea programada); descarga solo anuncios con 15+ días
python -m src.cli run --all --countries US,ES,MX --limit 15 --download-min-days 15 --max-ads 1000

# Búsqueda libre de una marca (sustituye al antiguo `main.py --query`)
python -m src.cli run --query "Notion" --countries US --limit 5 --media video

# Transcripción local con Whisper en la etapa media (requiere requirements-dev.txt)
python -m src.cli run --competitor "Kommo" --stages download,media --forensics
```

Las búsquedas por competidor usan su `page_id`; si un anuncio aparece por palabra clave pero su página **no es un competidor del catálogo**, queda como `out_of_catalog` y no se evalúa (por eso una búsqueda libre de una marca ajena descubre y guarda, pero no gasta cuota).

Hacia Meta se aplica una pausa aleatoria de 1 a 3 s entre consultas y backoff si hay bloqueo (`MetaBlockedError` detiene el descubrimiento, no el resto del pipeline).

### Cola de evaluación

```bash
python main.py queue --status                 # cola, evaluados, solicitudes de hoy, días estimados, fingerprint de la clave
python -m src.cli evaluate --pending          # evalúa la cola priorizada
python main.py queue --run --respect-hours    # igual, pero solo dentro de la ventana 01:00-06:00 (Lima)
python main.py queue --run --now              # ignora la ventana
python -m src.cli rescore                     # recalcula market_score y jinora_score, sin APIs
```

Orden de la cola: activos antes que inactivos, competidores de prioridad 1, mayor longevidad, más variantes. `rejected` y `out_of_catalog` nunca entran, ni los anuncios sin su archivo local. Días estimados = videos en cola / (`GEMINI_RPD` − `RESERVE_REQUESTS`).

Si Gemini responde **429 diario**, el anuncio sigue en `pending_evaluation` (no cuenta como fallo) y la corrida se detiene; continúa al día siguiente. `evaluate --force --prompt-version v2` reevalúa con otra versión del prompt y exige que existan `prompts/evaluator_v2.md` y `prompts/evaluator_static_v2.md`.

### Higiene del catálogo

```bash
python -m src.cli cleanup --dry-run           # lista (nombre, page_id, nº de anuncios) lo que se marcaría como out_of_catalog
python -m src.cli cleanup --apply             # lo marca; no borra nada
```

Si una página listada es en realidad un competidor, añade su `page_id` a `config/competitors_database.json` **antes** de aplicar.

### Worker (trabajos en segundo plano)

El dashboard y la API solo **encolan** trabajos (tabla `jobs`); un proceso aparte los ejecuta. Se puede detener con Ctrl+C sin perder trabajos.

```bash
python -m src.cli worker                      # queda escuchando la cola
python -m src.cli worker --once               # procesa lo pendiente y termina
```

## Tarea programada de Windows

`scripts/schedule_windows.ps1` define tres tareas. **Por defecto solo simula**; nada se registra sin `-Register`.

| Tarea | Cuándo | Qué ejecuta |
|---|---|---|
| `Jinora-EvalQueue-Nocturna` | Cada día 01:30 (Lima) | `python main.py queue --run --respect-hours` |
| `Jinora_Biweekly_Sweep` | **Días 1 y 15 de cada mes**, 09:00 | `python -m src.cli run --all` |
| `Jinora_Daily_Pending_Catchup` | Cada día 09:30 | `python -m src.cli evaluate --pending` |

Todas usan `StartWhenAvailable` (se recuperan si el equipo estaba apagado). A las 02:00 de Lima es la medianoche del Pacífico, cuando Google reinicia la cuota diaria.

```powershell
# 1) Simulación (no registra nada):
powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1

# 2) Registrar las tareas (lo ejecutas tú):
powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1 -Register
```

El barrido `run --all` consulta todo el catálogo y evalúa lo nuevo: consume cuota de Gemini y crédito de Claude, así que conviene probarlo antes en un pilar.

## Dashboard

```bash
python start_server.py                        # http://127.0.0.1:8000
```

Tres pestañas: **Ranking** (una fila por creativo con "x N variantes"; filtros de pilar, país, competidor, formato, idioma y voz; detalle con reproductor, las 4 dimensiones sobre 25, evidencias con timestamp clicable, "Qué copiar / Qué superar / Idea para Cosmiq" y botón "Encolar análisis"), **Por competidor** y **Cola**. Funciona en modo claro y oscuro y en móvil.

El dashboard **solo lee** los puntajes guardados; escribir solo escribe encolando trabajos (necesitas el worker para que se procesen). API principal: `GET /api/ads/top`, `GET /api/ads/{id}`, `GET /api/competitors`, `GET /api/queue/status`, `GET /api/media/{id}` (con `Range`), `POST /api/evaluate/{id}`, `GET /api/jobs/{id}` y `GET /health`. El texto de los anuncios se muestra siempre como texto (no HTML) y el servidor envía una Content-Security-Policy estricta.

## Reportes

```bash
python -m src.cli report --pillar crm --top 10                  # tabla en consola
python -m src.cli report --pillar crm --top 10 --format md      # reports/jinora_top10_crm_<fecha>.md
python -m src.cli report --pillar crm --top 10 --format html
python -m src.cli report --pillar crm --top 10 --format md --summary
```

El reporte incluye la tabla del top, el detalle de cada anuncio, y "Patrones comunes" calculados **con código** (ángulos `feature_focus`, formatos, rango de duración, uso de voz). `--summary` añade un resumen ejecutivo con **una sola solicitud** a Gemini (pasa por la cuota); está apagado por defecto y el reporte indica si se generó o por qué no. Los reportes no contienen claves ni rutas absolutas.

## Calibración

La rúbrica se calibra contra tus puntajes manuales:

```bash
python scripts/calibrate.py                   # lee calibration/manual_scores.csv (source=owner) y la base (analyses)
python scripts/compare_transcripts.py --ids 3172443476285076 --out calibration/results/transcripciones.json
```

`calibrate.py` compara tu total con el `creative_score` guardado (la evaluación más reciente con la rúbrica vigente) y reporta Spearman, error absoluto medio, diferencia media por formato y la tabla video | dueño | IA. Meta: Spearman ≥ 0.6 con al menos 15 puntajes tuyos. Si sale por debajo, el ajuste se hace en las anclas (`config/rubric.yaml`) y se vuelve a medir con anuncios que no se usaron para ajustar. `compare_transcripts.py` compara con WER la transcripción de Gemini contra Whisper local.

## Docker

```bash
cp .env.example .env                          # y rellena tus claves; .env nunca entra a la imagen
docker compose config                         # valida el archivo
docker compose build
docker compose up -d                          # servicios: web y worker
```

- **web**: el dashboard, publicado **solo en `127.0.0.1:8000`**; el `HEALTHCHECK` consulta `/health`.
- **worker**: `python -m src.cli worker`, comparte la base con `web`.
- Volúmenes: `./data`, `./downloads`, `./logs`, `./config` y `./reports`. `.env` se inyecta con `env_file`.
- La imagen no lleva ffmpeg, corre sin privilegios y excluye `.env`, `data/`, `downloads/`, `logs/` y los videos de calibración (`.dockerignore`).
- En Linux: `export JINORA_UID=$(id -u) JINORA_GID=$(id -g)` antes de `docker compose up` para que el contenedor pueda escribir en los volúmenes.
- Para PostgreSQL, cambia `DATABASE_URL` (ejemplo comentado en `docker-compose.yml`); las tablas de cuota (`llm_usage`, `llm_attempts`) siguen en el archivo SQLite de `data/`.

## Pruebas

```bash
python -m pytest                              # toda la suite; usa bases temporales y mocks, 0 llamadas a APIs
```

Los tests nunca tocan `data/meta_ads.db` ni llaman a Gemini o Claude. Incluyen un test de higiene que falla si algún archivo del repositorio contiene algo con forma de clave de API.

## Solución de problemas

| Síntoma | Causa y qué hacer |
|---|---|
| **503 "high demand"** de Gemini | Saturación temporal del modelo. Los reintentos con espera creciente (5, 15, 45, 120 s) ya están integrados y **cada intento cuenta** contra tu cuota diaria. Si persiste, el anuncio queda en `pending_evaluation`; programa la evaluación en la ventana nocturna (01:00-06:00 Lima) |
| **429 diario** (cuota agotada) | Se acabaron las solicitudes del día. La corrida se detiene sola y los anuncios siguen pendientes (no son fallos). Se reanuda a medianoche del Pacífico (02:00 Lima). `queue --status` muestra solicitudes de hoy y días estimados. No cambies de clave para saltarte el límite: la cuota gratuita es por proyecto de Google |
| **Claude responde 400 por falta de workspace** | Tu clave de Anthropic no está asociada a un workspace. Pon el ID en `ANTHROPIC_WORKSPACE_ID` (en tu `.env`) o crea la clave dentro de un workspace. Mientras tanto los estáticos quedan en `pending_evaluation` |
| **Cambié la clave de Gemini** | El contador de cuota es **por clave**: una clave nueva empieza el día en 0. `queue --status` muestra el *fingerprint* (primeros 8 caracteres del hash de la clave activa, nunca la clave). Si no coincide con el que esperabas, revisa `GEMINI_API_KEY`. Los registros de la clave anterior se conservan. Aviso: si la clave nueva es del **mismo proyecto de Google**, comparte el límite diario |
| `La base tiene el esquema anterior a la Fase 6` | Ejecuta `python -m src.db.migrate --dry-run` y luego `python -m src.db.migrate` |
| `La tabla competitors aún tiene UNIQUE(name) global` | Ejecuta `python -m src.db.tenancy --dry-run` y luego `python -m src.db.tenancy` |
| `El catálogo de competidores está vacío` | Ejecuta `python -m src.db.migrate` (carga `config/competitors_database.json`) |
| La cola dice "pendientes sin archivo local" | Esos anuncios no tienen su video o imagen descargado: `python -m src.cli run --competitor "<nombre>" --stages download,media` |
| El dashboard muestra "Aún no hay anuncios evaluados" | Todavía no hay `creative_score` guardados: mira la pestaña **Cola** y ejecuta `evaluate --pending` |
| Un trabajo encolado desde el dashboard no avanza | Falta el worker: `python -m src.cli worker` |
| Meta bloquea las consultas | Backoff automático; si el bloqueo persiste el descubrimiento se detiene. Espera y repite el mismo comando |

## Estructura

```
src/pipeline.py        etapas del pipeline         src/cli.py, main.py   comandos
src/work_queue.py      cola priorizada             src/worker.py, jobs.py  cola de trabajos
src/evaluator*.py      evaluadores (video, estático)   src/ai/            clientes LLM, cuota, esquemas
src/scoring/           market_score y jinora_score     src/db/            modelos y migración
src/web/               dashboard y API             src/report.py, summary.py  reportes
config/                catálogo, rúbricas, pesos   prompts/              prompts de los evaluadores
scripts/               calibrate, compare_transcripts, resolve_page_ids, run_phase4_real_tests, schedule_windows.ps1
scripts/legacy/        scripts históricos sin mantenimiento (ver su README)
tests/                 suite de pruebas            docs/                 plan y progreso
```

El plan de implementación está en `docs/PLAN_IMPLEMENTACION.md` y el avance en `docs/PROGRESO.md`. El agente de Antigravity (`.agents/agents/ad-hunter.md`) opera el sistema con estos mismos comandos.

> El modo clásico `python main.py --query ... --pillar ...` (descarga y reporte Markdown sin puntajes) sigue disponible pero está **obsoleto**; usa `python -m src.cli run`.
