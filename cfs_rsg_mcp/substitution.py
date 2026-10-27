# FILE: substitution.py
# PURPOSE: Pure-Python decision logic for find_wall_substitutions - depth
#          matching, spacing sweep, efficiency selection, cross-floor stud
#          alignment, gauge-ceiling detection, and the two output tables.
# STATUS: Version 0.1.0.2 - Developed (Phase 1 fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# This module NEVER touches COM. server.py evaluates each candidate's member
# strengths through CFS, then hands the plain numbers to these helpers so the
# selection rules are deterministic and unit-testable off-Windows.

from __future__ import annotations

import re
from typing import Dict, List, Optional

# Default candidate spacings (inches). The reference wall's own spacing is added
# in front of this list by default_spacings(); the caller can override the whole
# list with an explicit allowed_spacings argument.
BASE_SPACINGS = [12.0, 16.0, 24.0]

# Category preference: single is preferred over double at comparable weight.
CATEGORY_RANK = {"single": 0, "standard": 1, "double": 2}

# When choosing the most efficient passing candidate, a single section is
# preferred over a double as long as it is no more than this fraction heavier.
SINGLE_PREFERENCE_TOL = 0.05


# ---------------------------------------------------------------------------
# Parsing helpers (MSBS filenames + standard designations)
# ---------------------------------------------------------------------------
def depth_from_name(name: str) -> float:
    """Nominal depth (in) from a section name. '600SLS250-43_18_GA' -> 6.0."""
    m = re.match(r"\s*(\d{2,4})", name or "")
    return int(m.group(1)) / 100.0 if m else -1.0


def mils_from_name(name: str) -> Optional[int]:
    """Design thickness in mils from a name. '600SLS250-43_18_GA' -> 43.

    Matches the first '-NN' group (the SSMA-style mil designator), so both
    '600S200-43' and '600SLS250-43_18_GA' resolve to 43.
    """
    m = re.search(r"-(\d{2,3})", name or "")
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Depth matching - nearest AVAILABLE depth, never a hardcoded lookup
# ---------------------------------------------------------------------------
def nearest_depth(ref_depth_in: float, available_depths: List[float]) -> Optional[float]:
    """Return the available depth closest to the reference depth.

    Computed from the library's actual depths - there is NO fixed 4->3-5/8 table.
    Ties resolve to the deeper section (more conservative for flexure).
    """
    depths = sorted({round(float(d), 4) for d in available_depths if d and d > 0})
    if not depths:
        return None
    return min(depths, key=lambda d: (abs(d - ref_depth_in), -d))


# ---------------------------------------------------------------------------
# Spacing list
# ---------------------------------------------------------------------------
def default_spacings(original_spacing_in: float) -> List[float]:
    """[original_spacing, 12, 16, 24] with the original first and duplicates removed."""
    out: List[float] = []
    for s in [float(original_spacing_in)] + BASE_SPACINGS:
        s = round(float(s), 4)
        if s > 0 and s not in out:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# One (section x spacing) row of the candidate matrix
# ---------------------------------------------------------------------------
def spacing_row(spacing_in: float, ref_spacing_in: float,
                cap_axial_ratio: float, cap_flexural_ratio: float,
                cap_stiffness_ratio: float, member_weight_plf: float,
                ref_weight_psf: float, tol_factor: float,
                governing: str) -> Dict:
    """Compute ratios and pass/fail for one section evaluated AT one spacing.

    Member strength does not depend on spacing, so the per-foot capacity ratio at
    spacing s scales the reference-spacing ratio by (ref_spacing_in / s).
    """
    scale = ref_spacing_in / spacing_in
    aP = cap_axial_ratio * scale
    aMx = cap_flexural_ratio * scale
    aIx = cap_stiffness_ratio * scale
    weight_psf = member_weight_plf * 12.0 / spacing_in
    axial_ok = aP >= tol_factor
    flexural_ok = aMx >= tol_factor
    passes = _passes(axial_ok, flexural_ok, governing)
    return {
        "spacing_in": spacing_in,
        "axial_ratio": round(aP, 3),
        "flexural_ratio": round(aMx, 3),
        "stiffness_ratio": round(aIx, 3),
        "weight_psf": round(weight_psf, 4),
        "lighter_than_original": bool(ref_weight_psf and weight_psf < ref_weight_psf),
        "axial_ok": axial_ok,
        "flexural_ok": flexural_ok,
        "passes": passes,
    }


def _passes(axial_ok: bool, flexural_ok: bool, governing: str) -> bool:
    if governing == "axial":
        return axial_ok
    if governing == "flexural":
        return flexural_ok
    return axial_ok and flexural_ok


def ambiguous_mx_flag(axial_ok: bool, flexural_ok: bool,
                      flexural_ratio: float) -> Optional[str]:
    """Flag a P-passes / Mx-fails row as ambiguous for BOTH wall types.

    There is no auto-accept for interior walls; the caller decides.
    """
    if axial_ok and not flexural_ok:
        pct = round(flexural_ratio * 100)
        return (f"Mx fails at {pct}% of required capacity - P passes. Confirm "
                f"acceptability for this wall type before use (interior walls may "
                f"have little/no Mx demand, but that call is the engineer's).")
    return None


# ---------------------------------------------------------------------------
# Efficiency selection - lightest weight/ft of wall, single preferred
# ---------------------------------------------------------------------------
def select_most_efficient(passing_rows: List[Dict],
                          tol: float = SINGLE_PREFERENCE_TOL) -> Optional[Dict]:
    """Pick the most efficient passing (section x spacing) row.

    Efficiency = weight per ft^2 of wall (member_weight_plf x 12 / spacing).
    A single section is preferred over a double when it is within `tol` of the
    lightest candidate's weight.
    """
    if not passing_rows:
        return None
    by_weight = sorted(
        passing_rows,
        key=lambda r: (r["weight_psf"], CATEGORY_RANK.get(r.get("category"), 9)),
    )
    best = by_weight[0]
    if best.get("category") == "double":
        ceiling = best["weight_psf"] * (1.0 + tol)
        singles = [r for r in by_weight
                   if r.get("category") == "single" and r["weight_psf"] <= ceiling]
        if singles:
            return singles[0]
    return best


# ---------------------------------------------------------------------------
# Cross-floor stud alignment
# ---------------------------------------------------------------------------
def alignment_flag(selected_spacing_in: Optional[float],
                   adjacent_spacing_in: Optional[float],
                   load_transfer_present: bool) -> Optional[str]:
    """Flag when the selected stud spacing does not stack with the adjacent floor.

    Only relevant when the level above/below has NO load-transfer member (top
    track etc.) able to redistribute point loads across studs - then a point load
    must land directly on a stud. If the stud lines don't stack and there is no
    transfer member, the load path is unverified.

    Alignment rule (sufficient for this phase; full point-load-on-track solving is
    Phase 3): every selected stud lands on an adjacent stud line iff the selected
    spacing is an integer multiple of the adjacent spacing.
    """
    if load_transfer_present:
        return None
    if not selected_spacing_in or not adjacent_spacing_in:
        return None
    if adjacent_spacing_in <= 0:
        return None
    ratio = selected_spacing_in / adjacent_spacing_in
    aligned = selected_spacing_in >= adjacent_spacing_in and \
        abs(round(ratio) - ratio) < 1e-6
    if aligned:
        return None
    return (f"Stud spacing ({selected_spacing_in}\") does not align with the "
            f"adjacent floor's stud spacing ({adjacent_spacing_in}\"), and no "
            f"load-transfer member is present - point loads may not land on a "
            f"stud. Verify load path.")


# ---------------------------------------------------------------------------
# Gauge ceiling
# ---------------------------------------------------------------------------
def gauge_ceiling_flag(ref_mils: Optional[int],
                       candidate_mils: List[Optional[int]],
                       matched_depth_in: float) -> Optional[str]:
    """Flag when the reference gauge exceeds the heaviest MSBS gauge at this depth.

    Returned up front so 'no solution' is never confused with 'the library simply
    does not go that thick'.
    """
    avail = [m for m in candidate_mils if m is not None]
    if ref_mils is None or not avail:
        return None
    max_avail = max(avail)
    if ref_mils > max_avail:
        return (f"Reference gauge {ref_mils} mil exceeds the heaviest MSBS gauge "
                f"available at {matched_depth_in}\" depth ({max_avail} mil). No "
                f"MSBS single/double section can match the reference on gauge; "
                f"the tool falls back to standard sections at this depth.")
    return None


# ---------------------------------------------------------------------------
# Plain-language status for the final (client-facing) table
# ---------------------------------------------------------------------------
def plain_status(passes: bool, flagged: bool, any_passing_in_set: bool) -> str:
    """Human-readable status column for the always-returned substitution table."""
    if passes and not flagged:
        return "Pass"
    if passes and flagged:
        return "Pass - flagged, see notes"
    if any_passing_in_set:
        return "Not selected"
    return "No passing solution - closest shown"
