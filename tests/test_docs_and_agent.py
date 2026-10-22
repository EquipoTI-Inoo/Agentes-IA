"""
Phase 9: the agent definition and the README must stay truthful. Every example command is parsed by the real CLI parser
(so a renamed flag breaks this test), and the agent file must carry the mandatory rules and none of the old evaluator's.
"""

import re
import shlex
from pathlib import Path

import pytest

from src.cli import COMMANDS, build_parser

ROOT = Path(__file__).resolve().parent.parent
AGENT = (ROOT / ".agents" / "agents" / "ad-hunter.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def example_commands(markdown: str):
    """`python -m src.cli ...` and `python main.py <cli command> ...` lines from fenced code blocks, comments stripped."""
    commands = []
    for block in re.findall(r"```(?:bash|sh|powershell)?\n(.*?)```", markdown, re.S):
        for line in block.splitlines():
            line = re.sub(r"\s+#.*$", "", line.strip())
            line = re.sub(r"^(?:>>?\s*)", "", line)
            if line.startswith("python -m src.cli "):
                commands.append(shlex.split(line)[3:])
            elif line.startswith("python main.py ") and shlex.split(line)[2] in COMMANDS:
                commands.append(shlex.split(line)[2:])
    return commands


@pytest.mark.parametrize("name, text", [("ad-hunter.md", AGENT), ("README.md", README)])
def test_every_example_command_is_accepted_by_the_cli_parser(name, text):
    commands = example_commands(text)
    assert len(commands) >= 8, f"{name}: too few example commands found"
    parser = build_parser()
    for argv in commands:
        try:
            parser.parse_args(argv)
        except SystemExit:
            pytest.fail(f"{name}: invalid command -> {' '.join(argv)}")


def test_agent_covers_every_cli_command():
    commands = {argv[0] for argv in example_commands(AGENT)}
    assert commands >= {"run", "evaluate", "report", "cleanup", "queue", "rescore", "worker"}


REQUIRED_RULES = [
    "puntajes salen SIEMPRE de la base de datos",              # a) read scores from the database
    "Nunca inventes, estimes",                                   #    never invent or recalculate
    "nunca recibe días activos, variantes ni nombre de marca",   # b) evaluator isolation
    "Neutralidad frente a la IA y frente al formato",            # c) neutrality
    "no suma ni resta puntos",
    "No hay topes ni cupos numéricos",
    "No vende solo IA",                                          # d) Cosmiq scope
    "agendamiento", "gestión de leads", "mensajes masivos", "CRM", "agentes de voz",
    "capacidad que destaca el anuncio",
    "RESERVE_REQUESTS", "No cambies de clave", "429 diario",     # e) quota
    "No hagas `git add` ni `git commit`", "No leas ni imprimas `.env`", "No borres datos",   # f) safety
    "videos van a Gemini", "estáticos (imagen, carrusel, DCO) a Claude",                     # g) routing
]


@pytest.mark.parametrize("rule", REQUIRED_RULES)
def test_agent_has_the_mandatory_rules(rule):
    assert rule.lower() in AGENT.lower(), rule


@pytest.mark.parametrize("legacy", [
    "cognitive_score", "puntaje heurístico", "Rúbrica Cognitiva", "100 Puntos", "Mecanismo de IA Identificado",
    "conceptualizan la tecnología", "palabras clave aisladas", "Guión Adaptado",
])
def test_agent_dropped_the_old_evaluator_instructions(legacy):
    assert legacy.lower() not in AGENT.lower(), legacy


def test_agent_sql_example_runs_against_the_real_schema(clean_db):
    """The documented query must be valid on the actual schema (empty result is fine)."""
    from sqlalchemy import text

    from src.database import SessionLocal

    sql = re.search(r"```sql\n(.*?)```", AGENT, re.S).group(1)
    assert "ms.jinora_score" in sql and "creative_score IS NOT NULL" in sql
    with SessionLocal() as db:
        assert db.execute(text(sql.rstrip().rstrip(";"))).fetchall() == []


# --- scheduled tasks script: monthly sweep, simulation by default ------------------------------------

SCHEDULER = (ROOT / "scripts" / "schedule_windows.ps1").read_text(encoding="utf-8-sig")


def test_scheduler_sweep_runs_only_on_days_1_and_15_of_every_month():
    assert "MSFT_TaskMonthlyTrigger" in SCHEDULER  # CIM monthly trigger, not -Daily
    assert "(1 -shl 0) -bor (1 -shl 14)" in SCHEDULER  # bitmask: day 1 and day 15
    assert re.search(r"MonthOfYear\s*=\s*\[uint16\]4095", SCHEDULER)  # all 12 months
    sweep_block = SCHEDULER.split("# Task 2: Bi-weekly Full Sweep")[1].split("# Task 3")[0]
    assert "New-ScheduledTaskTrigger -Daily" not in sweep_block and "src.cli run --all" in sweep_block


def test_scheduler_never_registers_without_the_register_switch():
    assert "[switch]$Register" in SCHEDULER
    dry_run = SCHEDULER.split("if (-not $Register) {")[1].split("exit 0")[0]
    assert "Register-ScheduledTask" not in dry_run and "exit 0" in SCHEDULER
    assert SCHEDULER.index("if (-not $Register) {") < SCHEDULER.index("Register-ScheduledTask")  # simulation exits before any registration
