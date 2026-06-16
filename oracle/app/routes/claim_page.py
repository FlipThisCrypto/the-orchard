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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .. import pass_verify
from ..config import settings

router = APIRouter(tags=["claim-page"])

# The Orchard Genesis Passes collection on MintGarden — where to send a
# wallet that doesn't hold one yet.
_BUY_URL = (
    "https://mintgarden.io/collections/"
    "col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29"
)

# oracle/app/routes/claim_page.py -> oracle/app/claim_page.html
_PAGE_PATH = Path(__file__).resolve().parent.parent / "claim_page.html"
_WIDGET_PATH = Path(__file__).resolve().parent.parent / "connect_widget.js"

_LOGO = (
    "https://defiant-black-skink.myfilebase.com/ipfs/"
    "QmUWhqeByfKrVAa5Ev3MRymFmhMSoTMnzDwE3Gjd4Cvray"
)


@router.get("/claim", response_class=HTMLResponse)
def claim_page() -> HTMLResponse:
    """The claim UI. Static HTML; all dynamic bits come from /claim/config
    and the existing /auth + /provision endpoints (same-origin)."""
    return HTMLResponse(_PAGE_PATH.read_text(encoding="utf-8"))


@router.get("/connect.js")
def connect_widget() -> Response:
    """The shared 'Connect Wallet' widget, included by every Orchard page
    (landing, claim, future dashboard) so one connection flows across the
    subdomains via a .theorchard.network cookie. Short cache so updates
    propagate quickly during active development."""
    return Response(
        content=_WIDGET_PATH.read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=60"},
    )


@router.get("/claim/pass/{address}")
def claim_pass_check(address: str) -> dict:
    """Public, read-only Orchard Pass check used by the flasher to gate
    flashing on Pass ownership BEFORE an ESP32 is written — no point flashing
    a board you can't claim. Mounted under /claim so it rides the same edge
    allowlist as the rest of the claim flow (no new WAF path). No auth: a
    wallet address is public, and this only reads the on-chain Pass ownership
    the operator is about to prove at claim time anyway. Cached upstream in
    pass_verify, so a retry storm doesn't hammer the indexer."""
    address = (address or "").strip()
    if not address.startswith("xch1") or len(address) < 20:
        raise HTTPException(status_code=400, detail="expected an xch1... address")
    try:
        passes = pass_verify.list_passes_for_address(address)
    except pass_verify.PassVerifyError as e:
        raise HTTPException(status_code=502, detail=f"indexer error: {e}") from e
    if not passes:
        return {
            "address": address,
            "has_pass": False,
            "pass_nft_id": None,
            "pass_name": None,
            "edition_number": None,
            "owner_count": 0,
            "buy_url": _BUY_URL,
        }
    first = passes[0]
    nft_id = first.get("nft_coin_id") or first.get("launcher_id")
    return {
        "address": address,
        "has_pass": True,
        "pass_nft_id": nft_id,
        "pass_name": first.get("name"),
        "edition_number": first.get("edition_number"),
        "owner_count": len(passes),
        "mintgarden_url": f"https://mintgarden.io/nfts/{nft_id}" if nft_id else None,
        "buy_url": _BUY_URL,
    }


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
