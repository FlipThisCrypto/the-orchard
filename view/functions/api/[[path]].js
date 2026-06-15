// SPDX-License-Identifier: Apache-2.0
//
// Cloudflare Pages Function — public, read-only API for Orchard View.
//
// Why this exists: the public dashboard (view.theorchard.network) is a
// static page, but it must NOT expose two operator-sensitive things that
// the oracle's raw public endpoints return:
//
//   1. wallet_address  — couples a Tree (and its physical location) to the
//                        payout address. Doxx + financial linkage.
//   2. precise GPS      — full lat/lon pins a Tree to a specific house.
//
// The local Flask dashboard solves this with `public_mode` (scrubs wallet,
// coarsens GPS to ~111m). This Function is the serverless equivalent: the
// browser only ever talks to this same-origin /api, which fetches the
// oracle server-side, assembles the composite the page needs, and scrubs
// before anything reaches the client. The raw wallet/GPS never crosses the
// edge.
//
// Hard-locked to the Orchard oracle and to read-only GETs — NOT an open
// proxy. Node ids are validated hex; the upstream host is hard-coded.

const ORACLE = "https://oracle.theorchard.network";
const NODE_RE = /^[0-9A-Fa-f]{1,64}$/;

// ~111 m precision — enough to know the Tree's region, not its street.
const GPS_DECIMALS = 3;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...CORS,
      "Content-Type": "application/json; charset=utf-8",
      ...extra,
    },
  });
}

async function oracleGet(path) {
  // Returns { status, body } — body is parsed JSON or null. Never throws;
  // a blocked/missing upstream is data, not an exception, so one dead
  // sub-call (e.g. /readings behind the WAF) can't sink the whole view.
  try {
    const r = await fetch(ORACLE + path, {
      headers: { Accept: "application/json" },
    });
    let body = null;
    try {
      body = await r.json();
    } catch {
      body = null;
    }
    return { status: r.status, body };
  } catch {
    return { status: 0, body: null };
  }
}

function coarsen(v) {
  if (v === null || v === undefined) return v;
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  const f = 10 ** GPS_DECIMALS;
  return Math.round(n * f) / f;
}

// Drop wallet_address from a node record (location ↔ payout linkage).
function scrubNode(n) {
  if (!n || typeof n !== "object") return n;
  const out = { ...n };
  delete out.wallet_address;
  return out;
}

// Coarsen every GPS coordinate in a reading, top-level and nested.
function scrubReading(r) {
  if (!r || typeof r !== "object") return r;
  const out = { ...r };
  if ("gps_lat" in out) out.gps_lat = coarsen(out.gps_lat);
  if ("gps_lon" in out) out.gps_lon = coarsen(out.gps_lon);
  const payload = out.payload;
  if (payload && typeof payload === "object") {
    const sensors = payload.sensors;
    if (sensors && typeof sensors === "object" && sensors.gps) {
      const gps = { ...sensors.gps };
      for (const k of ["lat", "lon", "latitude", "longitude"]) {
        if (k in gps) gps[k] = coarsen(gps[k]);
      }
      out.payload = {
        ...payload,
        sensors: { ...sensors, gps },
      };
    }
  }
  return out;
}

// "alive" = a reading landed within 3× the 60 s sample interval. Oracle
// timestamps are naive UTC; treat a missing tz as UTC so fresh readings
// aren't marked stale forever.
function isAlive(iso) {
  if (!iso) return false;
  let s = String(iso);
  if (!/[zZ]$|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
  const t = Date.parse(s);
  if (Number.isNaN(t)) return false;
  return Date.now() - t < 180_000;
}

export async function onRequest(context) {
  const { request, params } = context;
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS });
  }
  if (request.method !== "GET") {
    return json({ error: "method not allowed" }, 405);
  }

  const seg = (Array.isArray(params.path) ? params.path : [params.path]).filter(
    Boolean
  );

  // GET /api/network/stats — aggregate counters for the home page.
  if (seg.length === 2 && seg[0] === "network" && seg[1] === "stats") {
    const r = await oracleGet("/network/stats");
    if (r.status !== 200 || !r.body) {
      return json({ error: "oracle unreachable" }, 502);
    }
    return json(r.body, 200, { "Cache-Control": "public, max-age=15" });
  }

  // GET /api/nodes — Tree list (wallet scrubbed).
  if (seg.length === 1 && seg[0] === "nodes") {
    const r = await oracleGet("/nodes");
    if (r.status !== 200 || !Array.isArray(r.body)) {
      return json({ error: "oracle unreachable" }, 502);
    }
    return json(r.body.map(scrubNode), 200, {
      "Cache-Control": "public, max-age=15",
    });
  }

  // GET /api/tree/<id>/latest — composite live view (scrubbed). Mirrors
  // the Flask dashboard's /api/tree/<id>/latest in public_mode.
  if (seg.length === 3 && seg[0] === "tree" && seg[2] === "latest") {
    const id = seg[1].toUpperCase();
    if (!NODE_RE.test(id)) return json({ error: "bad node id" }, 400);

    const nodeR = await oracleGet(`/nodes/${id}`);
    if (nodeR.status === 404) return json({ error: "unknown node" }, 404);
    if (nodeR.status !== 200 || !nodeR.body) {
      return json({ error: "oracle unreachable" }, 502);
    }
    const node = scrubNode(nodeR.body);

    // Readings may be blocked at the edge WAF (403) until the operator
    // opens /readings. Surface that as a status the page can explain,
    // rather than an error — the rest of the view still works.
    const readR = await oracleGet(`/readings/${id}?limit=20`);
    let readings = [];
    let readings_status = "ok";
    if (readR.status === 200 && Array.isArray(readR.body)) {
      readings = readR.body.map(scrubReading);
    } else if (readR.status === 403) {
      readings_status = "blocked";
    } else if (readR.status === 404) {
      readings_status = "ok"; // node exists, just no readings yet
    } else {
      readings_status = "unavailable";
    }

    // Current season → uptime. Both nice-to-have; never fail the view.
    let current_season = null;
    let uptime = null;
    const stats = await oracleGet("/network/stats");
    if (stats.status === 200 && stats.body) {
      current_season = stats.body.current_season ?? null;
    }
    if (current_season) {
      const up = await oracleGet(`/uptime/${id}/${current_season}`);
      if (up.status === 200) uptime = up.body;
    }

    // Latest on-chain attestation (may be empty).
    let attestation = null;
    const att = await oracleGet(`/attestations/${id}/latest`);
    if (att.status === 200 && att.body) attestation = att.body;

    const last_received_at = readings.length
      ? readings[0].received_at
      : node.last_reading_at || null;

    return json(
      {
        node,
        readings,
        readings_status,
        latest: readings.length ? readings[0] : null,
        alive: isAlive(last_received_at),
        last_received_at,
        current_season,
        uptime,
        attestation,
      },
      200,
      { "Cache-Control": "no-store" }
    );
  }

  return json({ error: "not found" }, 404);
}
