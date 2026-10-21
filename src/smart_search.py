"""
Smart Query Expander & Strategy Engine
Expands user intents into targeted search vectors (Niche x Pain Point x AI Mechanism).
"""

from typing import Dict, List


class SmartQueryEngine:
    """Generates intelligent search angles across niches, pains, and mechanisms."""

    NICHES = [
        "inmobiliarias", "bienes raices", "clinicas", "dentistas", "concesionarios autos",
        "agencias marketing", "ecommerce", "coaches", "abogados", "seguros", "restaurantes"
    ]

    PAIN_POINTS = [
        "perder leads", "no contestar de noche", "seguimiento lento",
        "agendar citas", "calificar prospectos", "costo por lead", "muchos mensajes sin responder"
    ]

    MECHANISMS = [
        "agente de voz", "recepcionista virtual", "bot de whatsapp",
        "closer ia", "setter ia", "crm inteligente", "automatizacion ventas"
    ]

    @classmethod
    def expand_intent(cls, intent: str, country: str = "US") -> List[str]:
        """Generate targeted semantic query strings based on intent."""
        intent_lower = intent.lower()
        queries = [intent]

        # Add semantic permutations based on keywords present
        if "voz" in intent_lower or "llamada" in intent_lower or "telefono" in intent_lower:
            queries.extend([
                f"{intent} recepcionista",
                f"{intent} agendar citas",
                f"ia llamadas telefonicas {intent.replace('voz', '').strip()}",
            ])
        elif "whatsapp" in intent_lower or "chat" in intent_lower or "conversacional" in intent_lower:
            queries.extend([
                f"{intent} ventas",
                f"{intent} calificar leads",
                f"cerrador de ventas {intent}",
            ])
        elif "crm" in intent_lower or "seguimiento" in intent_lower:
            queries.extend([
                f"{intent} automatizado",
                f"{intent} con inteligencia artificial",
                f"pipeline {intent}",
            ])
        else:
            # Multi-angle expansion
            queries.extend([
                f"{intent} agente ia",
                f"{intent} automatizacion",
                f"{intent} software",
            ])

        # Deduplicate and clean
        cleaned = []
        for q in queries:
            q_clean = " ".join(q.split())
            if q_clean and q_clean not in cleaned:
                cleaned.append(q_clean)

        return cleaned[:5]
