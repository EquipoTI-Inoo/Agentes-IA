"""
Jinora Intelligence - Cognitive Relevance Filter
Ensures scraped and analyzed ads strictly belong to our core commercial pillars:
1. Smart CRM & Sales Automation
2. Conversational AI Agents (WhatsApp, Instagram, Omnichannel)
3. Voice AI & Phone Automation (Inbound/Outbound/Receptionist)
4. B2B / SaaS / Agency Tech Stack

Discards irrelevant ads (consumer goods, cosmetics, shampoos, fashion, personal loans, pension advisory, etc.)
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

    # Generic Consumer E-commerce Gadgets & Household
    "funda de celular", "juguetes", "limpieza del hogar", "utensilios de cocina",
    "air wick", "ambientador", "ambientadores", "airmoto", "inflador de llantas",
    "tire inflator", "novela", "novelas", "rom-com", "romance book", "libro fisico",
]

# Core Positive Pillars & Terms (Must match at least one pillar or tech domain)
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


def is_ad_relevant(ad_data: Dict[str, Any], target_query: str = "") -> Tuple[bool, str]:
    """
    Evaluates whether an ad belongs to Jinora's target pillars.
    Returns (is_relevant: bool, reason: str).
    """
    page_name = (ad_data.get("page_name") or "").lower().strip()
    headline = (ad_data.get("headline") or "").lower()
    body = (ad_data.get("body_text") or "").lower()
    link = (ad_data.get("link_url") or "").lower()
    transcript = (ad_data.get("full_transcript") or "").lower()
    query_lower = (target_query or "").lower().strip()

    combined_text = f"{page_name} {headline} {body} {transcript}"

    # 1. Negative Filter: Immediate rejection if blacklisted terms appear
    for bad_term in NEGATIVE_KEYWORDS:
        # Check boundary or direct occurrence
        pattern = r"\b" + re.escape(bad_term) + r"\b"
        if re.search(pattern, combined_text, re.IGNORECASE) or bad_term in page_name:
            return False, f"Rechazado por Jinora: Niche prohibido/irrelevante ('{bad_term}')"

    # 2. Competitor Brand Specific Guard
    # If the user searched for a brand (e.g., 'Kommo', 'Manychat', 'Vapi', 'Bland AI', 'Notion', 'HubSpot')
    if query_lower and len(query_lower) >= 3:
        # If searching for Kommo, prevent Meta's false positive with Spanish '¿cómo?'
        if query_lower in ["kommo", "kommo crm"]:
            is_kommo_official = "kommo" in page_name or "kommo.com" in link
            speaks_crm = any(k in combined_text for k in ["crm", "ventas", "pipeline", "whatsapp", "leads", "mensajería"])
            if not is_kommo_official and not speaks_crm:
                return False, f"Rechazado por Jinora: Falsa coincidencia fonética con '{target_query}' (anuncio no trata de CRM/Software)"

        # If searching for Vapi
        if "vapi" in query_lower:
            if "vapi" not in combined_text and "vapi.ai" not in link and "voice" not in combined_text:
                return False, "Rechazado por Jinora: No menciona Vapi ni Voice AI"

        # If searching for Bland AI
        if "bland" in query_lower:
            if "bland" not in combined_text and "bland.ai" not in link and "phone" not in combined_text and "ai" not in combined_text:
                return False, "Rechazado por Jinora: No menciona Bland AI ni agentes de llamada"

    # 3. Positive Domain Check: Must relate to B2B software, CRM, AI, or sales automation
    has_positive = any(good_term in combined_text or good_term in link for good_term in POSITIVE_DOMAIN_KEYWORDS)

    # Check if page name itself is an approved competitor or tech page
    tech_in_page = any(k in page_name for k in ["ai", "crm", "tech", "software", "bot", "automati", "sales", "agency"])

    if has_positive or tech_in_page:
        return True, "Aprobado por Jinora: Alineado con CRM, Voice AI o Conversational AI"

    # If it is too vague or generic with zero B2B tech match, reject
    return False, "Rechazado por Jinora: Sin relevancia temática para CRM, Voice AI ni Agentes Conversacionales"
