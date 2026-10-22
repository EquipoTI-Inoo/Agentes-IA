# Plan de implementación — Jinora multiempresa

Complementa `docs/SERVICIO_MULTIEMPRESA.md` (qué y por qué). Aquí: cómo, en qué orden y cómo se verifica.

## Supuestos (cambiar si las decisiones abiertas se resuelven distinto)

1. Validación de competidores **manual por defecto**, con `auto_approve` opcional.
2. Fuente única en esta versión: **Meta Ads Library**. Otras fuentes quedan fuera de alcance.
3. Las APIs de IA las paga el operador, con `budget_usd` y tope de anuncios por proyecto.
4. Entrega por **dashboard + reporte Markdown**.

## Principios

- **Cosmiq no se rompe:** pasa a ser el proyecto semilla (`project_id` por defecto). Los tests existentes deben pasar en cada fase.
- Cada fase termina en un PR/commit independiente, con tests y con la app funcionando.
- Cada etapa nueva es idempotente y reanudable (estado en base de datos, no en memoria), igual que el pipeline actual.
- Migraciones con `--dry-run` y backup, como `src/db/migrate.py` ya hace.

## Fase 0 — Línea base (0.5 día)

- Correr la suite completa (`pytest`) y dejar el resultado como referencia.
- Congelar un snapshot de la base con los 105 competidores para comparar antes/después de la migración.
- **Hecho cuando:** suite verde documentada.

## Fase 1 — Modelo de datos y migración (2 días)

Archivos: `src/db/models.py`, `src/db/migrate.py`, `src/db/competitors.py`, `tests/test_migrate*.py`.

1. Modelos `Company` y `Project` (`status`, `countries`, `profile_json`, `auto_approve`, `budget_usd`).
2. `Competitor`: añadir `project_id` (FK), `status` (`proposed|approved|rejected`), `affinity_score`; cambiar `unique(name)` por `unique(project_id, name)`.
3. Migración: crear empresa "Cosmiq" y proyecto semilla; asignar todos los competidores existentes con `status=approved`. Reversible vía backup.
4. Helper `get_default_project()` para compatibilidad.

**Pruebas:** migración sobre copia de la base real (conteos idénticos), idempotencia (correrla dos veces), SQLite y PostgreSQL.
**Hecho cuando:** suite verde y Cosmiq intacto con `project_id` asignado.

## Fase 2 — Desacoplar Cosmiq del pipeline (3 días)

Archivos: `src/pipeline.py`, `src/collect/relevance.py`, `src/relevance.py`, `src/catalog.py`, `src/work_queue.py`, `src/evaluator*.py`, `src/ai/schemas.py`, `prompts/*.md`, `config/rubric*.yaml`.

1. `RunOptions.project_id` (por defecto: proyecto semilla); todas las etapas filtran por proyecto.
2. `RelevanceResult`: `is_competitor_of_cosmiq` → `is_competitor`; `pillar` de `Literal` a `str` validado contra los pilares del perfil. Mantener alias de lectura para datos antiguos en `relevance_json`.
3. Regex negativos, keywords y pilares salen del `profile_json` (los actuales se convierten en el perfil de Cosmiq).
4. Prompts como plantillas (`{company_name}`, `{industry}`, `{company_description}`, `{pillars}`); versionar como `v2` sin tocar `v1`.
5. `catalog.out_of_catalog` se calcula por proyecto.
6. La cola (`work_queue.select_queue`) filtra por proyecto y respeta su presupuesto.

**Pruebas:** tests existentes sin cambios; nuevos tests con dos proyectos en la misma base verificando que no se mezclan anuncios ni competidores.
**Hecho cuando:** una corrida de Cosmiq produce los mismos resultados que en la línea base.

## Fase 3 — Etapa Perfil con IA (2 días)

Archivos nuevos: `src/onboarding/profile.py`, `prompts/profile_v1.md`, esquema en `src/ai/schemas.py`.

1. Entrada: nombre, rubro, sitio web (opcional), descripción. Si hay sitio, descargar y resumir texto de la home (con límite de tamaño y timeout).
2. Salida validada con Pydantic: `description`, `audience`, `pillars` (4-6), `keywords`, `negative_terms`, `language`.
3. Usa el modelo barato (`LLM_MODEL_CHEAP`) vía `QuotaGuard`.
4. Job tipo `profile`; estado del proyecto `requested → profiled`. Perfil editable por la empresa antes de seguir.

**Pruebas:** LLM simulado; validación de esquema; entrada con rubro ambiguo; cuota agotada deja el proyecto en `requested` sin contar fallo.

## Fase 4 — Descubrimiento de competidores (4 días)

Archivos nuevos: `src/onboarding/discovery.py`, `prompts/competitor_affinity_v1.md`; reutiliza `src/collect/page_resolver.py` y `src/scraper.py`.

1. Búsqueda en Meta Ads Library con las keywords del perfil por país; agrupar anuncios por `page_id`.
2. Descartar páginas obvias (negative_terms, anunciantes con un solo anuncio salvo que sean afines).
3. Puntaje de afinidad por IA en lotes (página + muestra de textos de anuncios) → `affinity_score` y motivo.
4. Guardar como `Competitor(status=proposed)`; top 10-30 por afinidad. Estado `profiled → competitors_proposed`.
5. Respetar `ratelimit.py` y `MetaBlockedError` (el descubrimiento se pausa y se reanuda, no falla el proyecto).

**Pruebas:** respuestas de Meta grabadas (fixtures); agrupación por página; reanudación tras bloqueo; límite máximo de propuestas.

## Fase 5 — Validación y ejecución por proyecto (3 días)

Archivos: `src/web/app.py`, `src/jobs.py`, `src/worker.py`, `src/onboarding/orchestrator.py` (nuevo).

1. Rutas de la API (solo encolan trabajos): `POST/GET /api/projects`, `GET/POST /api/projects/{id}/competitors`, `POST /api/projects/{id}/approve`.
2. Orquestador: tras aprobar (o `auto_approve`), encola `run` con `project_id` para `discover → filter → download → media`, luego `evaluate → signals`; actualiza `Project.status` por etapa.
3. Tope de anuncios a analizar por proyecto (top N por longevidad/variantes) y control de `budget_usd`; al alcanzarlo, queda `analyzed` parcial con aviso.
4. Cambios de estado y errores visibles en `GET /api/projects/{id}` (etapa actual, progreso, último error).

**Pruebas:** flujo completo con LLM y Meta simulados (solicitud → informe); reanudación tras reinicio del worker; aprobar dos veces no duplica trabajos.

## Fase 6 — Informe y dashboard (3 días)

Archivos: `src/report_company.py` (nuevo), `src/dashboard_data.py`, `src/web/static/dashboard.*`.

1. Informe por proyecto: top anuncios, patrones por competidor (formatos, mensajes, duración), hallazgos accionables generados con IA a partir de datos ya puntuados. Salida en Markdown vía `GET /api/projects/{id}/report`.
2. Dashboard: selector de empresa/proyecto, pantalla de solicitud (formulario), pantalla de validación de competidores y vista de progreso. Las rutas `/api/ads/*` aceptan `?project_id=`.
3. Textos del dashboard sin referencias fijas a Cosmiq.

**Pruebas:** snapshot del informe con datos de ejemplo; tests de API de dashboard con dos proyectos.

## Fase 7 — Seguridad, costos y operación (3 días)

1. Autenticación por API key de empresa; aislamiento: toda consulta se limita al `company_id` de la clave. Pruebas de acceso cruzado (debe dar 404/403).
2. Presupuesto por proyecto con registro de costo (reutilizar `src/cost.py`) y reporte de gasto.
3. Límites de entrada (tamaño, caracteres, tasa de solicitudes por clave) y sanitización del sitio web recibido (solo http/https, bloquear IPs internas para evitar SSRF).
4. Cola justa entre proyectos (round-robin) para que uno grande no bloquee a los demás.
5. Actualizar README, Docker, `schedule_windows.ps1` y variables de `.env.example`.

## Resumen de calendario

| Fase | Entregable | Estimación |
|---|---|---|
| 0 | Línea base | 0.5 d |
| 1 | Modelo y migración | 2 d |
| 2 | Pipeline desacoplado | 3 d |
| 3 | Perfil con IA | 2 d |
| 4 | Descubrimiento de competidores | 4 d |
| 5 | Validación y ejecución por proyecto | 3 d |
| 6 | Informe y dashboard | 3 d |
| 7 | Seguridad y costos | 3 d |
| **Total** | | **~20 días de trabajo** |

El MVP utilizable de punta a punta (sin auth ni dashboard nuevo, usando solo la API) termina en la Fase 5, ~14.5 días.

## Riesgos y mitigación

| Riesgo | Mitigación |
|---|---|
| Regresión en Cosmiq al desacoplar | Fase 0 como línea base; Fase 2 sin cambiar comportamiento; tests de equivalencia |
| Cuota de Gemini (20/día) insuficiente | Tope de anuncios y presupuesto por proyecto; cola justa; evaluar plan de pago |
| Bloqueo de Meta por volumen | Rate limit existente, reanudación, considerar API oficial de Ad Library |
| Competidores mal propuestos por la IA | Validación humana por defecto; afinidad con motivo visible |
| Fuga de datos entre empresas | Auth por clave + pruebas de acceso cruzado (Fase 7) antes de abrirlo |
| Uso de datos de Meta fuera de sus términos | Revisión legal antes del lanzamiento público |

## Siguiente paso

Confirmar los 4 supuestos de arriba y arrancar por Fase 0 + Fase 1.
