"""
Tests for Jinora Configuration and Env Parsing
"""

from src.config import Settings


def test_default_countries_comma_separated(tmp_path):
    # Test that comma-separated countries string parses to clean uppercase list
    env_file = tmp_path / ".env"
    env_file.write_text("DEFAULT_COUNTRIES=PE, MX, co,cl\nGEMINI_RPD=15\n")

    cfg = Settings(_env_file=str(env_file))
    assert cfg.DEFAULT_COUNTRIES == ["PE", "MX", "CO", "CL"]
    assert cfg.GEMINI_RPD == 15


def test_default_countries_json_format(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('DEFAULT_COUNTRIES=["PE", "MX"]\n')

    cfg = Settings(_env_file=str(env_file))
    assert cfg.DEFAULT_COUNTRIES == ["PE", "MX"]


def test_phase_0_1_defaults():
    cfg = Settings(_env_file=None)
    assert cfg.FREE_TIER_MODE is True
    assert cfg.GEMINI_RPM == 5
    assert cfg.GEMINI_RPD == 20
    assert cfg.RESERVE_REQUESTS == 2
    assert cfg.EVAL_BATCH_VIDEOS == 1
    assert cfg.EVAL_BATCH_STATICS == 1
    assert cfg.RELEVANCE_BATCH_SIZE == 20
    assert cfg.FIRST_RUN_PRIORITY_ONLY is False
    assert cfg.VIDEO_MAX_SECONDS_FOR_LLM == 0
    assert cfg.GEMINI_MEDIA_RESOLUTION == "medium"
    assert cfg.CLAUDE_MODEL_STATIC == "claude-sonnet-5-5"
    assert cfg.CLAUDE_BUDGET_USD == 8.0
    assert cfg.COLLECT_INACTIVE is False
    assert cfg.get_rpd_for_model("gemini-2.5-flash") == 20
