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


def test_unbraced_lengths_custom_uses_brace_n():
    # brace_n = number of equal unbraced segments (renamed from custom_n)
    L = unbraced_lengths(12, "custom", brace_n=4)
    assert L["Lx"] == 144.0                 # strong axis always full height
    assert abs(L["Ly"] - 36.0) < 1e-9       # 12 ft / 4 = 36 in


def test_unbraced_lengths_bad_config():
    with pytest.raises(ValueError):
        unbraced_lengths(10, "nonsense")


def test_load_combinations_count_and_shape():
    geom = {"height_ft": 10, "spacing_in": 16, "trib_A_ft": 10, "wall_type": "exterior"}
    combos = build_load_combinations({"D_A": 80, "L_A": 80, "W_cc_w": 30}, geom)
    # ASCE 7-22 ASD: 1a-4a, 5a/6a/7a x (MWFRS windward, leeward), 5a C&C x2, 8-10
    assert len(combos) == 15
    for c in combos:
        assert {"combo", "label", "code_ref", "factors", "P_k", "Mx_kin", "Vy_k"} <= set(c)
        assert c["label"].startswith("ASCE 7-22 Eq.")


def test_load_combinations_requires_wall_type():
    # wall_type must be explicit — no silent 'exterior' default
    geom = {"height_ft": 10, "spacing_in": 16, "trib_A_ft": 10}
    with pytest.raises(ValueError):
        build_load_combinations({"D_A": 80}, geom)


def test_load_combinations_gravity_is_compression_positive():
    # dead+live gravity must produce a POSITIVE (compression) axial P
    geom = {"height_ft": 10, "spacing_in": 16, "trib_A_ft": 10, "wall_type": "exterior"}
    combos = build_load_combinations({"D_A": 80, "L_A": 80}, geom)
    assert combos[0]["P_k"] > 0


def test_member_lengths_lm_defaults_to_lt():
    from cfs_rsg_mcp.jobs import member_lengths
    notes = []
    L = member_lengths({"id": "B1", "Lx_in": 120, "Ly_in": 48, "Lt_in": 48}, notes=notes)
    assert L == {"Lx": 120.0, "Ly": 48.0, "Lt": 48.0, "Lm": 48.0}
    assert len(notes) == 1 and "Lm" in notes[0]
    notes = []
    L = member_lengths({"id": "B2", "lengths": {"Lx_in": 120, "Ly_in": 48,
                                                "Lt_in": 48, "Lm_in": 24}}, notes=notes)
    assert L["Lm"] == 24.0 and notes == []
