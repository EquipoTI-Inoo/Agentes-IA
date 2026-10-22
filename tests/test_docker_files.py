"""
Deployment files (Phase 9). Docker is not required to run these: they parse the files and check the rules
(no ffmpeg, non-root, no secrets or data in the image, healthcheck, web + worker, port bound to 127.0.0.1).
"""

import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = [l.strip() for l in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def instructions(text):
    """Dockerfile instructions without comments (line continuations joined)."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [l.strip() for l in joined.splitlines() if l.strip() and not l.strip().startswith("#")]


INSTR = instructions(DOCKERFILE)


def test_dockerfile_has_no_ffmpeg_no_build_tools_and_runs_unprivileged():
    body = "\n".join(INSTR).lower()
    assert "ffmpeg" not in body and "build-essential" not in body and " git " not in f" {body} "
    assert "libmediainfo0v5" in body  # the only system package pymediainfo needs
    users = [i for i in INSTR if i.startswith("USER ")]
    assert users and users[-1].split()[1] not in ("root", "0")
    assert INSTR.index(users[-1]) > max(i for i, line in enumerate(INSTR) if line.startswith("RUN "))  # drops privileges after installing
    assert INSTR[-1].startswith("CMD ") and "start_server.py" in INSTR[-1]


def test_dockerfile_binds_all_interfaces_only_inside_the_container():
    assert any(re.search(r"\bHOST=0\.0\.0\.0\b", i) for i in INSTR if i.startswith("ENV "))
    assert 'os.getenv("HOST", "127.0.0.1")' in (ROOT / "start_server.py").read_text(encoding="utf-8")  # outside Docker: localhost
    assert "HOST" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")  # compose does not widen the bind address


def test_dockerfile_healthcheck_probes_the_health_endpoint():
    health = [i for i in INSTR if i.startswith("HEALTHCHECK")]
    assert len(health) == 1 and "/health" in health[0] and "--interval" in health[0] and "--timeout" in health[0] and "--retries" in health[0]


def test_dockerfile_installs_pinned_requirements_and_copies_no_secrets_or_data():
    assert any(i.startswith("RUN pip install -r requirements.txt") for i in INSTR)
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            assert re.search(r"==\d", line), f"requirements.txt must pin exact versions: {line}"
    copies = [i for i in INSTR if i.startswith("COPY ")]
    for forbidden in (".env", "data", "downloads", "calibration", "logs", "tests"):
        for c in copies:
            sources = [tok for tok in c.split()[1:-1] if not tok.startswith("--")]
            assert forbidden not in sources and not any(s.startswith(forbidden + "/") or s == forbidden + "*" for s in sources), c
    assert not any(c.split()[1:-1] == ["."] for c in copies)  # no blanket COPY . .


@pytest.mark.parametrize("entry", [".env", ".git", "data/", "downloads/", "logs/", "calibration/videos*", "__pycache__/", ".pytest_cache/", "*.db"])
def test_dockerignore_excludes_secrets_data_and_caches(entry):
    assert entry in DOCKERIGNORE or any(e.rstrip("/") == entry.rstrip("/") or e.replace("**/", "") == entry for e in DOCKERIGNORE), entry


def test_compose_has_web_and_worker_with_env_file_restart_and_volumes():
    services = COMPOSE["services"]
    assert set(services) == {"web", "worker"}
    for name, svc in services.items():
        assert svc["restart"] == "unless-stopped", name
        assert svc["env_file"] in (".env", [".env"]), name  # secrets come from .env at run time, never from the image
        mounts = {v.split(":")[0]: v.split(":")[1] for v in svc["volumes"]}
        assert {"./data": "/app/data", "./downloads": "/app/downloads", "./logs": "/app/logs", "./config": "/app/config"}.items() <= mounts.items(), name
        assert svc["user"].startswith("${JINORA_UID")  # writable bind mounts on Linux hosts
    assert services["worker"]["command"] == ["python", "-m", "src.cli", "worker"]
    assert services["worker"]["healthcheck"] == {"disable": True}
    assert "command" not in services["web"]  # web uses the image CMD (start_server.py)


def test_compose_publishes_the_dashboard_only_on_localhost():
    ports = COMPOSE["services"]["web"]["ports"]
    assert ports == ["127.0.0.1:8000:8000"]
    assert "ports" not in COMPOSE["services"]["worker"]


def test_compose_and_dockerfile_contain_no_secret_values():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8") + DOCKERFILE
    assert not re.search(r"(PASSWORD|SECRET|TOKEN|API_KEY)\s*[:=]\s*[^\s$#]", text), "a secret value is hard-coded"


def test_health_endpoint_is_lightweight_and_reports_the_database():
    from src.web.app import app

    response = TestClient(app).get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert response.headers["content-security-policy"]  # same security headers as every other route
