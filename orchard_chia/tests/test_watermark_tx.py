from orchard_chia.datalayer.publish_watermark import PublishWatermark

def test_last_tx(tmp_path):
    with PublishWatermark(tmp_path / "w.db") as wm:
        assert wm.last_tx("AA"*16, 1, 0) is None
        wm.record(node_id="AA"*16, season=1, hour=0, tx_id="txid1")
        assert wm.last_tx("AA"*16, 1, 0) == "txid1"


def test_busy_timeout_pragma_set(tmp_path):
    with PublishWatermark(tmp_path / "w.db", busy_timeout_ms=7000) as wm:
        (val,) = wm._conn.execute("PRAGMA busy_timeout").fetchone()
        assert val == 7000


def test_default_busy_timeout(tmp_path):
    with PublishWatermark(tmp_path / "w.db") as wm:
        (val,) = wm._conn.execute("PRAGMA busy_timeout").fetchone()
        assert val == 5000
