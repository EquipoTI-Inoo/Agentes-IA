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

from src.ai.quota import QuotaExhausted, get_quota_guard, sort_ads_for_evaluation
from src.config import is_within_preferred_hours, settings
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
    cur.execute("SELECT id, page_name, has_video, video_path, image_path, headline, body_text, cognitive_score FROM ads")
    rows = cur.fetchall()

    cur.execute("SELECT COUNT(DISTINCT ad_id) FROM analyses")
    evaluated_count = cur.fetchone()[0] or 0
    conn.close()

    total_ads = len(rows)
    video_count = 0
    static_count = 0
    pending_count = 0

    for r in rows:
        if r["cognitive_score"] is None:
            pending_count += 1
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
    total_success_requests = main_usage.get("requests_success", 0) + cheap_usage.get("requests_success", 0)
    total_failed_requests = main_usage.get("requests_failed", 0) + cheap_usage.get("requests_failed", 0)

    usable_rpd = max(1, settings.GEMINI_RPD - settings.RESERVE_REQUESTS)
    eval_batch = 1
    requests_needed_videos = math.ceil(video_count / eval_batch)
    days_to_clear_videos = math.ceil(requests_needed_videos / usable_rpd) if video_count > 0 else 0

    print("=" * 70)
    print("📊 ESTADO DE LA COLA DE EVALUACIÓN (FASE 7.x)")
    print("=" * 70)
    print(f"Total anuncios en base de datos:             {total_ads}")
    print(f"  - ⏳ En cola pendiente (pending_eval):     {pending_count}")
    print(f"  - 🎥 Videos totales:                       {video_count}")
    print(f"  - 🖼️ Estáticos totales:                    {static_count}")
    print(f"Total anuncios evaluados con rúbrica IA:     {evaluated_count}")
    print("-" * 70)
    print("📈 CONSUMO Y CUOTA DE HOY (GEMINI - America/Los_Angeles):")
    status_str = "🛑 AGOTADA (429 RPD - reanudación a medianoche PT)" if main_usage.get("is_exhausted") else "🟢 ACTIVA"
    print(f"  - Estado de la cuota:                      {status_str}")
    print(f"  - Total solicitudes contadas hoy:          {total_today_requests} / {settings.GEMINI_RPD} RPD")
    print(f"    * Solicitudes exitosas hoy:              {total_success_requests}")
    print(f"    * Solicitudes fallidas hoy:              {total_failed_requests}")
    print(f"    * Modelo principal ({settings.LLM_MODEL}): {main_usage['requests']} req ({main_usage.get('requests_success', 0)} ok, {main_usage.get('requests_failed', 0)} fail)")
    print(f"    * Modelo barato ({settings.LLM_MODEL_CHEAP}):  {cheap_usage['requests']} req ({cheap_usage.get('requests_success', 0)} ok, {cheap_usage.get('requests_failed', 0)} fail)")
    in_hours = is_within_preferred_hours()
    print(f"  - Ventana preferida ({settings.EVAL_PREFERRED_HOURS} Lima): {'🟢 DENTRO DE LA VENTANA' if in_hours else '⏳ FUERA DE LA VENTANA'}")
    print(f"  - Reserva técnica de seguridad:            {settings.RESERVE_REQUESTS} solicitudes")
    print(f"  - Solicitudes útiles efectivas por día:    {usable_rpd} solicitudes/día")
    print("-" * 70)
    print("⏱️ ESTIMACIÓN DE TIEMPO PARA VACIAR LA COLA:")
    print(f"  - Política de evaluación:                  1 video por solicitud ({settings.GEMINI_MEDIA_RESOLUTION} resolution)")
    print(f"  - Solicitudes Gemini requeridas (videos):  {requests_needed_videos} solicitudes")
    print(f"  - Días estimados para vaciar videos:       ~{days_to_clear_videos} días (~{days_to_clear_videos * 24} horas)")
    print(f"  - Estáticos ({static_count} anuncios):                Vía Claude prepago con tope de ${settings.CLAUDE_BUDGET_USD} USD")
    print("=" * 70)


def run_evaluation_queue(respect_hours: bool = False, now: bool = False) -> None:
    """Processes pending evaluation queue with time-window and quota guardrails."""
    if respect_hours and not now:
        if not is_within_preferred_hours():
            print(
                f"[INFO] Fuera de la ventana preferida ({settings.EVAL_PREFERRED_HOURS} America/Lima). "
                f"Evaluación pospuesta para horario de baja demanda. Usa --now para procesar de inmediato."
            )
            return

    quota_guard = get_quota_guard()
    if quota_guard.is_exhausted(settings.LLM_MODEL):
        print(f"🛑 Cuota de hoy agotada para {settings.LLM_MODEL} (RPD). Cola detenida hasta medianoche PT.")
        return

    from src.database import AdRecord, SessionLocal
    from src.ai.router import route_and_evaluate_ad

    db = SessionLocal()
    try:
        pending_records = (
            db.query(AdRecord)
            .filter(AdRecord.cognitive_score.is_(None))
            .all()
        )
        if not pending_records:
            print("✨ No hay anuncios pendientes de evaluación en la cola.")
            return

        ads_dict_list = []
        for r in pending_records:
            ads_dict_list.append({
                "id": r.id,
                "ad_id": r.id,
                "page_name": r.page_name,
                "video_path": r.video_path,
                "image_path": r.image_path,
                "headline": r.headline,
                "body_text": r.body_text,
                "cta_text": r.cta_text,
                "link_url": r.link_url,
                "days_active": r.days_active,
                "is_active": r.is_active,
                "creatives": [{
                    "has_video": bool(r.has_video or r.video_path),
                    "has_image": bool(r.image_path),
                    "title": r.headline or "",
                    "body": r.body_text or "",
                }],
            })

        sorted_ads = sort_ads_for_evaluation(ads_dict_list)
        print(f"🚀 Procesando cola de evaluación: {len(sorted_ads)} anuncios pendientes...")

        for ad in sorted_ads:
            if not quota_guard.can_acquire(settings.LLM_MODEL):
                print("🛑 Cuota diaria alcanzada o reservada. Deteniendo cola.")
                break
            try:
                print(f"---> Evaluando {ad.get('page_name', 'ad')} ({ad.get('id')})...")
                res = route_and_evaluate_ad(ad)
                print(f"OK! Score={res.get('creative_score')}")
            except QuotaExhausted as q_err:
                print(f"🛑 {q_err}. Cola detenida hasta el reinicio de cuota.")
                break
            except Exception as err:
                print(f"⚠️ Error evaluando {ad.get('id')}: {err}. Permanece en cola.")
    finally:
        db.close()


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
    # Subcommand: queue --status / queue --run (Fase 7.x)
    if len(sys.argv) > 1 and sys.argv[1] == "queue":
        q_parser = argparse.ArgumentParser(description="Gestión de la cola de evaluación")
        q_parser.add_argument("queue_cmd", metavar="queue", choices=["queue"], help="Comando de cola")
        q_parser.add_argument("--status", action="store_true", help="Muestra el estado de la cola y cuota")
        q_parser.add_argument("--run", action="store_true", help="Ejecuta la evaluación de anuncios pendientes")
        q_parser.add_argument("--respect-hours", action="store_true", help="Respeta la ventana horaria preferida (01:00-06:00 Lima)")
        q_parser.add_argument("--now", action="store_true", help="Fuerza la ejecución inmediata ignorando la ventana horaria")
        q_args = q_parser.parse_args()

        if q_args.run:
            run_evaluation_queue(respect_hours=q_args.respect_hours, now=q_args.now)
        else:
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
