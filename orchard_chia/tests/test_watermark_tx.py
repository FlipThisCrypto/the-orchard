from orchard_chia.datalayer.publish_watermark import PublishWatermark

def test_last_tx(tmp_path):
    with PublishWatermark(tmp_path / "w.db") as wm:
        assert wm.last_tx("AA"*16, 1, 0) is None
        wm.record(node_id="AA"*16, season=1, hour=0, tx_id="txid1")
        assert wm.last_tx("AA"*16, 1, 0) == "txid1"
