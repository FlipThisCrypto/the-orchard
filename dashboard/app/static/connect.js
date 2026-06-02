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
  let wcModal    = null;
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
    //   chia_getCurrentAddress     — fetch the operator's current xch1
    //                                receive address from the wallet
    //                                (per CHIP-22, the account in the
    //                                CAIP-10 identifier is the wallet's
    //                                master-key fingerprint, not a
    //                                specific address; the wallet
    //                                derives many addresses from one
    //                                master key)
    //   chia_signMessageByAddress  — sign the challenge with that
    //                                specific address
    // No spend capability requested — least privilege.
    const { uri, approval } = await client.connect({
      requiredNamespaces: {
        chia: {
          chains:  ["chia:mainnet"],
          methods: [
            "chia_getCurrentAddress",
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

    // Try to fetch the current receive address from the wallet — works
    // on Goby + older Chia reference wallet. Sage doesn't implement
    // chia_getCurrentAddress, so on "Unsupported method" we fall back
    // to asking the operator directly. Either way, the wallet has to
    // sign with the resulting address; Sage will refuse to sign for
    // an address it doesn't own, which IS the security check we want.
    let address = null;
    try {
      console.log("[orchard.connect] trying chia_getCurrentAddress…");
      const r = await client.request({
        topic:   session.topic,
        chainId: "chia:mainnet",
        request: {
          method: "chia_getCurrentAddress",
          params: { fingerprint, walletId: 1 },
        },
      });
      address = (typeof r === "string")
        ? r
        : (r?.address || r?.data?.address);
      console.log("[orchard.connect] wallet supplied address:", address);
    } catch (e) {
      console.log(
        "[orchard.connect] wallet doesn't support getCurrentAddress " +
        "(this is normal for Sage); falling back to operator prompt.");
    }

    if (typeof address !== "string" ||
        !/^xch1[0-9a-z]{50,80}$/.test(address)) {
      // Sage's RPC surface doesn't include getCurrentAddress in this
      // build. Ask the operator directly. Native prompt is plain but
      // immediate; we'll upgrade to a proper in-page card in a
      // follow-up.
      address = window.prompt(
        "Sage didn't share an address automatically.\n\n" +
        "Paste the xch1 address you want to bind to your Tree. " +
        "Sage will refuse to sign if it doesn't actually control this " +
        "address — that's the security check.\n\n" +
        "Wallet fingerprint: " + fingerprint
      );
      if (!address) {
        throw new Error("Connect cancelled (no address provided).");
      }
      address = address.trim();
      if (!/^xch1[0-9a-z]{50,80}$/.test(address)) {
        throw new Error(
          "That doesn't look like a Chia mainnet address (expected " +
          "xch1... with 50-80 lowercase base32 characters after the prefix).");
      }
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
