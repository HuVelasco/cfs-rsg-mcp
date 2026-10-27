"""Pure-Python tests for the 0.1.0.3 load / stacking / trace / spec features."""
import json
import math
import os

import pytest

from cfs_rsg_mcp import stacked
from cfs_rsg_mcp.cfs_engine import parse_strength_trace, spec_code_check, spec_info
from cfs_rsg_mcp.cfs_loads import (
    CANTILEVER_K,
    build_combo_table,
    deflection_cases,
    effective_length_factors,
    lateral_deflection_in,
    lateral_effects,
    stud_effects,
    validate_loads,
)

GEOM = {"height_ft": 10.0, "spacing_in": 16.0, "trib_A_ft": 10.0, "trib_B_ft": 6.0,
        "wall_type": "interior"}
FIX = os.path.join(os.path.dirname(__file__), "fixtures",
                   "trace_600S200-43_Lu120_AISI2016.txt")


def _c(t, cid):
    return next(c for c in t["combos"] if c["combo"] == cid)


# ---------------------------------------------------------------------------
# Load inputs
# ---------------------------------------------------------------------------
def test_unknown_and_negative_loads_rejected():
    with pytest.raises(ValueError, match="Unknown load key"):
        validate_loads({"Snow": 30})
    with pytest.raises(ValueError, match="negative"):
        validate_loads({"D_A": -5})


def test_exterior_ignores_side_B_and_roof_uses_trib_A_only():
    g = {**GEOM, "wall_type": "exterior"}
    e = stud_effects({"D_wall": 0, "D_B": 50, "L_B": 40, "S": 30}, g)
    assert e["P"]["D"] == 0 and e["P"]["L"] == 0
    assert abs(e["P"]["S"] - 30 * 10 * (16 / 12) / 1000) < 1e-9


def test_line_and_point_loads_at_top():
    e = stud_effects({"D_wall": 0}, GEOM, line_loads={"D": 150}, point_loads={"L": 0.4})
    assert abs(e["P"]["D"] - 150 * (16 / 12) / 1000) < 1e-9
    assert abs(e["P"]["L"] - 0.4) < 1e-9
    with pytest.raises(ValueError):
        stud_effects({}, GEOM, line_loads={"W": 1})


def test_live_patterns_A_and_B():
    L = {"D_wall": 0, "L_A": 40, "L_B": 100}
    ab = stud_effects(L, GEOM, live_pattern="AB")["P"]["L"]
    a = stud_effects(L, GEOM, live_pattern="A")["P"]["L"]
    b = stud_effects(L, GEOM, live_pattern="B")["P"]["L"]
    assert abs(ab - (a + b)) < 1e-12 and a > 0 and b > 0


def test_wind_levels_and_cc_bending_only():
    t = build_combo_table({"D_A": 20, "W_cc_w": 30, "W_mwfw": 20}, GEOM)
    cc = _c(t, "5a-cw")
    assert cc["P_k"] == 0.0 and cc["factors"] == {"W_cc_w": 0.6}   # C&C: bending alone
    assert _c(t, "5a-mw")["factors"]["W_mwfw"] == 0.6              # MWFRS with axial
    assert _c(t, "6a-mw")["factors"]["W_mwfw"] == 0.45
    ta = build_combo_table({"W_cc_w": 30}, GEOM, wind_level="asd")
    assert _c(ta, "5a-cw")["factors"] == {"W_cc_w": 1.0}


def test_uplift_only_in_eq7():
    t = build_combo_table({"D_wall": 0, "D_A": 10, "W_uplift": 30}, GEOM)
    assert "W_uplift" in _c(t, "7a-mw")["factors"]
    assert _c(t, "7a-mw")["P_k"] < 0                                # net tension
    assert "W_uplift" not in _c(t, "5a-mw")["factors"]


def test_asce_7_16_set():
    t = build_combo_table({"S": 30}, GEOM, asce_edition="7-16")
    assert t["options"]["snow_level"] == "asd"
    assert _c(t, "3")["label"].startswith("ASCE 7-16 Comb. 3")
    assert _c(t, "3")["roof_term"]["factor"] == 1.0                 # S at 1.0
    assert _c(t, "9")["factors"]["S"] == 0.75


def test_ibc_alternative_set():
    t = build_combo_table({"D_A": 10, "L_A": 40, "S": 30, "W_mwfw": 25}, GEOM,
                          combo_set="IBC2024_ALT_ASD")
    ids = {c["combo"] for c in t["combos"]}
    assert {"16-1", "16-2-mw", "16-3-mw", "16-4-mw", "16-5", "16-6"} <= ids
    assert abs(_c(t, "16-2-mw")["factors"]["W_mwfw"] - 0.6 * 1.3) < 1e-9
    assert _c(t, "16-1")["factors"]["L"] == 1.0
    assert "ibc_note" in t["load_level_factors"]


def test_mismatched_combo_set_warns():
    t = build_combo_table({}, GEOM, asce_edition="7-22", combo_set="ASCE7-16_ASD")
    assert any("does not match" in w for w in t["warnings"])


# ---------------------------------------------------------------------------
# Support / deflection
# ---------------------------------------------------------------------------
def test_cantilever_effects_and_k():
    Mp, Vp = lateral_effects(20, 16, 4, "pinned-pinned")
    Mc, Vc = lateral_effects(20, 16, 4, "cantilever")
    assert abs(Mc - 4 * Mp) < 1e-9 and abs(Vc - 2 * Vp) < 1e-9
    assert effective_length_factors("cantilever", "none") == \
        {"Kx": CANTILEVER_K, "Ky": CANTILEVER_K, "Kt": CANTILEVER_K}
    assert effective_length_factors("cantilever", "48in")["Ky"] == 1.0
    with pytest.raises(ValueError):
        lateral_effects(1, 16, 4, "fixed-fixed")


def test_deflection_formula_and_cases():
    d = lateral_deflection_in(5, 24, 9.75, 29500, 1.650425)
    assert abs(d - 0.04176) < 1e-4                      # 5wL^4/384EI, validated live
    dc = lateral_deflection_in(5, 24, 9.75, 29500, 1.650425, "cantilever")
    assert abs(dc / d - 384 / 40) < 1e-9
    cases = deflection_cases({"W_cc_w": 30, "L_lat": 5}, "strength", 0.7)
    w = next(c for c in cases if c["load"] == "W_cc_w")
    assert abs(w["factor"] - 0.42) < 1e-9 and abs(w["psf"] - 12.6) < 1e-9
    assert all(c["load"] != "W_mwfw" for c in cases)


# ---------------------------------------------------------------------------
# Live load reduction / stacking
# ---------------------------------------------------------------------------
def test_llr_rules():
    assert stacked.llr_factor(1, 300, 1, 50)[0] == 1.0                     # KLL*AT < 400
    f, _ = stacked.llr_factor(4, 400, 1, 50)                                # KLL*AT = 1600
    assert abs(f - (0.25 + 15 / math.sqrt(1600))) < 1e-9
    assert stacked.llr_factor(4, 10000, 1, 50)[0] == 0.5                   # one-floor floor
    assert stacked.llr_factor(4, 10000, 3, 50)[0] == 0.4                   # multi-floor floor
    assert stacked.llr_factor(4, 10000, 1, 125)[0] == 1.0                  # >100 psf one floor
    assert stacked.llr_factor(4, 10000, 2, 125)[0] == 0.8                  # >100 psf, max 20%
    assert stacked.llr_factor(4, 10000, 2, 50, reducible=False)[0] == 1.0
    assert stacked.llr_factor(4, 10000, 2, 50, enabled=False)[0] == 1.0


def test_stack_normalize_errors():
    with pytest.raises(ValueError, match="unknown key"):
        stacked.normalize_level({"height_ft": 9, "spacing_in": 16, "Dead": 5}, 0)
    with pytest.raises(ValueError, match="either"):
        stacked.normalize_level({"height_ft": 9, "spacing_in": 16, "D": 5, "D_A": 5}, 0)
    with pytest.raises(ValueError, match="height_ft"):
        stacked.normalize_level({"spacing_in": 16}, 0)


def test_stack_alignment_flags():
    lv = [stacked.normalize_level({"height_ft": 9, "spacing_in": s, "name": n}, i)
          for i, (s, n) in enumerate([(24, "L3"), (16, "L2"), (16, "L1")])]
    flags = stacked.alignment_flags(lv)
    assert len(flags) == 1 and flags[0]["between"] == ["L3", "L2"]
    lv[0]["load_transfer_present"] = True
    assert stacked.alignment_flags(lv)[0]["load_transfer_present"] is True


def test_stack_llr_reduces_when_area_is_large():
    lv = [stacked.normalize_level({"height_ft": 10, "spacing_in": 48, "trib_A_ft": 60,
                                   "trib_B_ft": 60, "L": 50, "D_wall": 0}, i)
          for i in range(3)]
    acc = stacked.accumulate(lv, "interior", KLL=4.0)
    llr = acc[2]["llr"]
    assert llr["floors_supported"] == 3 and llr["KLL_AT_ft2"] == 4 * 3 * 4 * 120
    assert llr["L_reduced_k"] < llr["L_unreduced_k"]


# ---------------------------------------------------------------------------
# CFS trace parsing (fixture = real CFS 15 trace, SSMA 600S200-43, Lu = 120")
# ---------------------------------------------------------------------------
def test_parse_strength_trace_fixture():
    p = parse_strength_trace(open(FIX, encoding="utf-8").read())
    assert p["axial"]["Pne"] == 4.4743 and p["axial"]["Pnl"] == 3.2054
    assert p["axial"]["Pnd"] == 10.339 and p["axial"]["omega"] == 1.8
    assert p["flex_x"]["Mnl"] == 15.135 and p["flex_x"]["Mnd"] == 25.05
    assert p["flex_x"]["omega"] == 1.67


def test_parse_strength_trace_fully_braced_and_empty():
    txt = ("Flexural Strength about Y-axis\n  Mne=12.931 k-in\tEq. F2.1-1\n"
           "  Fn = Fy, see fully braced strength report for Mnl calculation\n"
           "    Mnd=11.421 k-in\n    Ωb=1.67, φb=0.9\n")
    p = parse_strength_trace(txt)
    assert p["flex_y"]["Mnl_is_fully_braced"] is True and "Mnl" not in p["flex_y"]
    assert parse_strength_trace(None) == {} and parse_strength_trace("") == {}


# ---------------------------------------------------------------------------
# Spec / building code
# ---------------------------------------------------------------------------
def test_spec_info_and_code_checks():
    assert spec_info(27)["name"] == "AISI2016USASD" and spec_info(27)["is_default"]
    assert spec_code_check(27, "BCNYS2025", "7-22")["warnings"] == []
    assert spec_code_check(27, "NYSBC2025", "7-22")["building_code"]["name"] == "BCNYS2025"
    assert any("ASCE MISMATCH" in w for w in spec_code_check(27, "IBC2024", "7-16")["warnings"])
    assert spec_code_check(28, "IBC2024", "7-22", asd_combos=True)["errors"]   # LRFD + ASD
    assert spec_code_check(42, "none")["warnings"] == []
    with pytest.raises(ValueError):
        spec_code_check(27, "UBC1997")


# ---------------------------------------------------------------------------
# Server validation paths (no COM session is opened)
# ---------------------------------------------------------------------------
server = pytest.importorskip("cfs_rsg_mcp.server")


def test_check_stud_loads_mode_requires_wall_type():
    d = json.loads(server.check_stud(section="600S200-43", height_ft=10, loads={"D_A": 10}))
    assert d["success"] is False and "wall_type" in d["error"]


def test_check_stud_rejects_lrfd_with_asd_combos():
    d = json.loads(server.check_stud(section="600S200-43", height_ft=10, loads={"D_A": 10},
                                     wall_type="exterior", spec="AISI2016USLRFD"))
    assert d["success"] is False and "LRFD" in d["error"]


def test_design_wall_rejects_bad_user_combo():
    d = json.loads(server.design_wall(height_ft=10, spacing_in=16, wall_type="exterior",
                                      combo_mode="user",
                                      user_combos=[{"label": "x", "factors": {"Q": 1}}]))
    assert d["success"] is False and "unknown factor key" in d["error"]


def test_stacked_requires_sections_and_wall_type():
    lv = [{"height_ft": 9, "spacing_in": 16, "D": 10}]
    assert "wall_type" in json.loads(server.design_stacked_wall(lv, "bogus"))["error"]
    assert "section" in json.loads(server.design_stacked_wall(lv, "interior"))["error"]


def test_tool_docs_describe_load_conventions():
    for fn in (server.check_stud, server.design_wall, server.design_stacked_wall):
        doc = fn.__doc__
        assert "wind_level" in doc and "snow_level" in doc and "user_combos" in doc
        assert "C&C" in doc and "MWFRS" in doc and "Ev" in doc
