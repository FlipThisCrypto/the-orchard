from orchard_chia.datalayer import metrics

def test_metrics_empty_sensors():
    assert metrics.metrics_from_sensors(None) == {}
    assert metrics.metrics_from_sensors({}) == {}
    assert metrics.metrics_from_sensors("bad") == {}
