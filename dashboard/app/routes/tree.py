# SPDX-License-Identifier: Apache-2.0
"""Live-view page for a single Tree.

The HTML is mostly static — actual data is fetched by JS polling the
`/api/tree/<node_id>/latest` endpoint on a short interval.
"""
from __future__ import annotations

from flask import Blueprint, abort, render_template

from .. import oracle_client
from ..config import settings

bp = Blueprint("tree", __name__)


def _read_datalayer_store_id() -> str:
    """Pull the operator's DataLayer store id from orchard_chia/config.yaml
    so the tree page's "On chain" card can render a copy-pasteable
    `chia data get_value --id <store_id> --key <key_hex>` command.

    Cheap to read on every render — the file is tiny and YAML parsing
    on a few-KB file is sub-millisecond. We don't cache because the
    operator might rotate stores between page loads and we'd rather
    pick up the change than serve a stale id.
    """
    try:
        from orchard_chia.datalayer import config as dl_config
        cfg = dl_config.load()
        return cfg.data_layer.store_id or ""
    except Exception:
        # config.yaml missing / unparseable / orchard_chia not importable.
        # The dashboard works fine without the verify command — the
        # On-chain card will show "<store_id>" as a placeholder.
        return ""


@bp.route("/tree/<node_id>")
def tree_page(node_id: str):
    try:
        node = oracle_client.get_node(node_id.upper())
    except oracle_client.OracleError:
        # Oracle down / non-JSON — treat as not found rather than 500.
        abort(404)
    if node is None:
        abort(404)
    # Don't expose the operator's DataLayer store id on the public tree
    # page — it's a per-operator identifier. Owners see it in private mode.
    store_id = "" if settings().public_mode else _read_datalayer_store_id()
    return render_template(
        "tree.html",
        node=node,
        datalayer_store_id=store_id,
    )
