# SPDX-License-Identifier: Apache-2.0
"""Orchard Chia DataLayer package (publish / attest / verify)."""
# Re-exported from schema — NOT a hand-maintained copy. A literal here silently
# drifted from the real schema version once already, and a mirrored literal is
# indistinguishable from a correct one until it is wrong.
from .schema import SCHEMA_VERSION  # noqa: F401

__all__ = ["SCHEMA_VERSION"]
