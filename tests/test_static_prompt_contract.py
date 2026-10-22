import re
from pathlib import Path

from src.ai.claude_client import mask_brand_url
from src.ai.schemas import StaticEvaluationResult

ROOT = Path(__file__).resolve().parent.parent


def test_static_prompt_example_validates_against_the_real_schema():
    """The JSON example in the static prompt is the contract Claude follows: it must parse with StaticEvaluationResult."""
    prompt = (ROOT / "prompts" / "evaluator_static_v1.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)```", prompt, re.S).group(1)
    StaticEvaluationResult.model_validate_json(example)
    for field in StaticEvaluationResult.model_fields:
        assert f"`{field}`" in prompt or f'"{field}"' in prompt, field


def test_brand_domain_and_tracking_are_masked_in_the_destination_url():
    masked = mask_brand_url("https://eliseai.com/blog/playbook?utm_source=facebook&utm_campaign=eliseai")
    assert "eliseai" not in masked and "utm_" not in masked and masked.endswith("/blog/playbook")
    assert mask_brand_url("") == ""
