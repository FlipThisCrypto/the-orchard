# SPDX-License-Identifier: Apache-2.0
"""Hosted browser claim page (HANDOVER T9 / ADR-0005).

Serves a self-contained page at ``GET /claim`` that lets an operator connect a
Chia wallet (WalletConnect — Sage/Goby), prove Orchard Pass ownership, and
claim a Tree by its claim code — all against THIS oracle, same-origin, so no
CORS is needed. The wallet-auth (``/auth/challenge`` + ``/auth/verify``) and
binding (``/provision/claim``) endpoints already exist; this is just the UI
that drives them, plus a tiny config endpoint exposing the (public)
WalletConnect project id to the browser.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config import settings

router = APIRouter(tags=["claim-page"])

# oracle/app/routes/claim_page.py -> oracle/app/claim_page.html
_PAGE_PATH = Path(__file__).resolve().parent.parent / "claim_page.html"

_LOGO = (
    "https://defiant-black-skink.myfilebase.com/ipfs/"
    "QmUWhqeByfKrVAa5Ev3MRymFmhMSoTMnzDwE3Gjd4Cvray"
)


@router.get("/claim", response_class=HTMLResponse)
def claim_page() -> HTMLResponse:
    """The claim UI. Static HTML; all dynamic bits come from /claim/config
    and the existing /auth + /provision endpoints (same-origin)."""
    return HTMLResponse(_PAGE_PATH.read_text(encoding="utf-8"))


@router.get("/claim/config")
def claim_config(request: Request) -> dict:
    """WalletConnect project id (a PUBLIC client id, not a secret) + dApp
    metadata for the browser. ``url`` is derived from the request origin so
    the WalletConnect modal shows the right site regardless of where the
    oracle is hosted. ``wc_configured`` is False when the operator hasn't set
    ORCHARD_ORACLE_WC_PROJECT_ID — the page then renders a helpful notice
    instead of a half-working Connect button."""
    s = settings()
    pid = s.wc_project_id or ""
    base = str(request.base_url).rstrip("/")
    return {
        "wc_project_id": pid,
        "wc_configured": bool(pid),
        # Where the page sends operators (set via oracle .env; no code change).
        "home_url": s.home_url or "",
        "dashboard_url": s.dashboard_url or "",
        "metadata": {
            "name": "The Orchard — Claim a Tree",
            "description": "Bind a Tree to your wallet by proving Orchard Pass ownership.",
            "url": base,
            "icons": [_LOGO],
        },
    }
