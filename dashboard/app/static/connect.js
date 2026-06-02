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
//
// -------- WalletConnect v2.17.0 ping-watchdog polyfill ---------------
//
// The SDK's relay client fires a setTimeout to "force-close stale
// connections" by calling `socket.terminate()`. That method ships on
// Node's `ws` library but not on browser-native WebSocket — browsers
// only have `.close()`. Without this shim every successful Connect
// flow leaves a scary
//   "Uncaught TypeError: s.terminate is not a function"
// in the console after auth completes. Functionally harmless (the
// auth/verify POST has already returned by the time the watchdog
// fires), but distracting.
//
// We only install the alias if the property is missing, so this is a
// no-op in a future where either the browser adds `terminate` natively
// or the WC SDK switches to `close()` directly.
if (typeof WebSocket !== "undefined" && !WebSocket.prototype.terminate) {
  WebSocket.prototype.terminate = function terminate() {
    try { this.close(); } catch { /* already closing */ }
  };
}

const Orchard = (() => {

  // localStorage key. Shared across all tabs of the same origin so
  // connecting in one tab updates the others (the per-tab
  // sessionStorage we used first broke the wizard when operators
  // had the Plant-a-Tree page open in a separate tab from where
  // they clicked Connect Wallet). Security boundary is enforced by
  // the JWT's exp claim + the oracle's expiry check, not by
  // storage lifetime.
  const TOKEN_KEY = "orchard.session";

  // Loaded once.
  let signClient = null;
  let wcModal    = null;
  let wcConfig   = null;
  let wcSession  = null;     // the active WC session (post-approve)

  // Cached so other modules can ask "am I logged in" without
  // re-parsing localStorage every call. Refreshed on every
  // write/clear here, and on cross-tab `storage` events below.
  let memoSession = readSession();

  // -------------------------- helpers --------------------------------

  function readSession() {
    try {
      const raw = localStorage.getItem(TOKEN_KEY);
      if (!raw) return null;
      const j = JSON.parse(raw);
      if (!j || !j.token || !j.expires_at) return null;
      // Drop if expired client-side too — the oracle would 401 anyway,
      // but we avoid attaching a stale token to every API call.
      if (j.expires_at * 1000 < Date.now()) {
        localStorage.removeItem(TOKEN_KEY);
        return null;
      }
      return j;
    } catch { return null; }
  }
  function writeSession(j) {
    localStorage.setItem(TOKEN_KEY, JSON.stringify(j));
    memoSession = j;
    renderConnectArea();
    // Other pages (e.g. the Plant-a-Tree wizard's wallet-status
    // panel) want to react when the operator connects or disconnects
    // mid-flow. A bubbling CustomEvent on window is the simplest
    // pub/sub here — no shared dependency, no polling.
    window.dispatchEvent(new CustomEvent("orchard:session", { detail: j }));
  }
  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    memoSession = null;
    renderConnectArea();
    window.dispatchEvent(new CustomEvent("orchard:session", { detail: null }));
  }
  function getSession() { return memoSession; }

  // Cross-tab sync: the browser fires a `storage` event on every
  // OTHER tab of the same origin when localStorage changes. (The
  // writing tab doesn't get it — that's why writeSession/clearSession
  // dispatch the orchard:session CustomEvent locally too.) Without
  // this listener, connecting in tab A would leave tab B's nav and
  // any wizard panel stuck on "Connect Wallet" until manual reload.
  window.addEventListener("storage", (e) => {
    if (e.key !== TOKEN_KEY) return;
    memoSession = readSession();
    renderConnectArea();
    window.dispatchEvent(new CustomEvent("orchard:session", { detail: memoSession }));
  });

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

  async function loadDeps() {
    // esm.sh hosts pre-bundled WalletConnect v2 with deps resolved.
    // Pinned to a v2.x major; minor bumps are SDK-compat.
    const [signMod, modalMod] = await Promise.all([
      import("https://esm.sh/@walletconnect/sign-client@2.17.0?bundle"),
      import("https://esm.sh/@walletconnect/modal@2.7.0?bundle"),
    ]);
    return {
      SignClient:           signMod.SignClient,
      WalletConnectModal:   modalMod.WalletConnectModal,
    };
  }

  async function ensureClients() {
    if (signClient && wcModal) return { signClient, wcModal };
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
    const { SignClient, WalletConnectModal } = await loadDeps();
    if (!signClient) {
      signClient = await SignClient.init({
        projectId: wcConfig.wc_project_id,
        metadata:  wcConfig.metadata,
      });
    }
    if (!wcModal) {
      // The modal handles QR rendering (mobile wallets), copy-URI
      // button, and a wallet picker. Chia chain ids tell it which
      // wallets to surface — wallets registered as Chia-capable in
      // the WalletConnect explorer get featured automatically.
      wcModal = new WalletConnectModal({
        projectId:        wcConfig.wc_project_id,
        chains:           ["chia:mainnet"],
        themeMode:        "dark",
        themeVariables: {
          // Match the Orchard brand palette so the modal doesn't feel
          // like a foreign popup.
          "--wcm-z-index":              "2000",
          "--wcm-accent-color":         "#ffae33",
          "--wcm-background-color":     "#0e1124",
          "--wcm-font-family":          "system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif",
        },
      });
    }
    return { signClient, wcModal };
  }

  async function connect() {
    const { signClient: client, wcModal: modal } = await ensureClients();

    // Request a Chia mainnet session with two methods:
    //   chia_getAddress            — fetch the operator's current xch1
    //                                receive address from the wallet
    //                                (per CHIP-22, the account in the
    //                                CAIP-10 identifier is the wallet's
    //                                master-key fingerprint, not a
    //                                specific address; the wallet
    //                                derives many addresses from one
    //                                master key — so we have to ask
    //                                for the address explicitly).
    //                                Method name confirmed against the
    //                                Sage source at
    //                                xch-dev/sage src/walletconnect/
    //                                commands.ts — Sage exposes this
    //                                (NOT chia_getCurrentAddress, which
    //                                is the older Chia-reference name).
    //   chia_signMessageByAddress  — sign the challenge with that
    //                                specific address.
    // No spend capability requested — least privilege.
    const { uri, approval } = await client.connect({
      requiredNamespaces: {
        chia: {
          chains:  ["chia:mainnet"],
          methods: [
            "chia_getAddress",
            "chia_signMessageByAddress",
          ],
          events:  [],
        },
      },
    });

    // Show the official WalletConnect modal: QR for mobile, copy-URI
    // button, and a wallet picker for any registered Chia wallets.
    // Modal lives in shadow DOM, so its DOM doesn't collide with the
    // dashboard's CSS.
    let session;
    try {
      await modal.openModal({ uri });
      // Wait for the wallet user to approve in their app. If the
      // operator dismisses the modal, the approval Promise will hang
      // until they retry — fine for v1.
      session = await approval();
    } catch (e) {
      modal.closeModal();
      throw e;
    } finally {
      // Always close the modal once approval resolves or rejects so
      // we're not leaving a stale QR on screen.
      modal.closeModal();
    }
    wcSession = session;
    console.log("[orchard.connect] session approved. namespaces:",
                session.namespaces);

    // CAIP-10 format per CHIP-22 is "chia:mainnet:<fingerprint>".
    // The fingerprint identifies the wallet (master key); a wallet
    // has many xch1 addresses derived from one master. We need a
    // specific xch1 to bind to the Tree.
    const accounts = session.namespaces.chia?.accounts || [];
    if (accounts.length === 0) {
      throw new Error("Wallet did not return any Chia accounts");
    }
    const fingerprintStr = accounts[0].split(":").slice(-1)[0];
    const fingerprint = parseInt(fingerprintStr, 10);
    if (!Number.isFinite(fingerprint)) {
      throw new Error(
        `Wallet returned an unexpected account format: ${JSON.stringify(accounts)}. ` +
        `Expected "chia:mainnet:<fingerprint>".`
      );
    }
    console.log("[orchard.connect] wallet fingerprint:", fingerprint);

    // Ask the wallet for the operator's current receive address.
    // Both Sage and Goby implement chia_getAddress with empty params
    // and return { address: "xch1..." } per the Sage commands.ts
    // schema. No spend prompt — Sage marks this method as
    // confirm: false, so it returns instantly.
    console.log("[orchard.connect] requesting chia_getAddress…");
    let getAddrResult;
    try {
      getAddrResult = await client.request({
        topic:   session.topic,
        chainId: "chia:mainnet",
        request: { method: "chia_getAddress", params: {} },
      });
    } catch (e) {
      console.error("[orchard.connect] chia_getAddress failed:", e);
      throw new Error(
        `Wallet refused to share its address: ${e?.message || e}. ` +
        `If you're on an older Chia reference wallet, please use Sage ` +
        `or Goby — chia_getAddress is the CHIP-22 method we use.`);
    }
    console.log("[orchard.connect] chia_getAddress result:", getAddrResult);

    const address = (typeof getAddrResult === "string")
      ? getAddrResult
      : (getAddrResult?.address || getAddrResult?.data?.address);
    if (typeof address !== "string" ||
        !/^xch1[0-9a-z]{50,80}$/.test(address)) {
      throw new Error(
        `Wallet returned an unexpected getAddress response shape: ` +
        `${JSON.stringify(getAddrResult)}. ` +
        `Expected {address: "xch1..."}.`);
    }
    console.log("[orchard.connect] address for signing:", address);

    // Get a challenge from the oracle.
    const ch = await jpost("/api/auth/challenge", {});
    if (!ch.ok) throw new Error(ch.body.error || "challenge failed");
    const nonce   = ch.body.nonce;
    const message = ch.body.message;

    // Ask the wallet to sign the message.
    console.log("[orchard.connect] requesting signature for address:",
                address);
    let signResult;
    try {
      signResult = await client.request({
        topic:   session.topic,
        chainId: "chia:mainnet",
        request: {
          method: "chia_signMessageByAddress",
          params: { address, message },
        },
      });
    } catch (e) {
      // Most useful failure mode to surface — wallet may have rejected
      // because it doesn't recognize the address format we sent.
      console.error("[orchard.connect] wallet rejected sign request:", e);
      throw new Error(
        `Wallet refused to sign: ${e?.message || e}. ` +
        `Check the browser console for the candidate account format.`);
    }
    console.log("[orchard.connect] sign result:", signResult);
    // CHIP-22 returns { publicKey, signature } (hex strings).
    const publicKey = signResult.publicKey || signResult.public_key;
    const signature = signResult.signature;
    if (!publicKey || !signature) {
      throw new Error(
        `Wallet returned an unexpected sign response shape: ` +
        `${JSON.stringify(signResult)}. Expected {publicKey, signature}.`);
    }

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

// Make Orchard accessible as window.Orchard for cross-script
// consumers that defensively guard with `window.Orchard && ...`
// (app.js's wizard does this so it doesn't blow up on pages that
// happen not to load connect.js). Top-level `const` in a non-module
// script enters the global lexical environment but does NOT attach
// to the global object — so without this line, `window.Orchard` is
// undefined even though `Orchard` is accessible by bare name. That
// mismatch was the bug that left the wizard's Step 2 stuck on
// "Connect your wallet first" while the nav correctly showed
// "Connected as xch1…".
window.Orchard = Orchard;
