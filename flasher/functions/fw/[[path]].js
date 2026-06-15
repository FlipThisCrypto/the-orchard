// SPDX-License-Identifier: Apache-2.0
//
// Cloudflare Pages Function — CORS-friendly proxy for Orchard firmware
// release assets.
//
// Why this exists: esp-web-tools runs in the browser and fetch()es each
// firmware .bin listed in manifest.json. GitHub release-asset downloads
// (github.com/.../releases/download/... -> release-assets.githubusercontent.com)
// do NOT send an Access-Control-Allow-Origin header, so a direct cross-origin
// fetch from the flasher page is blocked by the browser and the install fails.
// This route re-serves the asset SAME-ORIGIN with permissive CORS, so the
// manifest can point at `/fw/<tag>/<file>.bin` instead of github.com.
//
// Deployed automatically with the flasher when it's hosted on Cloudflare
// Pages (Functions ship with the static site). See ../README.md.
//
// Locked down to this repo's releases and .bin assets only — NOT an open
// proxy: tag and filename are validated, the upstream host is hard-coded.

const REPO = "FlipThisCrypto/the-orchard";
const TAG_RE = /^v[0-9A-Za-z.+\-]{1,40}$/;
const FILE_RE = /^[A-Za-z0-9._\-]{1,120}\.bin$/;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  "Access-Control-Allow-Headers": "Range, Content-Type",
  "Access-Control-Expose-Headers":
    "Content-Length, Content-Range, Accept-Ranges, ETag",
};

export async function onRequest(context) {
  const { request, params } = context;

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS });
  }
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("method not allowed", { status: 405, headers: CORS });
  }

  // params.path is the array of segments matched by [[path]] after /fw/.
  const seg = Array.isArray(params.path) ? params.path : [params.path];
  if (seg.length !== 2) {
    return new Response("expected /fw/<tag>/<file.bin>", {
      status: 400,
      headers: CORS,
    });
  }
  const [tag, file] = seg;
  if (!TAG_RE.test(tag) || !FILE_RE.test(file)) {
    return new Response("bad tag or filename", { status: 400, headers: CORS });
  }

  const upstream = `https://github.com/${REPO}/releases/download/${tag}/${file}`;
  const fwd = { method: request.method, redirect: "follow", headers: {} };
  const range = request.headers.get("Range");
  if (range) fwd.headers["Range"] = range;

  let resp;
  try {
    resp = await fetch(upstream, fwd);
  } catch (e) {
    return new Response("upstream fetch failed", { status: 502, headers: CORS });
  }
  if (!resp.ok && resp.status !== 206) {
    return new Response(`upstream returned ${resp.status}`, {
      status: resp.status === 404 ? 404 : 502,
      headers: CORS,
    });
  }

  const headers = new Headers(CORS);
  for (const h of [
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
  ]) {
    const v = resp.headers.get(h);
    if (v) headers.set(h, v);
  }
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/octet-stream");
  }
  // A release asset is immutable for a given tag — let the edge and the
  // browser cache it hard so repeat flashes don't re-pull from GitHub.
  headers.set("Cache-Control", "public, max-age=86400, immutable");

  return new Response(request.method === "HEAD" ? null : resp.body, {
    status: resp.status,
    headers,
  });
}
