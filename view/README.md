# view/ — Orchard View (public, hosted)

The **public, read-only** Orchard View: a live dashboard of the Trees
reporting to the hosted oracle, served at **view.theorchard.network**.

It is a static page plus a Cloudflare Pages Function, deployed to
Cloudflare Pages exactly like the [flasher](../flasher). Connecting the
home → flash → claim → **view** journey: a claimed Tree links straight
to its live page here.

> This is **not** the operator tool. Provisioning a Tree (USB serial,
> WiFi push, Pass verification) lives in [`dashboard/`](../dashboard) —
> Orchard View "local", which you run on your own PC. This `view/` is the
> hosted, look-only counterpart anyone can open.

## What's here

| Path                      | What                                                            |
|---------------------------|----------------------------------------------------------------|
| `index.html`              | Single-page app. `/` lists Trees; `/?tree=<node_id>` is a live per-Tree view (5 s polling). Holo-dark theme, shared `connect.js` nav widget. |
| `functions/api/[[path]].js` | Pages Function. The page's same-origin API; fetches the oracle server-side and **scrubs** before anything reaches the browser. |

## Why the Function exists — privacy at the edge

The page never calls the oracle directly. It calls its own `/api`, and the
Function fetches the oracle and **strips operator-sensitive fields** — the
serverless equivalent of the local dashboard's `public_mode`:

- **`wallet_address` is removed.** It couples a Tree (and its physical
  location) to a payout address — doxx + financial linkage.
- **GPS is coarsened to ~111 m** (3 decimals). Enough to show a Tree's
  region; not enough to pin it to a house.

The raw wallet/precise-GPS never cross the edge, so they can't be read out
of the browser's network tab. The Function is locked to the Orchard oracle
and read-only GETs — not an open proxy.

### Endpoints

| Route                       | Backed by oracle                                  |
|-----------------------------|---------------------------------------------------|
| `GET /api/network/stats`    | `/network/stats`                                  |
| `GET /api/nodes`            | `/nodes` (wallet scrubbed)                         |
| `GET /api/tree/<id>/latest` | composite of `/nodes/<id>`, `/readings/<id>`, `/uptime/<id>/<season>`, `/attestations/<id>/latest` (wallet scrubbed, GPS coarsened) |

## Deploy

```bash
cd view
npx wrangler pages deploy . --project-name orchard-view --branch main
```

Run it **from inside `view/`** so the Functions bundle ships with the
static page. Lands at `https://orchard-view.pages.dev`.

## Go-live checklist (Cloudflare dashboard)

Two one-time edits make it fully live at the real domain:

1. **Custom domain.** Pages → `orchard-view` → Custom domains → add
   `view.theorchard.network`. DNS is on Cloudflare, so the CNAME is created
   automatically.
2. **Open `/readings` at the WAF.** Live sensor tiles + the readings table
   need `GET /readings/<id>`, which the edge WAF currently blocks (403). Add
   `/readings` to the "Allow Orchard provisioning paths" Skip rule. Until
   then the page runs fine and shows a "readings not published yet" notice;
   status, Pass, uptime and on-chain data are all live.

No secrets, server IPs, or internal hostnames live in this directory — it
only ever talks to the public `oracle.theorchard.network`.
