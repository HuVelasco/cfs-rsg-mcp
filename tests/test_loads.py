"""Pure-Python tests for load-path helpers and unbraced-length rules."""
import pytest

from cfs_rsg_mcp.cfs_loads import (
    axial_plf,
    axial_stud,
    build_load_combinations,
    lateral_stud_moment,
    lateral_stud_shear,
    unbraced_lengths,
)


def test_axial_load_path():
    assert axial_plf(10, 10) == 100          # 10 psf over 10 ft trib = 100 plf
    assert abs(axial_stud(100, 16) - 0.1333) < 1e-3   # 100 plf @ 16 in -> kips


def test_lateral_moment_and_shear():
    # w = 10 psf * (16/12)/1000 k/ft ; M = wH^2/8 *12 ; V = wH/2
    m = lateral_stud_moment(10, 16, 10)
    v = lateral_stud_shear(10, 16, 10)
    assert m > 0 and v > 0
    assert abs(m - (10 * (16 / 12) / 1000) * 10 ** 2 / 8 * 12) < 1e-6


def test_unbraced_lengths_third():
    L = unbraced_lengths(10, "third")
    assert L["Lx"] == 120.0                   # strong axis always full height
    assert abs(L["Ly"] - 40.0) < 1e-9         # third points


def test_unbraced_lengths_custom_requires_n():
    with pytest.raises(ValueError):
        unbraced_lengths(10, "custom")


def test_unbraced_lengths_bad_config():
    with pytest.raises(ValueError):
        unbraced_lengths(10, "nonsense")


def test_load_combinations_count_and_shape():
    geom = {"height_ft": 10, "spacing_in": 16, "trib_A_ft": 10, "wall_type": "exterior"}
    combos = build_load_combinations({"D_A": 80, "L_A": 80, "W_cc_w": 30}, geom)
    assert len(combos) == 10
    for c in combos:
        assert {"combo", "label", "P_k", "Mx_kin", "Vy_k"} <= set(c)
