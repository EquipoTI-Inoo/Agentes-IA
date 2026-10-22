"""
A2: scripts/calibrate.py reads AI evaluations from the database (analyses table, newest row per ad with the
rubric version in force) and never from analysis.json files on disk.
"""

import json
from pathlib import Path

import pytest

from scripts import calibrate
from src.database import SessionLocal
from src.db.migrate import current_rubric_versions
from src.db.models import Ad, Analysis

RUBRIC = current_rubric_versions()


def _analysis(ad_id, score, rubric, analysis_type="video", fmt="ugc", **extra):
    body = {"creative_score": score, "format": fmt}
    for dim in ("hook", "mechanism", "narrative", "offer"):
        body[dim] = {"score": score // 4}
    body.update(extra)
    return Analysis(ad_id=ad_id, model="gemini-x", prompt_version="v1", rubric_version=rubric,
                    analysis_type=analysis_type, creative_score=score, result_json=json.dumps(body))


@pytest.fixture
def seeded(clean_db, tmp_path, monkeypatch):
    with SessionLocal() as s:
        for ad_id in ("cal_1", "cal_2", "cal_3", "cal_4"):
            s.add(Ad(id=ad_id, page_name="X"))
        s.flush()
        # cal_1: two current-rubric analyses (newest wins) and a NEWER old-rubric one that must be ignored
        s.add(_analysis("cal_1", 60, RUBRIC["video"]))
        s.add(_analysis("cal_1", 70, RUBRIC["video"], fmt="animacion"))
        s.add(_analysis("cal_1", 99, "v1"))
        # cal_2: only an old-rubric analysis -> no AI score available
        s.add(_analysis("cal_2", 80, "v1"))
        # cal_3: static analysis with the static rubric
        s.add(Analysis(ad_id="cal_3", model="claude-x", rubric_version=RUBRIC["static"], analysis_type="static", creative_score=55,
                       result_json=json.dumps({"stop_power": {"score": 14}, "message_clarity": {"score": 14},
                                               "proof": {"score": 13}, "offer_cta": {"score": 14}, "visual_style": "meme"})))
        # cal_4: a forensic transcript row must never be mistaken for an evaluation
        s.add(Analysis(ad_id="cal_4", model="faster-whisper", rubric_version=RUBRIC["video"], analysis_type="forensics",
                       creative_score=None, result_json=json.dumps({"full_transcript": "hola"})))
        s.commit()

    # analysis.json files on disk say something different: they must be ignored
    ad_dir = tmp_path / "downloads" / "X" / "cal_1"
    ad_dir.mkdir(parents=True)
    (ad_dir / "analysis.json").write_text(json.dumps({"creative_score": 5, "hook": {"score": 1}}), encoding="utf-8")
    monkeypatch.setattr(calibrate, "MANIFEST_PATH", tmp_path / "no_manifest.csv")
    return tmp_path


def test_load_ai_evaluation_uses_newest_analysis_of_the_current_rubric(seeded):
    with SessionLocal() as db:
        cal_1 = calibrate.load_ai_evaluation("cal_1", db)
        assert cal_1["total"] == 70.0 and cal_1["format"] == "animacion"  # not 99 (old rubric), not 5 (analysis.json)
        assert cal_1["hook"] == 17.0

        assert calibrate.load_ai_evaluation("cal_2", db) is None  # old rubric only
        assert calibrate.load_ai_evaluation("cal_4", db) is None  # forensics row
        assert calibrate.load_ai_evaluation("missing", db) is None

        cal_3 = calibrate.load_ai_evaluation("cal_3", db)
        assert cal_3["total"] == 55.0 and cal_3["hook"] == 14.0 and cal_3["format"] == "meme"


def test_report_ignores_ads_without_a_current_db_evaluation(seeded):
    csv_path = seeded / "manual_scores.csv"
    csv_path.write_text(
        "ad_id,total_score,hook,mechanism,narrative,offer,source\n"
        "cal_1,72,,,,,owner\ncal_2,50,,,,,owner\ncal_3,60,,,,,owner\ncal_4,40,,,,,owner\nghost,10,,,,,placeholder\n",
        encoding="utf-8",
    )
    report = calibrate.run_calibration_report(csv_path)
    assert report["status"] == "success"
    assert report["count"] == 2  # cal_1 and cal_3 only
    assert report["total"]["mae"] == pytest.approx(((72 - 70) + (60 - 55)) / 2)


def test_no_analysis_json_fallback_remains():
    source = Path(calibrate.__file__).read_text(encoding="utf-8")
    assert "analysis.json" in source  # only in the docstring explaining it is ignored
    assert "glob(" not in source and "DOWNLOADS_DIR" not in source
