# SPDX-License-Identifier: Apache-2.0
from pathlib import Path
from orchard_chia.cli import orchard_verify as cli

VPATH = Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"


def test_cli_vectors_json(capsys):
    rc = cli.main(["vectors", str(VPATH), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"valid": true' in out or '"valid":true' in out
