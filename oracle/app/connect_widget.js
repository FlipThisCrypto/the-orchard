// SPDX-License-Identifier: Apache-2.0
//
// The Orchard — shared "Connect Wallet" widget.
//
// One script, included on every Orchard page (landing, claim, dashboard).
// It runs the WalletConnect v2 / CHIP-22 flow once and stores the resulting
// session token in a cookie scoped to the PARENT domain (e.g.
// `.theorchard.network`) so every subdomain shares one connection — connect
// once, connected everywhere. Auth still travels as a Bearer header read from
// that cookie (never as an auto-sent cookie), so this doesn't open a CSRF hole.
//
// Usage on a page:
//   <script src="https://oracle.theorchard.network/connect.js"></script>
//   <div data-orchard-connect></div>            <!-- button renders here -->
// and in page JS: window.OrchardConnect.{getSession,connect,disconnect,
//   authFetch,onChange,render}.
(function () {
  var SELF = document.currentScript;
  // The oracle that issued this script == where /auth + /claim/config live.
  var ORACLE = SELF ? new URL(SELF.src).origin : location.origin;
  var COOKIE = "orchard_session";

  function cookieDomain() {
    var h = location.hostname;
    if (h === "localhost" || /^[0-9.]+$/.test(h)) return null; // host-only (dev/IP)
    var parts = h.split(".");
    return parts.length >= 2 ? "." + parts.slice(-2).join(".") : null;
  }

  function readSession() {
    var m = document.cookie.match(/(?:^|;\s*)orchard_session=([^;]+)/);
    if (!m) return null;
    try {
      var s = JSON.parse(decodeURIComponent(m[1]));
      if (!s || !s.token || !s.address) return null;
      if (s.exp && s.exp * 1000 < Date.now()) { clearSession(); return null; }
      return s;
    } catch (e) { return null; }
  }
  function writeSession(s) {
    var d = cookieDomain();
    var maxAge = s.exp ? Math.max(0, s.exp - Math.floor(Date.now() / 1000)) : 3600;
    document.cookie = COOKIE + "=" + encodeURIComponent(JSON.stringify(s)) +
      ";path=/" + (d ? ";domain=" + d : "") + ";max-age=" + maxAge +
      ";samesite=lax" + (location.protocol === "https:" ? ";secure" : "");
    notify(readSession());
  }
  function clearSession() {
    var d = cookieDomain();
    document.cookie = COOKIE + "=;path=/" + (d ? ";domain=" + d : "") + ";max-age=0;samesite=lax";
    notify(null);
  }

  var listeners = new Set();
  function notify(s) {
    listeners.forEach(function (fn) { try { fn(s); } catch (e) {} });
    try { window.dispatchEvent(new CustomEvent("orchard:session", { detail: s })); } catch (e) {}
    render();
  }

  // ---- WalletConnect flow (identical sequence to the proven claim flow) ----
  var signClient = null, wcModal = null, cfg = null, wcSession = null;

  async function jpost(path, body) {
    var r = await fetch(ORACLE + path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, status: r.status, body: await r.json().catch(function () { return {}; }) };
  }
  async function config() {
    if (!cfg) { try { cfg = await (await fetch(ORACLE + "/claim/config")).json(); } catch (e) { cfg = {}; } }
    return cfg;
  }
  async function ensure() {
    if (signClient && wcModal) return;
    var c = await config();
    if (!c.wc_configured) {
      throw new Error("WalletConnect isn't configured on the oracle (set ORCHARD_ORACLE_WC_PROJECT_ID).");
    }
    var mods = await Promise.all([
      import("https://esm.sh/@walletconnect/sign-client@2.17.0?bundle"),
      import("https://esm.sh/@walletconnect/modal@2.7.0?bundle"),
    ]);
    if (!signClient) signClient = await mods[0].SignClient.init({ projectId: c.wc_project_id, metadata: c.metadata });
    if (!wcModal) wcModal = new mods[1].WalletConnectModal({
      projectId: c.wc_project_id, chains: ["chia:mainnet"], themeMode: "dark",
      themeVariables: { "--wcm-z-index": "2000", "--wcm-accent-color": "#ffae33", "--wcm-background-color": "#0e1124" },
    });
  }
  async function connect() {
    await ensure();
    var conn = await signClient.connect({
      requiredNamespaces: { chia: { chains: ["chia:mainnet"], methods: ["chia_getAddress", "chia_signMessageByAddress"], events: [] } },
    });
    try { await wcModal.openModal({ uri: conn.uri }); wcSession = await conn.approval(); }
    finally { wcModal.closeModal(); }
    var ga = await signClient.request({ topic: wcSession.topic, chainId: "chia:mainnet", request: { method: "chia_getAddress", params: {} } });
    var address = (typeof ga === "string") ? ga : (ga && (ga.address || (ga.data && ga.data.address)));
    if (!/^xch1[0-9a-z]{50,80}$/.test(address || "")) throw new Error("wallet returned an unexpected address");
    var ch = await jpost("/auth/challenge", {});
    if (!ch.ok) throw new Error(ch.body.detail || "challenge failed");
    var sig = await signClient.request({ topic: wcSession.topic, chainId: "chia:mainnet", request: { method: "chia_signMessageByAddress", params: { address: address, message: ch.body.message } } });
    var publicKey = sig.publicKey || sig.public_key, signature = sig.signature;
    if (!publicKey || !signature) throw new Error("wallet returned an unexpected sign response");
    var ver = await jpost("/auth/verify", { address: address, public_key: publicKey, signature: signature, nonce: ch.body.nonce });
    if (!ver.ok) throw new Error("verify failed: " + (ver.body.detail || ver.status));
    writeSession({ token: ver.body.session_token, address: ver.body.address, exp: ver.body.expires_at });
    return readSession();
  }
  async function disconnect() {
    try { if (wcSession && signClient) await signClient.disconnect({ topic: wcSession.topic, reason: { code: 6000, message: "user disconnected" } }); } catch (e) {}
    wcSession = null; clearSession();
  }

  // Bearer-authenticated fetch. `path` may be oracle-relative ("/nodes") or absolute.
  function authFetch(path, opts) {
    opts = opts || {};
    var s = readSession();
    var h = new Headers(opts.headers || {});
    if (s) h.set("Authorization", "Bearer " + s.token);
    var url = /^https?:/.test(path) ? path : (ORACLE + path);
    var o = {}; for (var k in opts) o[k] = opts[k]; o.headers = h;
    return fetch(url, o);
  }

  // ---- default button UI (host page styles .orchard-connect-btn / .orchard-disc) ----
  function shorten(a) { return a && a.length > 16 ? a.slice(0, 10) + "…" + a.slice(-6) : (a || ""); }
  function render() {
    var s = readSession();
    document.querySelectorAll("[data-orchard-connect]").forEach(function (el) {
      if (s) {
        el.innerHTML = '<span class="orchard-connected">Connected <code>' + shorten(s.address) + '</code></span> <button class="orchard-disc" type="button">Disconnect</button>';
        el.querySelector(".orchard-disc").onclick = function () { disconnect(); };
      } else {
        el.innerHTML = '<button class="orchard-connect-btn" type="button">Connect Wallet</button>';
        el.querySelector(".orchard-connect-btn").onclick = async function (e) {
          var b = e.target; b.disabled = true; b.textContent = "Connecting…";
          try { await connect(); }
          catch (err) { alert("Connect failed: " + (err && err.message || err)); }
          finally { var btn = el.querySelector(".orchard-connect-btn"); if (btn) { btn.disabled = false; btn.textContent = "Connect Wallet"; } }
        };
      }
    });
  }

  window.OrchardConnect = {
    oracle: ORACLE,
    getSession: readSession,
    connect: connect,
    disconnect: disconnect,
    authFetch: authFetch,
    onChange: function (fn) { listeners.add(fn); return function () { listeners.delete(fn); }; },
    render: render,
  };

  if (document.readyState !== "loading") render();
  else document.addEventListener("DOMContentLoaded", render);
  // a session may have changed in another tab/subdomain — re-read on focus.
  window.addEventListener("focus", render);
})();
