# Jinora - Meta Ads Hunter & Downloader

Herramienta y Agente de IA para rastrear, extraer y descargar anuncios en video e imágenes desde la **Meta Ads Library** (Facebook & Instagram) sin necesidad de API Keys ni tokens oficiales.

Inspirado en la arquitectura de `meta-ads-competitor-tracker` utilizando impersonación de huella TLS de Google Chrome (`curl_cffi`).

---

## 🚀 Uso Rápido (CLI)

### 1. Buscar y descargar videos de una marca:
```bash
python main.py --query "Notion" --country "US" --limit 5 --media video
```

### 2. Buscar anuncios ganadores (+30 días activos):
```bash
python main.py --query "Shopify" --country "CO" --min-days 30 --limit 10
```

### 3. Extraer solo imágenes:
```bash
python main.py --query "Nike" --media image --limit 5
```

### 4. Solo metadatos (sin descargar videos/imágenes):
```bash
python main.py --query "Hubspot" --no-download --limit 20
```

---

## ⚙️ Pipeline unificado (CLI nuevo)

Etapas idempotentes `discover → filter → download → media → evaluate → signals`; una corrida interrumpida se reanuda ejecutando el mismo comando (no repite descargas ni evaluaciones). El estado de cada anuncio queda en `Ad.status`.

```bash
python -m src.cli run --pillar conversacional --countries PE,MX,CO --limit 20
python -m src.cli run --competitor "Kommo" --stages download,media,evaluate
python -m src.cli run --all --countries US,ES,MX --limit 15 --download-min-days 15 --max-ads 1000
python -m src.cli evaluate --pending                      # cola priorizada (videos → Gemini, estáticos → Claude)
python -m src.cli evaluate --force --prompt-version v2    # requiere prompts/evaluator_v2.md y evaluator_static_v2.md
python -m src.cli report --pillar crm --top 10
python -m src.cli cleanup --dry-run                       # lista anuncios de páginas fuera del catálogo; --apply los marca
python -m src.cli queue --status
python -m src.worker                                      # worker de la cola de trabajos (API: POST /api/search → job_id)
```

Orden de la cola: competidores de prioridad 1, mayor longevidad, más variantes; `rejected` y `out_of_catalog` nunca entran. Si Gemini responde 429 diario, el anuncio sigue en `pending_evaluation` (no cuenta como fallo) y la corrida se detiene hasta el día siguiente. Logs en `logs/jinora.log` (con rotación) y resumen de costo LLM al final de cada corrida. `main.py` sigue aceptando `queue --status`, `queue --run [--respect-hours] [--now]` y `rescore`.

---

## 📁 Estructura de Salida

Cada anuncio descargado genera su propia subcarpeta:
```text
downloads/
└── {query}/
    └── {ad_id}/
        ├── metadata.json       # Copys, CTAs, fecha de inicio, anunciante
        ├── video_0.mp4         # Video en máxima resolución (HD)
        └── image_0.jpg         # Imagen / miniatura
```

---

## 🤖 Agente de Antigravity

El agente **Ad Hunter** está configurado en `.agents/agents/ad-hunter.md`. Puedes pedirle en el chat:
> *"Ad Hunter, búscame los últimos 5 anuncios en video de Make.com y descarga sus creativos"*

---

## ⏰ Automatización Nocturna y Programador de Tareas (Windows Task Scheduler)

Para maximizar el aprovechamiento de la cuota diaria gratuita de Gemini (20 RPD) y evitar saturaciones en horas pico de Google (que provocan errores 503 / 429), la evaluación por lotes está programada para ejecutarse en la ventana de baja demanda (**01:00 a 06:00 America/Lima**).

A las 02:00 AM de Lima ocurre exactamente la medianoche en hora del Pacífico (`America/Los_Angeles`), momento en que Google restablece el contador de 20 solicitudes diarias de Gemini.

### Comandos de la Cola
- **Consultar estado de la cola y consumo de cuota:**
  ```bash
  python main.py queue --status
  ```
- **Ejecutar evaluación respetando la ventana de madrugada:**
  ```bash
  python main.py queue --run --respect-hours
  ```
- **Forzar ejecución inmediata (manual):**
  ```bash
  python main.py queue --run --now
  ```

### Configuración en Programador de Tareas de Windows (1:30 AM)

Para programar la ejecución automática diaria a las **01:30 AM (hora local de Lima)**, puedes usar el script automatizado o crearlo manualmente:

**Opción A — Mediante PowerShell:**
```powershell
# Simulación previa (dry run):
powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1

# Registrar las tareas en el sistema:
powershell -ExecutionPolicy Bypass -File .\scripts\schedule_windows.ps1 -Register
```

**Opción B — Manualmente en `taskschd.msc`:**
1. Abre el **Programador de tareas** de Windows (`taskschd.msc`).
2. Haz clic en **Crear tarea básica...**
   - **Nombre:** `Jinora-EvalQueue-Nocturna`
   - **Desencadenador:** Diariamente a las `01:30:00`.
   - **Acción:** Iniciar un programa.
   - **Programa o script:** `cmd.exe`
   - **Argumentos:** `/c python main.py queue --run --respect-hours >> logs\nightly_eval.log 2>&1`
   - **Iniciar en (directorio de trabajo):** Directorio raíz del proyecto (`C:\Users\riega\Documents\Jinora`).
3. En la pestaña **Condiciones / Configuración**, marca:
   - "Activar si el equipo entra en suspensión para ejecutar esta tarea" (Wake the computer to run this task).
   - "Si la tarea no se ejecuta a la hora programada, iniciarla lo antes posible".

---

## 🔬 Auditoría y Comparación de Transcripciones (Whisper vs Gemini)

Para ejecutar pruebas comparativas de transcripción y calcular la tasa de error por palabras (**Word Error Rate / WER**) entre Faster-Whisper local y Gemini multimodal:

1. Instala las dependencias de desarrollo (no incluidas en el entorno base):
   ```bash
   pip install -r requirements-dev.txt
   ```
2. Ejecuta el script de auditoría sobre anuncios específicos:
   ```bash
   python scripts/compare_transcripts.py --ids 1714051753080819,1605980027649675
   ```
El script transcribe el audio localmente con Whisper, extrae la transcripción de Gemini almacenada en `analysis.json` y calcula el WER normalizado.


