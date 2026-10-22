"""
Tests for the Phase 6 data model and migration (src/db/models.py, src/db/migrate.py).
Everything runs against a temporary SQLite file shaped like the pre-Phase-6 database:
0 external APIs, the real data/meta_ads.db is never opened.
"""

import json
import sqlite3
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.ai.quota import QuotaGuard
from src.db.competitors import load_competitors_config, normalize_name
from src.db.engine import LEGACY_ADS_TABLE, LEGACY_ANALYSES_TABLE, is_legacy_schema, make_engine, schema_version
from src.db.migrate import check_integrity, current_rubric_versions, format_report, migrate, normalize_rel_path
from src.db.models import Ad, Analysis, Base, Competitor, Job, Media, MediaInfo, MarketSignal
from src.scoring.jinora import compute_jinora_score, get_score_weights

LEGACY_ADS_DDL = """
CREATE TABLE ads (
    id VARCHAR(64) NOT NULL, page_id VARCHAR(64), page_name VARCHAR(255), delivery_start_time VARCHAR(64),
    days_active INTEGER, is_active BOOLEAN, publisher_platforms TEXT, headline TEXT, body_text TEXT,
    cta_text VARCHAR(128), link_url TEXT, video_path TEXT, image_path TEXT, has_video BOOLEAN,
    pillar VARCHAR(64), search_query VARCHAR(128), duration_seconds FLOAT, full_transcript TEXT,
    transcript_timeline TEXT, total_frames INTEGER, cognitive_score INTEGER, score_breakdown TEXT,
    created_at DATETIME, updated_at DATETIME, delivery_stop_time VARCHAR(64), countries TEXT,
    media_sha256 VARCHAR(64), phash VARCHAR(32), copy_hash VARCHAR(64), creative_hash VARCHAR(64),
    creative_group_id VARCHAR(64), variant_count INTEGER, market_score FLOAT, market_components TEXT,
    jinora_score FLOAT, PRIMARY KEY (id)
)
"""
LEGACY_ANALYSES_DDL = """
CREATE TABLE analyses (
    id INTEGER NOT NULL, ad_id VARCHAR(64) NOT NULL, model VARCHAR(64) NOT NULL, prompt_version VARCHAR(32),
    rubric_version VARCHAR(32), creative_hash VARCHAR(128), analysis_type VARCHAR(32),
    creative_score INTEGER NOT NULL, result_json TEXT NOT NULL, cost_usd FLOAT, created_at DATETIME,
    PRIMARY KEY (id)
)
"""
LEGACY_INDEXES = [
    "CREATE INDEX ix_ads_id ON ads (id)",
    "CREATE INDEX ix_ads_page_name ON ads (page_name)",
    "CREATE INDEX ix_ads_page_id ON ads (page_id)",
    "CREATE INDEX ix_ads_days_active ON ads (days_active)",
    "CREATE INDEX ix_ads_pillar ON ads (pillar)",
    "CREATE INDEX ix_ads_search_query ON ads (search_query)",
    "CREATE INDEX ix_ads_creative_group_id ON ads (creative_group_id)",
    "CREATE INDEX ix_analyses_ad_id ON analyses (ad_id)",
    "CREATE INDEX ix_analyses_creative_hash ON analyses (creative_hash)",
]

VAPI_PAGE, CLIENTIFY_PAGE = "111", "333"


def _ad_row(ad_id, **kw):
    row = {
        "id": ad_id, "page_id": None, "page_name": "Nadie", "delivery_start_time": "2026-05-21T07:00:00+00:00",
        "days_active": 10, "is_active": 1, "publisher_platforms": '["FACEBOOK"]', "headline": "h", "body_text": "b",
        "cta_text": None, "link_url": None, "video_path": None, "image_path": None, "has_video": 0,
        "pillar": "crm", "search_query": "q", "duration_seconds": None, "full_transcript": None,
        "transcript_timeline": None, "total_frames": 0, "cognitive_score": None, "score_breakdown": None,
        "created_at": "2026-10-01 10:00:00.000000", "updated_at": "2026-10-02 10:00:00.000000",
        "delivery_stop_time": None, "countries": None, "media_sha256": None, "phash": None, "copy_hash": None,
        "creative_hash": None, "creative_group_id": None, "variant_count": None, "market_score": None,
        "market_components": None, "jinora_score": None,
    }
    row.update(kw)
    return row


@pytest.fixture
def env(tmp_path):
    """A pre-Phase-6 SQLite database plus downloads, competitors config, rejected csv and backup dir."""
    rubrics = current_rubric_versions()
    downloads = tmp_path / "downloads"

    def touch(rel, data=b"x" * 64):
        f = downloads / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    for rel in ("Vapi AI/v1/video_0.mp4", "Vapi AI/v1/image_0.jpg", "Bland AI/b1/video_0.mp4", "Clientify/c1/video_0.mp4"):
        touch(rel)
    (downloads / "Skool" / "x1").mkdir(parents=True)
    Image.new("RGB", (40, 30), "white").save(downloads / "Skool" / "x1" / "image_0.jpg")
    # Clientify/c1/image_0.jpg intentionally missing on disk

    comp_json = tmp_path / "competitors.json"
    comp_json.write_text(json.dumps({"total_competitors": 3, "pillars": {
        "voz": {"name": "Voz", "competitors": [
            {"name": "Vapi AI", "website": "https://vapi.ai", "priority": 2, "markets": ["US", "PE"], "page_ids": [VAPI_PAGE], "region": "US"},
            {"name": "Bland AI", "website": "https://bland.ai", "priority": 1, "markets": ["US"], "page_ids": []},
        ]},
        "crm": {"name": "CRM", "competitors": [
            {"name": "Clientify", "website": "https://clientify.com", "priority": 1, "markets": ["MX"], "page_ids": [CLIENTIFY_PAGE]},
        ]},
    }}), encoding="utf-8")

    rejected = tmp_path / "rejected_ads.csv"
    rejected.write_text("ad_id,page_name,motivo\nr1,Air Wick,Sin relevancia tematica\n", encoding="utf-8")

    db_file = tmp_path / "meta_ads.db"
    engine = make_engine(f"sqlite:///{db_file}")
    QuotaGuard(db_path=str(db_file))  # creates llm_usage / llm_attempts exactly as the quota module does
    ads = [
        _ad_row("v1", page_id=VAPI_PAGE, page_name="Vapi AI", video_path="Vapi AI/v1/video_0.mp4", image_path="Vapi AI/v1/image_0.jpg",
                has_video=1, media_sha256="abc123", phash="f0f0f0f0f0f0f0f0", duration_seconds=30.0, full_transcript="hola mundo",
                transcript_timeline='[{"start": 0, "end": 1, "text": "hola"}]', total_frames=30, cognitive_score=88,
                market_score=60.0, market_components='{"used": {}, "missing": ["countries"]}', jinora_score=99.0, days_active=152),
        _ad_row("b1", page_id="999", page_name="BlandAI", video_path="Bland AI\\b1\\video_0.mp4", has_video=1, cognitive_score=70),
        _ad_row("c1", page_id=CLIENTIFY_PAGE, page_name="Clientify", has_video=1,
                video_path="C:\\Users\\Otro\\Jinora\\downloads\\Clientify\\c1\\video_0.mp4", image_path="Clientify/c1/image_0.jpg"),
        _ad_row("x1", page_id="555", page_name="Skool", image_path="Skool/x1/image_0.jpg"),
        _ad_row("r1", page_id="777", page_name="Air Wick"),
        _ad_row("n1", page_name="Unknown Co", has_video=1),  # video ad whose file was never downloaded
        _ad_row("abs1", page_name="Elsewhere", has_video=1, video_path="D:\\stuff\\clip.mp4"),  # cannot be relativized
        _ad_row("i1", page_id=VAPI_PAGE, page_name="Vapi AI", is_active=0, delivery_stop_time="2026-08-14T07:00:00+00:00", days_active=85),
    ]
    analyses = [
        (1, "v1", "v1", "video", 70), (2, "v1", rubrics["video"], "video", 80),
        (3, "b1", "v1", "video", 50), (4, "b1", rubrics["video"], "video", 66),
        (5, "ghost", rubrics["video"], "video", 61), (6, "c1", "v1", "video", 40),
        (7, "x1", rubrics["static"], "static", 55),
    ]
    with engine.begin() as conn:
        conn.execute(text(LEGACY_ADS_DDL))
        conn.execute(text(LEGACY_ANALYSES_DDL))
        for ddl in LEGACY_INDEXES:
            conn.execute(text(ddl))
        cols = list(ads[0])
        conn.execute(text(f"INSERT INTO ads ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"), ads)
        conn.execute(text(
            "INSERT INTO analyses (id, ad_id, model, prompt_version, rubric_version, analysis_type, creative_score, result_json, cost_usd, created_at) "
            "VALUES (:id, :ad_id, 'gemini-x', 'v1', :rv, :t, :s, :rj, 0.0, '2026-10-21 20:00:00.000000')"),
            [{"id": i, "ad_id": a, "rv": rv, "t": t, "s": s, "rj": json.dumps({"creative_score": s})} for i, a, rv, t, s in analyses])
    with sqlite3.connect(db_file) as raw:
        raw.execute("INSERT INTO llm_usage (day_pt, model, key_fingerprint, requests) VALUES ('2026-10-21', 'gemini-x', 'abcd1234', 5)")
        raw.execute("INSERT INTO llm_attempts (day_pt, timestamp_utc, model, status, key_fingerprint) VALUES ('2026-10-21', 't', 'gemini-x', 'success', 'abcd1234')")

    yield SimpleNamespace(
        engine=engine, db_file=db_file, downloads=downloads, comp_json=comp_json, rejected=rejected,
        backup_dir=tmp_path / "backup", rubrics=rubrics,
    )
    engine.dispose()


def run(env, **kw):
    args = dict(competitors_path=env.comp_json, rejected_csv=env.rejected, downloads_dir=env.downloads, backup_dir=env.backup_dir)
    args.update(kw)
    return migrate(env.engine, **args)


def _dump(engine, table, order="1"):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY {order}")).fetchall()]


# --------------------------------------------------------------------------------------

def test_dry_run_changes_nothing_and_predicts_the_real_counts(env):
    before = {t: _dump(env.engine, t) for t in ("ads", "analyses", "llm_usage", "llm_attempts")}
    dry = run(env, dry_run=True)

    assert dry["status"] == "dry_run"
    assert is_legacy_schema(env.engine)  # still the old layout
    assert schema_version(env.engine) == ""
    assert not env.backup_dir.exists()  # dry-run never writes a backup
    assert {t: _dump(env.engine, t) for t in before} == before

    real = run(env)
    expected = dry["expected_after"]
    assert {t: real["after"][t] for t in expected} == expected
    assert "MIGRACIÓN FASE 6" in format_report(dry) and "filas esperadas" in format_report(dry)


def test_no_ads_lost_and_per_competitor_counts_match(env):
    legacy = _dump(env.engine, "ads", "id")
    by_page_before = {}
    for r in legacy:
        by_page_before[r[2]] = by_page_before.get(r[2], 0) + 1

    report = run(env)
    assert report["status"] == "migrated"
    assert report["before"]["ads"] == report["after"]["ads"] == 8

    with Session(env.engine) as s:
        by_page_after = {}
        for ad in s.query(Ad).all():
            by_page_after[ad.page_name] = by_page_after.get(ad.page_name, 0) + 1
        assert by_page_after == by_page_before

        comp = {c.name: c.id for c in s.query(Competitor).all()}
        assignment = {ad.id: ad.competitor_id for ad in s.query(Ad).all()}
        assert assignment["v1"] == assignment["i1"] == comp["Vapi AI"]  # exact page_id
        assert assignment["b1"] == comp["Bland AI"]  # 'BlandAI' == 'Bland AI' normalized, page_id unknown
        assert assignment["c1"] == comp["Clientify"]
        assert assignment["x1"] is None and assignment["r1"] is None and assignment["n1"] is None
        per_competitor = {name: sum(1 for v in assignment.values() if v == cid) for name, cid in comp.items()}
        assert per_competitor == {"Vapi AI": 2, "Bland AI": 1, "Clientify": 1}
    assert report["stats"]["ads_sin_competitor"] == 4 and report["stats"]["competitor_asignado"] == 4  # x1, r1, n1, abs1


def test_media_paths_are_kept_relative_and_unusable_ones_reported(env):
    report = run(env)
    with Session(env.engine) as s:
        rows = {(m.ad_id, m.kind): m for m in s.query(Media).all()}
        assert set(rows) == {("v1", "video"), ("v1", "image"), ("b1", "video"), ("c1", "video"), ("c1", "image"), ("x1", "image")}
        assert rows[("b1", "video")].rel_path == "Bland AI/b1/video_0.mp4"  # backslashes -> forward slashes
        assert rows[("c1", "video")].rel_path == "Clientify/c1/video_0.mp4"  # other machine's absolute path relativized
        for m in rows.values():
            assert not m.rel_path.startswith("/") and ":" not in m.rel_path and "\\" not in m.rel_path
        assert rows[("v1", "video")].size == 64 and rows[("v1", "video")].sha256 is not None
        assert rows[("c1", "image")].size is None  # file missing on disk: row kept, size unknown

        ad = s.get(Ad, "v1")
        assert ad.video_path == "Vapi AI/v1/video_0.mp4" and ad.image_path == "Vapi AI/v1/image_0.jpg" and ad.has_video
        assert s.get(Ad, "abs1").video_path is None
        # The 'creative has a video' flag survives even when no file was downloaded (no Media row)
        assert s.get(Ad, "n1").has_video is True and s.get(Ad, "n1").video_path is None
        assert s.get(Ad, "abs1").has_video is True and s.get(Ad, "x1").has_video is False
        assert s.query(Ad).filter(Ad.has_video.is_(True)).count() == 5  # v1, b1, c1, n1, abs1
    assert report["stats"]["media_sin_archivo"] == 1
    assert report["stats"]["rutas_no_relativizables"] == 1
    assert {"table": "media", "id": "abs1:video", "reason": "ruta no relativizable: D:\\stuff\\clip.mp4"} in report["discarded"]
    assert report["stats"]["anuncios_video_sin_media"] == 2  # n1 (never downloaded) and abs1


def test_normalize_rel_path_cases():
    assert normalize_rel_path("Vapi AI/1/video_0.mp4") == "Vapi AI/1/video_0.mp4"
    assert normalize_rel_path("downloads\\Brand\\2\\image_0.jpg") == "Brand/2/image_0.jpg"
    assert normalize_rel_path("C:\\Users\\Gabo\\x\\downloads\\Brand\\3\\v.mp4") == "Brand/3/v.mp4"
    assert normalize_rel_path("/home/u/clip.mp4") is None and normalize_rel_path("D:\\clip.mp4") is None
    assert normalize_rel_path("../outside.mp4") is None and normalize_rel_path("") is None and normalize_rel_path(None) is None


def test_migration_is_idempotent_and_resumable(env):
    first = run(env)
    snapshot = {t: _dump(env.engine, t) for t in first["after"] if not t.startswith("sqlite_") and t != "schema_meta"}

    second = run(env)
    assert second["status"] == "already_migrated"
    assert {t: _dump(env.engine, t) for t in snapshot} == snapshot

    # Simulate a crash after the tables were created but before the version marker was written
    with env.engine.begin() as conn:
        conn.execute(text("DELETE FROM schema_meta"))
    third = run(env)
    assert third["status"] == "migrated"
    assert {t: _dump(env.engine, t) for t in snapshot} == snapshot  # nothing duplicated
    assert schema_version(env.engine) == "6"


def test_phase4_analyses_survive_and_old_evaluations_are_discarded(env):
    report = run(env)
    weights = get_score_weights()
    with Session(env.engine) as s:
        scored = {a.id: a for a in s.query(Analysis).filter(Analysis.analysis_type != "forensics")}
        assert set(scored) == {2, 4, 7}  # current rubric only, original ids preserved
        assert scored[2].creative_score == 80 and scored[7].analysis_type == "static"
        assert all(a.rubric_version == env.rubrics[a.analysis_type] for a in scored.values())

        assert s.get(Ad, "v1").status == "evaluated" and s.get(Ad, "x1").status == "evaluated"
        assert s.get(Ad, "c1").status == "pending_evaluation"  # only had a pre-1.1 evaluation
        # jinora_score is recomputed from the surviving analysis, not copied from the stale legacy value (99.0)
        signal = s.get(MarketSignal, "v1")
        assert signal.market_score == 60.0
        assert signal.jinora_score == compute_jinora_score(80, 60.0, weights)
        assert json.loads(signal.market_components)["missing"] == ["countries"]
        assert s.query(MarketSignal).count() == 1  # only ads that had a market score
    reasons = {(d["id"], d["reason"].split(" (")[0]) for d in report["discarded"] if d["table"] == "analyses"}
    assert {i for i, _ in reasons} == {"1", "3", "5", "6"}
    assert ("5", "anuncio inexistente en ads") in reasons
    assert all("evaluación previa a la rúbrica" in r for i, r in reasons if i != "5")
    assert report["stats"]["cognitive_score_descartado"] == 2
    with Session(env.engine) as s:
        assert "cognitive_score" not in Ad.__table__.columns
        assert s.query(Ad).filter(Ad.status == "evaluated").count() == 3

    # Nothing is dropped: every legacy row is still in the archive tables
    assert report["after"][LEGACY_ADS_TABLE] == 8 and report["after"][LEGACY_ANALYSES_TABLE] == 7


def test_transcript_moves_into_analysis_result_json_not_columns(env):
    run(env)
    with Session(env.engine) as s:
        ad = s.get(Ad, "v1")
        forensic = [a for a in ad.analyses if a.analysis_type == "forensics"]
        assert len(forensic) == 1 and forensic[0].creative_score is None
        payload = json.loads(forensic[0].result_json)
        assert payload["full_transcript"] == "hola mundo" and payload["total_frames"] == 30
        assert payload["transcript_timeline"][0]["text"] == "hola"
        assert "puede contener texto de Gemini" in payload["source"]  # v1 was evaluated before 21-oct: the old overwrite bug may apply
        assert ad.full_transcript == "hola mundo" and ad.duration_seconds == 30.0
        assert not {"full_transcript", "transcript_timeline", "language", "has_voiceover", "scene_cuts_estimate"} & set(Ad.__table__.columns.keys())
        assert not {"transcript", "has_voiceover", "language", "scene_cuts_estimate"} & set(Analysis.__table__.columns.keys())
        info = s.get(MediaInfo, "v1")
        assert info.phash == "f0f0f0f0f0f0f0f0" and info.sha256 == "abc123"
        x_info = s.get(MediaInfo, "x1")
        assert (x_info.width, x_info.height) == (40, 30)  # Pillow metadata, local


def test_rejected_ads_are_kept_and_flagged(env):
    report = run(env)
    with Session(env.engine) as s:
        ad = s.get(Ad, "r1")
        assert ad is not None and ad.status == "rejected"
        assert json.loads(ad.relevance_json) == {"is_relevant": False, "reason": "Sin relevancia tematica", "source": "data/rejected_ads.csv"}
    assert report["stats"]["anuncios_rechazados"] == 1


def test_foreign_keys_and_integrity(env):
    report = run(env)
    assert report["integrity"]["ok"] and check_integrity(env.engine)["foreign_key_check"] == 0

    with env.engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM media WHERE ad_id NOT IN (SELECT id FROM ads)")).scalar() == 0
    with Session(env.engine) as s:
        s.add(Media(ad_id="no_such_ad", kind="video", rel_path="a/b.mp4"))
        with pytest.raises(IntegrityError):
            s.commit()
    with Session(env.engine) as s:
        s.add(Ad(id="bad_comp", competitor_id=99999))
        with pytest.raises(IntegrityError):
            s.commit()
    with Session(env.engine) as s:  # deleting an ad removes its children (cascade)
        s.delete(s.get(Ad, "v1"))
        s.commit()
        assert s.query(Media).filter(Media.ad_id == "v1").count() == 0
        assert s.query(Analysis).filter(Analysis.ad_id == "v1").count() == 0
        assert s.query(MediaInfo).filter(MediaInfo.ad_id == "v1").count() == 0


def test_competitors_table_mirrors_config_with_page_ids(env):
    run(env)
    with Session(env.engine) as s:
        rows = {c.name: c for c in s.query(Competitor).all()}
        assert set(rows) == {"Vapi AI", "Bland AI", "Clientify"}
        assert json.loads(rows["Vapi AI"].page_ids) == [VAPI_PAGE] and json.loads(rows["Bland AI"].page_ids) == []
        assert rows["Bland AI"].priority == 1 and rows["Clientify"].pillar == "crm"
        assert json.loads(rows["Vapi AI"].markets) == ["US", "PE"]
        assert json.loads(rows["Vapi AI"].meta_json)["region"] == "US"


def test_real_config_has_105_competitors():
    rows = load_competitors_config()
    assert len(rows) == 105
    assert len({normalize_name(r["name"]) for r in rows}) == 105  # no collisions: name lookup is unambiguous
    assert sum(1 for r in rows if r["page_ids"]) >= 80


def test_quota_tables_are_untouched(env):
    before = {t: _dump(env.engine, t) for t in ("llm_usage", "llm_attempts")}
    ddl_before = [r for r in _dump(env.engine, "sqlite_master", "name") if r[1] in ("llm_usage", "llm_attempts")]
    run(env)
    assert {t: _dump(env.engine, t) for t in before} == before
    assert [r for r in _dump(env.engine, "sqlite_master", "name") if r[1] in ("llm_usage", "llm_attempts")] == ddl_before


def test_backup_is_created_before_migrating_and_matches_the_original(env):
    original = {t: len(_dump(env.engine, t)) for t in ("ads", "analyses", "llm_usage", "llm_attempts")}
    report = run(env)
    backup = report["backup_path"]
    assert backup and "before_phase6" in backup
    with sqlite3.connect(backup) as b:
        assert {t: b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in original} == original
        assert "competitor_id" not in [r[1] for r in b.execute("PRAGMA table_info(ads)")]  # old layout preserved


def test_schema_compiles_for_postgresql():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    for table in Base.metadata.sorted_tables:
        assert "CREATE TABLE" in str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))


def test_empty_database_gets_the_schema_without_legacy_tables(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    report = migrate(engine, backup=False, competitors_path=load_competitors_path(), rejected_csv=tmp_path / "none.csv", downloads_dir=tmp_path)
    assert report["status"] == "migrated" and report["after"]["ads"] == 0 and report["after"]["competitors"] == 105
    assert LEGACY_ADS_TABLE not in report["after"]
    assert migrate(engine, backup=False)["status"] == "already_migrated"


def load_competitors_path():
    from src.db.competitors import COMPETITORS_JSON

    return COMPETITORS_JSON


def test_save_or_update_ad_record_uses_the_new_model():
    """Scraper compatibility on the (temporary) global database created by tests/conftest.py."""
    from src.database import SessionLocal, get_ads_from_db, save_forensics, save_or_update_ad_record

    with SessionLocal() as s:
        s.add(Competitor(name="Compat Co", pillar="crm", priority=1, page_ids=json.dumps(["4242"])))
        s.commit()
    ad = save_or_update_ad_record({
        "ad_id": "compat_1", "page_id": "4242", "page_name": "Compat Co", "is_active": True, "days_active": 12,
        "delivery_start_time": "2026-10-01T07:00:00+00:00", "publisher_platforms": ["FACEBOOK", "INSTAGRAM"],
        "creatives": [{"title": "Titulo", "body": "Cuerpo", "cta_text": "Comprar", "link_url": "https://x.test"}],
        "downloaded_files": ["C:\\Otra\\Maquina\\downloads\\Compat Co\\compat_1\\video_0.mp4", "downloads/Compat Co/compat_1/image_0.jpg"],
        "forensics": {"full_transcript": "texto local", "transcript_timeline": [], "total_frames_extracted": 5, "duration_seconds": 12.5},
    }, pillar="crm", search_query="Compat Co")
    assert ad.id == "compat_1"

    found = [a for a in get_ads_from_db(has_video_only=True, limit=500) if a.id == "compat_1"]
    assert len(found) == 1
    got = found[0]
    assert got.video_path == "Compat Co/compat_1/video_0.mp4" and got.image_path == "Compat Co/compat_1/image_0.jpg"
    assert got.competitor_id is not None and got.full_transcript == "texto local" and got.duration_seconds == 12.5
    assert got.status == "pending_evaluation"

    # Re-saving the same ad updates it instead of duplicating rows
    save_or_update_ad_record({"ad_id": "compat_1", "page_id": "4242", "page_name": "Compat Co", "creatives": [],
                              "downloaded_files": ["downloads/Compat Co/compat_1/video_0.mp4"]}, pillar="crm")
    with SessionLocal() as s:
        assert s.query(Media).filter(Media.ad_id == "compat_1").count() == 2
        assert s.query(Ad).filter(Ad.id == "compat_1").count() == 1
        assert s.query(Job).count() >= 0
