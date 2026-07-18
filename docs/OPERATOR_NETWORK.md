# Operator path — hosted network

> **Plant on the shared Orchard network** (not a local-only oracle).  
> Local bring-up remains in [`OPERATOR_QUICKSTART.md`](./OPERATOR_QUICKSTART.md).  
> Pre-alpha: expect rough edges; [open an issue](https://github.com/FlipThisCrypto/the-orchard/issues) when something breaks.

## Done means

- [ ] Firmware flashed from the browser flasher  
- [ ] Tree on Wi‑Fi with a **claim code**  
- [ ] Tree **claimed** to your wallet via an **Orchard Pass**  
- [ ] Tree visible on Orchard View and posting readings  
- [ ] Tree left online for **≥1 Season** (~24h uptime cycle)  

## Readiness (60 seconds)

| Need | Notes |
|------|--------|
| Board | ESP32 **WROOM** or **ESP32-S3** + **data** USB cable |
| Browser | Chrome / Edge / Brave / Opera on **desktop** (Web Serial; not Firefox/Safari/mobile) |
| Pass | [Genesis Orchard Passes on MintGarden](https://mintgarden.io/collections/the-orchard---genesis-passes-col1a56lp9zufakywlq4k5nntu3nd7k6jy2pe6ee23046ydlahmungqslvmj29) |
| Wallet | Chia wallet with WalletConnect (e.g. Sage or Goby) |
| Network | 2.4 GHz Wi‑Fi where the Tree will live |
| Mindset | Pre-alpha OK |

## Links

| Step | URL |
|------|-----|
| Home | https://theorchard.network/ |
| Flash | https://flash.theorchard.network/ |
| Claim | https://oracle.theorchard.network/claim |
| View | https://view.theorchard.network/ |
| Worldview | https://worldview.theorchard.network/ |
| Issues | https://github.com/FlipThisCrypto/the-orchard/issues |

## Steps

1. **Hardware** — BYO ESP32 (WROOM or S3). Sensors optional for first bring-up; reference builds often add air-quality + GPS later.  
2. **Pass + wallet** — Pass in a WalletConnect-capable wallet.  
3. **Flash** — Open the flasher in a supported browser, plug in USB, install. Chip family is detected automatically.  
4. **Boot** — Complete Wi‑Fi / first-boot as the firmware prompts; note the **claim code**.  
5. **Claim** — Open the claim page, connect wallet, prove Pass, bind the Tree.  
6. **Verify** — Confirm the Tree on [Orchard View](https://view.theorchard.network/). Public location is coarse (~5 km); precise GPS is owner-only.  
7. **Season** — Leave the Tree online ~24h+ so uptime can accumulate for a Season.  

## Troubleshooting (common)

| Symptom | Try this |
|---------|----------|
| Flasher can't see the board | Chrome/Edge/Brave/Opera on **desktop**; data USB cable (not charge-only); close other serial monitors; another port/cable |
| Firefox / Safari / phone | Web Serial unsupported — use a desktop Chromium browser |
| Wrong board family | Flasher picks ESP32 vs S3 from the chip; confirm you have WROOM or ESP32-S3 |
| No claim code | Finish Wi‑Fi / first-boot; power-cycle; re-flash if the board never completes identity mint |
| Claim / wallet fails | Pass must be in the **connected** wallet; try Sage or Goby; confirm claim code matches the Tree |
| Claimed but nothing on View | Wait a few minutes for readings; confirm Wi‑Fi; open https://view.theorchard.network/ ; note `node_id` if you have it |
| "How big is the network?" | Small and honest — use View / worldview, not marketing maps |

## Help

Use the **Planter friction** issue template on GitHub (or open a normal issue with: board, browser/OS, step, error text).  
No seed phrases, no Wi‑Fi passwords, no precise GPS in public issues.

## Honest scope

The Orchard is early environmental DePIN infrastructure on Chia. The token ($JUICE) supports the network; it is not a passive-income pitch. Network size is small on purpose while the path is proven — see live View, not invented maps.
