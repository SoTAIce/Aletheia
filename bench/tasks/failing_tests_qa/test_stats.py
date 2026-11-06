import pytest

from stats import mean, median, mode, normalize, value_range


def test_mean_basic():
    assert mean([1, 2, 3, 4]) == 2.5


def test_mean_floats():
    assert mean([0.1, 0.2]) == 0.15


def test_mean_empty_raises_value_error():
    with pytest.raises(ValueError):
        mean([])


def test_median_odd():
    assert median([5, 1, 3]) == 3


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_mode_first_seen_wins_on_tie():
    assert mode([3, 1, 3, 1, 2]) == 3


def test_range_empty():
    with pytest.raises(ValueError):
        value_range([])


def test_range_negative():
    assert value_range([-4, 2, -1]) == 6


def test_normalize_bounds():
    assert normalize([2, 4, 6]) == [0.0, 0.5, 1.0]


def test_normalize_constant():
    assert normalize([7, 7]) == [0.0, 0.0]
