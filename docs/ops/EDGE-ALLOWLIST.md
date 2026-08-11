# The edge allowlists paths. Design for it.

Measured 2026-08-11 against `oracle.theorchard.network`.

## What is actually true

Cloudflare in front of the oracle **allowlists specific paths and blocks
everything else** with a WAF challenge page ("Sorry, you have been blocked",
HTTP 403, `text/html`).

| Path | Result |
|---|---|
| `/` | 200 JSON |
| `/health` | 200 JSON |
| `/network/stats` | 200 JSON |
| `/nodes` | 200 JSON |
| `/readings` | 405 JSON (POST-only — passes the edge) |
| `/attestations` | 405 JSON (POST-only — passes the edge) |
| `/claim` | 200 HTML |
| `/beacon` | **403 Cloudflare** |
| `/docs` | **403 Cloudflare** |
| `/definitely-not-a-route-xyz` | **403 Cloudflare** |

## The correction this makes

The standing task read "unblock `/beacon` at Cloudflare", on the assumption
that something about `/beacon` specifically had tripped a managed rule — the
word "beacon" appears in plenty of security rulesets, so the theory was
reasonable. It is wrong.

A path that has never existed is blocked identically. So renaming the endpoint
would not have helped, and neither would a rule exception for one path: **every
future endpoint is blocked by default.** That is a standing constraint on the
architecture, not a one-time misconfiguration.

Testing the negative case is what found this. `/beacon` and `/anchor` and
`/block-anchor` all returning 403 looked like confirmation of the rename
theory until a path that could not possibly match any rule returned 403 too.

## The idiom that already handles it

This repo solved the same problem once already, for deploy markers. From
`oracle/app/routes/health.py`:

> Deploy markers ride as RESPONSE HEADERS on every response instead […] that
> keeps this contract intact, keeps liveness dependency-free, and works on any
> endpoint the edge happens to allow rather than betting on one path.

Verified live — these come back through Cloudflare on `/health`:

    x-orchard-source = 23f4c0f2d2b9
    x-orchard-schema = unknown
    x-request-id     = 98dea767f9797e2f

## What this means for the block anchor

The Tree's anti-backdating anchor is the one remaining failing verifier check,
and `/beacon` is how firmware was meant to fetch it. It cannot get there.

Two options, neither of which needs a Cloudflare change:

1. **Ride the reading POST.** The firmware already calls `POST /readings` every
   sample, and that path passes the edge. Returning the current anchor with
   that response costs zero extra requests on a battery device, and the Tree
   stamps it onto the next reading — which still proves the reading was created
   after that block existed, the only property anti-backdating needs.
2. **A response header on any allowlisted endpoint**, following the deploy
   marker idiom above.

Option 1 is preferred: no extra round trip, and the anchor is bound to the
oracle's live view at the moment of the exchange.

Both need a firmware change and therefore a reflash of the live Tree.

`x-orchard-schema = unknown` is a separate small finding: the Alembic head is
not being primed at startup on the deployed box, so the header cannot answer
"did the migration land?". Worth fixing when the oracle next deploys.

## The rule that should not be relied on

Do not design a protocol path that assumes the edge will pass it. Add the
capability to an endpoint that already works, or carry it in a header. If a new
path is genuinely required, the allowlist must be updated in the Cloudflare
dashboard first — and that is founder-side work, not something the repo can
assert.
