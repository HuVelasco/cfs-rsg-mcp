"""Pure-Python tests for wall-spec and designation parsing.

These do NOT require CFS or COM, so they run anywhere (CI included).
"""
from cfs_rsg_mcp import catalog
from cfs_rsg_mcp.cfs_engine import parse_designation, resolve_spec


def test_parse_designation_basic():
    p = parse_designation("600S200-43")
    assert p["depth_in"] == 6.0
    assert p["flange_in"] == 2.0
    assert p["type_letter"] == "S"
    assert p["mils"] == 43


def test_parse_designation_rejects_junk():
    assert parse_designation("not-a-section") is None


def test_resolve_spec_name_and_int():
    assert resolve_spec("AISI2024USASD") == 42
    assert resolve_spec(37) == 42 - 5  # 37
    # default = code of record: AISI S100-16 (2020) w/S2-20 (IBC 2024)
    assert resolve_spec(None) == 27


def test_wall_spec_compound_spacing():
    # 2'-6" O.C. must be 30 in, not 6 in
    r = catalog.parse_wall_spec("400S300-68 @ 2'-6\" O.C.")
    assert r["spacing_in"] == 30.0
    assert r["designation"] == "400S300-68"


def test_wall_spec_height_arithmetic():
    # 10'-8" minus 4.5" = 123.5 in = 10.2917 ft
    r = catalog.parse_wall_spec("10'-8\" minus 4.5\", 400S300-68 @ 2'-6\" O.C.")
    assert abs(r["height_ft"] - 10.2917) < 1e-3
    assert r["spacing_in"] == 30.0
    assert r["warnings"] == []


def test_wall_spec_simple():
    r = catalog.parse_wall_spec("10ft tall, 600S200-43 @ 16 O.C.")
    assert r["height_ft"] == 10.0
    assert r["spacing_in"] == 16.0
    assert r["designation"] == "600S200-43"
