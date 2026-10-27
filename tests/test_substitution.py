"""Pure-Python tests for the substitution decision logic and sign convention.

These do NOT require CFS or COM, so they run anywhere (CI included). They cover
the Phase-1 acceptance criteria that can be checked without a live engine:
depth matching, spacing sweep, efficiency selection, alignment, gauge ceiling,
ambiguous-Mx flagging, and the axial sign convention.
"""
import pytest

from cfs_rsg_mcp import substitution as sub
from cfs_rsg_mcp.cfs_loads import resolve_axial_force


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_depth_and_mils_from_name():
    assert sub.depth_from_name("600SLS250-43_18_GA") == 6.0
    assert sub.depth_from_name("362SLS155-33_20_GA") == 3.62
    assert sub.mils_from_name("600SLS250-43_18_GA") == 43
    assert sub.mils_from_name("400S300-68") == 68
    assert sub.mils_from_name("335SLC-54_16_GA") == 54


# ---------------------------------------------------------------------------
# Depth matching — nearest AVAILABLE depth, no hardcoded table
# ---------------------------------------------------------------------------
def test_nearest_depth_picks_closest_available():
    # reference 4.0" with available 3.62 and 6.0 -> 3.62 is nearer
    assert sub.nearest_depth(4.0, [3.62, 6.0, 9.84]) == 3.62
    # reference 6.0 exact match
    assert sub.nearest_depth(6.0, [3.62, 6.0, 9.84]) == 6.0


def test_nearest_depth_tie_prefers_deeper():
    # 5.0 is equidistant from 4.0 and 6.0 -> deeper (6.0) wins
    assert sub.nearest_depth(5.0, [4.0, 6.0]) == 6.0


# ---------------------------------------------------------------------------
# Spacing list
# ---------------------------------------------------------------------------
def test_default_spacings_puts_original_first_and_dedupes():
    assert sub.default_spacings(16) == [16.0, 12.0, 24.0]
    assert sub.default_spacings(20) == [20.0, 12.0, 16.0, 24.0]


# ---------------------------------------------------------------------------
# Spacing row ratios scale with (ref_spacing / spacing)
# ---------------------------------------------------------------------------
def test_spacing_row_scaling_and_pass():
    # capacity ratio 1.0 at ref spacing 16"; at 24" it should exceed 1.0
    row24 = sub.spacing_row(24, 16, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, "both")
    assert row24["axial_ratio"] == pytest.approx(16 / 24, abs=1e-3)
    assert not row24["passes"]           # 0.667 < 1.0
    row12 = sub.spacing_row(12, 16, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, "both")
    assert row12["axial_ratio"] == pytest.approx(16 / 12, abs=1e-3)
    assert row12["passes"]               # 1.333 >= 1.0


def test_spacing_row_governing_axial_ignores_mx():
    # P passes, Mx fails; governing='axial' should pass, 'both' should not
    r_axial = sub.spacing_row(16, 16, 1.2, 0.5, 1.0, 1.0, 1.0, 1.0, "axial")
    r_both = sub.spacing_row(16, 16, 1.2, 0.5, 1.0, 1.0, 1.0, 1.0, "both")
    assert r_axial["passes"] and not r_both["passes"]


# ---------------------------------------------------------------------------
# Ambiguous Mx flag — P passes, Mx fails (no auto-accept)
# ---------------------------------------------------------------------------
def test_ambiguous_mx_flag_fires():
    flag = sub.ambiguous_mx_flag(axial_ok=True, flexural_ok=False, flexural_ratio=0.96)
    assert flag and "96%" in flag
    assert sub.ambiguous_mx_flag(True, True, 1.1) is None
    assert sub.ambiguous_mx_flag(False, False, 0.3) is None


# ---------------------------------------------------------------------------
# Efficiency selection — lightest weight/ft, single preferred at comparable weight
# ---------------------------------------------------------------------------
def _row(section, category, weight_psf, passes=True):
    return {"section": section, "category": category, "spacing_in": 16,
            "weight_psf": weight_psf, "passes": passes, "axial_ratio": 1.1,
            "flexural_ratio": 1.1, "flags": []}


def test_select_lightest_when_no_tie():
    rows = [_row("A", "single", 2.0), _row("B", "double", 1.0)]
    # double is clearly lighter (>5% lighter) -> it wins
    assert sub.select_most_efficient(rows)["section"] == "B"


def test_select_prefers_single_at_comparable_weight():
    # single only 3% heavier than the lightest double -> single preferred
    rows = [_row("dbl", "double", 1.00), _row("sgl", "single", 1.03)]
    assert sub.select_most_efficient(rows)["section"] == "sgl"


def test_select_none_when_empty():
    assert sub.select_most_efficient([]) is None


# ---------------------------------------------------------------------------
# Cross-floor stud alignment
# ---------------------------------------------------------------------------
def test_alignment_flag_when_misaligned_no_transfer():
    # 16" selected vs 24" adjacent, no transfer member -> flag
    assert sub.alignment_flag(16, 24, load_transfer_present=False)


def test_alignment_ok_when_multiple():
    # 24" selected lands on every 12" adjacent stud -> aligned, no flag
    assert sub.alignment_flag(24, 12, load_transfer_present=False) is None
    assert sub.alignment_flag(16, 16, load_transfer_present=False) is None


def test_alignment_suppressed_by_transfer_member():
    assert sub.alignment_flag(16, 24, load_transfer_present=True) is None


# ---------------------------------------------------------------------------
# Gauge ceiling
# ---------------------------------------------------------------------------
def test_gauge_ceiling_flag_when_ref_exceeds_library():
    # reference 97 mil, library tops out at 68 -> ceiling flag
    assert sub.gauge_ceiling_flag(97, [33, 43, 54, 68], 6.0)
    # reference within range -> no flag
    assert sub.gauge_ceiling_flag(54, [33, 43, 54, 68], 6.0) is None


# ---------------------------------------------------------------------------
# Axial sign convention (Fix 2)
# ---------------------------------------------------------------------------
def test_auto_negative_warns_and_stays_tension():
    P, warns = resolve_axial_force(-2.6, "auto")
    assert P == -2.6
    assert warns and "TENSION" in warns[0]


def test_auto_positive_no_warning():
    P, warns = resolve_axial_force(2.6, "auto")
    assert P == 2.6 and warns == []


def test_force_type_compression_makes_positive():
    P, warns = resolve_axial_force(-2.6, "compression")
    assert P == 2.6 and warns == []


def test_force_type_tension_makes_negative():
    P, warns = resolve_axial_force(2.6, "tension")
    assert P == -2.6 and warns == []


def test_force_type_invalid_raises():
    with pytest.raises(ValueError):
        resolve_axial_force(2.6, "sideways")


def test_none_force_passthrough():
    assert resolve_axial_force(None, "auto") == (None, [])
