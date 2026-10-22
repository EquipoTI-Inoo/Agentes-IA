"""
Unit tests for Calibration calculations (Spearman rank correlation and MAE).
"""

import pytest
from scripts.calibrate import compute_mae, compute_spearman


def test_compute_spearman_perfect_positive():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [10.0, 20.0, 30.0, 40.0, 50.0]
    corr = compute_spearman(x, y)
    assert pytest.approx(corr, 0.001) == 1.0


def test_compute_spearman_perfect_negative():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [50.0, 40.0, 30.0, 20.0, 10.0]
    corr = compute_spearman(x, y)
    assert pytest.approx(corr, 0.001) == -1.0


def test_compute_spearman_ties_and_edge_cases():
    # Empty or single item
    assert compute_spearman([], []) == 0.0
    assert compute_spearman([1.0], [2.0]) == 0.0

    # Constant array (zero variance)
    assert compute_spearman([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) == 0.0


def test_compute_mae():
    x = [10.0, 20.0, 30.0]
    y = [12.0, 18.0, 35.0]
    # |10-12|=2, |20-18|=2, |30-35|=5 -> mean = 9/3 = 3.0
    assert pytest.approx(compute_mae(x, y), 0.001) == 3.0
    assert compute_mae([], []) == 0.0


def test_validate_and_map_external_entry():
    from scripts.calibrate import validate_and_map_external_entry

    mock_manifest = {
        1: {
            "n": 1,
            "ad_id": "1714051753080819",
            "competitor": "Leadsales",
            "duration_s": 40.5,
            "filename": "01_Leadsales_1714051753080819.mp4",
        },
        2: {
            "n": 2,
            "ad_id": "1605980027649675",
            "competitor": "ManyChat",
            "duration_s": 40.4,
            "filename": "02_ManyChat_1605980027649675.mp4",
        },
    }

    # Case 1: Exact match by video number with matching competitor and duration
    entry_ok = {"video_number": 1, "competitor": "Leadsales", "duration_s": 40.5, "score": 62}
    ad_id, warnings = validate_and_map_external_entry(entry_ok, mock_manifest)
    assert ad_id == "1714051753080819"
    assert len(warnings) == 0

    # Case 2: Match by video number but competitor differs
    entry_comp_diff = {"n": "Video 1", "competitor": "WrongBrand", "duration_s": 40.5}
    ad_id, warnings = validate_and_map_external_entry(entry_comp_diff, mock_manifest)
    assert ad_id == "1714051753080819"
    assert any("competidor externo 'WrongBrand' no coincide" in w for w in warnings)

    # Case 3: Match by video number but duration differs by > 1.5s
    entry_dur_diff = {"pestaña": "2", "competitor": "ManyChat", "duration_s": 95.0}
    ad_id, warnings = validate_and_map_external_entry(entry_dur_diff, mock_manifest)
    assert ad_id == "1605980027649675"
    assert any("duración externa 95.0s no coincide" in w for w in warnings)

    # Case 4: Video number not in manifest
    entry_unknown = {"n": 99}
    ad_id, warnings = validate_and_map_external_entry(entry_unknown, mock_manifest)
    assert ad_id is None
    assert any("Video #99 no encontrado" in w for w in warnings)

