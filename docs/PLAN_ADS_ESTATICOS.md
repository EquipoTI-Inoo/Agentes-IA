# Jinora — Plan de Implementación: Análisis de Anuncios Estáticos

> **Para el agente de Antigravity:** este plan **complementa** `docs/PLAN_IMPLEMENTACION.md`. Reutiliza su arquitectura (config, `llm_client.py`, `schemas.py`, tabla `analyses`, `market_score`, pipeline por etapas). Ejecuta una fase a la vez, marca las casillas y detente para revisión. Requisito previo: Fases 0, 1 y 4.4 (cliente LLM) del plan principal terminadas.

---

## 0. Contexto

Hoy Jinora está pensado para video: los anuncios de imagen solo se descargan y reciben el mismo puntaje fijo. En la base actual hay **31 de 92 anuncios sin video** (imágenes y carruseles), y varios competidores LatAm de CRM/WhatsApp usan mucho formato estático.

### Problemas específicos detectados
| # | Problema | Evidencia |
|---|----------|-----------|
| E1 | Los anuncios de video también guardan `image_0.jpg` (es la miniatura). No se distingue “imagen real” de “thumbnail”. | Casi todas las carpetas con `video_0.mp4` tienen `image_0.jpg`. |
| E2 | Carruseles y creativos dinámicos (DCO) se guardan como archivos sueltos, sin orden ni tipo. | Aleco Air: 10 imágenes; Diffui: 2 videos + 2 imágenes en un mismo anuncio. |
| E3 | Imágenes idénticas repetidas se cuentan como anuncios distintos. | Deepgram: 5 anuncios con la misma imagen (mismo tamaño 3 178 082 bytes). |
| E4 | No se evalúa nada visual ni el texto dentro de la imagen; en estáticos el copy (texto principal + titular) pesa tanto como la imagen y hoy solo se guarda el primer creativo. | `save_or_update_ad_record` toma solo `creatives[0]`. |

### Resultado esperado
- Cada anuncio se clasifica en un **formato**: `video`, `imagen`, `carrusel`, `dco` (creativo dinámico con varias variantes).
- Los estáticos se evalúan con **una rúbrica propia** (no la de video), con evidencia por zona de la imagen o por tarjeta del carrusel.
- Mismo esquema de salida: `creative_score` (IA), `market_score` (código), `jinora_score`.
- Rankings **por formato** (no se compara un estático contra un video de 60 s en la misma tabla).

---

## Flujo para estáticos

```
anuncio ─► detectar formato ─► descargar imágenes en resolución original (orden de tarjetas)
                                   │
                                   ▼
                     preprocesado por código (Pillow / imagehash)
                     dimensiones, ratio, colores, hash, densidad de texto
                                   │
                                   ▼
                     LLM multimodal (1 llamada por anuncio)
                     imagen(es) + texto principal + titular + descripción + CTA
                                   │
                                   ▼
                     JSON validado (StaticEvaluationResult) → creative_score
                                   │
                                   ▼
                     market_score (mismo de la Fase 5) → jinora_score
```

**Por qué funciona bien con API:** a diferencia del video, una imagen se manda directo al LLM (Gemini, Claude u OpenAI la leen nativamente, incluido el texto que tiene escrito). No hace falta Whisper ni FFmpeg, y el costo por anuncio es menor que en video.

---

## FASE S1 — Detección de formato y descarga correcta (1 día)

- [ ] En `src/collect/scraper.py` → `_ad_to_dict`, agregar por anuncio:
  - `format`: 
    - `video` si todas las tarjetas tienen video,
    - `imagen` si hay 1 creativo sin video,
    - `carrusel` si hay ≥2 tarjetas con `link_url`/titular propios,
    - `dco` si hay varias variantes que mezclan video e imagen o repiten el mismo destino.
  - Verificar en `meta-ads-collector` si expone el tipo de display (`display_format` / `cards`); si existe, usarlo y dejar la heurística como respaldo.
- [ ] Por cada creativo guardar `role`: `image` (imagen real) o `thumbnail` (miniatura de un video). Las miniaturas no se evalúan como estático.
- [ ] Descargar la imagen en **máxima resolución disponible** (`original_image_url` si existe, si no `image_url`); nunca usar `thumbnail_url` para estáticos.
- [ ] Nombrar archivos en orden: `card_01.jpg`, `card_02.jpg`… y guardar por tarjeta su `title`, `body`, `link_url`, `cta_text`.
- [ ] Guardar **todos** los creativos en la base (tabla `Media` del plan principal, con `card_index` y `role`), no solo `creatives[0]`.

**Criterio de aceptación:** Aleco Air queda como `carrusel` con 10 tarjetas ordenadas; un anuncio de Bland AI queda como `video` con su `image_0.jpg` marcada como `thumbnail`; Diffui queda como `dco`.

> **Prompt para Antigravity:** “Ejecuta la FASE S1 de docs/PLAN_ADS_ESTATICOS.md. Primero revisa qué campos de formato/tarjetas expone meta-ads-collector y dímelo. Luego implementa la detección de formato, el rol image/thumbnail y la descarga en resolución original con nombres card_XX.”

---

## FASE S2 — Preprocesado por código (1 día)

Nuevo módulo `src/media/image_features.py` (Pillow + imagehash, sin costo de LLM):

- [ ] **Técnicas:** ancho, alto, ratio (`1:1`, `4:5`, `9:16`, `1.91:1`), peso, si la resolución es baja (< 600 px de lado menor).
- [ ] **Paleta:** 5 colores dominantes (k-means sobre la imagen reducida) y contraste de luminancia entre los dos principales.
- [ ] **Hash perceptual** (`phash`) por imagen → se usa en `fingerprint.py` para agrupar variantes (resuelve E3).
- [ ] **Imagen para el LLM:** copia redimensionada a máx. 1536 px del lado mayor, JPEG calidad 85, en `llm/` (reduce costo sin perder legibilidad del texto).
- [ ] **Carruseles:** generar además una tira horizontal (`strip.jpg`) con todas las tarjetas en miniatura, para que el LLM vea la secuencia de un vistazo.

**Criterio de aceptación:** los 5 anuncios de Deepgram comparten el mismo `phash` y se agrupan como 1 creativo con 5 variantes.

> **Prompt para Antigravity:** “Ejecuta la FASE S2: crea image_features.py con dimensiones, ratio, paleta, contraste, phash, copia optimizada para LLM y tira de carrusel. Integra el phash en fingerprint.py.”

---

## FASE S3 — Rúbrica para estáticos (½ día)

Archivo `config/rubric_static.yaml`. Cuatro dimensiones de 0–25 con anclas:

| Dimensión | 0–6 | 7–12 | 13–19 | 20–25 |
|-----------|-----|------|-------|-------|
| **1. Poder de detención (stop power)** — ¿frena el scroll en < 1 s? | Imagen genérica de stock, sin punto focal, se confunde con el feed. | Visual correcto pero predecible (logo grande, mockup plano). | Punto focal claro (rostro, contraste, elemento inesperado) y coherente con el mensaje. | Interrupción de patrón fuerte (contraste extremo, escena inesperada, dato enorme, captura “real” de WhatsApp) que se entiende al instante. |
| **2. Claridad del mensaje y jerarquía** — ¿se entiende qué ofrecen en 3 s? | Texto ilegible, saturado o ausente; no se sabe qué venden. | Se entiende con esfuerzo; demasiado texto o jerarquía confusa. | Un mensaje principal claro + apoyo legible en móvil. | Una sola idea dominante, jerarquía impecable (titular → beneficio → CTA), legible en pantalla de celular. |
| **3. Prueba y mecanismo** — ¿por qué creerles? | Solo afirmaciones (“la mejor IA”). | Menciona beneficio sin respaldo. | Muestra el producto (captura real, chat, dashboard) o un dato concreto. | Prueba fuerte: resultado con cifra, testimonio con nombre/empresa, antes/después, logos de clientes, captura de conversación real. |
| **4. Oferta y CTA (imagen + copy)** — ¿qué tan fácil es decir sí? | Sin CTA o contradicción entre imagen y copy. | CTA genérico (“Más información”) y oferta vaga. | CTA claro con beneficio; copy y titular coherentes con la imagen. | CTA de baja fricción + reversión de riesgo (prueba gratis, demo por WhatsApp, garantía) + urgencia o valor evidente; copy principal con gancho en la primera línea. |

**Reglas extra para carruseles:** la dimensión 3 y 4 consideran **toda la secuencia**; se agrega en el JSON un análisis de narrativa entre tarjetas (¿la tarjeta 1 invita a deslizar? ¿hay progresión problema → solución → CTA?).

---

## FASE S4 — Esquema y prompt de evaluación (1 día)

### Esquema — agregar en `src/ai/schemas.py`
```python
class StaticEvidence(BaseModel):
    card: int | None                 # nº de tarjeta (None si es el copy)
    zone: Literal["superior", "centro", "inferior", "completa", "copy", "titular", "cta"]
    quote: str                       # texto leído en la imagen o descripción visual concreta

class StaticDimensionScore(BaseModel):
    score: int = Field(ge=0, le=25)
    anchor_level: Literal["0-6", "7-12", "13-19", "20-25"]
    reasoning: str
    evidence: list[StaticEvidence] = Field(min_length=1)

class StaticEvaluationResult(BaseModel):
    stop_power: StaticDimensionScore
    message_clarity: StaticDimensionScore
    proof: StaticDimensionScore
    offer_cta: StaticDimensionScore
    text_in_image: str                     # transcripción literal del texto de la imagen (OCR del LLM)
    text_density: Literal["sin_texto", "baja", "media", "alta"]
    visual_style: Literal["captura_producto", "chat_whatsapp", "persona_rostro", "meme", "infografia",
                          "testimonial", "mockup_dispositivo", "ilustracion", "foto_stock", "otro"]
    has_human_face: bool
    has_product_screenshot: bool
    main_promise: str                      # promesa principal en una frase
    ai_mechanism_framing: str | None
    target_niche: str | None
    language: str
    carousel_narrative: str | None         # solo carruseles
    weakest_card: int | None               # solo carruseles
    what_to_model: str
    what_to_beat: str
    cosmiq_adaptation_idea: str            # idea concreta de estático para Cosmiq
    confidence: float = Field(ge=0, le=1)
```
`creative_score` = suma de las 4 dimensiones, **calculada por código**.

### Prompt — `prompts/evaluator_static_v1.md`
- [ ] Rol: director de arte y media buyer de respuesta directa, SaaS B2B LatAm.
- [ ] Entradas: imagen(es) en orden, `strip.jpg` si es carrusel, texto principal completo, titular, descripción, CTA, dominio de destino, y las features de S2 (ratio, contraste, baja resolución).
- [ ] Reglas:
  - Primero transcribir literalmente el texto de la imagen; después evaluar.
  - Evaluar **como se ve en el feed del celular** (imagen pequeña + primeras 125 caracteres del copy antes de “Ver más”).
  - Cada puntaje con ≥1 evidencia (tarjeta + zona o fragmento del copy).
  - No usar días activos ni datos del anunciante para puntuar (no se envían).
  - Usar todo el rango; un anuncio promedio cae en 10–15 por dimensión.
- [ ] Temperatura 0 y salida con JSON schema (igual que el evaluador de video).

### Evaluador — `src/ai/evaluator_static.py`
- [ ] `evaluate_static(ad) -> StaticEvaluationResult` usando `llm_client.py` (una sola llamada con todas las imágenes).
- [ ] Carruseles de > 10 tarjetas: enviar las 10 primeras + la tira.
- [ ] Guardar en `analyses` con `analysis_type="static"`, `prompt_version`, `model`, costo.
- [ ] Caché por `creative_hash` (phash + copy).
- [ ] Router en `pipeline.py`: `video → evaluator.py`, `imagen/carrusel → evaluator_static.py`, `dco → evaluar cada variante con su evaluador y guardar el mejor + promedio`.

**Criterio de aceptación:** evaluar 5 estáticos distintos produce puntajes diferentes, con el texto de la imagen transcrito y evidencias por zona/tarjeta.

> **Prompt para Antigravity:** “Ejecuta las FASES S3 y S4: crea rubric_static.yaml, el esquema StaticEvaluationResult, prompts/evaluator_static_v1.md y evaluator_static.py, y agrega el router por formato en el pipeline. Pruébalo con 5 anuncios estáticos de la base y muéstrame los JSON.”

---

## FASE S5 — Señal de mercado y ranking por formato (½ día)

- [ ] Reutilizar `market_score` del plan principal. Para estáticos, las variantes se cuentan por `phash` (misma imagen con distinto copy = variante de copy; distinta imagen con mismo copy = variante visual) → ambas suman al indicador de “escalado”.
- [ ] Guardar en `MarketSignal` dos campos extra: `copy_variants` y `visual_variants` (útil para saber **qué está testeando** el competidor).
- [ ] Rankings separados: “Top 10 video”, “Top 10 estáticos”, “Top 10 carruseles”, más un ranking general por `jinora_score` normalizado por percentil dentro de cada formato.

---

## FASE S6 — Dashboard para estáticos (1 día)

- [ ] Pestaña/filtro por formato.
- [ ] Tarjeta estática: imagen en grande, copy como se ve en el feed (cortado a ~125 caracteres con “Ver más”), CTA y titular.
- [ ] Desglose de las 4 dimensiones + evidencias; al pasar el mouse sobre una evidencia, resaltar la zona (superior/centro/inferior) en la imagen.
- [ ] Carrusel: visor deslizable con indicador de la tarjeta más débil.
- [ ] Vista “Qué están testeando”: anuncios del mismo competidor agrupados por imagen con sus distintos copys, y viceversa.
- [ ] Tabla resumen por competidor: % de estáticos vs video, estilos visuales más usados (`visual_style`), promesas principales recurrentes.

---

## FASE S7 — Calibración y pruebas (½ día + tu revisión)

- [ ] Elegir 10 estáticos variados (incluir: uno con mucho texto, una captura de WhatsApp, un carrusel, una foto de stock, un testimonial).
- [ ] Puntuarlos a mano en `calibration/manual_scores_static.csv`.
- [ ] Extender `scripts/calibrate.py` con `--type static`: Spearman ≥ 0.6 y desviación ≤ 3 puntos en 3 corridas repetidas.
- [ ] Tests:
  - `test_format_detection.py` (video con miniatura, imagen, carrusel, DCO).
  - `test_image_features.py` (ratio, phash idéntico para imágenes duplicadas).
  - `test_static_schema.py` (JSON inválido rechazado).

---

## Encaje con el plan principal

| Plan principal | Qué se agrega para estáticos |
|----------------|------------------------------|
| Fase 1 (bugs) | S1: formato + rol image/thumbnail + todos los creativos en DB |
| Fase 3 (forense) | S2: `image_features.py` (equivalente “forense” para imágenes) |
| Fase 4 (evaluador) | S3–S4: rúbrica, esquema, prompt y evaluador estático + router |
| Fase 5 (mercado) | S5: variantes de copy/visual, rankings por formato |
| Fase 8 (dashboard) | S6 |
| Fase 9 (tests) | S7 |

**Orden recomendado:** S1 junto con la Fase 1 → S2 → S3/S4 inmediatamente después de la Fase 4 (comparten cliente LLM y tabla `analyses`) → S5–S7. Tiempo extra total: ~5 días.

## Costos (orientativo, verificar precios vigentes)
- Un estático = 1 llamada con 1 imagen optimizada + texto → normalmente **más barato que un video**.
- Un carrusel de 10 tarjetas cuesta varias veces un estático simple; por eso se limita a 10 tarjetas + tira.
- La caché por `phash` evita pagar dos veces la misma imagen repetida en muchos anuncios.

## Definición de “terminado”
- [ ] 100 % de anuncios con `format` correcto y miniaturas de video excluidas del análisis estático.
- [ ] 100 % de estáticos/carruseles con `StaticEvaluationResult` válido, texto de imagen transcrito y evidencias.
- [ ] Duplicados agrupados por `phash`.
- [ ] Rankings por formato en el dashboard.
- [ ] Calibración estática con Spearman ≥ 0.6.
