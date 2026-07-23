# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from orchard_chia.cli import orchard_verify as cli

VPATH = Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"


def test_cli_vectors_json(capsys):
    rc = cli.main(["vectors", str(VPATH), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"valid": true' in out or '"valid":true' in out
    assert '"result": "VALID"' in out


def test_json_result_label_tri_state(capsys):
    # A report that is invalid but flagged CANNOT-VERIFY must surface that in
    # the JSON `result`, not just `valid: false`.
    from orchard_chia.datalayer import verify
    rep = verify.Report(
        node_id="N", season=1,
        checks=[verify.Check("x", False, "rpc down")],
    )
    cli._print_report(rep, as_json=True, result_label="CANNOT-VERIFY")
    out = capsys.readouterr().out
    assert '"result": "CANNOT-VERIFY"' in out
    assert '"valid": false' in out
