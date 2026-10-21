"""
Jinora Central Configuration Module
Loads settings from .env file using pydantic-settings.
Supports Gemini Free Tier guardrails and automated bi-weekly execution batches.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Literal, Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database & Local Storage
    DATABASE_URL: str = Field(
        default="sqlite:///data/meta_ads.db",
        description="SQLAlchemy database connection URL",
    )
    DOWNLOADS_DIR: str = Field(
        default="downloads",
        description="Directory for storing scraped creative assets",
    )

    # LLM Providers & Models (Gemini-only in Free Tier mode)
    LLM_PROVIDER: Literal["gemini"] = Field(
        default="gemini",
        description="Primary LLM provider for evaluation and intelligence (Gemini Free Tier)",
    )
    LLM_MODEL: str = Field(
        default="gemini-2.5-flash",
        description="High-tier multimodal model for creative auditing",
    )
    LLM_MODEL_CHEAP: str = Field(
        default="gemini-2.5-flash-lite",
        description="Cost-effective model for fast classification and relevance checks",
    )

    # API Keys
    GEMINI_API_KEY: Optional[str] = Field(
        default=None,
        description="Google Gemini API Key",
    )
    ANTHROPIC_API_KEY: Optional[str] = Field(
        default=None,
        description="Anthropic Claude API Key for static ad evaluations",
    )
    CLAUDE_BUDGET_USD: float = Field(
        default=8.0,
        description="Spending cap in USD for Claude static evaluations",
    )
    CLAUDE_MODEL_STATIC: str = Field(
        default="claude-sonnet-5-5",
        description="Claude model for static ad evaluations",
    )

    # Geographical Markets (comma-separated string or list)
    DEFAULT_COUNTRIES: Union[List[str], str] = Field(
        default=["PE", "MX", "CO", "AR", "CL", "ES", "US"],
        description="Target country ISO codes for competitor surveillance",
    )

    # Scoring Weights
    SCORE_WEIGHT_CREATIVE: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Weight assigned to creative score in jinora_score (0.0 to 1.0)",
    )
    SCORE_WEIGHT_MARKET: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Weight assigned to market score in jinora_score (0.0 to 1.0)",
    )

    # --- Fase 0.1: Gemini Free Tier & Quota Control ---
    FREE_TIER_MODE: bool = Field(
        default=True,
        description="Enable Gemini Free Tier quotas, batching and rate-limiting guardrails",
    )
    GEMINI_RPM: int = Field(
        default=5,
        description="Requests per minute rate limit for Gemini Free Tier",
    )
    GEMINI_RPD: int = Field(
        default=20,
        description="Requests per day limit for Gemini Free Tier (confirmed in Google AI Studio)",
    )
    GEMINI_TPM: int = Field(
        default=250000,
        description="Tokens per minute rate limit for Gemini Free Tier",
    )
    GEMINI_RPD_MAIN: Optional[int] = Field(
        default=None,
        description="Specific RPD limit for LLM_MODEL (if None, falls back to GEMINI_RPD)",
    )
    GEMINI_RPD_CHEAP: Optional[int] = Field(
        default=None,
        description="Specific RPD limit for LLM_MODEL_CHEAP (if None, falls back to GEMINI_RPD)",
    )
    RESERVE_REQUESTS: int = Field(
        default=2,
        description="Daily requests reserved for manual queries, testing or retries",
    )

    # Batching & Evaluation Volume
    EVAL_BATCH_VIDEOS: int = Field(
        default=1,
        description="Number of video ads evaluated per single multimodal request (1 video per request)",
    )
    EVAL_BATCH_STATICS: int = Field(
        default=1,
        description="Number of static image ads evaluated per single LLM request (1 ad per request; carousel/DCO = all cards in 1 request)",
    )
    RELEVANCE_BATCH_SIZE: int = Field(
        default=20,
        description="Number of candidate ads classified in one cheap text LLM call",
    )
    FIRST_RUN_PRIORITY_ONLY: bool = Field(
        default=False,
        description="Prioritize evaluating only priority=1 competitors on initial sweeps (false = all competitors)",
    )
    MAX_EVALS_PER_RUN: int = Field(
        default=0,
        description="Maximum evaluations to process in a single run (0 = unlimited until quota)",
    )
    VIDEO_MAX_SECONDS_FOR_LLM: int = Field(
        default=0,
        description="Max video duration in seconds sent to LLM for creative audit (0 = full video without clipping)",
    )
    GEMINI_MEDIA_RESOLUTION: Literal["low", "medium", "high"] = Field(
        default="medium",
        description="Media resolution for video/image processing in Gemini API",
    )
    COLLECT_INACTIVE: bool = Field(
        default=False,
        description="Enable collection of inactive competitor ads for historical data only (no LLM calls)",
    )

    @field_validator(
        "GEMINI_RPD_MAIN",
        "GEMINI_RPD_CHEAP",
        "GEMINI_API_KEY",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "" or (isinstance(v, str) and not v.strip()):
            return None
        return v

    @field_validator("DEFAULT_COUNTRIES", mode="after")
    @classmethod
    def parse_countries(cls, v: Union[List[str], str]) -> List[str]:
        if isinstance(v, str):
            clean_v = v.strip()
            if clean_v.startswith("[") and clean_v.endswith("]"):
                try:
                    return [c.strip().upper() for c in json.loads(clean_v)]
                except Exception:
                    pass
            return [c.strip().upper() for c in clean_v.split(",") if c.strip()]
        return v

    @property
    def downloads_path(self) -> Path:
        """Absolute Path to the downloads directory."""
        p = Path(self.DOWNLOADS_DIR)
        return p if p.is_absolute() else (BASE_DIR / p)

    def get_rpd_for_model(self, model: str) -> int:
        """Return the effective RPD limit for a specific model."""
        if model == self.LLM_MODEL and self.GEMINI_RPD_MAIN is not None:
            return self.GEMINI_RPD_MAIN
        if model == self.LLM_MODEL_CHEAP and self.GEMINI_RPD_CHEAP is not None:
            return self.GEMINI_RPD_CHEAP
        return self.GEMINI_RPD


settings = Settings()
