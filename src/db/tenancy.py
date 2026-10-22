"""
Multi-company migration (docs/PLAN_MULTIEMPRESA.md, Phase 1): companies / projects, and competitors per project.

    python -m src.db.tenancy --dry-run     # prints what it would change
    python -m src.db.tenancy               # backup (SQLite), migrate, verify

`python -m src.db.migrate` runs it too, after the Phase 6 migration. Steps (all idempotent, nothing is deleted):

1. Creates the companies / projects tables and adds competitors.project_id / status / affinity_score.
2. Creates the default company "Cosmiq" and its project (Project.is_default) with the profile of the
   current pillars (src/pillars.py), and attaches every competitor without a project to it as 'approved'.
3. Swaps the global UNIQUE(name) of competitors for UNIQUE(project_id, name), so two companies can share a
   competitor. SQLite cannot drop a constraint: the table is rebuilt with the documented
   create-copy-drop-rename procedure, with foreign keys off and a foreign_key_check before committing.

Ads, media, analyses and scores are never touched: they keep pointing to the same competitor ids.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List, Optional

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.db.engine import add_missing_columns, column_names, default_database_url, has_table, make_engine
from src.db.models import (
    COMPETITOR_APPROVED,
    Base,
    Company,
    Competitor,
    Project,
    get_default_project,
    to_json,
)

logger = logging.getLogger("Jinora.Tenancy")

UNIQUE_NAME = "uq_competitors_project_name"


def default_profile() -> Dict[str, Any]:
    """Profile of the default project, built from the pillars that were hard-coded for Cosmiq."""
    from src.pillars import PILLARS

    return {
        "description": (
            "Plataforma de automatización de ventas y atención para empresas de LatAm: WhatsApp, CRM, agendamiento, "
            "gestión de leads, mensajes masivos, agentes conversacionales y agentes de voz."
        ),
        "pillars": [{"key": key, "name": p["name"], "description": p["description"]} for key, p in PILLARS.items()],
        "keywords": {key: list(p["keywords"]) for key, p in PILLARS.items()},
        "negative_terms": [],
        "source": "src/pillars.py",
    }


def needs_unique_rebuild(engine: Engine) -> bool:
    """True while competitors still has the old global UNIQUE(name) instead of UNIQUE(project_id, name)."""
    if not has_table(engine, "competitors"):
        return False
    names = {uc.get("name") for uc in inspect(engine).get_unique_constraints("competitors")}
    return UNIQUE_NAME not in names


def _name_only_uniques(engine: Engine) -> Dict[str, List[str]]:
    insp = inspect(engine)
    constraints = [uc["name"] for uc in insp.get_unique_constraints("competitors") if uc["column_names"] == ["name"]]
    indexes = [ix["name"] for ix in insp.get_indexes("competitors") if ix.get("unique") and ix["column_names"] == ["name"]]
    return {"constraints": constraints, "indexes": indexes}


def _rebuild_sqlite(engine: Engine) -> None:
    """Recreates competitors with the current model (SQLite: create new, copy, drop old, rename)."""
    old_cols = column_names(engine, "competitors")
    md = MetaData()
    for table in (Company.__table__, Project.__table__):  # the FK targets must exist in the metadata to compile
        table.to_metadata(md)
    new_table = Competitor.__table__.to_metadata(md, name="competitors_new")
    cols = ", ".join(f'"{c.name}"' for c in Competitor.__table__.columns if c.name in old_cols)
    old_indexes = [ix["name"] for ix in inspect(engine).get_indexes("competitors")]

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")  # outside a transaction, or SQLite ignores it
        try:
            conn.exec_driver_sql("BEGIN")
            for name in old_indexes:  # their names are reused by the new table
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')
            new_table.create(conn)
            conn.exec_driver_sql(f'INSERT INTO competitors_new ({cols}) SELECT {cols} FROM competitors')
            conn.exec_driver_sql("DROP TABLE competitors")
            conn.exec_driver_sql("ALTER TABLE competitors_new RENAME TO competitors")
            broken = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"foreign_key_check falló tras reconstruir competitors: {broken[:5]}")
            conn.exec_driver_sql("COMMIT")
        except Exception:
            conn.exec_driver_sql("ROLLBACK")
            raise
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _rebuild_generic(engine: Engine) -> None:
    """PostgreSQL and others: drop the name-only unique, add the FK and the per-project unique in place."""
    olds = _name_only_uniques(engine)
    has_fk = any(fk["constrained_columns"] == ["project_id"] for fk in inspect(engine).get_foreign_keys("competitors"))
    with engine.begin() as conn:
        for name in olds["constraints"]:
            conn.execute(text(f'ALTER TABLE competitors DROP CONSTRAINT "{name}"'))
        for name in olds["indexes"]:
            conn.execute(text(f'DROP INDEX "{name}"'))
        if not has_fk:
            conn.execute(text(
                "ALTER TABLE competitors ADD CONSTRAINT fk_competitors_project "
                "FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE"
            ))
        conn.execute(text(f'ALTER TABLE competitors ADD CONSTRAINT "{UNIQUE_NAME}" UNIQUE (project_id, name)'))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_competitors_project_id ON competitors (project_id)"))


def rebuild_competitor_uniqueness(engine: Engine) -> bool:
    """Applies step 3 if needed. Returns True when the table was changed."""
    if not needs_unique_rebuild(engine):
        return False
    if engine.url.get_backend_name() == "sqlite":
        _rebuild_sqlite(engine)
    else:
        _rebuild_generic(engine)
    return True


def attach_to_default_project(session: Session) -> Dict[str, Any]:
    """Step 2: default company/project (with its profile) and every competitor without a project attached to it."""
    existed = get_default_project(session, create=False) is not None
    project = get_default_project(session)
    if not project.profile_json:
        project.profile_json = to_json(default_profile())
    session.flush()
    orphans = session.query(Competitor).filter(Competitor.project_id.is_(None)).all()
    for comp in orphans:
        comp.project_id = project.id
    no_status = session.query(Competitor).filter(Competitor.status.is_(None)).all()
    for comp in no_status:
        comp.status = COMPETITOR_APPROVED
    return {"default_project_created": not existed, "default_project_id": project.id,
            "competitors_attached": len(orphans), "competitors_status_set": len(no_status)}


def plan_tenancy(engine: Engine) -> Dict[str, Any]:
    """What apply_tenancy would do, without writing anything (dry-run)."""
    plan: Dict[str, Any] = {
        "create_tables": [t for t in ("companies", "projects") if not has_table(engine, t)],
        "add_columns": [],
        "default_project_exists": False,
        "default_profile_missing": True,
        "competitors_to_attach": 0,
        "rebuild_unique": needs_unique_rebuild(engine),
    }
    if not has_table(engine, "competitors"):
        return plan
    cols = column_names(engine, "competitors")
    plan["add_columns"] = [f"competitors.{c}" for c in ("project_id", "status", "affinity_score") if c not in cols]
    with engine.connect() as conn:
        if "project_id" in cols:
            plan["competitors_to_attach"] = int(conn.execute(text("SELECT COUNT(*) FROM competitors WHERE project_id IS NULL")).scalar() or 0)
        else:
            plan["competitors_to_attach"] = int(conn.execute(text("SELECT COUNT(*) FROM competitors")).scalar() or 0)
        if has_table(engine, "projects") and "is_default" in column_names(engine, "projects"):
            plan["default_project_exists"] = bool(conn.execute(text("SELECT COUNT(*) FROM projects WHERE is_default = :t"), {"t": True}).scalar())
            plan["default_profile_missing"] = bool(conn.execute(
                text("SELECT COUNT(*) FROM projects WHERE is_default = :t AND profile_json IS NULL"), {"t": True}).scalar()) \
                or not plan["default_project_exists"]
    return plan


def verify_tenancy(engine: Engine) -> Dict[str, Any]:
    """Checks after the migration: one default project, no competitor without project, valid references."""
    checks = {
        "competitors_sin_proyecto": "SELECT COUNT(*) FROM competitors WHERE project_id IS NULL",
        "competitors_proyecto_invalido": "SELECT COUNT(*) FROM competitors WHERE project_id NOT IN (SELECT id FROM projects)",
        "proyectos_empresa_invalida": "SELECT COUNT(*) FROM projects WHERE company_id NOT IN (SELECT id FROM companies)",
        "ads_competitor_invalido": "SELECT COUNT(*) FROM ads WHERE competitor_id IS NOT NULL AND competitor_id NOT IN (SELECT id FROM competitors)",
    }
    out: Dict[str, Any] = {}
    with engine.connect() as conn:
        for name, sql in checks.items():
            if name.startswith("ads") and not has_table(engine, "ads"):
                out[name] = 0
                continue
            out[name] = int(conn.execute(text(sql)).scalar() or 0)
        defaults = int(conn.execute(text("SELECT COUNT(*) FROM projects WHERE is_default = :t"), {"t": True}).scalar() or 0)
    out["proyectos_por_defecto"] = defaults
    out["unique_por_proyecto"] = not needs_unique_rebuild(engine)
    out["ok"] = defaults == 1 and out["unique_por_proyecto"] and all(v == 0 for k, v in out.items() if k in checks)
    return out


def apply_tenancy(engine: Engine) -> Dict[str, Any]:
    """Runs steps 1-3 (idempotent). Returns a report with what changed and the verification."""
    Base.metadata.create_all(bind=engine)
    added = add_missing_columns(engine, Base.metadata)
    with Session(engine) as session:
        attached = attach_to_default_project(session)
        session.commit()
    rebuilt = rebuild_competitor_uniqueness(engine)
    return {"columns_added": added, **attached, "unique_rebuilt": rebuilt, "verify": verify_tenancy(engine)}


def migrate_tenancy(engine: Engine, dry_run: bool = False, backup: bool = True) -> Dict[str, Any]:
    """CLI entry: dry-run plan, or backup + apply + verify."""
    if dry_run:
        return {"status": "dry_run", "plan": plan_tenancy(engine)}
    report: Dict[str, Any] = {"status": "migrated", "plan": plan_tenancy(engine)}
    if backup:
        from src.db.migrate import backup_sqlite

        path = backup_sqlite(engine, label="multiempresa")
        report["backup_path"] = str(path) if path else None
    report.update(apply_tenancy(engine))
    return report


def format_tenancy_report(report: Dict[str, Any]) -> str:
    lines = ["=" * 78, f"MIGRACIÓN MULTIEMPRESA — {report['status'].upper()}", "=" * 78]
    plan = report["plan"]
    lines += [
        f"Tablas a crear: {', '.join(plan['create_tables']) or 'ninguna'}",
        f"Columnas a añadir: {', '.join(plan['add_columns']) or 'ninguna'}",
        f"Proyecto por defecto (Cosmiq) ya existe: {'sí' if plan['default_project_exists'] else 'no'}",
        f"Competidores a asignar al proyecto por defecto: {plan['competitors_to_attach']}",
        f"Reconstruir UNIQUE(name) -> UNIQUE(project_id, name): {'sí' if plan['rebuild_unique'] else 'no'}",
    ]
    if report["status"] == "migrated":
        if report.get("backup_path"):
            lines.append(f"Backup verificado: {report['backup_path']}")
        lines += [
            "",
            f"Proyecto por defecto: id={report['default_project_id']} ({'creado' if report['default_project_created'] else 'existente'})",
            f"Competidores asignados: {report['competitors_attached']}",
            f"Tabla competitors reconstruida: {'sí' if report['unique_rebuilt'] else 'no'}",
            f"Verificación: {report['verify']}",
        ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Migración multiempresa: empresas, proyectos y competidores por proyecto")
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime lo que haría")
    parser.add_argument("--no-backup", action="store_true", help="No crear backup (SQLite)")
    parser.add_argument("--db-url", default=None, help="URL SQLAlchemy (por defecto DATABASE_URL o data/meta_ads.db)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = make_engine(args.db_url or default_database_url())
    report = migrate_tenancy(engine, dry_run=args.dry_run, backup=not args.no_backup)
    print(format_tenancy_report(report))
    return 0 if report.get("verify", {"ok": True})["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
