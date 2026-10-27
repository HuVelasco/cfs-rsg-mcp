"""Non-live server validation tests.

These exercise the argument-validation paths that return an honest error BEFORE
any COM session is opened, so they run without a licensed CFS. The server module
imports cleanly off-Windows because pywin32/COM is imported lazily inside
cfs_session().
"""
import json

import pytest

try:
    from cfs_rsg_mcp import server
    _HAVE_SERVER = True
except Exception:  # noqa: BLE001
    _HAVE_SERVER = False

pytestmark = pytest.mark.skipif(not _HAVE_SERVER, reason="server import failed")


def test_find_wall_substitutions_rejects_bad_wall_type():
    # wall_type is required; an invalid value returns an honest error, never a
    # silent 'exterior' default — and it happens before any CFS session.
    out = json.loads(server.find_wall_substitutions(
        standard="600S200-43", height_ft=10.0, wall_type="bogus"))
    assert out["success"] is False
    assert "wall_type" in out["error"]


def test_find_wall_substitutions_rejects_bad_governing():
    out = json.loads(server.find_wall_substitutions(
        standard="600S200-43", height_ft=10.0, wall_type="exterior",
        governing="sideways"))
    assert out["success"] is False
    assert "governing" in out["error"]


def test_design_wall_rejects_missing_wall_type():
    out = json.loads(server.design_wall(
        height_ft=10.0, spacing_in=16.0, wall_type="bogus"))
    assert out["success"] is False
    assert "wall_type" in out["error"]


def test_wall_type_is_required_positionally():
    # Omitting wall_type entirely is a hard TypeError (required parameter), not a
    # silent default — the strongest possible "no silent default" guarantee.
    with pytest.raises(TypeError):
        server.find_wall_substitutions(standard="600S200-43", height_ft=10.0)
