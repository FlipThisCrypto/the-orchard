from orchard_chia.datalayer import schema

def test_season_score_bounds():
    assert schema.season_score(0) == 0
    assert schema.season_score(24) == 100
    assert 0 <= schema.season_score(12) <= 100
    # round half up: 1/24 -> (100+12)//24 = 4
    assert schema.season_score(1) == 4

def test_verified_hours_empty():
    assert schema.verified_hours({}, "02" + "ab"*32) == 0
