"""
Two-layer relevance filter for Jinora intelligence pipeline.
- Layer 1: Deterministic rules (instant approval for mapped competitor page_ids,
  instant rejection for clear negative keyword/regex matches).
- Layer 2: LLM classifier (Gemini Flash-Lite via QuotaGuard) in batches
  for candidate ads coming from keyword discovery.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

from src.ai.quota import QuotaExhausted, get_quota_guard
from src.config import settings

logger = logging.getLogger("Jinora.Relevance")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"
CONFIG_DIR = BASE_DIR / "config"

DEFAULT_COMPETITORS_DB = CONFIG_DIR / "competitors_database.json"
DEFAULT_CANDIDATES_PATH = CONFIG_DIR / "candidate_competitors.json"
DEFAULT_PROMPT_PATH = PROMPTS_DIR / "relevance_v1.md"


class RelevanceResult(BaseModel):
    """Structured evaluation for a single ad candidate."""
    ad_id: str
    is_relevant: bool
    pillar: Literal["conversacional", "voz", "crm", "ninguno"]
    is_competitor_of_cosmiq: bool
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: str
    pending_relevance: bool = False


class RelevanceBatchResult(BaseModel):
    """Pydantic container for batch LLM relevance classification."""
    results: List[RelevanceResult]


# Strict negative regex patterns (consumer goods, novels, personal apparel, unrelated local services)
NEGATIVE_PATTERNS = [
    re.compile(r"\bair\s*wick\b", re.IGNORECASE),
    re.compile(r"\bairmoto\b", re.IGNORECASE),
    re.compile(r"\bchair\s+co\b", re.IGNORECASE),
    re.compile(r"\brom-?com\b", re.IGNORECASE),
    re.compile(r"\bnovela(?:s)?\b", re.IGNORECASE),
    re.compile(r"\btelenovela(?:s)?\b", re.IGNORECASE),
    re.compile(r"\bwebnovel\b", re.IGNORECASE),
    re.compile(r"\blibro(?:s)?\s+de\s+ficci[oó]n\b", re.IGNORECASE),
    re.compile(r"\bcolch[oó]n(?:es)?\b", re.IGNORECASE),
    re.compile(r"\bzapat(?:os|illas)\b", re.IGNORECASE),
    re.compile(r"\bvestido(?:s)?\b", re.IGNORECASE),
    re.compile(r"\bropar?intima\b", re.IGNORECASE),
    re.compile(r"\bcerrajer[ií]a\b", re.IGNORECASE),
    re.compile(r"\bplomer[ií]a\b", re.IGNORECASE),
    re.compile(r"\bcasino(?:s)?\b", re.IGNORECASE),
    re.compile(r"\bapuestas\b", re.IGNORECASE),
    re.compile(r"\bcripto(?:moneda)?\b", re.IGNORECASE),
]


class TwoLayerRelevanceFilter:
    """Two-layer relevance classifier with deterministic fast-path and Gemini batching."""

    def __init__(
        self,
        competitors_path: Path = DEFAULT_COMPETITORS_DB,
        candidates_path: Path = DEFAULT_CANDIDATES_PATH,
        prompt_path: Path = DEFAULT_PROMPT_PATH,
    ) -> None:
        self.competitors_path = Path(competitors_path)
        self.candidates_path = Path(candidates_path)
        self.prompt_path = Path(prompt_path)
        self.quota_guard = get_quota_guard()

        self.mapped_pages: Dict[str, Dict[str, Any]] = {}
        self.candidate_counts: Dict[str, Dict[str, Any]] = {}
        self._load_competitors_map()
        self._load_candidates()

    def _load_competitors_map(self) -> None:
        """Map official page_ids to competitor entities."""
        if not self.competitors_path.exists():
            return
        try:
            with open(self.competitors_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            competitors_list: List[Dict[str, Any]] = []
            if isinstance(data, dict):
                if "pillars" in data:
                    for pillar_name, pdata in data["pillars"].items():
                        for comp in pdata.get("competitors", []):
                            comp_copy = dict(comp)
                            comp_copy.setdefault("pillar", pillar_name)
                            competitors_list.append(comp_copy)
                elif "competitors" in data:
                    competitors_list = data["competitors"]
            elif isinstance(data, list):
                competitors_list = data

            for comp in competitors_list:
                for pid in comp.get("page_ids", []):
                    if pid:
                        self.mapped_pages[str(pid)] = comp
        except Exception as e:
            logger.error(f"Error cargando competitors_database.json: {e}")

    def _load_candidates(self) -> None:
        """Load candidate competitors tracking file."""
        if not self.candidates_path.exists():
            self.candidate_counts = {}
            return
        try:
            with open(self.candidates_path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            for entry in entries:
                pid = str(entry.get("page_id", ""))
                if pid:
                    self.candidate_counts[pid] = entry
        except Exception as e:
            logger.warning(f"Error cargando candidate_competitors.json: {e}")
            self.candidate_counts = {}

    def _save_candidates(self) -> None:
        """Save candidate competitors safely."""
        self.candidates_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                json.dump(list(self.candidate_counts.values()), f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Error guardando candidate_competitors.json: {e}")

    def classify_layer1(self, ad: Dict[str, Any]) -> Tuple[Optional[RelevanceResult], bool]:
        """
        Layer 1 deterministic rules (free, no LLM call).
        Returns: (result, needs_layer2)
        """
        ad_id = str(ad.get("ad_id") or "")
        page_id = str(ad.get("page_id") or "")
        page_name = str(ad.get("page_name") or "")

        # Extract text snippets from creatives
        creatives = ad.get("creatives", [])
        title = ""
        body = ""
        caption = ""
        if creatives:
            c0 = creatives[0]
            title = c0.get("title") or ""
            body = c0.get("body") or ""
            caption = c0.get("caption") or ""

        full_text = f"{page_name} {title} {body} {caption}".lower()

        # Rule 1: Official page_id already mapped -> Direct Approval
        if page_id and page_id in self.mapped_pages:
            comp = self.mapped_pages[page_id]
            comp_name = comp.get("name", page_name)
            pillar = comp.get("pillar", "conversacional")
            if pillar not in ("conversacional", "voz", "crm", "ninguno"):
                pillar = "conversacional"
            return (
                RelevanceResult(
                    ad_id=ad_id,
                    is_relevant=True,
                    pillar=pillar,
                    is_competitor_of_cosmiq=True,
                    confidence=1.0,
                    reason=f"Página oficial mapeada para competidor '{comp_name}' (ID: {page_id})",
                ),
                False,
            )

        # Rule 2: Strict negative keyword/regex blacklist
        for pattern in NEGATIVE_PATTERNS:
            if pattern.search(full_text):
                match = pattern.pattern.replace(r"\b", "").replace(r"(?:s)?", "s")
                return (
                    RelevanceResult(
                        ad_id=ad_id,
                        is_relevant=False,
                        pillar="ninguno",
                        is_competitor_of_cosmiq=False,
                        confidence=0.95,
                        reason=f"Descartado por coincidencia negativa en Capa 1: '{match}'",
                    ),
                    False,
                )

        # Needs Layer 2 LLM evaluation
        return (None, True)

    def _call_gemini_batch(self, batch_payload: List[Dict[str, Any]]) -> RelevanceBatchResult:
        """Call Gemini LLM_MODEL_CHEAP with structured prompt and JSON response schema."""
        from google import genai
        from google.genai import types

        prompt_instructions = ""
        if self.prompt_path.exists():
            prompt_instructions = self.prompt_path.read_text(encoding="utf-8")
        else:
            prompt_instructions = "Clasifica si los anuncios pertenecen a competidores B2B de IA conversacional, voz o CRM."

        prompt_text = (
            f"{prompt_instructions}\n\n"
            f"=== LOTES DE ANUNCIOS CANDIDATOS ===\n"
            f"{json.dumps(batch_payload, indent=2, ensure_ascii=False)}\n\n"
            f"Responde estrictamente en formato JSON con la clave 'results'."
        )

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = client.models.generate_content(
            model=settings.LLM_MODEL_CHEAP,
            contents=[prompt_text],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RelevanceBatchResult,
            ),
        )

        # Record token usage in quota guard
        usage = getattr(response, "usage_metadata", None)
        tokens_in = getattr(usage, "prompt_token_count", 0) if usage else 0
        tokens_out = getattr(usage, "candidates_token_count", 0) if usage else 0
        self.quota_guard.record_usage(
            model=settings.LLM_MODEL_CHEAP,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            requests=1,
        )

        response_text = response.text or "{}"
        data = json.loads(response_text)
        return RelevanceBatchResult.model_validate(data)

    def classify_layer2_batch(
        self,
        ads_to_classify: List[Dict[str, Any]],
        mock_response: Optional[RelevanceBatchResult] = None,
    ) -> List[RelevanceResult]:
        """
        Classify candidate ads via Gemini LLM_MODEL_CHEAP under QuotaGuard gating.
        If quota is exhausted, results are marked pending_relevance (not discarded).
        """
        if not ads_to_classify:
            return []

        # Prepare lightweight payload (text only)
        batch_payload = []
        for ad in ads_to_classify:
            creatives = ad.get("creatives", [])
            c0 = creatives[0] if creatives else {}
            batch_payload.append({
                "ad_id": str(ad.get("ad_id") or ""),
                "page_name": str(ad.get("page_name") or ""),
                "ad_creative_body": c0.get("body") or "",
                "ad_creative_link_title": c0.get("title") or "",
                "ad_creative_link_caption": c0.get("caption") or "",
            })

        # Use mock response if provided (for tests and zero-cost simulation)
        if mock_response is not None:
            return mock_response.results

        # Gated call through QuotaGuard
        try:
            # Estimate tokens: ~150 tokens per candidate item + prompt instructions (~500 tokens)
            est_tokens = 500 + len(batch_payload) * 150
            batch_result = self.quota_guard.execute_with_retry(
                model=settings.LLM_MODEL_CHEAP,
                func=lambda: self._call_gemini_batch(batch_payload),
                est_tokens=est_tokens,
            )
            return batch_result.results
        except QuotaExhausted as qe:
            logger.warning(f"⚠️ Cuota agotada al clasificar relevancia. Marcando como 'pending_relevance': {qe}")
            pending_results = []
            for ad in ads_to_classify:
                pending_results.append(
                    RelevanceResult(
                        ad_id=str(ad.get("ad_id") or ""),
                        is_relevant=False,
                        pillar="ninguno",
                        is_competitor_of_cosmiq=False,
                        confidence=0.0,
                        reason="pending_relevance: QuotaExhausted",
                        pending_relevance=True,
                    )
                )
            return pending_results
        except Exception as e:
            logger.error(f"Error en clasificación LLM Capa 2: {e}")
            # Mark as pending rather than discard
            return [
                RelevanceResult(
                    ad_id=str(ad.get("ad_id") or ""),
                    is_relevant=False,
                    pillar="ninguno",
                    is_competitor_of_cosmiq=False,
                    confidence=0.0,
                    reason=f"pending_relevance: Error inesperado ({e})",
                    pending_relevance=True,
                )
                for ad in ads_to_classify
            ]

    def record_candidate_page(
        self,
        page_id: str,
        page_name: str,
        ad_id: str,
        pillar: str,
    ) -> None:
        """
        Record relevant ads for unknown pages.
        If a page accumulates >= 3 relevant ads, it is marked in candidate_competitors.json.
        """
        if not page_id or page_id in self.mapped_pages:
            return

        entry = self.candidate_counts.get(page_id)
        if not entry:
            entry = {
                "page_id": page_id,
                "page_name": page_name,
                "relevant_ads_count": 0,
                "sample_ad_ids": [],
                "detected_pillar": pillar,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "meets_threshold": False,
            }
            self.candidate_counts[page_id] = entry

        if ad_id not in entry["sample_ad_ids"]:
            entry["sample_ad_ids"].append(ad_id)
            entry["relevant_ads_count"] += 1

        if entry["relevant_ads_count"] >= 3:
            entry["meets_threshold"] = True
            logger.info(
                f"🌟 Página candidata descubierta (>=3 anuncios relevantes): "
                f"'{page_name}' (ID: {page_id}) | Pilar: {pillar}"
            )

        self._save_candidates()

    def filter_ads(
        self,
        ads: List[Dict[str, Any]],
        mock_layer2_results: Optional[Dict[str, RelevanceResult]] = None,
    ) -> List[Tuple[Dict[str, Any], RelevanceResult]]:
        """
        Run the two-layer filter over a list of scraped ads.
        Returns a list of tuples (ad_dict, RelevanceResult).
        """
        results: List[Tuple[Dict[str, Any], RelevanceResult]] = []
        needs_layer2_ads: List[Dict[str, Any]] = []

        # Layer 1
        for ad in ads:
            res, needs_l2 = self.classify_layer1(ad)
            if not needs_l2 and res is not None:
                results.append((ad, res))
            else:
                needs_layer2_ads.append(ad)

        # Layer 2 in batches
        batch_size = max(1, settings.RELEVANCE_BATCH_SIZE)
        for i in range(0, len(needs_layer2_ads), batch_size):
            chunk = needs_layer2_ads[i : i + batch_size]
            if mock_layer2_results:
                chunk_results = [
                    mock_layer2_results.get(
                        str(a.get("ad_id")),
                        RelevanceResult(
                            ad_id=str(a.get("ad_id")),
                            is_relevant=False,
                            pillar="ninguno",
                            is_competitor_of_cosmiq=False,
                            confidence=0.5,
                            reason="Mock default",
                        ),
                    )
                    for a in chunk
                ]
            else:
                chunk_results = self.classify_layer2_batch(chunk)

            res_by_id = {r.ad_id: r for r in chunk_results}
            for ad in chunk:
                ad_id = str(ad.get("ad_id") or "")
                res = res_by_id.get(
                    ad_id,
                    RelevanceResult(
                        ad_id=ad_id,
                        is_relevant=False,
                        pillar="ninguno",
                        is_competitor_of_cosmiq=False,
                        confidence=0.0,
                        reason="pending_relevance: no response",
                        pending_relevance=True,
                    ),
                )
                results.append((ad, res))

        # Check and record candidate competitors for relevant non-official pages
        for ad, res in results:
            if res.is_relevant and not res.pending_relevance:
                page_id = str(ad.get("page_id") or "")
                page_name = str(ad.get("page_name") or "")
                ad_id = str(ad.get("ad_id") or "")
                self.record_candidate_page(page_id, page_name, ad_id, res.pillar)

        return results
