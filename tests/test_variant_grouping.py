"""
Tests for Phase 5 variant grouping (src/media/fingerprint.py).
All local: no Gemini, no Claude, no network.
"""

import sqlite3
from pathlib import Path

import pytest

from src.media.fingerprint import (
    build_creative_hash,
    compute_copy_hash,
    group_creative_variants,
    normalize_copy,
)
from src.scoring.market import load_scoring_config

PHASH_MAX = load_scoring_config()["fingerprint"]["phash_max_distance"]
REAL_DB = Path(__file__).resolve().parent.parent / "data" / "meta_ads.db"

# 64-bit pHash strings: A and A_NEAR differ in 2 bits, A_FAR differs in 32 bits
PHASH_A = "f0f0f0f0f0f0f0f0"
PHASH_A_NEAR = "f0f0f0f0f0f0f0f3"
PHASH_A_FAR = "f0f00f0ff0f00f0f"

# Synthetic fixture mirroring the 8 real "eight alarms" ads: same body, 4 different pages,
# 2 rotating headlines and no media on disk. Some copies add punctuation/spacing noise.
ALARMS_BODY = (
    '"They turned off all eight of my alarms so I\'d miss the SAT."💔\n'
    'My four genius friends called it "protecting" me.\n'
    "They scored 1530+. I scored 1300, so I studied through a fever just to catch up."
)
ALARMS_ADS = [
    ("1091178577133773", "Unfold Tales", "Read the FULL Version👉👉", ALARMS_BODY),
    ("1418731587131462", "Unfold Tales", "Read the FULL Version👉👉", ALARMS_BODY),
    ("1356935846517403", "Unfold Tales", "Read the FULL Version👉👉", ALARMS_BODY.upper()),
    ("1133478362773607", "Unfold Tales", "Read the FULL Version👉👉", ALARMS_BODY),
    ("1440784577979883", "Rom-Com Reads", "😍Read the next chapters👉", ALARMS_BODY.replace(" ", "   ")),
    ("1126203126622915", "Rom-Com Reads", "😍Read the next chapters👉", ALARMS_BODY),
    ("1541028611465010", "FrostRead", "Read the FULL Version👉👉", ALARMS_BODY + "!!"),
    ("1042243218873513", "The Comeback Queen", "😍Read the next chapters👉", ALARMS_BODY),
]


def _ad(ad_id, sha=None, phash=None, copy=None):
    return {"id": ad_id, "media_sha256": sha, "phash": phash, "copy_hash": compute_copy_hash(copy) if copy else None}


def test_phash_fixture_distances():
    from src.media.fingerprint import phash_distance

    assert phash_distance(PHASH_A, PHASH_A_NEAR) <= PHASH_MAX
    assert phash_distance(PHASH_A, PHASH_A_FAR) > PHASH_MAX


def test_normalize_copy_strips_case_punctuation_and_spaces():
    assert normalize_copy("  ¡Hola,   MUNDO!! 👉 ") == "hola mundo"
    assert normalize_copy("Automatiza_tus chats.") == "automatiza tus chats"
    assert normalize_copy(None) == ""
    assert compute_copy_hash("Vende MÁS!") == compute_copy_hash("vende   más")
    assert compute_copy_hash("") is None


def test_build_creative_hash_combines_all_parts():
    base = build_creative_hash(PHASH_A, "abc", "copy1")
    assert base == build_creative_hash(PHASH_A, "abc", "copy1")
    assert base != build_creative_hash(PHASH_A_NEAR, "abc", "copy1")
    assert base != build_creative_hash(PHASH_A, "abd", "copy1")
    assert base != build_creative_hash(PHASH_A, "abc", "copy2")


def test_acceptance_eight_alarms_ads_are_one_creative_with_8_variants():
    """Plan criterion (synthetic fixture): 8 ads with the same copy -> 1 creative, 8 variants."""
    ads = [_ad(ad_id, copy=body) for ad_id, _page, _headline, body in ALARMS_ADS]
    ads.append(_ad("other_1", copy="Un copy totalmente distinto"))
    groups = group_creative_variants(ads, PHASH_MAX)

    alarm_groups = {groups[a[0]]["creative_group_id"] for a in ALARMS_ADS}
    assert len(alarm_groups) == 1
    assert all(groups[a[0]]["variant_count"] == 8 for a in ALARMS_ADS)
    assert groups["other_1"]["variant_count"] == 1
    assert groups["other_1"]["creative_group_id"] not in alarm_groups


@pytest.mark.skipif(not REAL_DB.exists(), reason="data/meta_ads.db no disponible")
def test_acceptance_eight_alarms_ads_from_real_db_read_only():
    """Same criterion over the real rows (read-only connection; ads have no media on disk)."""
    conn = sqlite3.connect(f"file:{REAL_DB.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT id, body_text, headline, "
                "(SELECT COUNT(*) FROM media WHERE ad_id = ads.id) AS n_media FROM ads "
                "WHERE body_text LIKE '%They turned off all eight of my alarms%'"
            ).fetchall()
        except sqlite3.OperationalError:
            pytest.skip("La base real aún no está en el esquema de la Fase 6")
    finally:
        conn.close()
    if len(rows) != 8:
        pytest.skip(f"La base real tiene {len(rows)} anuncios 'eight alarms', no 8")
    assert all(r[3] == 0 for r in rows)  # no media on disk for these ads
    ads = [{"id": r[0], "media_sha256": None, "phash": None, "copy_hash": compute_copy_hash(r[1], r[2])} for r in rows]
    groups = group_creative_variants(ads, PHASH_MAX)
    assert len({g["creative_group_id"] for g in groups.values()}) == 1
    assert {g["variant_count"] for g in groups.values()} == {8}


def test_same_sha256_groups_even_with_different_copy():
    groups = group_creative_variants(
        [
            _ad("v1", sha="same_sha", phash=PHASH_A, copy="Copy uno"),
            _ad("v2", sha="same_sha", phash=PHASH_A_FAR, copy="Copy dos distinto"),
        ],
        PHASH_MAX,
    )
    assert groups["v1"]["creative_group_id"] == groups["v2"]["creative_group_id"]
    assert groups["v1"]["variant_count"] == 2


def test_close_phash_and_same_copy_groups():
    groups = group_creative_variants(
        [
            _ad("p1", sha="sha1", phash=PHASH_A, copy="Agenda citas por WhatsApp"),
            _ad("p2", sha="sha2", phash=PHASH_A_NEAR, copy="agenda citas por whatsapp!"),
        ],
        PHASH_MAX,
    )
    assert groups["p1"]["creative_group_id"] == groups["p2"]["creative_group_id"]


def test_close_phash_with_different_copy_is_not_grouped():
    groups = group_creative_variants(
        [
            _ad("c1", sha="sha1", phash=PHASH_A, copy="Agenda citas por WhatsApp"),
            _ad("c2", sha="sha2", phash=PHASH_A_NEAR, copy="CRM para inmobiliarias"),
        ],
        PHASH_MAX,
    )
    assert groups["c1"]["creative_group_id"] != groups["c2"]["creative_group_id"]
    assert groups["c1"]["variant_count"] == 1


def test_same_copy_with_far_phash_is_not_grouped():
    groups = group_creative_variants(
        [
            _ad("f1", sha="sha1", phash=PHASH_A, copy="Agenda citas por WhatsApp"),
            _ad("f2", sha="sha2", phash=PHASH_A_FAR, copy="Agenda citas por WhatsApp"),
        ],
        PHASH_MAX,
    )
    assert groups["f1"]["creative_group_id"] != groups["f2"]["creative_group_id"]


def test_same_copy_one_with_media_one_without_is_not_grouped():
    groups = group_creative_variants(
        [
            _ad("m1", sha="sha1", phash=PHASH_A, copy="Agenda citas por WhatsApp"),
            _ad("m2", copy="Agenda citas por WhatsApp"),
        ],
        PHASH_MAX,
    )
    assert groups["m1"]["creative_group_id"] != groups["m2"]["creative_group_id"]


def test_grouping_is_transitive_and_ids_are_stable():
    ads = [
        _ad("t1", sha="shaX", phash=PHASH_A, copy="Copy A"),
        _ad("t2", sha="shaX", phash=PHASH_A_FAR, copy="Copy B"),
        _ad("t3", sha="shaY", phash=PHASH_A_FAR, copy="Copy B"),
    ]
    g1 = group_creative_variants(ads, PHASH_MAX)
    g2 = group_creative_variants(list(reversed(ads)), PHASH_MAX)
    assert len({g["creative_group_id"] for g in g1.values()}) == 1
    assert g1["t1"]["variant_count"] == 3
    assert g1 == g2
