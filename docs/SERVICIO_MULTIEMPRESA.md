# Jinora como servicio para cualquier empresa — flujo y diseño

Hoy Jinora está atado a Cosmiq: catálogo fijo de 105 competidores, pilares fijos (conversacional / voz / crm),
prompts y rúbrica que mencionan a Cosmiq. Este documento define el camino que sigue una empresa solicitante
y qué hay que cambiar en el código para soportarlo.

## 1. Camino de la empresa solicitante

```
1. Solicitud ──> 2. Perfil ──> 3. Descubrir competidores ──> 4. Validar ──> 5. Descargar ──> 6. Analizar ──> 7. Informe
```

| # | Etapa | Entrada | Qué hace | Salida / estado del `Project` |
|---|---|---|---|---|
| 1 | Solicitud | nombre, rubro, sitio web (opcional), países, objetivo | Crea la empresa y un proyecto de análisis | `requested` |
| 2 | Perfil | datos de la solicitud | La IA (modelo barato) genera el perfil: qué vende, público, 4-6 "pilares"/categorías del rubro, palabras clave, términos negativos (reemplaza a los pilares y regex fijos) | `profiled` |
| 3 | Descubrir competidores | perfil | IA + búsqueda en Meta Ads Library por palabras clave y países; agrupa anuncios por página, resuelve `page_id` (reutiliza `collect/page_resolver.py`) y propone 10-30 competidores con puntaje de afinidad | `competitors_proposed` |
| 4 | Validar | lista propuesta | La empresa confirma, quita o agrega competidores (punto humano; evita gastar cuota en páginas irrelevantes). Opción `auto_approve` para uso sin revisión | `competitors_approved` |
| 5 | Descargar | competidores aprobados | Pipeline existente `discover → filter → download → media` limitado a ese proyecto | `downloaded` |
| 6 | Analizar | anuncios descargados | `evaluate → signals` con la rúbrica parametrizada por el perfil (no por Cosmiq) | `analyzed` |
| 7 | Informe | anuncios puntuados | Top anuncios, patrones por competidor, hallazgos accionables para la empresa (dashboard + reporte) | `done` |

Cada etapa es idempotente y reanudable, igual que el pipeline actual; el proyecto guarda su `status` y un registro de etapas.

## 2. Cambios al modelo de datos (`src/db/models.py`)

Nuevas tablas:

- `Company` — `id`, `name`, `industry` (rubro), `website`, `description`, `created_at`.
- `Project` — `id`, `company_id`, `status`, `countries` (JSON), `profile_json` (perfil IA: pilares, keywords, negativos), `auto_approve`, `budget_usd`, timestamps.

Cambios a tablas existentes:

- `Competitor`: añadir `project_id` (FK). Hoy `name` es `unique`; pasa a `unique(project_id, name)`. Añadir `status` (`proposed | approved | rejected`) y `affinity_score`. El catálogo actual de Cosmiq se migra como un `Project` semilla.
- `Ad`: sin cambio estructural (se llega al proyecto vía `competitor_id`); `pillar` pasa de enum fijo a texto libre tomado del perfil.
- `Job`: `payload` lleva `project_id`; nuevos tipos de job: `profile`, `find_competitors`, `run` (ya existe).

Se mantiene compatibilidad con SQLite/PostgreSQL y se agrega migración en `src/db/migrate.py`.

## 3. Cambios de código (desacoplar Cosmiq)

| Archivo | Cambio |
|---|---|
| `src/collect/relevance.py` | `is_competitor_of_cosmiq` → `is_competitor`; `pillar` deja de ser `Literal` fijo; regex negativos y keywords salen del `profile_json` del proyecto (los actuales quedan como perfil Cosmiq) |
| `prompts/*.md` | Plantillas con variables `{company_name}`, `{industry}`, `{company_description}`, `{pillars}` en lugar de texto Cosmiq |
| `config/rubric*.yaml` | Dimensiones de la rúbrica se mantienen (calidad creativa); las referencias específicas de Cosmiq se sacan a variables del perfil |
| `src/pipeline.py` | `RunOptions.project_id`; todas las etapas filtran por proyecto; sin `project_id` se usa el proyecto Cosmiq (compatibilidad) |
| `src/catalog.py` | `out_of_catalog` se calcula contra los competidores aprobados del proyecto |
| **Nuevo** `src/onboarding/profile.py` | Etapa 2: genera el perfil con IA (esquema Pydantic validado) |
| **Nuevo** `src/onboarding/discovery.py` | Etapa 3: búsqueda por keywords, agrupación por página, afinidad con IA |
| **Nuevo** `src/report_company.py` | Etapa 7: informe por proyecto |

## 4. API (`src/web/app.py`)

Todas solo encolan jobs, como las rutas actuales.

```
POST /api/projects                       # {company_name, industry, website?, countries, auto_approve?} -> project_id, encola profile
GET  /api/projects/{id}                  # estado + etapa actual + progreso
GET  /api/projects/{id}/competitors      # propuestos / aprobados
POST /api/projects/{id}/competitors      # agregar uno manualmente (nombre o page_id)
POST /api/projects/{id}/approve          # {competitor_ids: [...]} -> encola download + analyze
GET  /api/projects/{id}/ads/top          # resultado del análisis
GET  /api/projects/{id}/report           # informe final
```

El dashboard recibe un selector de proyecto/empresa; las rutas `/api/ads/*` actuales aceptan `?project_id=`.

## 5. Costos y límites (importante al abrirlo a terceros)

- Cuota de Gemini (20 solicitudes/día en gratis) y crédito de Claude **no alcanzan** para múltiples clientes: cada proyecto necesita `budget_usd` y un tope de anuncios a analizar (p. ej. top N por longevidad), y la cola prioriza por proyecto.
- Autenticación y aislamiento por empresa (API key por cliente) antes de exponerlo públicamente; hoy la API no tiene auth.
- Meta Ads Library: el scraping aplica rate limit por IP; con varios clientes conviene la API oficial de Ad Library o un pool de proxies. Revisar términos de uso.

## 6. Plan de implementación sugerido

1. Modelo `Company`/`Project` + migración (Cosmiq pasa a ser el proyecto semilla). Tests de migración.
2. Parametrizar relevancia, prompts y pipeline por `project_id` sin cambiar el comportamiento de Cosmiq (los tests actuales deben pasar).
3. Etapa Perfil (IA) + API `POST /api/projects`.
4. Etapa Descubrir competidores + validación + API de aprobación.
5. Conectar descarga/análisis por proyecto + informe + selector en el dashboard.
6. Auth, presupuesto por proyecto y límites.

## 7. Decisiones abiertas

1. ¿La validación de competidores es manual (empresa confirma) o automática por defecto?
2. ¿Solo Meta Ads Library, o también otras fuentes (TikTok, Google Ads Transparency, web/redes) para "contenidos"?
3. ¿Quién paga las APIs de IA: tú (con límites por plan) o la empresa trae su clave?
4. ¿Entrega vía dashboard web, reporte PDF/MD, o ambos?
