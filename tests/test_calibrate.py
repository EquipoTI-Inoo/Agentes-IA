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
