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
