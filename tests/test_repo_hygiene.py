"""
Repository hygiene (Phase 9): no file may contain something that looks like an API key, .env.example keeps every secret
empty, and .gitignore covers what must never be committed. The scan never opens .env (the real secrets file).
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Never scanned: VCS, caches, runtime data, downloads, logs and the sample videos of the calibration set
SKIP_DIRS = {".git", "downloads", "data", "logs", "__pycache__", ".pytest_cache", ".venv", "venv", "env", "node_modules"}
SKIP_DIR_PREFIXES = (("calibration", "videos"),)  # calibration/videos*, any depth below calibration/
SKIP_FILES = {".env"}  # the real secrets file is not part of the repository and is never read here
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".ps1", ".html", ".js", ".css", ".toml", ".cfg", ".ini", ".csv",
                 ".example", ".gitignore", ".dockerignore", ""}
MAX_BYTES = 2_000_000

KEY_PATTERNS = {
    "Google/Gemini API key (AIza...)": re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    "Anthropic API key (sk-ant-...)": re.compile(r"sk-ant-[0-9A-Za-z_\-]{10,}"),
    "OpenAI-style API key (sk-...)": re.compile(r"(?<![0-9A-Za-z])sk-[0-9A-Za-z_\-]{20,}"),
}
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|WORKSPACE_ID)", re.I)


def _skipped(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts
    if any(p in SKIP_DIRS for p in parts[:-1]) or path.name in SKIP_FILES:
        return True
    return any(len(parts) > len(prefix) and parts[: len(prefix) - 1] == prefix[:-1] and parts[len(prefix) - 1].startswith(prefix[-1])
               for prefix in SKIP_DIR_PREFIXES)


def scan_for_keys(root: Path):
    """[(relative_path, line_number, pattern_name)] for every key-shaped string. Matches are never echoed."""
    hits = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _skipped(path, root) or path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > MAX_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for name, pattern in KEY_PATTERNS.items():
                if pattern.search(line):
                    hits.append((path.relative_to(root).as_posix(), number, name))
                    break  # one hit per line is enough
    return hits


def test_no_file_in_the_repository_contains_an_api_key():
    hits = scan_for_keys(ROOT)
    assert not hits, "Cadenas con forma de clave de API (el valor no se muestra): " + "; ".join(f"{p}:{n} [{what}]" for p, n, what in hits)


def test_env_example_has_every_secret_empty():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assignments = [l.split("=", 1) for l in lines if "=" in l and not l.lstrip().startswith("#")]
    assert assignments, ".env.example no define variables"
    secrets = [(name, value) for name, value in assignments if SECRET_NAME.search(name)]
    assert {n for n, _ in secrets} >= {"GEMINI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"}
    assert all(value.strip() == "" for _, value in secrets), "Hay una variable secreta con valor en .env.example"
    for name, pattern in KEY_PATTERNS.items():
        assert not any(pattern.search(l) for l in lines), name


def test_scanner_catches_planted_keys_and_honours_the_exclusions(tmp_path):
    """Proves the scan itself works (the fake keys are assembled at runtime, never written in this file)."""
    gemini, anthropic, openai = "AI" + "za" + "A" * 30, "sk-" + "ant-" + "B" * 20, "sk-" + "C" * 30
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(f'KEY = "{gemini}"\n', encoding="utf-8")
    (tmp_path / "notes.md").write_text(f"x\n{anthropic}\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(f"token: {openai}\n", encoding="utf-8")
    assert sorted((p, n) for p, n, _ in scan_for_keys(tmp_path)) == [("config.yaml", 1), ("notes.md", 2), ("src/a.py", 1)]

    for skipped in ("downloads/x.json", "data/backup/y.txt", "logs/z.log", ".git/config", "calibration/videos_scrape/lista.csv", "calibration/videos20/l.csv"):
        f = tmp_path / skipped
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(gemini, encoding="utf-8")
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={gemini}\n", encoding="utf-8")  # real secrets file: never scanned
    (tmp_path / "words.md").write_text("risk-reversal, task-based, desk-lamp-for-the-office-team\n", encoding="utf-8")  # look-alikes are not keys
    assert len(scan_for_keys(tmp_path)) == 3


GITIGNORE_MUST_COVER = [
    (".env", ".env"), ("data/*.db", "data/meta_ads.db"), ("data/backup/", "data/backup/meta_ads_before_phase9.db"),
    ("downloads/", "downloads/Brand/1/video_0.mp4"), ("logs/", "logs/jinora.log"), ("calibration/videos*", "calibration/videos_scrape/01.mp4"),
    ("__pycache__/", "src/__pycache__/x.pyc"), (".pytest_cache/", ".pytest_cache/v/cache/lastfailed"),
]


@pytest.mark.parametrize("rule, example", GITIGNORE_MUST_COVER)
def test_gitignore_covers_secrets_data_and_caches(rule, example):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    result = subprocess.run(["git", "check-ignore", "-q", example], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, f"{example} no está ignorado por .gitignore (regla esperada: {rule})"
