from orchard_chia.datalayer import schema
def test_schema_version_semver():
    parts = schema.SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
