"""
Tests for Jinora Cognitive Relevance Filter (Word-Boundary Regex Matching)
"""

import pytest
from src.relevance import is_ad_relevant


@pytest.mark.parametrize(
    "page_name, headline, body, query",
    [
        ("Air Wick", "Fresh home fragrances", "Buy our new air freshener for your home", "Air AI"),
        ("Airmoto", "Smart Tire Inflator", "Inflate your car tires in 2 minutes", "Air AI"),
        ("Chair Co", "Ergonomic Office Chairs", "Best wooden chairs and tables for home", "AI"),
        ("Rom-Com Reads", "Forbidden Love Novela", "Read chapter 1 of our romantic novel now", "Bland AI"),
    ],
)
def test_must_reject_consumer_and_false_positives(page_name, headline, body, query):
    ad_data = {
        "page_name": page_name,
        "headline": headline,
        "body_text": body,
        "link_url": "",
    }
    relevant, reason = is_ad_relevant(ad_data, target_query=query)
    assert relevant is False, f"Expected '{page_name}' to be rejected, but was approved: {reason}"
    assert "Rechazado" in reason


@pytest.mark.parametrize(
    "page_name, headline, body, query",
    [
        ("Bland AI", "Build Phone Agents with AI", "Create ultra-realistic phone calling bots", "Bland AI"),
        ("Kommo", "CRM para WhatsApp y Ventas", "Gestiona tus leads y automatiza tu pipeline comercial", "Kommo"),
        ("Vapi AI", "Developers Voice AI Platform", "Low latency voice agent API for businesses", "Vapi AI"),
        ("HubSpot", "CRM Platform for Scaling Businesses", "Automate inbound leads and marketing workflows", "HubSpot"),
    ],
)
def test_must_approve_real_competitors(page_name, headline, body, query):
    ad_data = {
        "page_name": page_name,
        "headline": headline,
        "body_text": body,
        "link_url": "",
    }
    relevant, reason = is_ad_relevant(ad_data, target_query=query)
    assert relevant is True, f"Expected '{page_name}' to be approved, but was rejected: {reason}"
    assert "Aprobado" in reason


# --- "Kommo" is not the Spanish word "cómo" ---------------------------------------------------

@pytest.mark.parametrize(
    "page_name, headline, body",
    [
        ("Tienda Hogar", "Cómo organizar tu cocina", "Descubre cómo ordenar tu hogar paso a paso y como ahorrar espacio"),
        ("Como Hogar SA", "Como cuidar tus plantas", "Cómo regar, cómo podar y cómo abonar"),
        ("Recetas Express", "¿Cómo cocinar rápido?", "Cómo preparar la cena en 10 minutos"),
    ],
)
def test_como_is_never_mistaken_for_kommo(page_name, headline, body):
    ad = {"page_name": page_name, "headline": headline, "body_text": body, "link_url": ""}
    relevant, reason = is_ad_relevant(ad, target_query="Kommo")
    assert relevant is False, f"'cómo' was taken for the competitor Kommo: {reason}"


def test_real_kommo_still_passes_when_its_copy_says_como():
    ad = {"page_name": "Kommo", "headline": "Descubre cómo vender más por WhatsApp",
          "body_text": "Cómo automatizar tu pipeline comercial con Kommo CRM", "link_url": ""}
    relevant, reason = is_ad_relevant(ad, target_query="Kommo")
    assert relevant is True, reason
