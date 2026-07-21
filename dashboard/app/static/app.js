// SPDX-License-Identifier: Apache-2.0
// Orchard View — vanilla JS for the provisioning wizard and live Tree view.
//
// XSS posture: every dynamic value rendered through innerHTML below is
// passed through esc() first. makeField/makeStep escape their args
// internally. Sensor values come from Tree firmware POST bodies which
// are not under our control — a compromised or hostile Tree must not
// be able to script the operator's (or a public viewer's) browser.

const OrchardView = (() => {

  // -------------------------- tiny helpers --------------------------

  function $(sel, root = document) { return root.querySelector(sel); }

  // HTML-escape a value for safe inclusion inside an HTML attribute or
  // element body. Treats null/undefined as empty. Numbers and booleans
  // are stringified by String(). Anything else gets the five-char
  // escape table applied.
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;',
      '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  async function jget(url) {
    const r = await fetch(url, { method: 'GET' });
    return { ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) };
  }

  async function jpost(url, body) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return { ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) };
  }

  function relativeAge(iso) {
    if (!iso) return '—';
    // The oracle's stored timestamps are naive UTC. Date.parse on a
    // string with no tz designator interprets as LOCAL per the spec,
    // which produced negative-age clamps and bogus "just now" labels.
    // Append Z when the string is missing a tz so Date treats it as
    // UTC and the math matches the server's alive check.
    let s = iso;
    if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) {
      s = s + 'Z';
    }
    const then = Date.parse(s);
    if (isNaN(then)) return iso;
    const ageSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (ageSec < 5) return 'just now';
    if (ageSec < 60) return `${ageSec}s ago`;
    if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`;
    return `${Math.floor(ageSec / 3600)}h ago`;
  }

  // Build a k/v row. Both args are HTML-escaped; pass {mono: true} to
  // give the value a monospaced font (via class, not by injecting a
  // <span> wrapper from the caller).
  function makeField(k, v, opts = {}) {
    const vCls = opts.mono ? 'v mono' : 'v';
    const vText = (v === null || v === undefined || v === '') ? '—' : v;
    return `<div class="field"><div class="k">${esc(k)}</div>` +
           `<div class="${vCls}">${esc(vText)}</div></div>`;
  }

  function makeStep(label, state, msg = '') {
    const icon = state === 'done' ? '✓' : state === 'doing' ? '…' : state === 'err' ? '✗' : '·';
    return `<div class="step-status ${esc(state)}">` +
           `<span class="icon">${icon}</span> ${esc(label)} ` +
           (msg ? `<span class="muted">— ${esc(msg)}</span>` : '') +
           `</div>`;
  }

  // -------------------------- provision page -------------------------

  async function refreshPorts() {
    const sel = $('#port-select');
    sel.innerHTML = '';
    const r = await jget('/api/serial/ports');
    if (!r.ok) {
      sel.innerHTML = '<option value="">(error listing ports)</option>';
      return;
    }
    const ports = r.body.ports || [];
    if (!ports.length) {
      sel.innerHTML = '<option value="">(no serial ports)</option>';
      return;
    }
    for (const p of ports) {
      const opt = document.createElement('option');
      opt.value = p.device;
      opt.textContent = `${p.device} — ${p.description || ''}`;
      sel.appendChild(opt);
    }
  }

  let identified = null;   // { node_id, signing_key_hex, status }
  // After step 2 (Verify Pass): { wallet, pass_nft_id, pass_name }.
  // Required to reach step 3. The Skip path was removed — operating
  // a Tree on the network now requires a verified Pass at all times.
  // Phase 6.6: `wallet` is always sourced from Orchard.getSession()
  // (the wallet that signed the challenge), never from a typed input.
  let passDecision = null;

  // Render the connected-wallet status panel in step 2. Called from
  // initProvisionPage at load and again whenever the session changes
  // (Connect / Disconnect from the top nav, which fires a custom
  // event via writeSession / clearSession in connect.js).
  function renderWalletStatus() {
    const slot = $('#wallet-status');
    if (!slot) return;
    const sess = (window.Orchard && Orchard.getSession()) || null;
    const verifyBtn = $('#verify-pass-btn');
    if (!sess) {
      // No session — gate Step 2 entirely and point at the nav
      // Connect button. We don't render a duplicate Connect button
      // here so there's only one canonical place to authenticate.
      slot.innerHTML =
        '<div class="muted" style="padding:10px;border:1px dashed #555;border-radius:8px">' +
        '<strong>Connect your wallet first.</strong> ' +
        'Use the <em>Connect Wallet</em> button in the top right. ' +
        'Your wallet signs a challenge to prove you control the address ' +
        'before any Tree gets bound to it.</div>';
      if (verifyBtn) verifyBtn.disabled = true;
      passDecision = null;
      return;
    }
    const addr = sess.address || '';
    const short = addr.length > 16
      ? `${addr.slice(0, 12)}…${addr.slice(-8)}` : addr;
    slot.innerHTML =
      '<div class="field" style="padding:8px 12px;background:#1a1c2e;border-radius:8px">' +
      '<div class="k">Connected wallet</div>' +
      `<div class="v"><code title="${esc(addr)}">${esc(short)}</code></div>` +
      '</div>';
    if (verifyBtn) verifyBtn.disabled = false;
  }

  async function onIdentify() {
    const port = $('#port-select').value;
    const out = $('#identify-result');
    out.innerHTML = '<div class="muted">Talking to Tree…</div>';
    const r = await jpost('/api/serial/identify', { port });
    if (!r.ok) {
      out.innerHTML = `<div class="err">${esc(r.body.error || 'failed')}</div>`;
      return;
    }
    identified = { ...r.body, port };
    // Every dynamic value below is server-controlled (it came from the
    // Tree over USB) — esc() guards against a malicious Tree firmware
    // that puts HTML/JS into its identity fields.
    const sk = r.body.signing_key_hex || '';
    const hw = r.body.hw_info;   // Phase 9.0: nullable for fw <0.4.0

    // Build the identify card. Board + sensor list come from HW_INFO
    // when the firmware is 9.0-aware; we keep the legacy fields
    // (node_id/signing_key/fw/wifi/oracle url) regardless so the card
    // is informative even on older firmware that returns hw_info=null.
    const parts = [];
    parts.push(makeField('node_id',     r.body.node_id, {mono: true}));
    parts.push(makeField('signing_key', sk ? `${sk.slice(0, 16)}…` : '—', {mono: true}));
    // ADR-0003 provenance key — published to DataLayer node:<id>.pubkey.
    const dpub = r.body.device_pubkey || (hw && hw.pubkey) || '';
    parts.push(makeField(
      'device_pubkey',
      dpub ? `${dpub.slice(0, 18)}…` : '(none — upgrade firmware for verifiable readings)',
      {mono: true}
    ));
    parts.push(makeField('fw',          r.body.status?.fw));
    if (hw && hw.board) {
      parts.push(makeField('board', hw.board));
    }
    if (hw && hw.chip) {
      parts.push(makeField('chip', hw.chip));
    }
    parts.push(makeField('wifi',        r.body.status?.wifi));
    parts.push(makeField('oracle url',  r.body.status?.oracle || '(unset)'));

    // Sensor chips. For each registered sensor on the Tree we render
    // a small badge: ✓ green = detected on bus, ⊘ grey = compiled-in
    // but not present (probe failed). Operator can immediately see
    // wiring issues like "I have a BME280 but the firmware says ⊘
    // bme280" — means the wires are wrong or the address is unusual.
    if (hw && Array.isArray(hw.sensors) && hw.sensors.length) {
      const chips = hw.sensors.map(s => {
        const cls  = s.active ? 'ok' : 'muted';
        const mark = s.active ? '✓' : '⊘';
        const addr = (s.bus === 'i2c' && typeof s.addr === 'number')
          ? ` <span class="muted">0x${s.addr.toString(16).padStart(2,'0')}</span>` : '';
        return `<span class="sensor-chip ${cls}">${mark} ${esc(s.name)}${addr}</span>`;
      }).join(' ');
      parts.push(
        `<div class="field"><div class="k">sensors</div>` +
        `<div class="v">${chips}</div></div>`
      );
    }

    out.innerHTML = parts.join('');
    // Advance to the Pass verification step. Step 3 stays hidden until
    // step 2 resolves one way or the other.
    $('#step-pass').hidden = false;
    $('#step-config').hidden = true;
  }

  async function onVerifyPass() {
    const sess = (window.Orchard && Orchard.getSession()) || null;
    const out = $('#verify-pass-result');
    if (!sess) {
      out.innerHTML =
        '<div class="err">Connect your wallet first (top right).</div>';
      return;
    }
    const wallet = sess.address;
    out.innerHTML = '<div class="muted">Querying chain via MintGarden…</div>';
    const r = await jpost('/api/oracle/verify_pass', { wallet_address: wallet });
    if (!r.ok) {
      out.innerHTML = `<div class="err">${esc(r.body.error || 'verification failed')}</div>`;
      return;
    }
    if (!r.body.has_pass) {
      // No Pass at this wallet. Show buy link; do NOT advance — Pass
      // ownership is now mandatory for Tree provisioning.
      out.innerHTML =
        `<div class="err">No Orchard Pass found at ${esc(wallet)}.</div>` +
        `<div class="muted" style="margin-top:6px">` +
        `Pass ownership is required to operate a Tree on the network. ` +
        `Acquire one on <a href="${esc(r.body.buy_url)}" target="_blank" rel="noopener">MintGarden</a> ` +
        `and re-run Verify Pass here.</div>`;
      passDecision = null;
      return;
    }
    // Has Pass — show the bound NFT and advance.
    out.innerHTML =
      `<div class="ok">✓ Orchard Pass verified.</div>` +
      makeField('pass', r.body.pass_name || '—') +
      makeField('edition', r.body.edition_number ?? '—') +
      makeField('nft id', r.body.pass_nft_id || '—', {mono: true}) +
      `<div class="field"><div class="k">on chain</div><div class="v">` +
      `<a href="${esc(r.body.mintgarden_url)}" target="_blank" rel="noopener">View on MintGarden →</a>` +
      `</div></div>`;
    passDecision = {
      wallet:      wallet,
      pass_nft_id: r.body.pass_nft_id,
      pass_name:   r.body.pass_name,
    };
    $('#step-config').hidden = false;
  }

  async function onProvision() {
    const port = identified?.port;
    if (!identified) return;
    if (!passDecision) {
      alert('Verify your Orchard Pass first.');
      return;
    }
    const sess = (window.Orchard && Orchard.getSession()) || null;
    if (!sess) {
      // Defensive — Step 2 also gates on this, but the operator could
      // have disconnected between verifying the Pass and clicking
      // Provision.
      alert('Your wallet session ended. Please reconnect and re-verify.');
      return;
    }
    const ssid = $('#ssid').value.trim();
    const password = $('#password').value;
    const oracleUrl = $('#oracle-url').value.trim();
    const label = $('#label').value.trim();

    if (!ssid) { alert('WiFi SSID is required'); return; }
    if (!oracleUrl) { alert('Oracle URL is required'); return; }

    const progress = $('#provision-progress');
    const btn = $('#provision-btn');
    btn.disabled = true;
    progress.innerHTML = '';

    function setStep(idx, label, state, msg = '') {
      const slots = progress.querySelectorAll('.step-status');
      const html = makeStep(label, state, msg);
      if (slots[idx]) slots[idx].outerHTML = html;
      else progress.insertAdjacentHTML('beforeend', html);
    }

    // Register via authFetch so the session Bearer is attached.
    // wallet_address is intentionally omitted from the body — the
    // oracle uses session.address as the source of truth, and an
    // explicit mismatched value would be a 400. (See
    // oracle.app.routes.register._resolve_wallet_for_register.)
    setStep(0, 'Register with oracle', 'doing');
    const regResp = await Orchard.authFetch('/api/oracle/register', {
      method: 'POST',
      body: JSON.stringify({
        node_id: identified.node_id,
        signing_key_hex: identified.signing_key_hex,
        label: label || null,
        fw_version: identified.status?.fw || null,
        // Compressed secp256r1 pubkey for DataLayer node:<id>.pubkey.
        device_pubkey: identified.device_pubkey
          || identified.hw_info?.pubkey
          || null,
      }),
    });
    let r = { ok: regResp.ok, status: regResp.status,
              body: await regResp.json().catch(() => ({})) };
    if (!r.ok) {
      // Friendlier copy for the most common 401 cause: oracle restart
      // rotated its session-signing secret since we connected. The
      // authFetch wrapper already cleared the stale token so a single
      // reconnect fixes it.
      let msg;
      if (r.status === 401) {
        msg = 'Wallet session was rejected by the oracle ' +
              '(likely an oracle restart since you connected). ' +
              'Click Disconnect, then Connect Wallet again, then Verify Pass, then re-click Provision Tree.';
      } else {
        msg = r.body.error || `HTTP ${r.status}`;
      }
      setStep(0, 'Register with oracle', 'err', msg);
      btn.disabled = false;
      return;
    }
    const regMsg = r.body.register?.pass_nft_id
      ? `Pass bound: ${r.body.register.pass_nft_id.slice(0, 16)}…`
      : (r.body.register?.new ? 'new' : 'updated');
    setStep(0, 'Register with oracle', 'done', regMsg);

    setStep(1, 'Push WiFi credentials', 'doing');
    r = await jpost('/api/serial/wifi', { port, ssid, password });
    if (!r.ok) { setStep(1, 'Push WiFi credentials', 'err', r.body.error); btn.disabled = false; return; }
    setStep(1, 'Push WiFi credentials', 'done');

    setStep(2, 'Push oracle URL', 'doing');
    r = await jpost('/api/serial/oracle', { port, url: oracleUrl });
    if (!r.ok) { setStep(2, 'Push oracle URL', 'err', r.body.error); btn.disabled = false; return; }
    setStep(2, 'Push oracle URL', 'done');

    setStep(3, 'Trigger first sample', 'doing');
    r = await jpost('/api/serial/sample', { port });
    if (!r.ok) { setStep(3, 'Trigger first sample', 'err', r.body.error); btn.disabled = false; return; }
    setStep(3, 'Trigger first sample', 'done');

    setStep(4, 'Done — opening live view…', 'done');
    setTimeout(() => { window.location.href = `/tree/${encodeURIComponent(identified.node_id)}`; }, 800);
  }

  function initProvisionPage() {
    $('#refresh-ports').addEventListener('click', refreshPorts);
    $('#identify-btn').addEventListener('click', onIdentify);
    $('#verify-pass-btn').addEventListener('click', onVerifyPass);
    $('#provision-btn').addEventListener('click', onProvision);
    // Render initial wallet-status panel and re-render whenever the
    // session changes (Connect / Disconnect in the top nav).
    renderWalletStatus();
    window.addEventListener('orchard:session', renderWalletStatus);
    refreshPorts();
  }

  // -------------------------- tree (live) page -----------------------

  // ---- Temperature unit preference (localStorage-persisted) -------
  // Stored as 'c' (Celsius, default) or 'f' (Fahrenheit). The toggle
  // button on the Tree page flips this and re-renders immediately;
  // the choice survives page reloads but is per-browser (no server
  // round-trip — sensible for a per-operator viewing preference).
  const TEMP_UNIT_KEY = 'orchard.tempUnit';

  function getTempUnit() {
    try {
      return (localStorage.getItem(TEMP_UNIT_KEY) === 'f') ? 'f' : 'c';
    } catch {
      return 'c';   // localStorage unavailable (private mode, etc.)
    }
  }
  function setTempUnit(u) {
    try { localStorage.setItem(TEMP_UNIT_KEY, u); } catch {}
  }

  // Format a Celsius value for display, respecting the current
  // operator preference. Numbers come off the wire as Celsius (the
  // firmware's native unit and the SI standard); conversion happens
  // only at display time, so the stored data in the oracle DB stays
  // unit-canonical.
  function formatTemp(c, decimals = 1) {
    if (c === null || c === undefined) return '—';
    const u = getTempUnit();
    const v = (u === 'f') ? c * 9 / 5 + 32 : c;
    return `${v.toFixed(decimals)} °${u.toUpperCase()}`;
  }

  // ---- GPS coordinate formatting + interpretation -------------------

  // Directional decimal: "38.004648°N", "85.737465°W"
  function formatLat(lat) {
    if (lat === null || lat === undefined) return '—';
    const n = Number(lat);
    if (!Number.isFinite(n)) return '—';
    return `${Math.abs(n).toFixed(6)}°${n >= 0 ? 'N' : 'S'}`;
  }
  function formatLon(lon) {
    if (lon === null || lon === undefined) return '—';
    const n = Number(lon);
    if (!Number.isFinite(n)) return '—';
    return `${Math.abs(n).toFixed(6)}°${n >= 0 ? 'E' : 'W'}`;
  }

  // Reverse-geocode lat/lon to a "City, Region, Country" label using
  // Nominatim (OSM's free reverse-geocoder). We cache per
  // ~0.001° = ~111m cell in localStorage for 1 hour so a stationary
  // Tree isn't hitting Nominatim every 5 seconds.
  //
  // Nominatim policy: 1 req/s max, identify your app. Browsers send
  // their own User-Agent (we can't override), but the rate is enforced
  // via the cell cache. If the request fails (rate-limited, offline,
  // CORS), we silently fall back to "—" — coordinates are still
  // visible regardless.
  const GEOCODE_KEY_PREFIX = 'orchard.geo.';
  const GEOCODE_TTL_MS = 60 * 60 * 1000;     // 1 hour

  function geocodeCellKey(lat, lon) {
    const la = Number(lat).toFixed(3);   // 3 decimals = ~111m
    const lo = Number(lon).toFixed(3);
    return `${GEOCODE_KEY_PREFIX}${la},${lo}`;
  }

  async function reverseGeocode(lat, lon) {
    if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lon))) {
      return null;
    }
    const key = geocodeCellKey(lat, lon);
    try {
      const cached = JSON.parse(localStorage.getItem(key) || 'null');
      if (cached && Date.now() - cached.when < GEOCODE_TTL_MS) {
        return cached.name;
      }
    } catch { /* storage unavailable; fall through to fetch */ }

    try {
      const url = `https://nominatim.openstreetmap.org/reverse` +
                  `?format=jsonv2&zoom=10` +
                  `&lat=${encodeURIComponent(lat)}` +
                  `&lon=${encodeURIComponent(lon)}`;
      const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
      if (!r.ok) return null;
      const j = await r.json();
      const a = j.address || {};
      const parts = [
        a.city || a.town || a.village || a.hamlet || a.suburb,
        a.state,
        a.country,
      ].filter(Boolean);
      const name = parts.length ? parts.join(', ') : (j.display_name || null);
      try {
        localStorage.setItem(key, JSON.stringify({ name, when: Date.now() }));
      } catch { /* storage full or blocked; that's fine */ }
      return name;
    } catch {
      return null;
    }
  }

  // Async kick: when the GPS tile renders, we trigger a geocode lookup
  // and patch the city label in place once the result arrives. The
  // tile re-render that happens on the next poll picks up the cached
  // value directly without async, so this only runs the first time
  // (or after the 1h TTL expires).
  function patchGeocodeLabel(lat, lon) {
    const slot = document.querySelector('[data-gps-locality]');
    if (!slot) return;
    reverseGeocode(lat, lon).then(name => {
      const fresh = document.querySelector('[data-gps-locality]');
      if (!fresh) return;
      fresh.textContent = name || '—';
    });
  }

  // Synchronous cached lookup — used when rendering the tile so we
  // can show the city immediately on subsequent polls.
  function cachedLocality(lat, lon) {
    try {
      const cached = JSON.parse(
        localStorage.getItem(geocodeCellKey(lat, lon)) || 'null');
      if (cached && Date.now() - cached.when < GEOCODE_TTL_MS) {
        return cached.name;
      }
    } catch {}
    return null;
  }

  // Per-sensor render config. Adding a new known sensor = add a key
  // here with its display title and field list; the dashboard tile
  // appears automatically the next time a Tree reports that sensor.
  // Unknown sensor names fall through to a generic auto-tile that
  // renders every field as-is.
  //
  // Field formats are functions so we can compose units (`${v} °C`)
  // and precision (`.toFixed(2)`). null/undefined values render as
  // an em-dash via makeField's null check.
  const SENSOR_TILES = {
    mq135: {
      title: "MQ-135 — air quality",
      fields: [
        ["adc_raw",      v => v?.toFixed?.(1) ?? v],
        ["voltage_v",    v => v?.toFixed?.(3) ?? v],
        ["baseline",     v => v?.toFixed?.(1) ?? v,   "adc_baseline"],
        ["deviation",    v => v?.toFixed?.(2) ?? v,   "adc_dev"],
      ],
    },
    bme280: {
      title: "BME280 — temp / humidity / pressure",
      fields: [
        ["temperature",  v => formatTemp(v, 1),                  "temperature_c"],
        ["humidity",     v => `${v?.toFixed?.(1) ?? "—"} %`,     "humidity_pct"],
        ["pressure",     v => `${v?.toFixed?.(2) ?? "—"} hPa`,   "pressure_hpa"],
        ["i2c address",  v => v ? `0x${v.toString(16)}` : "—",   "i2c_addr"],
      ],
    },
    ds18b20: {
      title: "DS18B20 — 1-Wire temperature probe",
      fields: [
        ["temperature",   v => formatTemp(v, 2),                  "temperature_c"],
        ["probes on bus", v => v,                                 "device_count"],
        ["rom id",        v => v,                                 "rom_id"],
      ],
    },
    gps: {
      title: "GPS",
      // GPS is special: when fix=false we want to show different fields.
      // Use a custom render to handle that branch cleanly.
      render: (gps) => {
        let html =
          makeField("fix",        gps.fix ? "yes" : "no") +
          makeField("satellites", gps.satellites ?? "—");
        if (gps.fix) {
          const lat = Number(gps.lat);
          const lon = Number(gps.lon);
          html +=
            makeField("lat",   formatLat(gps.lat), {mono: true}) +
            makeField("lon",   formatLon(gps.lon), {mono: true}) +
            makeField("alt_m", gps.alt_m?.toFixed?.(1) ?? gps.alt_m) +
            makeField("utc",   gps.utc || "—");
          // Map link + reverse-geocoded locality. URL is built from
          // numeric values only — no string injection path.
          if (Number.isFinite(lat) && Number.isFinite(lon)) {
            const url =
              `https://www.openstreetmap.org/?` +
              `mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`;
            const locality = cachedLocality(lat, lon);
            html +=
              `<div class="field">` +
                `<div class="k">locality</div>` +
                `<div class="v" data-gps-locality>${esc(locality || '…')}</div>` +
              `</div>` +
              `<div class="field">` +
                `<div class="k">map</div>` +
                `<div class="v"><a href="${esc(url)}" target="_blank" rel="noopener">` +
                `View on OpenStreetMap →</a></div>` +
              `</div>`;
          }
        } else {
          // No fix → surface the auto-baud diagnostic so an operator
          // can tell module-silent (baud=0) apart from has-bytes-but-
          // no-fix (baud > 0, chars > 0, sentences > 0).
          html +=
            makeField("baud",      gps.baud != null
                                     ? (gps.baud === 0 ? "no lock" : gps.baud)
                                     : "—") +
            makeField("bytes rx",  gps.chars_processed ?? "—") +
            makeField("sentences", gps.sentences_passed ?? "—") +
            makeField("bad checksum", gps.sentences_failed_csum ?? "—");
        }
        return html;
      },
    },
  };

  function renderSensorCard(sensorName, data) {
    const cfg = SENSOR_TILES[sensorName];
    const title = cfg?.title ?? sensorName;
    let body;
    if (cfg?.render) {
      body = cfg.render(data);
    } else if (cfg?.fields) {
      body = cfg.fields.map(([label, fmt, key]) => {
        // Triplet form: [display label, formatter, payload key].
        // Doublet form: [display label, formatter] uses label as key.
        const payloadKey = key ?? label;
        const raw = data[payloadKey];
        const formatted = (raw === null || raw === undefined)
          ? undefined
          : (fmt ? fmt(raw) : raw);
        return makeField(label, formatted);
      }).join("");
    } else {
      // Unknown sensor — auto-tile from every key in the payload.
      // This is the "future-proof" path: a new sensor on the Tree
      // shows up on the dashboard with no code change.
      body = Object.entries(data).map(([k, v]) => makeField(k, v)).join("");
    }
    // makeField escapes every dynamic value, so a malicious Tree can't
    // inject HTML/JS via a sensor field.
    return `<div class="card"><h3>${esc(title)}</h3>${body}</div>`;
  }

  function renderSensors(sensors) {
    const grid = $("#sensor-grid");
    if (!grid) return;
    // Stable display order: render known sensors in SENSOR_TILES order
    // first, then anything else alphabetically. Keeps the tile layout
    // from jumping around between polls when sensor key order changes.
    const known   = Object.keys(SENSOR_TILES).filter(k => k in sensors);
    const unknown = Object.keys(sensors)
                      .filter(k => !(k in SENSOR_TILES))
                      .sort();
    const order   = [...known, ...unknown];
    if (order.length === 0) {
      grid.innerHTML =
        `<div class="card"><span class="muted">Waiting for first reading…</span></div>`;
      return;
    }
    grid.innerHTML = order
      .map(name => renderSensorCard(name, sensors[name]))
      .join("");
  }

  // Render the Operator credentials card from data.node.pass_nft_id.
  // Hidden entirely when no Pass is bound — legacy/unverified nodes
  // shouldn't show an empty placeholder.
  function renderOperatorCredentials(node) {
    const card = $('#operator-credentials-card');
    const body = $('#operator-credentials-body');
    if (!card || !body) return;
    const nftId = node?.pass_nft_id;
    if (!nftId) {
      card.hidden = true;
      body.innerHTML = '';
      return;
    }
    card.hidden = false;
    const mgUrl = `https://mintgarden.io/nfts/${encodeURIComponent(nftId)}`;
    const verifiedAt = node.pass_verified_at
      ? relativeAge(node.pass_verified_at)
      : '—';
    body.innerHTML =
      makeField('pass nft', nftId, {mono: true}) +
      makeField('verified', verifiedAt) +
      `<div class="field">` +
        `<div class="k">on chain</div>` +
        `<div class="v"><a href="${esc(mgUrl)}" target="_blank" rel="noopener">` +
        `View on MintGarden →</a></div>` +
      `</div>`;
  }

  // Render the "On chain" card from data.attestation. Hidden when the
  // Tree has no attestation yet (new operator, first Season not closed,
  // writer not yet run). When present, shows the Season + hours + tx_id
  // linking to spacescan, plus a copy-paste command the operator can
  // run on any machine with a Chia full node to independently verify
  // the chain copy.
  function renderOnChain(attest) {
    const card = $('#onchain-card');
    const body = $('#onchain-body');
    if (!card || !body) return;
    if (!attest) {
      card.hidden = true;
      body.innerHTML = '';
      return;
    }
    card.hidden = false;

    // tx_id may come back with or without a leading "0x"; Spacescan
    // accepts either. Trim to a useful display length but keep the
    // full value in the link.
    const txId = String(attest.dl_tx_id || '');
    const txDisplay = txId
      ? `${txId.slice(0, 10)}…${txId.slice(-6)}`
      : '—';
    const txClean = txId.startsWith('0x') ? txId.slice(2) : txId;
    const spacescanUrl = txClean
      ? `https://www.spacescan.io/coin/0x${txClean}`
      : null;

    // The verification command — copy-paste-friendly. We escape via
    // textContent on the pre to be safe against any future change.
    const verifyCmd =
      `chia data get_value \\\n` +
      `  --id  ${esc(window.ORCHARD_STORE_ID || '<store_id>')} \\\n` +
      `  --key ${esc(attest.dl_key_hex || '<key_hex>')}`;

    body.innerHTML =
      makeField('last attested', `Season ${attest.season_number}`) +
      makeField('hours online', `${attest.hours_online} / 24`) +
      makeField('signed at',
                attest.written_to_datalayer_at
                  ? relativeAge(attest.written_to_datalayer_at)
                  : '—') +
      makeField('block height', attest.block_height_at_write ?? '—') +
      (spacescanUrl
        ? `<div class="field">` +
            `<div class="k">tx</div>` +
            `<div class="v mono"><a href="${esc(spacescanUrl)}" ` +
              `target="_blank" rel="noopener">${esc(txDisplay)} →</a></div>` +
          `</div>`
        : makeField('tx', txDisplay)) +
      `<div class="field">` +
        `<div class="k">verify</div>` +
        `<div class="v"><pre class="mono" style="margin:0">${esc(verifyCmd)}</pre></div>` +
      `</div>`;
  }

  function renderTree(data) {
    const aliveDot = $('#alive-light');
    const aliveLbl = $('#alive-label');
    if (data.alive) {
      aliveDot.className = 'dot dot-green'; aliveLbl.textContent = 'Alive';
    } else if (data.latest) {
      aliveDot.className = 'dot dot-red'; aliveLbl.textContent = 'Stale (no recent reading)';
    } else {
      aliveDot.className = 'dot dot-grey'; aliveLbl.textContent = 'Waiting for first reading…';
    }
    $('#last-age').textContent = 'Last reading: ' + (data.last_received_at ? relativeAge(data.last_received_at) : '—');

    if (data.uptime) {
      $('#uptime-line').textContent = `Uptime this Season: ${data.uptime.hours_online} / 24 hours`;
    }

    renderOperatorCredentials(data.node);
    renderOnChain(data.attestation);

    const latest = data.latest;
    const sensors = latest?.payload?.sensors ?? {};
    renderSensors(sensors);

    // After the tiles render, kick a reverse-geocode for the GPS fix
    // if there is one. The result patches the locality field in place
    // and gets cached in localStorage for an hour, so the next render
    // of this tile shows the city without an async wait.
    const gps = sensors.gps;
    if (gps?.fix) {
      const lat = Number(gps.lat);
      const lon = Number(gps.lon);
      if (Number.isFinite(lat) && Number.isFinite(lon)) {
        patchGeocodeLabel(lat, lon);
      }
    }

    const tbody = $('#readings-body');
    if (data.readings?.length) {
      tbody.innerHTML = data.readings.map(r => {
        const m = r.payload?.sensors?.mq135 || {};
        const g = r.payload?.sensors?.gps || {};
        // Every column escapes; numbers stringify safely, but firmware
        // could in principle put strings in these fields.
        return `<tr>` +
          `<td class="mono">${esc(r.received_at)}</td>` +
          `<td>${esc(m.adc_raw?.toFixed?.(1) ?? '—')}</td>` +
          `<td>${g.fix ? '✓' : '·'}</td>` +
          `<td>${esc(g.lat?.toFixed?.(4) ?? '—')}</td>` +
          `<td>${esc(g.lon?.toFixed?.(4) ?? '—')}</td>` +
        `</tr>`;
      }).join('');
    }

    if (latest) {
      // textContent never executes — safe for arbitrary JSON.
      $('#readings-raw').textContent = JSON.stringify(latest.payload, null, 2);
    }
  }

  let pollTimer = null;
  let lastTreeData = null;     // cached so the toggle can re-render
                               // immediately without waiting for poll
  async function pollTree() {
    const r = await jget(`/api/tree/${encodeURIComponent(window.NODE_ID)}/latest`);
    if (r.ok) {
      lastTreeData = r.body;
      renderTree(r.body);
    }
  }

  function updateTempUnitButton() {
    const btn = $('#temp-unit-toggle');
    if (!btn) return;
    btn.textContent = `°${getTempUnit().toUpperCase()}`;
  }

  function initTreePage() {
    updateTempUnitButton();
    const btn = $('#temp-unit-toggle');
    if (btn) {
      btn.addEventListener('click', () => {
        setTempUnit(getTempUnit() === 'c' ? 'f' : 'c');
        updateTempUnitButton();
        // Re-render the cached payload immediately so the operator
        // sees the unit flip without a 5-second wait.
        if (lastTreeData) renderTree(lastTreeData);
      });
    }
    pollTree();
    pollTimer = setInterval(pollTree, 5000);
  }

  return { initProvisionPage, initTreePage };
})();
