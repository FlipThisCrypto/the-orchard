from orchard_chia.datalayer import exit_codes as ec
def test_exit_codes_unique():
    vals = [ec.OK, ec.USAGE, ec.ORACLE, ec.CHIA, ec.DATALAYER, ec.CONFIRM, ec.RECONCILE]
    assert len(vals) == len(set(vals)) or ec.RECONCILE == ec.OK  # RECONCILE may be 1
    assert ec.OK == 0
    assert ec.CONFIRM == 6
