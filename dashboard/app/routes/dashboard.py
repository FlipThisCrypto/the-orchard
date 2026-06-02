# SPDX-License-Identifier: Apache-2.0
"""Home + nodes-list pages."""
from __future__ import annotations

from flask import Blueprint, render_template

from .. import oracle_client
from ..config import settings

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    """Home page.

    Private mode (operator on their own PC): per-Tree table, "Plant a
    Tree" CTA when empty. The operator sees their own fleet.

    Public mode (tunneled to the world): aggregate network stats
    card instead — Trees online, Readings in the last 24h, Attestations
    on chain — with no per-Tree details. The list_nodes call is still
    made for the count fallback, but the wallet/GPS fields it carries
    have already been scrubbed by the oracle's public-mode awareness
    further down the stack.
    """
    is_public = settings().public_mode
    oracle_ok = False
    oracle_info: dict = {}
    nodes: list[dict] = []
    stats: dict | None = None
    error: str | None = None
    try:
        oracle_info = oracle_client.root()
        oracle_ok = True
        if is_public:
            # Public-mode home page is one card sourced from the
            # aggregate endpoint. We skip list_nodes entirely — the
            # public visitor has no business iterating over Trees.
            stats = oracle_client.network_stats()
        else:
            nodes = oracle_client.list_nodes()
    except oracle_client.OracleError as e:
        error = str(e)
    return render_template(
        "index.html",
        oracle_ok=oracle_ok,
        oracle_info=oracle_info,
        nodes=nodes,
        stats=stats,
        public_mode=is_public,
        error=error,
    )
