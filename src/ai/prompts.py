"""Versioned evaluator prompts: prompts/evaluator_<version>.md (video) and prompts/evaluator_static_<version>.md."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
DEFAULT_VERSION = "v1"


def prompt_path(kind: str, version: str = DEFAULT_VERSION) -> Path:
    """Path of the prompt for `kind` ('video' | 'static') and `version`; fails early and clearly if it is missing."""
    name = "evaluator" if kind == "video" else "evaluator_static"
    path = PROMPTS_DIR / f"{name}_{version}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"No existe prompts/{path.name}. Crea el prompt de la versión '{version}' antes de evaluar con --prompt-version {version}."
        )
    return path
