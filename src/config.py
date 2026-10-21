"""
Jinora Central Configuration Module
Loads settings from .env file using pydantic-settings.
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

    # LLM Providers & Models
    LLM_PROVIDER: Literal["gemini", "anthropic", "openai"] = Field(
        default="gemini",
        description="Primary LLM provider for evaluation and intelligence",
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
        description="Anthropic Claude API Key",
    )
    OPENAI_API_KEY: Optional[str] = Field(
        default=None,
        description="OpenAI API Key",
    )

    # Forensics & Transcription
    WHISPER_MODEL: str = Field(
        default="small",
        description="Whisper model size for audio transcription (e.g. tiny, base, small)",
    )
    WHISPER_DEVICE: Literal["cpu", "cuda"] = Field(
        default="cpu",
        description="Hardware acceleration target for Whisper",
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


settings = Settings()
