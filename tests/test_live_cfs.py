"""Live integration tests — require a licensed, running CFS 15 (Automation option).

Run only these:   pytest -m live
Skip these:       pytest -m "not live"

They are auto-skipped when CFS/pywin32 is unavailable, so the suite stays green
on machines without CFS (e.g. CI).
"""
import json

import pytest

pytestmark = pytest.mark.live

try:
    from cfs_rsg_mcp import server
    from cfs_rsg_mcp.cfs_engine import cfs_session
    _HAVE = True
except Exception:  # noqa: BLE001  (pywin32 or COM missing)
    _HAVE = False


@pytest.fixture(scope="session")
def cfs():
    """Skip the test (at call time, not collection) unless CFS connects."""
    if not _HAVE:
        pytest.skip("pywin32 / CFS module not importable")
    try:
        with cfs_session():
            pass
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"CFS 15 not available / not licensed: {e}")
    return True


def test_connect(cfs):
    assert json.loads(server.connect_cfs())["success"] is True


def test_600s200_43_matches_ssma_table(cfs):
    d = json.loads(server.get_section_properties("600S200-43"))
    gp = d["gross_properties"]
    assert abs(gp["area_in2"] - 0.492) < 5e-3
    assert abs(gp["Ix_in4"] - 2.683) < 5e-3
    assert abs(gp["Sx_in3"] - 0.894) < 5e-3


def test_batch_zero_shear_is_honest(cfs):
    # A section with a BendRadius flag returns Vy=0; with no applied shear the
    # member must NOT report the bogus 9.999 — it should PASS on axial/bending.
    d = json.loads(server.check_members_batch([{
        "id": "M1", "section": "600S200-43",
        "forces": {"P_k": 2.0, "Mx_kin": 3.0}, "Lu_in": 48,
    }]))
    assert d["success"] is True
    assert d["members"][0]["UC"] < 9.0


def test_reference_n_doubles_reference_capacity(cfs):
    # Fix 1: reference_n=2 must materially change (roughly double) the reference
    # axial capacity vs reference_n=1 for the same standard wall.
    common = dict(standard="600S200-43", height_ft=10.0, wall_type="exterior",
                  spacing_in=16.0, brace_config="48in", depth_filter_in=[6.0])
    d1 = json.loads(server.find_wall_substitutions(reference_n=1, **common))
    d2 = json.loads(server.find_wall_substitutions(reference_n=2, **common))
    assert d1["success"] and d2["success"]
    p1 = d1["reference"]["axial_P_k"]
    p2 = d2["reference"]["axial_P_k"]
    assert p1 > 0
    assert abs(p2 - 2 * p1) / (2 * p1) < 1e-3     # ~2x stacked plies


def test_pk_sign_convention_and_warning(cfs):
    # Fix 2: same magnitude, opposite meaning. A negative P_k under 'auto' runs as
    # TENSION and warns; the compression case (positive / force_type) gives a
    # different, higher UC.
    neg = json.loads(server.check_stud(
        section="600S200-43", height_ft=10.0, brace_config="48in",
        P_k=-2.6, force_type="auto"))
    comp = json.loads(server.check_stud(
        section="600S200-43", height_ft=10.0, brace_config="48in",
        P_k=2.6, force_type="compression"))
    assert neg["success"] and comp["success"]
    assert any("TENSION" in w for w in neg["warnings"])
    assert comp["warnings"] == []
    assert comp["governing_UC"] != neg["governing_UC"]


def test_no_msbs_pass_falls_back_to_standard(cfs):
    # Fix 3: a very tall wall where no MSBS single/double passes must fall back to
    # standard sections rather than returning an empty/no-solution response.
    d = json.loads(server.find_wall_substitutions(
        standard="600S200-97", height_ft=24.0, wall_type="exterior",
        spacing_in=16.0, brace_config="none", depth_filter_in=[6.0]))
    assert d["success"] is True
    cats = {p.get("category") for p in d["profiles"]}
    # either a passing standard fallback appears, or the gauge-ceiling/fallback
    # flags explain why — never a silent empty result.
    assert d["substitution_table"]
    assert "standard" in cats or any("standard" in f for f in d["flags"])
