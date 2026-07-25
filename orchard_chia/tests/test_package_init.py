# SPDX-License-Identifier: Apache-2.0
"""Sanity: import surface for orchard_chia.datalayer."""
import orchard_chia.datalayer as dl
from orchard_chia.datalayer import schema


def test_package_schema_version():
    assert dl.SCHEMA_VERSION == "1.1.0"


def test_package_version_is_the_same_object_as_schema():
    """Not a mirrored literal — the package re-exports the real constant, so the
    two can never drift (a mirrored literal is indistinguishable from a correct
    one until it is wrong)."""
    assert dl.SCHEMA_VERSION is schema.SCHEMA_VERSION
