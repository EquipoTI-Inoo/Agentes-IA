"""
Jinora Intelligence - Cognitive Relevance Filter
Ensures scraped and analyzed ads strictly belong to our core commercial pillars:
1. Smart CRM & Sales Automation
2. Conversational AI Agents (WhatsApp, Instagram, Omnichannel)
3. Voice AI & Phone Automation (Inbound/Outbound/Receptionist)
4. B2B / SaaS / Agency Tech Stack

Discards irrelevant ads (consumer goods, cosmetics, shampoos, fashion, personal loans, romance books, household items, etc.)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Explicit Blacklist for Consumer / Physical / Non-B2B Niches
NEGATIVE_KEYWORDS: List[str] = [
    # Cosmetics & Haircare
    "shampoo", "shampoos", "acondicionador", "keratina", "cabello", "pelo",
    "caida del cabello", "crema facial", "skincare", "maquillaje", "serum",
    "pestañas", "uñas", "estetica", "spa", "barberia", "cosmetico", "cosmeticos",
    "jabon", "perfume", "arrugas", "mascarilla", "botox", "alisado",

    # Apparel, Fashion & Physical Accessories
    "ropa", "vestido", "vestidos", "zapatos", "zapatillas", "sneakers",
    "lenceria", "outfit", "moda femenina", "moda masculina", "joyeria", "anillos",
    "collares", "relojes de pulsera", "jeans", "camiseta", "bolsos",

    # Personal Finance, Pensions & Consumer Loans
    "pensionado", "pensionados", "jubilacion", "cesantias", "credito personal",
    "prestamo personal", "gota a gota", "subsidio de vivienda", "subsidio del gobierno",
    "pensiones", "fondo de pensiones", "retiro laboral", "asesoria financiera para pensionados",

    # Weight loss, Diet, Supplements & Physical Food
    "adelgazar", "bajar de peso", "quemador de grasa", "dieta keto", "colageno",
    "suplemento", "proteina whey", "receta de cocina", "restaurante", "hamburguesa",
    "comida rapida", "envio de flores",

    # Gambling, Occult & Entertainment
    "casino", "apuestas deportivas", "pronosticos", "ruleta", "tragamonedas",
    "tarot", "amarres de amor", "astrologia", "horoscopo",

    # Generic Consumer E-commerce Gadgets & Household & Fiction
    "funda de celular", "juguetes", "limpieza del hogar", "utensilios de cocina",
    "air wick", "ambientador", "ambientadores", "airmoto", "inflador de llantas",
    "tire inflator", "novela", "novelas", "rom-com", "romance book", "rom-com reads",
    "libro fisico", "chair co", "muebles", "decoracion hogar",
]

# Core Positive Pillars & Terms (B2B SaaS, CRM, Voice AI, Conversational AI)
POSITIVE_DOMAIN_KEYWORDS: List[str] = [
    # Voice AI
    "voice ai", "voicebot", "agente de voz", "llamadas con ia", "llamadas automatizadas",
    "phone agent", "recepcionista virtual", "recepcionista con ia", "inbound call",
    "outbound call", "voice assistant", "telefonia ia", "call center ia", "vapi", "bland ai",
    "retell", "elevenlabs", "synthflow", "bland",

    # Conversational AI (WhatsApp / Instagram / Omnichannel)
    "whatsapp", "chatbot", "chat bot", "manychat", "agente de whatsapp", "bot de ventas",
    "automatizacion de whatsapp", "dm instagram", "respuestas automaticas", "omnichannel",
    "omnicanal", "conversational ai", "chatgpt para empresas", "bot conversacional",
    "atencion al cliente con ia", "cerrar ventas por whatsapp",

    # Smart CRM & Sales Automation
    "crm", "pipeline", "pipeline de ventas", "leads", "prospectos", "calificacion de leads",
    "embudo de ventas", "funnel", "sales automation", "automatizar ventas", "seguimiento de clientes",
    "kommo", "hubspot", "salesforce", "close crm", "pipedrive", "zoho", "gohighlevel",
    "activecampaign", "prospeccion b2b", "sdr automatizado",

    # B2B Software / SaaS / Workflow Automation
    "saas", "software de ventas", "software para empresas", "b2b", "workflow", "zapier",
    "make.com", "integracion api", "agencia de marketing", "escalar tu negocio",
    "aumentar roas", "generacion de demanda", "ia para negocios",
]

# Exact word boundary regexes for tech / B2B signals in page names
TECH_PAGE_REGEXES: List[re.Pattern] = [
    re.compile(r"\bai\b", re.IGNORECASE),
    re.compile(r"\bcrm\b", re.IGNORECASE),
    re.compile(r"\btech\b", re.IGNORECASE),
    re.compile(r"\bsoftware\b", re.IGNORECASE),
    re.compile(r"\bbots?\b", re.IGNORECASE),
    re.compile(r"\bautomati\w*", re.IGNORECASE),
    re.compile(r"\bsales\b", re.IGNORECASE),
    re.compile(r"\bagency\b", re.IGNORECASE),
    re.compile(r"\bsaas\b", re.IGNORECASE),
    re.compile(r"\bkommo\b", re.IGNORECASE),
    re.compile(r"\bvapi\b", re.IGNORECASE),
    re.compile(r"\bhubspot\b", re.IGNORECASE),
]


def is_ad_relevant(ad_data: Dict[str, Any], target_query: str = "") -> Tuple[bool, str]:
    """
    Evaluates whether an ad belongs to Jinora's target pillars.
    Enforces whole-word regex matching to eliminate substring false positives.
    Returns (is_relevant: bool, reason: str).
    """
    page_name = (ad_data.get("page_name") or "").strip()
    page_lower = page_name.lower()
    headline = (ad_data.get("headline") or "").lower()
    body = (ad_data.get("body_text") or "").lower()
    link = (ad_data.get("link_url") or "").lower()
    transcript = (ad_data.get("full_transcript") or "").lower()
    query_lower = (target_query or "").lower().strip()

    combined_text = f"{page_lower} {headline} {body} {transcript}"

    # 1. Negative Filter: Reject if blacklisted consumer niche terms appear (whole-word boundary)
    for bad_term in NEGATIVE_KEYWORDS:
        pattern = r"\b" + re.escape(bad_term) + r"\b"
        if re.search(pattern, combined_text, re.IGNORECASE) or re.search(pattern, page_lower, re.IGNORECASE):
            return False, f"Rechazado por Jinora: Niche prohibido/irrelevante ('{bad_term}')"

    # 2. Competitor Brand Specific Guard
    if query_lower and len(query_lower) >= 3:
        # If searching for Kommo, prevent Meta's false positive with Spanish '¿cómo?'
        if query_lower in ["kommo", "kommo crm"]:
            is_kommo_official = bool(re.search(r"\bkommo\b", page_lower) or "kommo.com" in link)
            speaks_crm = any(
                re.search(r"\b" + re.escape(k) + r"\b", combined_text, re.IGNORECASE)
                for k in ["crm", "ventas", "pipeline", "whatsapp", "leads", "mensajería"]
            )
            if not is_kommo_official and not speaks_crm:
                return False, f"Rechazado por Jinora: Falsa coincidencia fonética con '{target_query}' (anuncio no trata de CRM/Software)"

        # If searching for Vapi
        if "vapi" in query_lower:
            if not re.search(r"\bvapi\b", combined_text) and "vapi.ai" not in link and "voice" not in combined_text:
                return False, "Rechazado por Jinora: No menciona Vapi ni Voice AI"

        # If searching for Bland AI
        if "bland" in query_lower:
            has_bland = bool(re.search(r"\bbland\b", combined_text))
            has_voice = bool(re.search(r"\b(phone|calls?|llamadas?)\b", combined_text))
            if not has_bland and "bland.ai" not in link and not has_voice:
                return False, "Rechazado por Jinora: No menciona Bland AI ni agentes de llamada"

    # 3. Positive Domain Check: Must relate to B2B software, CRM, AI, or sales automation
    has_positive = any(
        re.search(r"\b" + re.escape(good_term) + r"\b", combined_text, re.IGNORECASE) or good_term in link
        for good_term in POSITIVE_DOMAIN_KEYWORDS
    )

    # Check if page name itself matches tech patterns using strict word boundaries
    tech_in_page = any(regex.search(page_name) for regex in TECH_PAGE_REGEXES)

    if has_positive or tech_in_page:
        return True, "Aprobado por Jinora: Alineado con CRM, Voice AI o Conversational AI"

    # If it has zero B2B tech match, reject
    return False, "Rechazado por Jinora: Sin relevancia temática para CRM, Voice AI ni Agentes Conversacionales"
