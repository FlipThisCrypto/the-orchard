// SPDX-License-Identifier: Apache-2.0
//
// Phase 6.6 — Connect Wallet (browser side).
//
// Talks to Sage + Goby via WalletConnect v2 (CHIP-22). The flow:
//
//   1. Fetch GET /api/auth/config       — get project_id + dApp metadata
//   2. Lazy-init the WC SignClient (only on first Connect click)
//   3. SignClient.connect({...}) opens the modal; user picks a wallet
//   4. Wallet returns an approved session
//   5. Fetch POST /api/auth/challenge   — get { nonce, message }
//   6. SignClient.request(chia_signMessageByAddress, {address, message})
//   7. Wallet shows the operator the message; they tap Approve
//   8. POST /api/auth/verify with {address, public_key, signature, nonce}
//   9. Oracle verifies BLS + pk-binding; returns { session_token, ... }
//  10. We store token in sessionStorage; nav updates to "Connected as xch1…"
//
// On Disconnect, we drop the session token and call SignClient.disconnect.
//
// When ORCHARD_VIEW_WC_PROJECT_ID is unset the page renders an
// instructional "WalletConnect not configured" state and the button is
// disabled — no half-working state.

const Orchard = (() => {

  // sessionStorage key. Survives tab reloads but not new windows.
  const TOKEN_KEY = "orchard.session";

  // Loaded once.
  let signClient = null;
  let wcConfig   = null;
  let wcSession  = null;     // the active WC session (post-approve)

  // Cached so other modules can ask "am I logged in" without
  // re-parsing sessionStorage every call.
  let memoSession = readSession();

  // -------------------------- helpers --------------------------------

  function readSession() {
    try {
      const raw = sessionStorage.getItem(TOKEN_KEY);
      if (!raw) return null;
      const j = JSON.parse(raw);
      if (!j || !j.token || !j.expires_at) return null;
      // Drop if expired client-side too — the oracle would 401 anyway,
      // but we avoid attaching a stale token to every API call.
      if (j.expires_at * 1000 < Date.now()) {
        sessionStorage.removeItem(TOKEN_KEY);
        return null;
      }
      return j;
    } catch { return null; }
  }
  function writeSession(j) {
    sessionStorage.setItem(TOKEN_KEY, JSON.stringify(j));
    memoSession = j;
    renderConnectArea();
  }
  function clearSession() {
    sessionStorage.removeItem(TOKEN_KEY);
    memoSession = null;
    renderConnectArea();
  }
  function getSession() { return memoSession; }

  function shorten(addr) {
    if (!addr) return "";
    return addr.length > 16 ? `${addr.slice(0, 10)}…${addr.slice(-6)}` : addr;
  }

  function $(sel, root = document) { return root.querySelector(sel); }

  // Small escape helper — duplicated from app.js so connect.js can
  // load standalone on pages that don't include app.js.
  function esc(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  // -------------------------- backend calls --------------------------

  async function jget(url) {
    const r = await fetch(url, { method: "GET" });
    return { ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) };
  }
  async function jpost(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) };
  }

  // Authenticated fetch wrapper — attaches Bearer if a session exists.
  // Exposed via Orchard.authFetch so the wizard + tree pages can use it.
  async function authFetch(url, opts = {}) {
    const s = getSession();
    const headers = new Headers(opts.headers || {});
    if (s) headers.set("Authorization", `Bearer ${s.token}`);
    if (opts.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(url, { ...opts, headers });
  }

  // -------------------------- WalletConnect --------------------------

  async function loadSignClient() {
    // esm.sh hosts pre-bundled WalletConnect v2 with deps resolved.
    // Pinned to v2.x major; minor bumps are SDK-compat.
    const mod = await import(
      "https://esm.sh/@walletconnect/sign-client@2.17.0?bundle"
    );
    return mod.SignClient;
  }

  async function ensureSignClient() {
    if (signClient) return signClient;
    if (!wcConfig) {
      const r = await jget("/api/auth/config");
      wcConfig = r.body;
    }
    if (!wcConfig.wc_configured) {
      throw new Error(
        "WalletConnect not configured. Set ORCHARD_VIEW_WC_PROJECT_ID " +
        "in dashboard/.env (get a free Project ID at " +
        "https://cloud.walletconnect.com)."
      );
    }
    const SignClient = await loadSignClient();
    signClient = await SignClient.init({
      projectId: wcConfig.wc_project_id,
      metadata:  wcConfig.metadata,
    });
    return signClient;
  }

  async function connect() {
    const client = await ensureSignClient();

    // Request a Chia mainnet session with the signMessageByAddress
    // permission only — least privilege, no spend capability.
    const { uri, approval } = await client.connect({
      requiredNamespaces: {
        chia: {
          chains:  ["chia:mainnet"],
          methods: ["chia_signMessageByAddress"],
          events:  [],
        },
      },
    });

    // For desktop browser wallets (Goby) the uri is consumed via
    // postMessage; for mobile wallets (Sage in iOS/Android) it's
    // rendered as a QR. WalletConnect's own modal lib handles both,
    // but pulling in the modal package is heavy — for v1 we show
    // the raw URI and let operators copy it into Sage's "Scan QR"
    // or Goby's "Connect via WC" flow. Modal lib comes later.
    showConnectUri(uri);

    // Wait for the wallet user to approve in their app.
    const session = await approval();
    wcSession = session;

    // The wallet might return one or more accounts. Take the first
    // chia:mainnet account.
    const accounts = session.namespaces.chia?.accounts || [];
    if (accounts.length === 0) {
      throw new Error("Wallet did not return any Chia accounts");
    }
    // Format is "chia:mainnet:xch1...".
    const address = accounts[0].split(":").slice(-1)[0];

    // Get a challenge from the oracle.
    const ch = await jpost("/api/auth/challenge", {});
    if (!ch.ok) throw new Error(ch.body.error || "challenge failed");
    const nonce   = ch.body.nonce;
    const message = ch.body.message;

    // Ask the wallet to sign the message.
    const signResult = await client.request({
      topic:   session.topic,
      chainId: "chia:mainnet",
      request: {
        method: "chia_signMessageByAddress",
        params: { address, message },
      },
    });
    // CHIP-22 returns { publicKey, signature } (hex strings).
    const publicKey = signResult.publicKey || signResult.public_key;
    const signature = signResult.signature;

    // Verify with the oracle.
    const ver = await jpost("/api/auth/verify", {
      address, public_key: publicKey, signature, nonce,
    });
    if (!ver.ok) {
      throw new Error(
        `verify failed: ${ver.body.detail || ver.body.error || ver.status}`);
    }

    writeSession({
      token:      ver.body.session_token,
      address:    ver.body.address,
      expires_at: ver.body.expires_at,
    });

    hideConnectUri();
    return ver.body;
  }

  async function disconnect() {
    try {
      if (wcSession && signClient) {
        await signClient.disconnect({
          topic: wcSession.topic,
          reason: { code: 6000, message: "user disconnected" },
        });
      }
    } catch {}
    wcSession = null;
    clearSession();
  }

  // -------------------------- UI ------------------------------------

  function showConnectUri(uri) {
    const out = $("#connect-uri");
    if (!out) return;
    out.innerHTML =
      `<div class="muted" style="margin-top:8px;font-size:13px">` +
        `Approve in your wallet. If your wallet asks for a connection ` +
        `URI:<br>` +
        `<code style="word-break:break-all;display:block;margin-top:4px;` +
          `padding:6px 8px;background:var(--bg-card-2);border-radius:4px;` +
          `font-size:11px">${esc(uri)}</code>` +
      `</div>`;
  }
  function hideConnectUri() {
    const out = $("#connect-uri");
    if (out) out.innerHTML = "";
  }

  function renderConnectArea() {
    const slot = $("#connect-slot");
    if (!slot) return;
    const s = getSession();
    if (s) {
      slot.innerHTML =
        `<span class="muted" style="font-size:13px;margin-right:8px">` +
          `Connected as <code>${esc(shorten(s.address))}</code></span>` +
        `<button id="connect-disconnect-btn" class="btn">Disconnect</button>`;
      $("#connect-disconnect-btn").addEventListener("click", () => disconnect());
    } else {
      slot.innerHTML =
        `<button id="connect-wallet-btn" class="btn primary">Connect Wallet</button>`;
      $("#connect-wallet-btn").addEventListener("click", async () => {
        const btn = $("#connect-wallet-btn");
        btn.disabled = true;
        btn.textContent = "Connecting…";
        try {
          await connect();
        } catch (e) {
          alert(`Connect failed: ${e.message}`);
        } finally {
          // If connect succeeded, the button is gone (replaced by
          // disconnect). If it failed, restore.
          if ($("#connect-wallet-btn")) {
            $("#connect-wallet-btn").disabled = false;
            $("#connect-wallet-btn").textContent = "Connect Wallet";
          }
        }
      });
    }
  }

  function initConnect() {
    renderConnectArea();
  }

  return {
    initConnect,
    authFetch,
    getSession,
    disconnect,
  };
})();
