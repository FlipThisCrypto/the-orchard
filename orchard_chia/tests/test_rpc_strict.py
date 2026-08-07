from orchard_chia.datalayer.rpc import ChiaRpcError, DataLayerRpc
from orchard_chia.datalayer.retry import RetryPolicy
import pytest

def test_get_value_strict_raises(monkeypatch):
    dl = DataLayerRpc("127.0.0.1", 1, "c", "k", retry_policy=RetryPolicy(max_attempts=1))
    def boom(route, body):
        raise ChiaRpcError("unreachable")
    monkeypatch.setattr(dl, "_post", boom)
    with pytest.raises(ChiaRpcError):
        dl.get_value_strict("s", "aa")
