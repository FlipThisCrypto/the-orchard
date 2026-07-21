# SPDX-License-Identifier: Apache-2.0
"""Sanity: import surface for orchard_chia.datalayer."""
import orchard_chia.datalayer as dl

def test_package_schema_version():
    assert dl.SCHEMA_VERSION == "1.0.0"
