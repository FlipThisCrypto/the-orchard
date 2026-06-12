# SPDX-License-Identifier: Apache-2.0
"""TLS-wrapped HTTP client for the Chia reference wallet RPC.

Default port is 9256. mutual-TLS via the operator's wallet cert + key,
which are referenced by absolute path in ``orchard_chia/config.yaml``.

Used by:
  - orchard_chia.nft.mint    — mints Orchard Pass NFTs (nft_mint_nft)
  - orchard_chia.nft.verify  — checks NFT ownership for the oracle's
                                /register gate
  - orchard_chia.payout      — builds $JUICE CAT spend bundles (Phase 7)
"""
from __future__ import annotations

import requests
import urllib3

# Self-signed local CA — silence the legitimate-but-noisy warning.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class WalletRpcError(RuntimeError):
    """Raised for any non-2xx wallet RPC response or transport failure."""


# verify=False (no server-cert verification) is only safe on localhost.
# Over a network it allows a MITM to impersonate the wallet daemon and
# observe/suppress spends — refuse it for non-loopback hosts.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class WalletRpc:
    def __init__(self, host: str, port: int, cert_path: str, key_path: str,
                 fingerprint: int = 0):
        self._host = host
        self._port = port
        self._cert = (cert_path, key_path)
        self._fingerprint = fingerprint

    def _url(self, route: str) -> str:
        return f"https://{self._host}:{self._port}/{route.lstrip('/')}"

    def _post(self, route: str, body: dict, timeout: int = 60) -> dict:
        if self._host not in _LOOPBACK_HOSTS:
            raise WalletRpcError(
                f"refusing verify=False against non-loopback host "
                f"{self._host!r}: wallet RPC over a network needs real TLS "
                f"verification (a MITM could observe or suppress $JUICE "
                f"spends). Only localhost mTLS may run with cert "
                f"verification disabled."
            )
        try:
            r = requests.post(
                self._url(route),
                json=body,
                cert=self._cert,
                verify=False,
                timeout=timeout,
            )
        except requests.RequestException as e:
            raise WalletRpcError(f"wallet {route} unreachable: {e}") from e
        if r.status_code != 200:
            raise WalletRpcError(f"wallet {route} -> {r.status_code}: {r.text}")
        data = r.json()
        if not data.get("success", True):
            raise WalletRpcError(f"wallet {route} success=false: {data}")
        return data

    # ------------------------------------------------------------------
    # Discovery / identity
    # ------------------------------------------------------------------

    def get_wallets(self, wallet_type: int | None = None) -> list[dict]:
        """List all wallets in the current key. ``wallet_type``:
            0 = standard XCH
            6 = CAT
            9 = DID
            10 = NFT
        """
        body: dict = {}
        if wallet_type is not None:
            body["type"] = wallet_type
        return self._post("get_wallets", body).get("wallets", [])

    def first_nft_wallet_id(self) -> int:
        """Return the wallet_id of the first NFT wallet, creating one
        lazily if needed isn't supported here — the reference wallet
        usually auto-creates an NFT wallet on first NFT receipt.
        """
        wallets = self.get_wallets(wallet_type=10)
        if not wallets:
            raise WalletRpcError(
                "no NFT wallet found. Create one in the Chia wallet GUI / CLI: "
                "`chia wallet show` then follow prompts to enable NFT support."
            )
        return int(wallets[0]["id"])

    def get_next_address(self, wallet_id: int = 1) -> str:
        body = {"wallet_id": wallet_id, "new_address": False}
        return self._post("get_next_address", body)["address"]

    # ------------------------------------------------------------------
    # NFT minting
    # ------------------------------------------------------------------

    def nft_mint_nft(
        self,
        *,
        wallet_id: int,
        target_address: str,
        royalty_address: str,
        uris: list[str],
        meta_uris: list[str],
        hash: str,             # noqa: A002 — Chia's field name
        meta_hash: str,
        edition_number: int = 1,
        edition_total: int = 1,
        license_uris: list[str] | None = None,
        license_hash: str = "",
        royalty_percentage: int = 0,
        did_id: str | None = None,
        fee: int = 0,
    ) -> dict:
        """Mint a single Chia NFT1 via ``nft_mint_nft`` RPC.

        ``hash`` and ``meta_hash`` are SHA-256 hex of the actual files
        the URIs point at. The chain stores these hashes alongside the
        URIs so consumers can verify the URI contents haven't drifted.

        Returns the RPC response (includes ``spend_bundle`` and the
        new ``nft_id`` once the spend is processed by the full node).
        """
        body: dict = {
            "wallet_id": wallet_id,
            "target_address": target_address,
            "royalty_address": royalty_address,
            "uris": uris,
            "meta_uris": meta_uris,
            "hash": hash,
            "meta_hash": meta_hash,
            "edition_number": edition_number,
            "edition_total": edition_total,
            "royalty_percentage": royalty_percentage,
            "fee": fee,
        }
        # Chia's RPC parses ``license_hash`` as bytes32 — an empty
        # string fails the conversion (`ValueError: bad bytes32
        # initializer b''`). Only include license_uris/license_hash
        # when they actually have content; the same applies to did_id.
        if license_uris:
            body["license_uris"] = license_uris
        if license_hash:
            body["license_hash"] = license_hash
        if did_id:
            body["did_id"] = did_id
        return self._post("nft_mint_nft", body, timeout=180)

    def nft_mint_bulk(
        self,
        *,
        wallet_id: int,
        metadata_list: list[dict],
        royalty_address: str = "",
        royalty_percentage: int = 0,
        target_list: list[str] | None = None,
        mint_number_start: int = 1,
        mint_total: int | None = None,
        did_id: str | None = None,
        fee: int = 0,
    ) -> dict:
        """Mint a batch of NFTs in a single Chia transaction.

        The DID (if the NFT wallet is bound to one) gets spent exactly
        once for the whole batch, which avoids the "DID is not
        currently spendable" error you hit firing off individual
        `nft_mint_nft` calls back-to-back.

        Each entry in ``metadata_list`` is one mint's worth of fields:
            {
              "uris":          [data uris],
              "hash":          "<sha256 hex>",
              "meta_uris":     [meta uris],
              "meta_hash":     "<sha256 hex>",
              "license_uris":  [license uris]      (optional),
              "license_hash":  "<sha256 hex>"      (optional),
              "edition_number": <int>,
              "edition_total":  <int>,
            }

        ``target_list`` is one address per mint — they don't have to
        be the same. Omit to send all to the operator's default address.
        """
        body: dict = {
            "wallet_id": wallet_id,
            "metadata_list": metadata_list,
            "royalty_address": royalty_address,
            "royalty_percentage": royalty_percentage,
            "mint_number_start": mint_number_start,
            "fee": fee,
        }
        if target_list:
            body["target_list"] = target_list
        if mint_total is not None:
            body["mint_total"] = mint_total
        if did_id:
            body["did_id"] = did_id
        return self._post("nft_mint_bulk", body, timeout=300)

    def nft_transfer_nft(
        self,
        *,
        wallet_id: int,
        nft_coin_id: str,
        target_address: str,
        fee: int = 0,
    ) -> dict:
        """Transfer one NFT to ``target_address``. Used for burning
        (target = the null-puzzle-hash burn address) and for delivering
        Passes from the minting wallet to operator wallets.

        ``nft_coin_id`` is the launcher coin id (the ``nft1...`` bech32
        when run through nft_get_info, or its hex form here).
        """
        body = {
            "wallet_id": wallet_id,
            "nft_coin_id": nft_coin_id,
            "target_address": target_address,
            "fee": fee,
        }
        return self._post("nft_transfer_nft", body, timeout=120)

    # ------------------------------------------------------------------
    # NFT discovery (used by ownership verification)
    # ------------------------------------------------------------------

    def nft_get_nfts(self, wallet_id: int, start_index: int = 0,
                    num: int = 50) -> list[dict]:
        """Page through NFTs owned by a wallet. Each item includes the
        NFT's metadata URIs, on-chain coin info, and collection hint.
        """
        body = {"wallet_id": wallet_id, "start_index": start_index, "num": num}
        return self._post("nft_get_nfts", body).get("nft_list", [])

    def nft_get_info(self, coin_id: str) -> dict:
        """Look up a single NFT by its coin id (or launcher id)."""
        body = {"coin_id": coin_id}
        return self._post("nft_get_info", body).get("nft_info", {})

    # ------------------------------------------------------------------
    # CAT (Phase 7 payout)
    # ------------------------------------------------------------------

    def cat_get_asset_id(self, wallet_id: int) -> str:
        body = {"wallet_id": wallet_id}
        return self._post("cat_get_asset_id", body).get("asset_id", "")

    def find_cat_wallet_id_by_asset(self, asset_id_hex: str) -> int | None:
        """Iterate every CAT wallet and return the wallet_id whose
        asset_id matches the given $JUICE-style hex. Returns None if
        no CAT wallet for that asset exists in this key.
        """
        asset_id_hex = asset_id_hex.lower().replace("0x", "")
        for w in self.get_wallets(wallet_type=6):
            wid = int(w["id"])
            try:
                aid = self.cat_get_asset_id(wid).lower().replace("0x", "")
            except WalletRpcError:
                continue
            if aid == asset_id_hex:
                return wid
        return None

    def cat_spend(
        self,
        *,
        wallet_id: int,
        inner_address: str,
        amount: int,                # CAT mojos (1 CAT = 1000 mojos for 3-decimal CATs)
        fee: int = 0,
        memos: list[str] | None = None,
    ) -> dict:
        """Send `amount` CAT mojos from `wallet_id` to `inner_address`.
        Returns the wallet RPC's response including transaction_id."""
        body: dict = {
            "wallet_id": wallet_id,
            "inner_address": inner_address,
            "amount": amount,
            "fee": fee,
        }
        if memos:
            body["memos"] = memos
        return self._post("cat_spend", body, timeout=120)
