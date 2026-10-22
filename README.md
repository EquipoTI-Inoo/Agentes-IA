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

Para programar la ejecución automática diaria a las **01:30 AM**:

1. Abre el **Programador de tareas** de Windows (`taskschd.msc`).
2. Haz clic en **Crear tarea básica...**
   - **Nombre:** `Jinora - Evaluacion Nocturna Meta Ads`
   - **Desencadenador:** Diariamente a las `01:30:00`.
   - **Acción:** Iniciar un programa.
   - **Programa o script:** `powershell.exe` (o la ruta absoluta a tu ejecutable de Python).
   - **Argumentos:** `-NoProfile -ExecutionPolicy Bypass -Command "python main.py queue --run --respect-hours >> logs/nightly_eval.log 2>&1"`
   - **Iniciar en (directorio de trabajo):** `C:\Users\riega\Documents\Jinora` (o la ruta raíz de tu clon de Jinora).
3. En la pestaña **Condiciones / Configuración**, marca:
   - "Activar si el equipo entra en suspensión para ejecutar esta tarea" (Wake the computer to run this task).
   - "Si la tarea no se ejecuta a la hora programada, iniciarla lo antes posible".

