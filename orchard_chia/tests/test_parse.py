from orchard_chia.datalayer.parse import parse_hour, parse_season
import pytest

def test_parse_season_ok():
    assert parse_season("12") == 12
    assert parse_season(1) == 1

def test_parse_season_bad():
    with pytest.raises(ValueError):
        parse_season(0)
    with pytest.raises(ValueError):
        parse_season(True)

def test_parse_hour():
    assert parse_hour(0) == 0
    assert parse_hour(23) == 23
    with pytest.raises(ValueError):
        parse_hour(24)
