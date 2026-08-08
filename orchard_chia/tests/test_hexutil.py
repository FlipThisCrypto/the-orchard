# SPDX-License-Identifier: Apache-2.0
from orchard_chia.datalayer.hexutil import is_compressed_p256_pubkey, is_hex


def test_is_hex():
    assert is_hex("ab")
    assert not is_hex("a")
    assert not is_hex("zz")
    assert is_hex("ab", length=2)
    assert not is_hex("ab", length=4)


def test_p256_pubkey():
    assert is_compressed_p256_pubkey("02" + "ab" * 32)
    assert is_compressed_p256_pubkey("03" + "00" * 32)
    assert not is_compressed_p256_pubkey("01" + "ab" * 32)
    assert not is_compressed_p256_pubkey("02" + "ab" * 31)
