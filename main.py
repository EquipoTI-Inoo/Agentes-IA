"""
Meta Ads Hunter CLI
Entrypoint for humans and AI agents to search, filter, download and audit competitor ads.
Supports keyword queries as well as specialized business pillars:
 - voz (Agentes de IA de Voz)
 - conversacional (Agentes de IA WhatsApp/Instagram)
 - crm (CRM Inteligente y Automatización de Ventas)
"""

import argparse
from datetime import datetime
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.ai.quota import get_quota_guard
from src.config import settings
from src.media.fingerprint import detect_ad_format, route_ad_for_evaluation
from src.pillars import PILLARS
from src.scraper import MetaAdsHunter
from src.video_forensics import VideoForensicsEngine


def show_queue_status() -> None:
    """Displays current evaluation queue status, today's LLM consumption, and ETA to empty queue."""
    db_path = Path("data/meta_ads.db")
    if not db_path.exists():
        print("❌ Base de datos no encontrada en data/meta_ads.db")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, page_name, has_video, video_path, image_path, headline, body_text FROM ads")
    rows = cur.fetchall()
    conn.close()

    total_ads = len(rows)
    video_count = 0
    static_count = 0

    for r in rows:
        ad_dict = {
            "ad_id": str(r["id"]),
            "video_path": r["video_path"],
            "image_path": r["image_path"],
            "creatives": [{
                "has_video": bool(r["has_video"] or r["video_path"]),
                "has_image": bool(r["image_path"]),
                "title": r["headline"] or "",
                "body": r["body_text"] or "",
            }],
        }
        fmt = detect_ad_format(ad_dict)
        if fmt == "video":
            video_count += 1
        else:
            static_count += 1

    quota_guard = get_quota_guard()
    main_usage = quota_guard.get_daily_usage(settings.LLM_MODEL)
    cheap_usage = quota_guard.get_daily_usage(settings.LLM_MODEL_CHEAP)
    total_today_requests = main_usage["requests"] + cheap_usage["requests"]

    usable_rpd = max(1, settings.GEMINI_RPD - settings.RESERVE_REQUESTS)
    # Decision vigente: 1 video por solicitud para máxima precisión sin recorte
    eval_batch = 1
    requests_needed_videos = math.ceil(video_count / eval_batch)
    days_to_clear_videos = math.ceil(requests_needed_videos / usable_rpd) if video_count > 0 else 0

    print("=" * 70)
    print("📊 ESTADO DE LA COLA DE EVALUACIÓN (FASE 7.x)")
    print("=" * 70)
    print(f"Total anuncios en cola (pending_evaluation): {total_ads}")
    print(f"  - 🎥 Videos en cola (Gemini Free Tier):    {video_count}")
    print(f"  - 🖼️ Estáticos en cola (Claude API):       {static_count}")
    print(f"Total evaluados con nueva rúbrica IA:       0 (Fase 4 pendiente)")
    print("-" * 70)
    print("📈 CONSUMO Y CUOTA DE HOY (GEMINI):")
    print(f"  - Solicitudes usadas hoy (PT):             {total_today_requests} / {settings.GEMINI_RPD} RPD")
    print(f"    * Modelo principal ({settings.LLM_MODEL}): {main_usage['requests']} req ({main_usage['tokens_in'] + main_usage['tokens_out']} tokens)")
    print(f"    * Modelo barato ({settings.LLM_MODEL_CHEAP}):  {cheap_usage['requests']} req ({cheap_usage['tokens_in'] + cheap_usage['tokens_out']} tokens)")
    print(f"  - Reserva técnica de seguridad:            {settings.RESERVE_REQUESTS} solicitudes")
    print(f"  - Solicitudes útiles efectivas por día:    {usable_rpd} solicitudes/día")
    print("-" * 70)
    print("⏱️ ESTIMACIÓN DE TIEMPO PARA VACIAR LA COLA:")
    print(f"  - Política de evaluación:                  1 video por solicitud ({settings.GEMINI_MEDIA_RESOLUTION} resolution)")
    print(f"  - Solicitudes Gemini requeridas:           {requests_needed_videos} solicitudes")
    print(f"  - Días estimados para vaciar videos:       ~{days_to_clear_videos} días (~{days_to_clear_videos * 24} horas)")
    print(f"  - Estáticos ({static_count} anuncios):                Vía Claude prepago con tope de ${settings.CLAUDE_BUDGET_USD} USD")
    print("=" * 70)


def generate_markdown_report(
    pillar_or_query: str,
    ads: List[Dict[str, Any]],
    output_dir: str = "reports",
) -> Path:
    """Generate executive intelligence markdown report from extracted ads."""
    rep_dir = Path(output_dir)
    rep_dir.mkdir(parents=True, exist_ok=True)
    clean_name = "".join(c for c in pillar_or_query if c.isalnum() or c in ("-", "_")).lower()
    report_path = rep_dir / f"reporte_{clean_name}.md"

    lines = [
        f"# Reporte de Inteligencia Publicitaria: {pillar_or_query.upper()}",
        f"**Total Anuncios Analizados:** {len(ads)}",
        f"**Fecha de Extracción:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
        "## Resumen de Anuncios Ganadores (Ordenados por Longevidad)",
        "",
    ]

    for idx, ad in enumerate(ads, 1):
        ad_id = ad.get("ad_id")
        page = ad.get("page_name") or "Desconocido"
        days = ad.get("days_active", 0)
        platforms = ", ".join(ad.get("publisher_platforms") or ["Facebook/Instagram"])
        creatives = ad.get("creatives", [])

        lines.append(f"### #{idx}. {page} (Ad ID: `{ad_id}`)")
        lines.append(f"- **Días Activo:** `{days} días` {'🔥 Ganador Longevo' if days >= 30 else ''}")
        lines.append(f"- **Plataformas:** {platforms}")

        for c in creatives[:2]:
            body = (c.get("body") or "").strip()
            headline = c.get("title") or ""
            cta = c.get("cta_text") or "Ver más"
            link = c.get("link_url") or "N/A"
            has_vid = c.get("has_video")

            if headline:
                lines.append(f"- **Titular:** *{headline}*")
            if body:
                lines.append(f"- **Gancho / Copy:**")
                lines.append(f"> {body.replace(chr(10), ' ')}")
            lines.append(f"- **CTA:** `{cta}` -> [{link}]({link})")
            lines.append(f"- **Formato:** {'🎥 Video MP4' if has_vid else '🖼️ Imagen Estática'}")

        files = ad.get("downloaded_files", [])
        if files:
            lines.append(f"- **Archivos Descargados Localmente:**")
            for f in files:
                lines.append(f"  - `{Path(f).name}`")

        f_data = ad.get("forensics")
        if f_data:
            lines.append(f"- **🎙️ Transcripción Completa del Video (100%):**")
            lines.append(f"> \"{f_data.get('full_transcript')}\"")
            lines.append(f"- **⏱️ Ritmo de Locución:** `{f_data.get('pacing_words_per_minute')} palabras/min` | **Duración:** `{f_data.get('duration_seconds')}s`")
            lines.append(f"- **🎞️ Desglose Visual (1 frame/seg):** `{f_data.get('total_frames_extracted')} fotogramas extraídos` en carpeta `frames/`")
            lines.append(f"- **Línea de Tiempo Exacta:**")
            for seg in f_data.get("transcript_timeline", []):
                lines.append(f"  - `[{seg['start']:05.2f}s -> {seg['end']:05.2f}s]` {seg['text']}")

        lines.append("")
        lines.append("---")
        lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main():
    # Subcommand: queue --status (Fase 7.x)
    if len(sys.argv) > 1 and sys.argv[1] == "queue":
        show_queue_status()
        return

    parser = argparse.ArgumentParser(
        description="Meta Ads Hunter: Espionaje y descarga de creativos para CRMs y Agentes de IA."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--query",
        type=str,
        help="Término o marca específica (ej: 'Vapi', 'Kommo', 'Bland AI', 'chatbot whatsapp').",
    )
    group.add_argument(
        "--pillar",
        type=str,
        choices=["voz", "conversacional", "crm", "todos"],
        help="Pilar comercial a auditar: voz, conversacional, crm o todos.",
    )

    parser.add_argument(
        "--country",
        type=str,
        default="US",
        help="Código de país ISO (CO, MX, US, ES). Por defecto: US.",
    )
    parser.add_argument(
        "--media",
        type=str,
        choices=["video", "image", "all"],
        default="video",
        help="Tipo de creativo (video, image, all). Por defecto: video.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Límite de anuncios por búsqueda. Por defecto: 5.",
    )
    parser.add_argument(
        "--min-days",
        type=int,
        default=0,
        help="Filtrar solo anuncios con al menos N días activos (winning ads). Por defecto: 0.",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        default=True,
        help="Descargar videos e imágenes a disco. Por defecto activado.",
    )
    parser.add_argument(
        "--forensics",
        action="store_true",
        default=False,
        help="Ejecutar análisis forense completo (Transcripción 100% con Whisper + Extracción visual de fotogramas).",
    )
    parser.add_argument(
        "--no-download",
        dest="download",
        action="store_false",
        help="Solo recolectar metadatos sin descargar archivos multimedia.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="downloads",
        help="Directorio destino para guardar los medios. Por defecto: downloads/.",
    )

    args = parser.parse_args()

    # Determine execution targets
    tasks: List[Dict[str, str]] = []
    if args.query:
        tasks.append({"subfolder": args.query, "term": args.query})
    elif args.pillar == "todos":
        for pkey, pdata in PILLARS.items():
            for kw in pdata["keywords"][:2]:
                tasks.append({"subfolder": f"{pkey}/{kw}", "term": kw})
            for comp in pdata["top_competitors"][:2]:
                tasks.append({"subfolder": f"{pkey}/{comp}", "term": comp})
    elif args.pillar in PILLARS:
        pdata = PILLARS[args.pillar]
        print(f"\n🎯 AUDITANDO PILAR: {pdata['name']}")
        print(f"ℹ️ {pdata['description']}\n")
        # Sweep top keywords & competitors for this pillar
        for kw in pdata["keywords"][:3]:
            tasks.append({"subfolder": f"{args.pillar}/{kw}", "term": kw})
        for comp in pdata["top_competitors"][:2]:
            tasks.append({"subfolder": f"{args.pillar}/{comp}", "term": comp})

    print("=" * 65)
    print(f"🚀 INICIANDO BARRIDO FORENSE DE META ADS")
    print(f"   Objetivos ({len(tasks)}): {[t['term'] for t in tasks]}")
    print(f"   País:       {args.country}")
    print(f"   Tipo Media: {args.media}")
    print(f"   Límite/Obj: {args.limit}")
    print(f"   Descarga:   {'Activada' if args.download else 'Solo metadatos'}")
    print("=" * 65 + "\n")

    all_processed_ads: List[Dict[str, Any]] = []
    forensics_engine = VideoForensicsEngine("base") if args.forensics else None

    with MetaAdsHunter(output_dir=args.output_dir) as hunter:
        for t in tasks:
            term = t["term"]
            subfolder = t["subfolder"]
            print(f"\n🔎 Buscando para objetivo: '{term}'...")
            
            try:
                ads = hunter.search_ads(
                    query=term,
                    country=args.country,
                    media_filter=args.media,
                    limit=args.limit,
                    min_active_days=args.min_days,
                )
            except Exception as e:
                print(f"⚠️ Error buscando '{term}': {e}")
                continue

            if not ads:
                print(f"  [i] Sin anuncios activos para '{term}'.")
                continue

            print(f"  [+] Encontrados {len(ads)} anuncios relevantes:")

            for i, ad in enumerate(ads, 1):
                page = ad.get("page_name") or "Desconocido"
                days = ad.get("days_active", 0)
                ad_id = ad.get("ad_id")
                creatives = ad.get("creatives", [])
                has_vid = any(c.get("has_video") for c in creatives)

                print(f"   [{i}] Ad ID: {ad_id} | {page} | {days}d activo | Vid: {'SÍ' if has_vid else 'NO'}")

                if args.download:
                    hunter.download_ad(ad, subfolder=subfolder)
                    if forensics_engine:
                        for f in ad.get("downloaded_files", []):
                            if f.endswith(".mp4"):
                                try:
                                    print(f"   🔬 Ejecutando auditoría forense completa (Audio + Video): {Path(f).name}...")
                                    f_report = forensics_engine.analyze_video(Path(f))
                                    ad["forensics"] = f_report
                                except Exception as fe:
                                    print(f"   ⚠️ Error en forense de {f}: {fe}")

                all_processed_ads.append(ad)

    if all_processed_ads:
        report_label = args.pillar if args.pillar else args.query
        report_file = generate_markdown_report(report_label, all_processed_ads)
        print("\n" + "=" * 65)
        print(f"✅ BARRIDO COMPLETADO CON ÉXITO")
        print(f"📊 Total de anuncios recopilados: {len(all_processed_ads)}")
        print(f"📄 Reporte forense generado en: {report_file}")
        print("=" * 65 + "\n")
    else:
        print("\n⚠️ No se encontraron anuncios en los términos evaluados.")


if __name__ == "__main__":
    main()
