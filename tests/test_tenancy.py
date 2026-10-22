"""
Tests for the multi-company step (src/db/tenancy.py, docs/PLAN_MULTIEMPRESA.md Phase 1): companies / projects,
the default Cosmiq project and UNIQUE(project_id, name) on competitors. Temporary SQLite files, 0 external APIs.
"""

import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.engine import make_engine
from src.db.migrate import migrate
from src.db.models import (
    COMPETITOR_APPROVED,
    DEFAULT_COMPANY_NAME,
    PROJECT_COMPETITORS_APPROVED,
    Ad,
    Base,
    Company,
    Competitor,
    Project,
    get_default_project,
)
from src.db.tenancy import (
    UNIQUE_NAME,
    apply_tenancy,
    format_tenancy_report,
    migrate_tenancy,
    needs_unique_rebuild,
    verify_tenancy,
)

# competitors as it was before projects existed (Phase 6): global UNIQUE(name), no project columns
OLD_COMPETITORS_DDL = """
CREATE TABLE competitors (
    id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, website VARCHAR(512), pillar VARCHAR(64),
    priority INTEGER NOT NULL, markets TEXT, page_ids TEXT, meta_json TEXT,
    PRIMARY KEY (id), UNIQUE (name)
)
"""
NEW_TABLES = ("companies", "projects", "competitors")


@pytest.fixture
def old_db(tmp_path):
    """A schema_version 6 database whose competitors table predates projects, with ads pointing at it."""
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(OLD_COMPETITORS_DDL))
        conn.execute(text("CREATE INDEX ix_competitors_pillar ON competitors (pillar)"))
        conn.execute(text("CREATE INDEX ix_competitors_priority ON competitors (priority)"))
    Base.metadata.create_all(bind=engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in NEW_TABLES])
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO competitors (id, name, pillar, priority, page_ids) VALUES "
            "(1, 'Kommo', 'crm', 1, '[\"10\"]'), (2, 'Vapi AI', 'voz', 1, '[\"20\"]'), (3, 'Wati', 'conversacional', 2, '[]')"
        ))
        conn.execute(text(
            "INSERT INTO ads (id, competitor_id, page_id, days_active, is_active, has_video, platforms, pillar, status, created_at, updated_at) VALUES "
            "('a1', 1, '10', 5, 1, 0, '[]', 'crm', 'scored', '2026-10-01', '2026-10-01'), "
            "('a2', 2, '20', 9, 1, 1, '[]', 'voz', 'evaluated', '2026-10-01', '2026-10-01'), "
            "('a3', NULL, '99', 1, 1, 0, '[]', 'general', 'out_of_catalog', '2026-10-01', '2026-10-01')"
        ))
        conn.execute(text("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '6')"))
    yield engine
    engine.dispose()


def _ad_links(engine):
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, competitor_id FROM ads ORDER BY id")).fetchall())


def test_dry_run_reports_the_work_and_writes_nothing(old_db, tmp_path):
    report = migrate_tenancy(old_db, dry_run=True)
    plan = report["plan"]
    assert report["status"] == "dry_run"
    assert plan["create_tables"] == ["companies", "projects"]
    assert plan["add_columns"] == ["competitors.project_id", "competitors.status", "competitors.affinity_score"]
    assert plan["competitors_to_attach"] == 3 and plan["rebuild_unique"] and not plan["default_project_exists"]
    assert not inspect(old_db).has_table("projects") and needs_unique_rebuild(old_db)
    assert not (tmp_path / "backup").exists()
    assert "MIGRACIÓN MULTIEMPRESA — DRY_RUN" in format_tenancy_report(report)


def test_old_database_is_upgraded_without_touching_ads(old_db, tmp_path):
    links_before = _ad_links(old_db)
    report = migrate_tenancy(old_db, dry_run=False)

    assert report["status"] == "migrated" and report["verify"]["ok"], report["verify"]
    assert report["backup_path"] and "before_multiempresa" in report["backup_path"]
    assert report["default_project_created"] and report["competitors_attached"] == 3 and report["unique_rebuilt"]
    assert _ad_links(old_db) == links_before  # same competitor ids, nothing re-pointed

    with Session(old_db) as s:
        project = get_default_project(s, create=False)
        assert project is not None and project.company.name == DEFAULT_COMPANY_NAME
        assert project.status == PROJECT_COMPETITORS_APPROVED and project.is_default and project.auto_approve
        profile = json.loads(project.profile_json)
        assert {p["key"] for p in profile["pillars"]} == {"voz", "conversacional", "crm"}
        assert profile["keywords"]["crm"]  # the pillar keywords travel with the profile
        comps = s.query(Competitor).order_by(Competitor.id).all()
        assert [(c.id, c.name, c.project_id, c.status) for c in comps] == [
            (1, "Kommo", project.id, COMPETITOR_APPROVED),
            (2, "Vapi AI", project.id, COMPETITOR_APPROVED),
            (3, "Wati", project.id, COMPETITOR_APPROVED),
        ]
        assert json.loads(comps[0].page_ids) == ["10"] and comps[0].priority == 1  # data kept as it was

    insp = inspect(old_db)
    assert UNIQUE_NAME in {uc["name"] for uc in insp.get_unique_constraints("competitors")}
    assert not any(uc["column_names"] == ["name"] for uc in insp.get_unique_constraints("competitors"))
    assert {"ix_competitors_pillar", "ix_competitors_priority", "ix_competitors_project_id"} <= {i["name"] for i in insp.get_indexes("competitors")}
    assert any(fk["referred_table"] == "projects" for fk in insp.get_foreign_keys("competitors"))


def test_upgrade_is_idempotent(old_db):
    migrate_tenancy(old_db, backup=False)
    with old_db.connect() as conn:
        before = {t: conn.execute(text(f"SELECT * FROM {t} ORDER BY id")).fetchall() for t in ("companies", "projects", "competitors", "ads")}
    again = migrate_tenancy(old_db, backup=False)
    assert not again["default_project_created"] and again["competitors_attached"] == 0 and not again["unique_rebuilt"]
    with old_db.connect() as conn:
        assert {t: conn.execute(text(f"SELECT * FROM {t} ORDER BY id")).fetchall() for t in before} == before


def test_names_are_unique_per_project_not_globally(old_db):
    migrate_tenancy(old_db, backup=False)
    with Session(old_db) as s:
        other = Project(company=Company(name="Panaderia Sol", industry="Panaderías"), name="Competidores 2026")
        s.add(Competitor(name="Kommo", project=other, pillar="crm", priority=2))  # same name, other project: allowed
        s.commit()
        assert s.query(Competitor).filter(Competitor.name == "Kommo").count() == 2
    with Session(old_db) as s:
        s.add(Competitor(name="Kommo", project=get_default_project(s), priority=2))  # same name, same project
        with pytest.raises(IntegrityError):
            s.commit()


def test_foreign_keys_still_enforced_after_rebuild(old_db):
    migrate_tenancy(old_db, backup=False)
    with old_db.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    with Session(old_db) as s:
        s.add(Ad(id="bad", competitor_id=999))
        with pytest.raises(IntegrityError):
            s.commit()
    with Session(old_db) as s:
        s.add(Competitor(name="Huérfano", project_id=999, priority=2))
        with pytest.raises(IntegrityError):
            s.commit()


def test_deleting_a_project_removes_its_competitors_and_unlinks_ads(old_db):
    migrate_tenancy(old_db, backup=False)
    with Session(old_db) as s:
        other = Project(company=Company(name="Otra"), name="P")
        s.add(Competitor(name="Solo de Otra", project=other, priority=2, page_ids="[]"))
        s.commit()
        comp_id = s.query(Competitor).filter(Competitor.name == "Solo de Otra").one().id
        s.add(Ad(id="o1", competitor_id=comp_id))
        s.commit()
        s.delete(s.get(Project, other.id))
        s.commit()
        assert s.get(Competitor, comp_id) is None
        assert s.get(Ad, "o1").competitor_id is None  # ads are never deleted with their competitor
        assert s.query(Competitor).count() == 3


def test_competitor_saved_without_project_joins_the_default_project(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as s:
        s.add_all([Competitor(name="Alpha CRM", priority=1), Competitor(name="Beta Chat", priority=2)])
        s.commit()
        project = get_default_project(s, create=False)
        assert project is not None
        assert {(c.project_id, c.status) for c in s.query(Competitor)} == {(project.id, COMPETITOR_APPROVED)}
        assert s.query(Project).count() == 1 and s.query(Company).count() == 1
    report = apply_tenancy(engine)
    assert not report["default_project_created"] and not report["unique_rebuilt"] and report["verify"]["ok"]


def test_phase6_migrate_on_an_old_database_applies_the_tenancy_step(old_db, tmp_path):
    report = migrate(old_db, backup=True, backup_dir=tmp_path / "bk")
    assert report["status"] == "already_migrated"
    assert report["tenancy"]["verify"]["ok"] and report["tenancy"]["competitors_attached"] == 3
    assert "before_multiempresa" in report["backup_path"]
    assert report["after"]["projects"] == 1 and report["after"]["competitors"] == 3
    # a second run has nothing left to do and takes no backup
    again = migrate(old_db, backup=True, backup_dir=tmp_path / "bk2")
    assert "tenancy" not in again and not (tmp_path / "bk2").exists()
    assert verify_tenancy(old_db)["ok"]
