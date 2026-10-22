"""
Pillars and Global Competitor Intelligence Matrix
Loads the 105+ mapped competitors across Voice AI, Conversational (WhatsApp/IG) and Smart CRM.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

CONFIG_FILE = Path(__file__).parent.parent / "config" / "competitors_database.json"

if CONFIG_FILE.exists():
    try:
        RAW_DB = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        RAW_DB = {}
else:
    RAW_DB = {}


PILLARS: Dict[str, Dict[str, Any]] = {
    "voz": {
        "name": "Agentes de IA de Voz (Voice AI & Phone Agents)",
        "description": "Llamadas telefónicas salientes/entrantes, recepcionistas con IA, calificación de leads y soporte por voz.",
        "keywords": [
            "agente de voz ia",
            "ia para llamadas",
            "recepcionista virtual ia",
            "llamadas automatizadas ia",
            "voice ai agente",
            "asistente de voz ventas",
            "voicebot inteligencia artificial",
            "ai phone agent",
            "voice sdr",
            "voice ai agent",  # search term kept from the retired scripts/mass_global_harvester.py
        ],
        "top_competitors": [c["name"] for c in RAW_DB.get("pillars", {}).get("voz", {}).get("competitors", [])],
        "competitors_data": RAW_DB.get("pillars", {}).get("voz", {}).get("competitors", []),
    },
    "conversacional": {
        "name": "Agentes Conversacionales (WhatsApp, Instagram, Omnicanal)",
        "description": "Chatbots con LLMs para cierre de ventas, prospección 24/7 y atención al cliente en canales de mensajería.",
        "keywords": [
            "agente ia whatsapp",
            "chatbot whatsapp ventas",
            "inteligencia artificial whatsapp",
            "agente ia instagram",
            "automatizacion whatsapp ia",
            "chatbot ventas 24/7",
            "cerrador de ventas ia whatsapp",
            "whatsapp crm bot",
        ],
        "top_competitors": [c["name"] for c in RAW_DB.get("pillars", {}).get("conversacional", {}).get("competitors", [])],
        "competitors_data": RAW_DB.get("pillars", {}).get("conversacional", {}).get("competitors", []),
    },
    "crm": {
        "name": "CRM Inteligente & Ventas Automatizadas",
        "description": "Plataformas de gestión comercial integradas con IA para seguimiento de prospectos, pipelines y WhatsApp.",
        "keywords": [
            "crm con inteligencia artificial",
            "crm inteligente",
            "crm whatsapp",
            "software crm ventas ia",
            "automatizar seguimiento clientes",
            "pipeline de ventas automatizado",
            "ai crm sales",
            "crm whatsapp ventas",  # search term kept from the retired scripts/mass_global_harvester.py
        ],
        "top_competitors": [c["name"] for c in RAW_DB.get("pillars", {}).get("crm", {}).get("competitors", [])],
        "competitors_data": RAW_DB.get("pillars", {}).get("crm", {}).get("competitors", []),
    },
}
