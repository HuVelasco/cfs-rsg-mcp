"""Acceptance tests T1-T9 (production issue report, Cortlandt Storage).

Common inputs: interior wall, h = 9.75 ft, spacing = 24", trib_A = trib_B = 5 ft,
brace = 48in, spec = AISI2016USASD, D_wall = 6 psf, L_lat = 5 psf.

The load-combination / accumulation / watchdog parts are pure Python and run
everywhere (CI included). The capacity parts (T1, T2-UC, T9-UC) need a licensed
CFS 15 and the bundled MSBS sections; they are marked `live` and skip cleanly
when either is unavailable.
"""
import json
import os
import time

import pytest

from cfs_rsg_mcp import stacked, watchdog
from cfs_rsg_mcp.cfs_loads import build_combo_table, lateral_effects

H, SP, TRIB = 9.75, 24.0, 5.0
GEOM = {"height_ft": H, "spacing_in": SP, "trib_A_ft": TRIB, "trib_B_ft": TRIB,
        "wall_type": "interior"}
BASE = {"D_wall": 6.0, "L_lat": 5.0}
P_WALL = 6.0 * H * (SP / 12.0) / 1000.0            # 0.117 k

_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "Info-and-Examples", "Sections")
S43 = os.path.join(_ROOT, "SINGLE", "362SLS155-43_18_GA.cfss")
B43 = os.path.join(_ROOT, "B2B", "362SLS155-43_18_GA.cfss")
B54 = os.path.join(_ROOT, "B2B", "362SLS155-54_16_GA.cfss")


def _combo(table, cid):
    return next(c for c in table["combos"] if c["combo"] == cid)


def _near(a, b, rel=0.01):
    return abs(a - b) <= rel * abs(b)


# ---------------------------------------------------------------------------
# Live fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfs_live():
    try:
        from cfs_rsg_mcp import server  # noqa: F401
        from cfs_rsg_mcp.cfs_engine import cfs_session
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"server / pywin32 not importable: {e}")
    if not all(os.path.isfile(p) for p in (S43, B43, B54)):
        pytest.skip("MSBS example sections (Info-and-Examples/Sections) not present")
    try:
        with cfs_session():
            pass
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"CFS 15 not available / not licensed: {e}")
    from cfs_rsg_mcp import server
    return server


# ---------------------------------------------------------------------------
# T1 - Spec: AISI2016USASD is the default; Pa = 2.658 k, UC = 0.796
# ---------------------------------------------------------------------------
@pytest.mark.live
def test_T1_default_spec_is_S100_16(cfs_live):
    d = json.loads(cfs_live.check_stud(
        section=S43, height_ft=H, spacing_in=SP, brace_config="48in",
        P_k=2.117, force_type="compression"))
    assert d["success"], d.get("error")
    assert d["spec_used"]["name"] == "AISI2016USASD"       # default, echoed
    assert _near(d["strengths"]["Pa_k"], 2.658)
    assert _near(d["governing_UC"], 0.796)


def test_T1_spec_mismatch_warns():
    from cfs_rsg_mcp.cfs_engine import resolve_spec, spec_code_check
    assert resolve_spec(None) == 27
    ok = spec_code_check(27, "IBC2024", "7-22")
    assert ok["warnings"] == [] and ok["errors"] == []
    bad = spec_code_check(resolve_spec("AISI2024USASD"), "IBC2024", "7-22")
    assert any("SPEC MISMATCH" in w for w in bad["warnings"])


# ---------------------------------------------------------------------------
# T2 - Combo 1 "D + L" contains NO roof load
# ---------------------------------------------------------------------------
T2_LOADS = {**BASE, "D_A": 161.7, "D_B": 161.7, "L_A": 250, "L_B": 250,
            "Lr": 40, "S": 49.6}


def test_T2_combo_D_plus_L_excludes_roof():
    t = build_combo_table(T2_LOADS, GEOM, snow_level="strength")
    c = _combo(t, "2a")
    assert c["label"] == "ASCE 7-22 Eq. 2a: D + L"
    assert abs(c["P_k"] - 8.351) < 5e-4                 # not 9.151 (D+L+S)
    assert abs(c["Mx_kin"] - 1.426) < 5e-4              # L_lat moment stays in D+L
    assert not ({"Lr", "S", "R"} & set(c["factors"]))


@pytest.mark.live
def test_T2_combo_D_plus_L_uc(cfs_live):
    d = json.loads(cfs_live.check_stud(
        section=B54, height_ft=H, spacing_in=SP, brace_config="48in",
        trib_A_ft=TRIB, trib_B_ft=TRIB, wall_type="interior", loads=T2_LOADS))
    assert d["success"], d.get("error")
    c = next(r for r in d["combos"] if r["combo"] == "2a")
    assert abs(c["P_k"] - 8.351) < 5e-4
    assert _near(c["UC"], 0.974)
    assert d["governing_combo"]["combo"] == "2a"


# ---------------------------------------------------------------------------
# T3 - Snow over trib_A + trib_B
# ---------------------------------------------------------------------------
def test_T3_snow_uses_both_tributaries():
    t = build_combo_table({**BASE, "S": 49.6}, GEOM, asce_edition="7-22",
                          snow_level="strength")
    c = _combo(t, "3a")
    assert c["roof_term"]["governing"] == "S"
    assert abs(c["roof_term"]["P_k"] - 0.7 * 49.6 * 10 * 2 / 1000) < 1e-4   # 0.694 k
    assert abs(c["roof_term"]["P_k"] - 0.694) < 5e-4


# ---------------------------------------------------------------------------
# T4 - Lr vs 0.7S: the governing one, never both
# ---------------------------------------------------------------------------
def test_T4_roof_governing_never_both():
    t = build_combo_table({**BASE, "Lr": 40, "S": 49.6}, GEOM, snow_level="strength")
    c = _combo(t, "3a")
    assert c["roof_term"]["governing"] == "Lr"
    assert abs(c["roof_term"]["P_k"] - 0.800) < 1e-6
    assert abs(c["roof_term"]["alternatives_P_k"]["S"] - 0.6944) < 1e-4
    assert "Lr" in c["factors"] and "S" not in c["factors"]
    assert abs(c["P_k"] - (P_WALL + 0.800)) < 1e-4
    for row in t["combos"]:                              # no combo sums Lr and S
        assert not ({"Lr", "S"} <= set(row["factors"]))


# ---------------------------------------------------------------------------
# T5 - Seismic: Eq. 8 = D + 0.7Ev + 0.7Eh
# ---------------------------------------------------------------------------
def test_T5_seismic_eq8():
    loads = {**BASE, "D_A": 60, "D_B": 60, "Eh": 0.1, "Ev": 0.04}
    t = build_combo_table(loads, GEOM)
    c = _combo(t, "8")
    D = P_WALL + 60 * TRIB * 2 * (SP / 12) / 1000          # 1.317 k
    assert abs(c["P_k"] - D * (1 + 0.7 * 0.04)) < 1e-4
    M_expected, _ = lateral_effects(0.7 * 0.1 * 6.0, SP, H)  # 0.7 x 0.1 x wall weight
    assert abs(c["Mx_kin"] - M_expected) < 1e-4
    c10 = _combo(t, "10")
    assert abs(c10["P_k"] - D * (0.6 - 0.7 * 0.04)) < 1e-4
    c9 = _combo(t, "9")
    assert c9["factors"]["Ev"] == 0.525 and c9["factors"]["Eh"] == 0.525


# ---------------------------------------------------------------------------
# T6 - Eq. 9 with ASD-level snow -> 0.1/0.7 = 0.143
# ---------------------------------------------------------------------------
def test_T6_eq9_asd_snow_factor():
    t = build_combo_table({**BASE, "S": 34.7}, GEOM, snow_level="asd")
    c = _combo(t, "9")
    assert abs(c["factors"]["S"] - 0.143) < 5e-4
    assert t["load_level_factors"]["snow_level"] == "asd"
    # and with strength-level snow Eq. 9 applies 0.1 exactly
    t2 = build_combo_table({**BASE, "S": 49.6}, GEOM, snow_level="strength")
    assert _combo(t2, "9")["factors"]["S"] == 0.1


# ---------------------------------------------------------------------------
# T7 - User combos: exactly as given, nothing hidden
# ---------------------------------------------------------------------------
USER = [{"label": "D+L only", "factors": {"D": 1, "L": 1, "L_lat": 1}},
        {"label": "D+0.75L+0.75Lr", "factors": {"D": 1, "L": 0.75, "L_lat": 0.75, "Lr": 0.75}}]


def test_T7_user_combos_exact():
    t = build_combo_table(T2_LOADS, GEOM, combo_mode="user", user_combos=USER)
    assert [c["label"] for c in t["combos"]] == ["D+L only", "D+0.75L+0.75Lr"]
    assert t["combos"][0]["factors"] == {"D": 1.0, "L": 1.0, "L_lat": 1.0}
    assert t["combos"][1]["factors"] == {"D": 1.0, "L": 0.75, "L_lat": 0.75, "Lr": 0.75}
    assert abs(t["combos"][0]["P_k"] - 8.351) < 5e-4
    assert abs(t["combos"][1]["P_k"] - (3.351 + 0.75 * 5.0 + 0.75 * 0.8)) < 5e-4
    assert "no hidden" in t["load_level_factors"]["user_combo_note"]
    # wind factor given as 1 is applied at 1 (no hidden 0.6)
    tw = build_combo_table({**BASE, "W_cc_w": 30}, GEOM, combo_mode="user",
                           user_combos=[{"label": "W", "factors": {"W_cc_w": 1}}])
    assert abs(tw["combos"][0]["lateral_psf"] - 30.0) < 1e-9


def test_T7_user_combos_validation():
    with pytest.raises(ValueError, match="unknown factor key"):
        build_combo_table(BASE, GEOM, combo_mode="user",
                          user_combos=[{"label": "x", "factors": {"Snow": 1}}])
    t = build_combo_table(BASE, GEOM, combo_mode="user",
                          user_combos=[{"label": "uplift", "factors": {"D": -0.6}}])
    assert any("negative factor on D" in w for w in t["warnings"])
    with pytest.raises(ValueError):
        build_combo_table(BASE, GEOM, combo_mode="user", user_combos=None)
    ap = build_combo_table(BASE, GEOM, combo_mode="append", user_combos=USER)
    assert len(ap["combos"]) == 15 + 2 and ap["combos"][-1]["label"] == "D+0.75L+0.75Lr"


# ---------------------------------------------------------------------------
# T8 - Timeout: simulated COM hang returns within the timeout with partials
# ---------------------------------------------------------------------------
def test_T8_watchdog_hang_returns_partial():
    t0 = time.monotonic()
    jr = watchdog.run(watchdog.selftest_hang_job, {"n_before_hang": 2}, timeout_s=3)
    elapsed = time.monotonic() - t0
    assert elapsed < 3 + 10          # timeout + process teardown margin
    assert not jr.ok and jr.timed_out and jr.engine_restarted
    assert len(jr.partial) == 2
    assert "HUNG-SECTION" in jr.error and "did not respond within 3 s" in jr.error


def test_T8_tool_level_timeout(monkeypatch):
    server = pytest.importorskip("cfs_rsg_mcp.server")
    # substitute the real COM job with the hang simulator (runs in the child)
    monkeypatch.setattr(server.jobs, "check_sections_job", watchdog.selftest_hang_job)
    t0 = time.monotonic()
    d = json.loads(server.check_stud(section="600S200-43", height_ft=H, P_k=1.0,
                                     force_type="compression", timeout_s=3))
    assert time.monotonic() - t0 < 3 + 10
    assert d["success"] is False and d["timed_out"] is True
    assert len(d["partial_results"]) == 2
    assert d["watchdog"]["last_activity"]["section"] == "HUNG-SECTION"
    assert d["spec_used"]["name"] == "AISI2016USASD"


def test_T8_watchdog_ok_and_error_paths():
    jr = watchdog.run(watchdog.selftest_ok_job, {"x": 4}, timeout_s=30)
    assert jr.ok and jr.result == {"value": 8} and jr.partial == [{"i": 4}]
    jr = watchdog.run(watchdog.selftest_error_job, {}, timeout_s=30)
    assert not jr.ok and not jr.timed_out and "simulated CFS failure" in jr.error


# ---------------------------------------------------------------------------
# T9 - Stacked wall: 3 levels
# ---------------------------------------------------------------------------
def _t9_levels(with_sections=False):
    base = {"height_ft": H, "spacing_in": SP, "trib_A_ft": TRIB, "trib_B_ft": TRIB}
    lv = [{**base, "name": "Level 3", "D": 60, "Lr": 40, "S": 49.6},
          {**base, "name": "Level 2", "D": 45, "L": 125},
          {**base, "name": "Level 1", "D": 45, "L": 125}]
    if with_sections:
        for x, s in zip(lv, (S43, B43, B54)):
            x["section"] = s
    return lv


T9_DEFAULTS = {"D_wall": 6.0, "L_lat": 5.0, "brace_config": "48in"}


def test_T9_accumulation():
    norm = [stacked.normalize_level(x, i, T9_DEFAULTS) for i, x in enumerate(_t9_levels())]
    acc = stacked.accumulate(norm, "interior", "base", True, 1.0, ["AB"])
    D = [a["effects_by_pattern"]["AB"]["P"]["D"] for a in acc]
    Lk = [a["effects_by_pattern"]["AB"]["P"]["L"] for a in acc]
    Lr = [a["effects_by_pattern"]["AB"]["P"]["Lr"] for a in acc]
    assert [round(x, 3) for x in D] == [1.317, 2.334, 3.351]
    assert [round(x, 3) for x in Lk] == [0.0, 2.5, 5.0]
    assert [round(x, 3) for x in Lr] == [0.8, 0.8, 0.8]          # roof carried down
    llr = acc[2]["llr"]                                          # per-stud AT, no reduction
    assert llr["AT_ft2"] == 40.0 and llr["floors_supported"] == 2
    assert all(f["factor"] == 1.0 for f in llr["floors"])
    mid = stacked.accumulate(norm, "interior", "mid_height", True, 1.0, ["AB"])
    assert abs(mid[2]["effects_by_pattern"]["AB"]["P"]["D"] - (3.351 - P_WALL / 2)) < 1e-4


@pytest.mark.live
def test_T9_stacked_wall_ucs(cfs_live):
    d = json.loads(cfs_live.design_stacked_wall(
        levels=_t9_levels(with_sections=True), wall_type="interior",
        level_defaults=T9_DEFAULTS))
    assert d["success"], d.get("error")
    uc = {r["level"]: r["UC"] for r in d["summary"]}
    assert _near(uc["Level 3"], 0.886)
    assert _near(uc["Level 2"], 0.784)
    assert _near(uc["Level 1"], 0.974)
    row = d["summary"][0]
    for col in ("Pa_k", "Pa_dist_k", "Ma_Fy_kin", "Ma_dist_kin", "Ma_brc_kin", "Va_k",
                "web_crippling_UC", "stiffener_required", "deflection_in", "L_over"):
        assert col in row
