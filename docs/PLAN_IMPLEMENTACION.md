# Jinora — Plan de Implementación de Mejoras

> **Para el agente de Antigravity:** ejecuta este plan **una fase a la vez**, en orden. Al terminar cada fase: marca las casillas `[x]`, corre los criterios de aceptación y detente para revisión antes de pasar a la siguiente. No borres datos sin hacer backup (Fase 0). Mantén el código en Python 3.11, comentarios en español y nombres de variables en inglés.

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
                │ 4. FORENSICS │  Whisper+VAD, keyframes por escena, gancho 0–3 s
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
    forensics.py         # Whisper+VAD, keyframes, gancho
    fingerprint.py       # hash de creativo para variantes
  ai/
    llm_client.py        # abstracción Gemini / Claude / OpenAI
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

**Decisión del usuario:** usar la **API gratuita de Gemini** y lanzar el barrido **una vez cada 15 días**.

**Contexto de límites:** el nivel gratuito tiene límites por minuto (RPM), por día (RPD, se reinicia a medianoche hora del Pacífico) y por tokens (TPM), **por proyecto**. Google ya no publica una tabla fija: el usuario debe revisar los valores reales en el panel de *Rate limits* de AI Studio y copiarlos al `.env`. Solo usar modelos **Flash / Flash-Lite** (los Pro recientes no tienen nivel gratuito). En el nivel gratuito Google puede usar el contenido para mejorar sus productos → **nunca enviar datos de clientes**, solo anuncios públicos.

**Límite confirmado por el usuario: 20 solicitudes diarias (RPD)** en su proyecto. El cuello de botella son las **solicitudes**, no los tokens → la estrategia central es **evaluar varios anuncios por solicitud (lotes)**.

**Volumen esperado (estimado, se medirá con `llm_usage`):** primera corrida ~300–500 anuncios; corridas siguientes ~50–150 anuncios nuevos (lo ya evaluado queda en caché).

**Capacidad con 20 RPD:**
| Estrategia | Anuncios/día | Primera corrida (~400) | Cada 15 días (~100) |
|---|---|---|---|
| 1 anuncio por solicitud | 20 | ~20 días ❌ | ~5 días |
| Lotes (3 videos o 6 estáticos por solicitud) | ~60–100 | ~5–7 días | 1–2 días ✅ |
| Lotes + filtro de “vale la pena evaluar” + primera corrida solo prioridad 1 | ~60–100 | ~3–4 días | 1 día ✅ |

Tokens estimados por llamada (verificar con la documentación vigente): video 90 s en resolución baja ≈ 13.500; imagen ≈ 5.500; carrusel 10 tarjetas ≈ 17.000. Un lote de 3 videos ≈ 40.000 tokens, muy por debajo del TPM.

### 0.1.1 Configuración (extender `src/config.py` y `.env.example`)
- [ ] `GEMINI_RPM` (default 5), `GEMINI_RPD` (default **20**, valor real del usuario), `GEMINI_TPM` (default 250000) — el usuario los ajusta según AI Studio.
- [ ] **Cuota por modelo:** en el nivel gratuito cada modelo suele tener su propio límite. Permitir límites separados: `GEMINI_RPD_MAIN` (para `LLM_MODEL`) y `GEMINI_RPD_CHEAP` (para `LLM_MODEL_CHEAP`). El usuario verifica en AI Studio si Flash-Lite tiene cuota propia; si es así, relevancia y estáticos simples van por Flash-Lite y los videos por Flash.
- [ ] `EVAL_BATCH_VIDEOS` (default 3) y `EVAL_BATCH_STATICS` (default 6): anuncios por solicitud.
- [ ] `RESERVE_REQUESTS` (default 2): solicitudes diarias que no se usan en el barrido (para pruebas manuales o reintentos).
- [ ] `FIRST_RUN_PRIORITY_ONLY` (default `true`): la primera corrida solo evalúa competidores `priority=1`.
- [ ] `MAX_EVALS_PER_RUN` (default 0 = sin tope; útil para pruebas).
- [ ] `VIDEO_MAX_SECONDS_FOR_LLM` (default 90) y `GEMINI_MEDIA_RESOLUTION` (`low` | `medium`, default `low`).
- [ ] `RELEVANCE_BATCH_SIZE` (default 20).
- [ ] `FREE_TIER_MODE` (bool, default `true`).
- [ ] Verificar que `DEFAULT_COUNTRIES=PE,MX,CO` (texto separado por comas) en `.env` se lea bien: pydantic-settings intenta decodificar como JSON los campos tipo lista; si falla, anotar el campo con `NoDecode` o declararlo como `str` y exponer una propiedad que devuelva la lista. Agregar un test.

### 0.1.2 Control de cuota — `src/ai/quota.py`
- [ ] Tabla `llm_usage(day_pt, model, requests, tokens_in, tokens_out)` en la DB (día calculado en hora del Pacífico, que es cuando Google reinicia el RPD).
- [ ] `QuotaGuard.acquire(model, est_tokens)`: espera lo necesario para respetar RPM/TPM; si se alcanzó `GEMINI_RPD` del día lanza `QuotaExhausted`.
- [ ] Ante error **429** de la API: backoff exponencial (30 s, 60 s, 120 s); si persiste o el mensaje indica cuota diaria agotada → `QuotaExhausted`.
- [ ] Registrar uso real (tokens de la respuesta) tras cada llamada.

### 0.1.3 Comportamiento del pipeline con cuota
- [ ] Al capturar `QuotaExhausted`: guardar estado, dejar los anuncios restantes como `pending_evaluation`, imprimir resumen (“evaluados X, pendientes Y, se retoma en la próxima corrida”) y salir con código 0 (no es error).
- [ ] **Cola priorizada** de evaluación: 1) competidores `priority=1` (WhatsApp/CRM LatAm), 2) `is_active=true`, 3) mayor `market_score`, 4) más reciente. Así, si la cuota se acaba, lo importante ya quedó evaluado.
- [ ] Las etapas sin LLM (scraping, descarga, Whisper, FFmpeg, features de imagen, `market_score`) **siempre** se completan; solo la evaluación queda pendiente.

### 0.1.4 Ahorro de cuota y tokens
- [ ] **Relevancia en lote:** el clasificador recibe `RELEVANCE_BATCH_SIZE` anuncios (solo texto: página, copy, titular, dominio) en **una** llamada y devuelve una lista JSON. Los anuncios de competidores mapeados por `page_id` no gastan llamada.
- [ ] **Video liviano:** enviar a Gemini solo los primeros `VIDEO_MAX_SECONDS_FOR_LLM` segundos (recorte con FFmpeg sin recodificar si es posible) y con `media_resolution=low` (≈100 tokens/s en lugar de ≈300 tokens/s; verificar en la documentación vigente).
- [ ] Borrar los archivos subidos a la Files API de Gemini después de evaluarlos.
- [ ] Caché estricta por `creative_hash`: variantes del mismo creativo **heredan** la evaluación (1 llamada por creativo, no por anuncio).
- [ ] Una sola llamada por anuncio (evaluación completa); sin llamadas de “resumen” extra durante el barrido. El resumen ejecutivo por pilar (Fase 8) se genera al final con 1 llamada por pilar, solo si queda cuota.

### 0.1.5 Programación cada 15 días (Windows, sin servidor)
- [ ] Comandos del CLI (Fase 7):
  - `python -m src.cli run --all` → barrido completo (scraping + preparación + evaluación hasta agotar cuota).
  - `python -m src.cli evaluate --pending` → solo evalúa lo pendiente (no hace scraping).
- [ ] `scripts/schedule_windows.ps1` que registre con el Programador de tareas:
  - **Barrido:** días **1 y 15** de cada mes, 09:00 → `run --all`.
  - **Recuperación de pendientes:** días **2, 3, 4, 16, 17 y 18**, 09:00 → `evaluate --pending` (termina sin hacer nada si no hay pendientes).
  - Opción “ejecutar lo antes posible si se omitió el inicio programado” activada (por si la PC estaba apagada).
  - Usar la ruta absoluta del Python del entorno virtual y el directorio del proyecto como carpeta de inicio.
- [ ] Log por corrida en `logs/run_YYYYMMDD.log` + línea resumen en `logs/history.csv` (fecha, anuncios nuevos, evaluados, pendientes, requests usadas).

**Criterio de aceptación:**
- Con `GEMINI_RPD=3` y 10 anuncios, el pipeline evalúa 3, deja 7 pendientes, sale sin error; `evaluate --pending` al día siguiente continúa sin repetir los ya evaluados.
- Un 429 simulado activa el backoff y no rompe la corrida.
- Las tareas aparecen en el Programador de tareas de Windows.

> **Prompt para Antigravity:** “Ejecuta la FASE 0.1 de docs/PLAN_IMPLEMENTACION.md. La Fase 0 ya está hecha: extiende el src/config.py y el .env.example existentes con las nuevas variables (no los reescribas). Crea src/ai/quota.py y deja preparada la cola priorizada y el manejo de QuotaExhausted para conectarlos en las Fases 4 y 7. Crea scripts/schedule_windows.ps1 pero NO lo ejecutes hasta que el pipeline (Fase 7) esté listo.”

---

## FASE 1 — Correcciones rápidas (1 día)

**Objetivo:** eliminar bugs que corrompen datos o rompen la ejecución.

- [ ] `scripts/mass_global_harvester.py`: quitar `fps_sample=1.0` de `analyze_video()` (hoy lanza `TypeError` silenciado y nunca transcribe).
- [ ] **Rutas relativas:** guardar `video_path`/`image_path` relativos a `DOWNLOADS_DIR`. Script `scripts/fix_paths.py` que convierta las rutas `C:\Users\Gabo\Documents\Jinora\downloads\...` existentes a relativas.
- [ ] `src/relevance.py`: reemplazar `any(k in page_name for k in ["ai", ...])` por coincidencia de **palabra completa** con regex (`\bai\b`, `\bcrm\b`…). Agregar tests con "Air Wick", "Airmoto", "Chair Co" (deben rechazarse) y "Bland AI", "Kommo" (aprobarse).
- [ ] `src/scraper.py` → descarga en **streaming** a archivo temporal y `rename` al terminar; timeout configurable (120 s para video); reintentos con `tenacity`; límite de tamaño (ej. 200 MB).
- [ ] `start_server.py`: host desde env (`0.0.0.0` en Docker), `reload` solo si `ENV=dev`.
- [ ] `main.py` → `generate_markdown_report`: usar `datetime.now()` en lugar de `Path.cwd()`.
- [ ] Whisper como **singleton** (cargar una vez por proceso), no en cada request de la API.
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

## FASE 3 — Análisis forense multimodal (2 días)

**Objetivo:** que la IA reciba evidencia real de audio **y** video, sin basura.

### 3.1 Audio
- [ ] Whisper con `vad_filter=True`, `condition_on_previous_text=False`, modelo `small` por defecto.
- [ ] Detectar **“sin voz”**: si no hay segmentos o `no_speech_prob` promedio > 0.6 → `has_voiceover=false`, transcript vacío (no alucinaciones).
- [ ] Guardar `language`, `language_probability`; si la probabilidad < 0.5, re-transcribir forzando `es` y `en` y quedarse con la de mejor log-prob.

### 3.2 Video
- [ ] **Keyframes por cambio de escena** con FFmpeg: `select='gt(scene,0.3)'`, máximo 20 frames, redimensionados a 768 px de ancho.
- [ ] **Gancho:** frames a 4 fps en los primeros 3 s (12 frames) → carpeta `hook/`.
- [ ] Métricas objetivas por código: duración, nº de cortes de escena, cortes por minuto, ratio de aspecto (9:16 / 1:1 / 16:9), si tiene subtítulos quemados (lo detecta el LLM en 3.3).
- [ ] Borrar la extracción a 1 fps del video completo (ocupa disco y no se usa).

### 3.3 Comprensión visual
- [ ] Si `LLM_PROVIDER=gemini`: subir el video (Gemini procesa audio+imagen nativamente) además de la transcripción. En `FREE_TIER_MODE`: recortado a `VIDEO_MAX_SECONDS_FOR_LLM` y con `media_resolution` baja (Fase 0.1.4).
- [ ] Si `anthropic`/`openai`: enviar keyframes + frames del gancho como imágenes + transcripción con timestamps.
- [ ] La salida de esta etapa se guarda en `forensics.json` (reemplaza `full_forensic_analysis.json`).

**Criterio de aceptación:** el video de Attio (solo música) queda con `has_voiceover=false` y sin transcripción inventada; un video de Bland AI produce ≤20 keyframes y 12 frames de gancho.

> **Prompt para Antigravity:** “Ejecuta la FASE 3. Refactoriza video_forensics.py a src/media/forensics.py con VAD, detección de ‘sin voz’, keyframes por escena y frames del gancho. Pruébalo con downloads/Attio/1698707541211279/video_0.mp4 y un video de Bland AI.”

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

class EvaluationResult(BaseModel):
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
- [ ] Temperatura 0, salida JSON con schema (Gemini `response_schema`, Claude tool-use / JSON, OpenAI `response_format`).

### 4.4 Cliente LLM — `src/ai/llm_client.py`
- [ ] Interfaz única: `evaluate(media: ForensicsBundle, copy: AdCopy) -> EvaluationResult`.
- [ ] Implementaciones `GeminiClient` (video nativo) y `AnthropicClient` (imágenes); seleccionadas por `LLM_PROVIDER`.
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

> **Prompt para Antigravity:** “Ejecuta la FASE 4. Crea rubric.yaml, schemas.py, prompts/evaluator_v1.md y llm_client.py con soporte Gemini y Claude, y reemplaza evaluator.py. El LLM nunca debe recibir days_active ni sumar el puntaje. Luego crea scripts/calibrate.py y prepara calibration/manual_scores.csv con 20 ad_ids variados para que yo los puntúe.”

---

## FASE 5 — Señal de mercado y puntaje final (1 día)

**Objetivo:** separar “qué tan bueno es el creativo” de “qué tanto le está funcionando al competidor”.

- [ ] `src/media/fingerprint.py`: `creative_hash` = hash perceptual de 3 frames (inicio/medio/fin) + hash del copy normalizado. Agrupa variantes del mismo creativo.
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
  - `Forensics(ad_id, duration, has_voiceover, language, transcript, timeline_json, keyframes_json, cuts_per_min, aspect_ratio)`
  - `Analysis(...)` (Fase 4), `MarketSignal(ad_id, market_score, breakdown_json)`
  - `Job(id, type, payload, status, error, created_at, finished_at)`
- [ ] `src/db/migrate.py`: migra la tabla `ads` actual → nuevo esquema (las evaluaciones viejas NO se migran; se re-evalúan).
- [ ] Mantener SQLite local; PostgreSQL opcional vía `DATABASE_URL`.

**Criterio de aceptación:** migración corre sin pérdida de anuncios relevantes ni de rutas de media.

---

## FASE 7 — Pipeline unificado y cola de trabajos (2 días)

- [ ] `src/pipeline.py` con etapas `discover → filter → download → forensics → evaluate → signals`. Cada etapa es **idempotente** (se salta lo ya procesado) y registra estado en `Ad.status`.
- [ ] `src/cli.py`:
  ```bash
  python -m src.cli run --pillar conversacional --countries PE,MX,CO --limit 20
  python -m src.cli run --competitor "Kommo" --stages download,forensics,evaluate
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
  - `test_forensics.py` (video sin voz → `has_voiceover=false`).
  - `test_pipeline_idempotent.py`.
- [ ] Dockerfile: ffmpeg, usuario no-root, `HOST=0.0.0.0`, healthcheck; servicio `worker` en `docker-compose.yml`.
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
- **Modo elegido: nivel gratuito de Gemini (costo $0)**, con corrida cada 15 días. El límite real es la **cuota diaria**, no el dinero (ver Fase 0.1).
- Primera corrida: puede tomar varios días en evaluarse completa (se retoma sola con las tareas de recuperación).
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
- [ ] 100 % de anuncios con video tienen forense sin alucinaciones y evaluación LLM con evidencia.
- [ ] Calibración con Spearman ≥ 0.6 frente a puntajes humanos.
- [ ] Dashboard muestra creative / market / jinora score y evidencias clicables.
- [ ] Un solo comando corre todo el pipeline de forma reanudable.
- [ ] Funciona dentro del nivel gratuito de Gemini y se ejecuta solo los días 1 y 15 (más recuperación de pendientes).
