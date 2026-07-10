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
