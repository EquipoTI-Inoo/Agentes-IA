"""
Reports (Phase 8): top ads of a pillar by STORED jinora_score, as console table, Markdown or HTML.
Nothing is recalculated and no evaluator is called. "Patrones comunes" is computed with code (no LLM). The optional
executive summary (src/summary.py) is a separate, explicit opt-in. Reports never contain keys or absolute local paths.
"""

from __future__ import annotations

import html
import re
import statistics
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src import dashboard_data
from src.config import settings

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"

FEATURE_LABELS = {
    "automatizacion": "Automatización", "agendamiento": "Agendamiento", "gestion_leads_crm": "Gestión de leads / CRM",
    "mensajes_masivos_campanas": "Mensajes masivos y campañas", "atencion_omnicanal": "Atención omnicanal",
    "agentes_ia": "Agentes conversacionales", "pagos_ventas_en_chat": "Pagos y ventas en chat",
    "integraciones_api": "Integraciones / API", "otro": "Otro",
}
_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s|)\]\"']+|/(?:Users|home|var|etc|opt)/[^\s|)\]\"']+")


def _label(tag: Optional[str]) -> str:
    return FEATURE_LABELS.get(tag or "", tag or "—")


def scrub(text: str) -> str:
    """Last line of defense: no keys and no absolute local paths in anything written to disk."""
    for secret in (settings.GEMINI_API_KEY, settings.ANTHROPIC_API_KEY):
        if secret:
            text = text.replace(secret, "<redacted>")
    return _ABS_PATH.sub("<ruta-local>", text)


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------

def _item(row: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    ev = detail["evaluation"] or {}
    return {
        "ad_id": row["ad_id"],
        "competitor": row["competitor"] or row["page_name"] or "?",
        "pillar": row["pillar"],
        "jinora_score": row["jinora_score"],
        "creative_score": row["creative_score"],
        "market_score": row["market_score"],
        "days_active": row["days_active"],
        "variants": row["variants"],
        "format": row["format"],
        "language": row["language"],
        "has_voiceover": row["has_voiceover"],
        "duration_s": detail["ad"]["duration_s"],
        "feature_focus": row["feature_focus"],
        "ai_mention": row["ai_mention"],
        "rubric_outdated": row["rubric_outdated"],
        "value_proposition": ev.get("value_proposition"),
        "dimensions": [{"label": d["label"], "score": d["score"], "max": d["max"], "anchor_level": d["anchor_level"]} for d in ev.get("dimensions", [])],
        "what_to_model": ev.get("what_to_model"),
        "what_to_beat": ev.get("what_to_beat"),
        "cosmiq_adaptation_idea": ev.get("cosmiq_adaptation_idea"),
        "model": ev.get("model"),
    }


def compute_patterns(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Common patterns of the listed ads, computed with plain code (no LLM)."""
    n = len(items)

    def top(counter: Counter) -> List[Dict[str, Any]]:
        return [{"tag": tag, "count": c, "share": round(100 * c / n) if n else 0} for tag, c in counter.most_common(6)]

    durations = [i["duration_s"] for i in items if i["duration_s"]]
    voice = [i["has_voiceover"] for i in items if i["has_voiceover"] is not None]
    return {
        "n": n,
        "feature_focus": top(Counter(t for i in items for t in i["feature_focus"])),
        "formats": top(Counter(i["format"] for i in items if i["format"])),
        "languages": top(Counter(i["language"] for i in items if i["language"])),
        "duration": {"min": round(min(durations), 1), "max": round(max(durations), 1), "median": round(statistics.median(durations), 1), "n": len(durations)} if durations else None,
        "voice": {"with_voice": sum(1 for v in voice if v), "without_voice": sum(1 for v in voice if not v), "known": len(voice),
                  "share_with_voice": round(100 * sum(1 for v in voice if v) / len(voice)) if voice else None},
        "ai_mention": top(Counter(i["ai_mention"] for i in items if i["ai_mention"])),  # descriptive distribution
    }


def build_report(db: Session, pillar: Optional[str] = None, top: int = 10) -> Dict[str, Any]:
    """
    Top creatives by stored jinora_score (one per creative group). Ads without creative_score are never ranked, only counted.
    rejected / out_of_catalog ads are left out.
    """
    filters = dashboard_data.parse_filters(db, pillar=pillar)  # whitelist: raises FilterError (a ValueError) on a bad pillar
    data = dashboard_data.top_ads(db, filters, limit=1000, unevaluated_limit=0)
    items = [_item(row, dashboard_data.ad_detail(db, row["ad_id"])) for row in data["items"][:top]]
    with_jinora = sum(1 for r in data["items"] if r["jinora_score"] is not None)
    return {
        "pillar": pillar,
        "top": items,
        "evaluated": with_jinora,  # evaluated creatives that have a jinora_score
        "evaluated_without_jinora": data["total"] - with_jinora,
        "unevaluated": data["unevaluated"]["total"],
        "patterns": compute_patterns(items),
        "generated_on": date.today().isoformat(),
        "summary": {"requested": False, "generated": False, "text": None, "model": None, "reason": None},
    }


# --------------------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------------------

def _num(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def format_report(report: Dict[str, Any]) -> str:
    """Console table (default output of `cli report`)."""
    title = f"TOP {len(report['top'])} POR jinora_score" + (f" — pilar {report['pillar']}" if report["pillar"] else "")
    lines = ["=" * 92, title, "=" * 92]
    if not report["top"]:
        lines.append("Todavía no hay anuncios evaluados con puntaje final (jinora_score) en este alcance.")
    else:
        lines.append(f"{'#':>2}  {'competidor':<22}{'jinora':>7}{'creativo':>9}{'mercado':>8}{'días':>6}{'var.':>5}  {'formato':<14}{'ad_id'}")
        for i, r in enumerate(report["top"], 1):
            lines.append(
                f"{i:>2}  {r['competitor'][:21]:<22}{_num(r['jinora_score']):>7}{_num(r['creative_score'], 0):>9}{_num(r['market_score']):>8}"
                f"{r['days_active']:>6}{r['variants']:>5}  {str(r['format'] or '-')[:13]:<14}{r['ad_id']}"
            )
    lines.append(f"\nEvaluados con jinora_score: {report['evaluated']} | sin evaluar (sin jinora_score, no se rankean): {report['unevaluated']}")
    return "\n".join(lines)


def _summary_line(summary: Dict[str, Any]) -> str:
    if summary.get("generated"):
        return f"generado con {summary.get('model') or 'el modelo configurado'} (una sola solicitud)"
    if summary.get("requested"):
        return f"no generado: {summary.get('reason') or 'motivo desconocido'}"
    return "no generado (opción --summary apagada)"


def _pattern_lines(report: Dict[str, Any]) -> List[str]:
    p, n = report["patterns"], report["patterns"]["n"]
    if not n:
        return ["No hay anuncios en el top; no hay patrones que calcular."]
    out = []
    if p["feature_focus"]:
        out.append("Ángulos recurrentes (feature_focus): " + "; ".join(f"{_label(x['tag'])} ({x['count']} de {n})" for x in p["feature_focus"]))
    if p["formats"]:
        out.append("Formatos más frecuentes: " + "; ".join(f"{x['tag']} ({x['count']})" for x in p["formats"]))
    d = p["duration"]
    out.append(f"Duración: de {d['min']:g} s a {d['max']:g} s (mediana {d['median']:g} s, {d['n']} videos)" if d else "Duración: sin datos de video")
    v = p["voice"]
    out.append(f"Voz: {v['with_voice']} de {v['known']} con voz ({v['share_with_voice']} %)" if v["known"] else "Voz: sin datos")
    if p["languages"]:
        out.append("Idiomas: " + "; ".join(f"{x['tag']} ({x['count']})" for x in p["languages"]))
    if p["ai_mention"]:
        out.append("Mención de IA (dato descriptivo, no suma ni resta): " + "; ".join(f"{x['tag']} ({x['count']})" for x in p["ai_mention"]))
    return out


def _md(text: Any) -> str:
    """Escapes third-party text for a Markdown table/paragraph (no raw HTML, no table breaks)."""
    s = "—" if text in (None, "") else str(text)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").strip()


def render_markdown(report: Dict[str, Any]) -> str:
    scope = f"pilar {report['pillar']}" if report["pillar"] else "todos los pilares"
    lines = [
        f"# Top {len(report['top'])} por jinora_score — {scope}",
        "",
        f"Generado el {report['generated_on']}. Fuente: puntajes guardados en la base de datos (no se recalculó nada). "
        f"Evaluados con jinora_score: {report['evaluated']}; sin evaluar (no se rankean): {report['unevaluated']}.",
        "",
        f"**Resumen ejecutivo con LLM:** {_summary_line(report['summary'])}.",
        "",
    ]
    if report["summary"].get("generated"):
        lines += ["## Resumen ejecutivo", "", *[_md_block(l) for l in report["summary"]["text"].splitlines()], ""]
    lines += ["## Ranking", "", "| # | Competidor | Formato | jinora | Creativo | Mercado | Días | Variantes | ID |", "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(report["top"], 1):
        lines.append(f"| {i} | {_md(r['competitor'])} | {_md(r['format'])} | {_num(r['jinora_score'])} | {_num(r['creative_score'], 0)} | {_num(r['market_score'])} "
                     f"| {r['days_active']} | {r['variants']} | {_md(r['ad_id'])} |")
    if not report["top"]:
        lines.append("| — | Todavía no hay anuncios evaluados con puntaje final | | | | | | | |")
    lines += ["", "## Patrones comunes", "", "_Calculados con código a partir de los anuncios de arriba, sin LLM._", ""]
    lines += [f"- {_md(l)}" for l in _pattern_lines(report)]
    lines += ["", "## Detalle por anuncio", ""]
    for i, r in enumerate(report["top"], 1):
        lines += [
            f"### {i}. {_md(r['competitor'])} — jinora {_num(r['jinora_score'])}" + (" (rúbrica anterior)" if r["rubric_outdated"] else ""),
            "",
            f"- ID: {_md(r['ad_id'])} · formato: {_md(r['format'])} · idioma: {_md(r['language'])} · "
            f"voz: {'con voz' if r['has_voiceover'] else ('sin voz' if r['has_voiceover'] is not None else '—')} · {r['days_active']} días · {r['variants']} variantes",
            f"- Ángulos (feature_focus): {_md(', '.join(_label(t) for t in r['feature_focus']))} · mención de IA (dato descriptivo): {_md(r['ai_mention'])}",
            "",
            f"**Propuesta de valor:** {_md(r['value_proposition'])}",
            "",
            "**Desglose:** " + " · ".join(f"{_md(d['label'])} {d['score']}/{d['max']} (nivel {_md(d['anchor_level'])})" for d in r["dimensions"]),
            "",
            f"**Qué copiar:** {_md(r['what_to_model'])}",
            "",
            f"**Qué superar:** {_md(r['what_to_beat'])}",
            "",
            f"**Idea para Cosmiq:** {_md(r['cosmiq_adaptation_idea'])}",
            "",
        ]
    return scrub("\n".join(lines))


def _md_block(line: str) -> str:
    return line.replace("<", "&lt;").replace(">", "&gt;")


_HTML_CSS = (
    ":root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1b1f27;--muted:#5d6675;--card:#fff;--line:#d9dde5;--accent:#2557d6}"
    "@media (prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e8ebf0;--muted:#9aa3b2;--card:#1c2027;--line:#323946;--accent:#7aa2ff}}"
    "body{margin:0;padding:1rem;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin-inline:auto}"
    "table{width:100%;border-collapse:collapse;font-size:.9rem}th,td{text-align:left;padding:.35rem .5rem;border-bottom:1px solid var(--line);overflow-wrap:anywhere}"
    "article{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.75rem 1rem;margin:1rem 0}.muted{color:var(--muted)}"
    "h1,h2,h3{line-height:1.25}.box{border-left:4px solid var(--accent);padding:.25rem .75rem;margin:.5rem 0}@media (max-width:700px){table{display:block;overflow-x:auto}}"
)


def render_html(report: Dict[str, Any]) -> str:
    """Standalone HTML. Every value is escaped; the CSP meta blocks scripts, images and network access."""
    e = html.escape
    scope = f"pilar {report['pillar']}" if report["pillar"] else "todos los pilares"
    parts = [
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        f"<title>Top {len(report['top'])} por jinora_score — {e(scope)}</title><style>{_HTML_CSS}</style></head><body>",
        f"<h1>Top {len(report['top'])} por jinora_score — {e(scope)}</h1>",
        f"<p class=\"muted\">Generado el {e(report['generated_on'])}. Fuente: puntajes guardados en la base (no se recalculó nada). "
        f"Evaluados con jinora_score: {report['evaluated']}; sin evaluar (no se rankean): {report['unevaluated']}.</p>",
        f"<p><strong>Resumen ejecutivo con LLM:</strong> {e(_summary_line(report['summary']))}.</p>",
    ]
    if report["summary"].get("generated"):
        parts.append("<h2>Resumen ejecutivo</h2>" + "".join(f"<p>{e(l)}</p>" for l in report["summary"]["text"].splitlines() if l.strip()))
    parts.append("<h2>Ranking</h2><table><thead><tr>" + "".join(f"<th>{h}</th>" for h in ["#", "Competidor", "Formato", "jinora", "Creativo", "Mercado", "Días", "Variantes", "ID"]) + "</tr></thead><tbody>")
    for i, r in enumerate(report["top"], 1):
        cells = [i, r["competitor"], r["format"] or "—", _num(r["jinora_score"]), _num(r["creative_score"], 0), _num(r["market_score"]), r["days_active"], r["variants"], r["ad_id"]]
        parts.append("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in cells) + "</tr>")
    parts.append("</tbody></table><h2>Patrones comunes</h2><p class=\"muted\">Calculados con código, sin LLM.</p><ul>" + "".join(f"<li>{e(l)}</li>" for l in _pattern_lines(report)) + "</ul><h2>Detalle por anuncio</h2>")
    for i, r in enumerate(report["top"], 1):
        voice = "con voz" if r["has_voiceover"] else ("sin voz" if r["has_voiceover"] is not None else "—")
        breakdown = " · ".join("{} {}/{} (nivel {})".format(d["label"], d["score"], d["max"], d["anchor_level"]) for d in r["dimensions"]) or "—"
        parts.append(
            f"<article><h3>{i}. {e(r['competitor'])} — jinora {_num(r['jinora_score'])}{' (rúbrica anterior)' if r['rubric_outdated'] else ''}</h3>"
            f"<p class=\"muted\">ID {e(r['ad_id'])} · formato {e(str(r['format'] or '—'))} · idioma {e(str(r['language'] or '—'))} · {voice} · {r['days_active']} días · {r['variants']} variantes<br>"
            f"Ángulos: {e(', '.join(_label(t) for t in r['feature_focus']) or '—')} · mención de IA (dato descriptivo): {e(str(r['ai_mention'] or '—'))}</p>"
            f"<p><strong>Propuesta de valor:</strong> {e(r['value_proposition'] or '—')}</p>"
            f"<p><strong>Desglose:</strong> {e(breakdown)}</p>"
            f"<div class=\"box\"><strong>Qué copiar:</strong> {e(r['what_to_model'] or '—')}</div>"
            f"<div class=\"box\"><strong>Qué superar:</strong> {e(r['what_to_beat'] or '—')}</div>"
            f"<div class=\"box\"><strong>Idea para Cosmiq:</strong> {e(r['cosmiq_adaptation_idea'] or '—')}</div></article>"
        )
    parts.append("</body></html>")
    return scrub("".join(parts))


def write_report(report: Dict[str, Any], fmt: str = "md", output_dir: Optional[Path] = None) -> Path:
    """Writes reports/jinora_top<N>_<pillar>_<date>.<md|html> and returns the path."""
    if fmt not in ("md", "html"):
        raise ValueError("formato de reporte inválido: use md o html")
    directory = Path(output_dir) if output_dir else REPORTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"jinora_top{len(report['top'])}_{report['pillar'] or 'todos'}_{report['generated_on'].replace('-', '')}.{fmt}"
    path.write_text(render_markdown(report) if fmt == "md" else render_html(report), encoding="utf-8")
    return path
