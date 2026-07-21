# The Orchard — Oracle deployment & hardening runbook (home server, v1 PoC)

**Target:** Linux box (assumes Ubuntu Server 22.04/24.04 — adapt package
commands for other distros) already running a Chia full node, hosted on a home
network, serving as the **central oracle** per ADR-0004 / HANDOVER_2026-06-11.

**Threat model:** the oracle is internet-reachable (Trees in the field must
POST to it). The #1 risk of home hosting is an attacker pivoting from the
exposed service into the home LAN. Everything below is structured around:
(1) no inbound ports open on the home router at all, (2) the oracle process
can't touch anything even if compromised, (3) the Chia wallet on the same box
holds as little as possible.

**Production host:** `https://oracle.theorchard.network` (Cloudflare Tunnel,
Option A below); the public placeholder site is `theorchard.network`.

**Last reviewed:** 2026-06-15 — updated for secp256r1 device signatures
(ADR-0007), automatic schema migration on startup (the T3 `last_seq` column is
added by an idempotent `ALTER`, no manual SQL), and the `oracle.app.admin`
node-management CLI. The oracle is a **transitional bridge** (ADR-0004) toward
the serverless target (ADR-0008) — scoped to be decommissioned, not grown.

---

## Part 0 — Architecture decision: how the internet reaches the oracle

Two options. **Option A is strongly recommended for home hosting.**

### Option A (recommended): Cloudflare Tunnel — zero open inbound ports
`cloudflared` runs on the server and makes an *outbound* connection to
Cloudflare's edge. Trees POST to `https://oracle.theorchard.network`; Cloudflare
terminates TLS and relays through the tunnel. Result:
- **No port forwarding on your router. Nothing inbound. Ever.**
- Your home IP is never published (it's not in DNS).
- Free tier. Free TLS. Free DDoS absorption. Free WAF + rate-limiting rules.
- When you move to a hosted server later, you move the tunnel credential —
  Trees never need re-pointing.

Cost: a domain name (~$10/yr) on Cloudflare DNS. That's the entire PoC
infrastructure bill.

### Option B: Caddy reverse proxy + router port-forward (443 only)
Classic setup; automatic Let's Encrypt. Acceptable if you refuse a Cloudflare
dependency, but it opens a port to your home IP, publishes that IP in DNS, and
puts DDoS/scanning traffic directly on your router. For a home network: worse
on every axis that matters here. Not detailed further; ask if you want it.

### Regardless of option: isolate the box on the LAN
If your router supports VLANs or a guest/IoT network with client isolation,
put this server on it. The goal: even a full compromise of the server can't
reach your PCs/NAS/cameras. If your router can't do that, the firewall rules
in Part 2 are the fallback (egress restriction + no LAN-facing services), but
real network segmentation is better. This is the single highest-value
"no leakage into the home network" control.

---

## Part 1 — OS baseline

```bash
# Fresh updates + automatic security patching
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # enable

# Dedicated unprivileged user for the oracle (no shell login needed)
sudo useradd --system --create-home --home-dir /opt/orchard --shell /usr/sbin/nologin orchard
```

### SSH hardening (you'll administer this box for months)
`/etc/ssh/sshd_config.d/hardening.conf`:

```
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
AllowUsers <your-admin-username>
```

```bash
# Make sure your key works BEFORE applying, then:
sudo systemctl restart ssh
sudo apt install -y fail2ban && sudo systemctl enable --now fail2ban
```

### Firewall — default deny inbound, restrict what the box can reach
With Option A (tunnel), **no inbound service ports are needed at all**:

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp   # SSH from YOUR LAN subnet only — adjust subnet
sudo ufw enable
```

Notes:
- Do NOT allow 8000 (oracle) or 8444/8555/9256 (Chia RPCs) from anywhere.
  The oracle binds to localhost; cloudflared reaches it over loopback; Chia
  RPCs are localhost+mTLS already.
- Stricter egress (default-deny outgoing with allowlists for Chia peers,
  Cloudflare, apt, NTP) is possible but fiddly with Chia's peer mesh; skip for
  PoC, revisit on the hosted server.

---

## Part 2 — Install the oracle

```bash
sudo -u orchard -s /bin/bash    # work as the service user (temp shell)
cd /opt/orchard
git clone https://github.com/FlipThisCrypto/the-orchard.git app
cd app
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install \
  --require-hashes -r oracle/requirements.lock \
  --require-hashes -r orchard_chia/requirements.lock
# (dashboard/ is the local dev UI — not needed on the server)

# Config: copy the example env, then edit
cp oracle/.env.example oracle/.env
chmod 600 oracle/.env
exit
```

In `oracle/.env`, the critical production values:
- `host = 127.0.0.1` — **loopback only.** The tunnel/proxy is the only path in.
- `port = 8000`
- `require_wallet_session = true`
- `require_seq = true` once the whole fleet runs seq-capable firmware
  (v0.4.8+ ships `seq`; hardware-verified path is **v0.5.1**. Safe to flip
  on hosted oracle after confirming no pre-0.4.8 Trees remain.)
- `max_reading_future_seconds` (default **300**) rejects device clocks far
  ahead of the oracle; set `0` only if you must accept unsynced clocks
- `max_reading_body_bytes` (default **65536**) hard-caps POST /readings
- `provision_rate_limit_per_min` (default **30**) and
  `register_rate_limit_per_min` (default **20**) throttle remote claim-code
  and registration spam (loopback exempt)
- `GET /health` exposes non-secret `flags` + process `metrics` (accepted /
  replay rejections / rate_limited — counters only, no payloads)
  (v0.4.8+). It defaults **false** so a mixed-firmware fleet isn't locked out
  mid-rollout; flip it after every Tree is reflashed/OTA'd.
- `db_url = sqlite:////opt/orchard/data/orchard.db` (note the **four** slashes —
  `sqlite:///` + an absolute `/opt/...` path). Create the dir owned by
  `orchard`, `chmod 700`.

The schema migrates itself on startup — idempotent `ALTER`s add any missing
columns (including the T3 `last_seq` column), so an existing DB never needs
hand-patching. Full Alembic migrations are T4. SQLite durability for a
long-running service: enable WAL once (T4 will make this automatic; until then):

```bash
sudo -u orchard sqlite3 /opt/orchard/data/orchard.db "PRAGMA journal_mode=WAL;"
```

---

## Part 3 — Hardened systemd unit

`/etc/systemd/system/orchard-oracle.service`:

```ini
[Unit]
Description=The Orchard — Oracle (FastAPI)
After=network-online.target
Wants=network-online.target

[Service]
User=orchard
Group=orchard
WorkingDirectory=/opt/orchard/app
ExecStart=/opt/orchard/app/.venv/bin/uvicorn oracle.app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# ---- Sandbox: a compromised oracle process can do almost nothing ----
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/opt/orchard/data
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
# Block the oracle process from initiating connections INTO the home LAN.
# Loopback stays available (Chia RPC, tunnel). Adjust to your subnets.
IPAddressDeny=192.168.0.0/16 10.0.0.0/8 172.16.0.0/12 169.254.0.0/16
IPAddressAllow=127.0.0.0/8 ::1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now orchard-oracle
curl -s http://127.0.0.1:8000/health    # sanity check
```

The `IPAddressDeny` line is your belt-and-suspenders for "no leakage into the
home network": even with code execution inside the process, it cannot open
sockets to LAN addresses. (Note: `IPAddressAllow` for loopback also permits
outbound internet since only RFC1918 is denied — that's intentional; the
oracle's Pass verification calls the MintGarden indexer.)

---

## Part 4 — Cloudflare Tunnel (Option A ingress)

One-time: add your domain to Cloudflare (free plan), then on the server:

```bash
# Install cloudflared (Cloudflare's apt repo)
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared

cloudflared tunnel login                 # one-time browser auth
cloudflared tunnel create orchard-oracle
cloudflared tunnel route dns orchard-oracle oracle.theorchard.network
```

`/etc/cloudflared/config.yml`:

```yaml
tunnel: orchard-oracle
credentials-file: /root/.cloudflared/<TUNNEL-UUID>.json

ingress:
  - hostname: oracle.theorchard.network
    service: http://127.0.0.1:8000
  - service: http_status:404      # everything else
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
curl -s https://oracle.theorchard.network/health   # end-to-end check
```

### Edge protections (Cloudflare dashboard, free tier)
- SSL/TLS mode: **Full (strict)** is ideal but loopback HTTP behind a tunnel
  is fine — the tunnel itself is encrypted; set SSL mode to Full.
- **WAF custom rules:** allow only the paths Trees and tooling actually use
  (`/readings`, `/register`, `/auth/*`, `/health`, `/nodes*`, `/uptime*`,
  `/attestations*`, `/network*`); block the rest. Block requests with no
  `X-Orchard-Node` header to `/readings` if you want to be aggressive.
- **Rate limiting rule:** e.g. 60 requests / 1 min per IP on `/readings` —
  generous for real Trees (one POST per sample interval), hostile to floods.
- Turn OFF Cloudflare features that mangle bodies (Rocket Loader, email
  obfuscation — they're HTML features, harmless to a JSON API, but keep the
  zone minimal). **Never enable anything that rewrites request bodies** — both
  the HMAC envelope and the device's secp256r1 reading signature (ADR-0007)
  are over the exact bytes; a single rewritten byte fails verification.

---

## Part 5 — Coexisting with the Chia full node (wallet hygiene)

The same box runs the full node, wallet, and DataLayer — that's required for
the Season attestation writer and payout script. Contain the blast radius:

1. **Dedicated payout wallet key.** Create a fresh key (new fingerprint) used
   ONLY by The Orchard. Your personal Chia funds live elsewhere — different
   key, ideally different machine.
2. **Fund it thinly.** Keep roughly one payout cycle of $JUICE + a small XCH
   fee balance. Top it up on your schedule. Worst-case wallet compromise =
   one cycle of rewards, not the treasury.
3. **RPC certs stay where Chia puts them** (`~/.chia/mainnet/config/ssl/`),
   owned by the user running Chia, mode 600. The payout/attestation scripts
   run as that user, manually, per your decision — they are NOT a service and
   the `orchard` service user has no read access to wallet certs. Keep it
   that way: the internet-facing process and the money-moving credentials
   never share a uid.
4. Chia's RPCs bind to localhost with mTLS by default — verify nothing in
   `~/.chia/mainnet/config/config.yaml` was changed to `0.0.0.0` except the
   full-node P2P port (8444), which is the only Chia port that may face the
   internet (and even that can stay outbound-only; you'll just hold fewer
   peers).
5. `chia keys` mnemonics: never on this box in a file. If the wallet key was
   imported here, that's acceptable for PoC given (2), but the long-term plan
   (hosted server) should move payouts to an offline-signing flow.

---

## Part 6 — Backups & monitoring (minimum viable)

**Canonical runbook:** [`docs/ops/ORACLE_BACKUP_RESTORE.md`](ops/ORACLE_BACKUP_RESTORE.md)  
**Tool:** `python -m tools.oracle_backup` (SQLite online backup API + integrity_check + restore-drill).

```bash
# On the oracle host, from the repo root:
python -m tools.oracle_backup backup \
  --db /opt/orchard/data/orchard.db \
  --dest /opt/orchard/backups \
  --keep 14

# Off-box copy, then prove restore (never points at the live DB):
python -m tools.oracle_backup restore-drill \
  --backup /path/to/offbox/orchard-YYYYMMDD-HHMMSSZ.db \
  --scratch /tmp/orchard-restore-scratch.db
```

Legacy one-liner (acceptable if the Python tool is not yet deployed on the box):

```bash
# /etc/cron.daily/orchard-backup  (chmod +x)
#!/bin/sh
set -e
STAMP=$(date +%F)
DEST=/opt/orchard/backups
mkdir -p "$DEST"
sqlite3 /opt/orchard/data/orchard.db ".backup '$DEST/orchard-$STAMP.db'"
find "$DEST" -name 'orchard-*.db' -mtime +14 -delete
```

- Copy backups off-box (even `scp` to your desktop weekly). The DB **is** the
  uptime ledger — losing it loses unattested Season credit.
- A backup that has never passed `restore-drill` is **not** a backup.
- Also back up: `oracle/.env`, the oracle's attestation signing key, the
  cloudflared tunnel credential JSON (`python -m tools.oracle_backup companion-list`).
- Monitoring for PoC: a free uptime pinger (e.g. UptimeRobot) on
  `https://oracle.theorchard.network/health`, and `journalctl -u orchard-oracle`
  when something looks off. Defer real metrics to the hosted move.
- **Node housekeeping:** `python -m oracle.app.admin list` shows every
  registered Tree with reading/uptime counts and last-seen age;
  `... keep <node_id> …` (or `... delete <node_id> …`) prunes stale ones —
  e.g. orphaned identities left behind when a Tree's NVS is wiped by a full
  reflash and it re-registers under a new node_id. Dry-run by default; `--yes`
  backs up the DB first and cascades across readings/uptime/attestations.

---

## Part 7 — Final checklist

- [ ] Server on isolated VLAN/IoT network (or accepted fallback: ufw + IPAddressDeny)
- [ ] Router has **zero** port forwards to this box (Option A)
- [ ] SSH: key-only, no root, LAN-only, fail2ban running
- [ ] unattended-upgrades enabled
- [ ] `orchard` system user, nologin shell; app + data under /opt/orchard, env 600
- [ ] Oracle bound to 127.0.0.1; reachable only via tunnel
- [ ] systemd unit has the full sandbox block incl. IPAddressDeny for RFC1918
- [ ] Cloudflare: tunnel up, WAF path allowlist, rate limit on /readings, no body-rewriting features
- [ ] `require_wallet_session=true` (and `require_seq=true` after fleet update)
- [ ] Fleet on v0.4.8+ (secp256r1 keys) before flipping `require_seq`
- [ ] Trees re-pointed at `https://oracle.theorchard.network`; one signed `/readings` verified end-to-end
- [ ] Dedicated, thinly-funded payout wallet key; certs unreadable by `orchard` user
- [ ] Chia RPCs on localhost only; only P2P 8444 internet-facing (optional)
- [ ] Daily SQLite backup + off-box copy; tunnel credential + .env backed up
- [ ] /health monitored externally

**Migration note:** when you move to a hosted server, this entire runbook
transfers — copy `/opt/orchard/data`, the env, and the tunnel credential;
Trees keep posting to the same hostname and never notice.
