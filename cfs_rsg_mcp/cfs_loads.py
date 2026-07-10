# FILE: cfs_loads.py
# PURPOSE: Load-path helpers, unbraced-length rules, and ASD load combinations
#          for cold-formed steel wall-stud design.
# STATUS: Version 0.1.0.1 - Developed (bug-fix pass from tester report); not yet re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# Adapted directly from the user's CFS15_Wall_Stud_Design notebook so the MCP
# server produces the same forces the user already validated against Simpson CFS.
# Units: forces in kips, moments in kip-inches, lengths in inches unless noted.

from __future__ import annotations

from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Load-path helpers (a stud is analyzed as a simply-supported beam-column)
# ---------------------------------------------------------------------------
def axial_plf(psf: float, trib_ft: float) -> float:
    """PSF area load -> PLF delivered along the wall top plate (lb/ft)."""
    return psf * trib_ft


def axial_stud(plf: float, spacing_in: float) -> float:
    """PLF along wall -> axial force per stud (kips)."""
    return plf * (spacing_in / 12.0) / 1000.0


def lateral_stud_moment(psf: float, spacing_in: float, height_ft: float) -> float:
    """Lateral PSF (wind/seismic) -> Mx (k-in) at mid-height (simply supported)."""
    w = psf * (spacing_in / 12.0) / 1000.0      # k/ft uniform load on the stud
    return (w * height_ft ** 2 / 8.0) * 12.0    # k-in


def lateral_stud_shear(psf: float, spacing_in: float, height_ft: float) -> float:
    """Lateral PSF -> Vy (kips) at stud ends."""
    w = psf * (spacing_in / 12.0) / 1000.0
    return w * height_ft / 2.0


# ---------------------------------------------------------------------------
# Unbraced lengths
# ---------------------------------------------------------------------------
BRACE_CONFIGS = ("none", "midpoint", "third", "quarter", "48in", "60in", "72in", "custom")


def unbraced_lengths(height_ft: float, brace_config: str = "none",
                     custom_n: Optional[int] = None) -> Dict[str, float]:
    """Return {Lx, Ly, Lt, Lm} in inches.

    Lx (strong-axis buckling) is ALWAYS the full height - wall sheathing/bridging
    does not brace strong-axis flexural buckling. Ly, Lt, Lm are set by the
    bridging/bracing configuration.

    brace_config: 'none' | 'midpoint' | 'third' | 'quarter' |
                  '48in' | '60in' | '72in' | 'custom' (needs custom_n)
    """
    H = height_ft * 12.0
    fixed = {
        "none": H, "midpoint": H / 2.0, "third": H / 3.0, "quarter": H / 4.0,
        "48in": 48.0, "60in": 60.0, "72in": 72.0,
    }
    if brace_config == "custom":
        if not custom_n or custom_n < 1:
            raise ValueError("custom_n must be >= 1 when brace_config='custom'")
        Lb = H / custom_n
    elif brace_config in fixed:
        Lb = fixed[brace_config]
    else:
        raise ValueError(
            f"Unknown brace_config '{brace_config}'. Use one of: {', '.join(BRACE_CONFIGS)}"
        )
    Lb = min(Lb, H)
    return {"Lx": H, "Ly": Lb, "Lt": Lb, "Lm": Lb}


# ---------------------------------------------------------------------------
# ASD load combinations (ASCE 7, matching the notebook / Simpson CFS reference)
# ---------------------------------------------------------------------------
DEFAULT_LOADS = {
    "D_wall": 12.0,   # wall self-weight (psf)
    "D_A": 0.0, "L_A": 0.0,   # dead / live, side A (psf)
    "D_B": 0.0, "L_B": 0.0,   # dead / live, side B (interior walls only)
    "S": 0.0,                 # roof snow (psf)
    "W_uplift": 0.0,          # roof wind uplift (psf)
    "W_mwfw": 0.0, "W_mwfl": 0.0,   # MWFRS windward / leeward lateral (psf)
    "W_cc_w": 0.0, "W_cc_l": 0.0,   # C&C windward / leeward lateral (psf)
    "L_lat": 0.0,             # interior lateral live (psf)
    "Eh": 0.0, "Ev": 0.0,     # seismic horizontal / vertical coefficients
}


def build_load_combinations(loads: Dict, geometry: Dict) -> List[Dict]:
    """Build ASD load combos. Returns list of
    {'combo', 'label', 'P_k', 'Mx_kin', 'Vy_k'} per stud.
    """
    L = {**DEFAULT_LOADS, **(loads or {})}
    H = geometry["height_ft"]
    sp = geometry["spacing_in"]
    tA = geometry.get("trib_A_ft", 0.0)
    tB = geometry.get("trib_B_ft", 0.0)
    wtype = geometry.get("wall_type", "exterior")

    def P(D_coeff=1.0, L_coeff=1.0, S_coeff=1.0, uplift_coeff=0.0):
        P_wall = L["D_wall"] * H * (sp / 12.0) / 1000.0
        P_DA = axial_stud(axial_plf(L["D_A"], tA), sp)
        P_DB = axial_stud(axial_plf(L["D_B"], tB), sp) if wtype == "interior" else 0.0
        P_LA = axial_stud(axial_plf(L["L_A"], tA), sp)
        P_LB = axial_stud(axial_plf(L["L_B"], tB), sp) if wtype == "interior" else 0.0
        P_S = axial_stud(axial_plf(L["S"], tA), sp)
        P_up = axial_stud(axial_plf(L["W_uplift"], tA), sp)
        return (D_coeff * (P_wall + P_DA + P_DB)
                + L_coeff * (P_LA + P_LB)
                + S_coeff * P_S
                - uplift_coeff * P_up)

    def MV(lat_psf, factor=1.0):
        return (lateral_stud_moment(lat_psf * factor, sp, H),
                lateral_stud_shear(lat_psf * factor, sp, H))

    lat_E = L["Eh"] * L["D_wall"]
    combos: List[Dict] = []

    def add(n, label, P_k, MV_tuple):
        Mx, Vy = MV_tuple
        combos.append({"combo": n, "label": label,
                       "P_k": round(P_k, 5), "Mx_kin": round(Mx, 5),
                       "Vy_k": round(Vy, 5)})

    add(1, "D + L", P(), MV(L["L_lat"]) if wtype == "interior" else (0.0, 0.0))
    add(2, "D + (Lr or S)", P(L_coeff=0.0), (0.0, 0.0))
    add(3, "D + 0.75L + 0.75S + 0.75W_mwfw",
        P(L_coeff=0.75, S_coeff=0.75), MV(L["W_mwfw"], 0.75))
    add(4, "D + 0.75L + 0.75S + 0.75W_mwfl",
        P(L_coeff=0.75, S_coeff=0.75), MV(L["W_mwfl"], 0.75))
    add(5, "D + W_cc_w", P(L_coeff=0.0, S_coeff=0.0), MV(L["W_cc_w"]))
    add(6, "D + W_cc_l", P(L_coeff=0.0, S_coeff=0.0), MV(L["W_cc_l"]))
    add(7, "D + E", P(L_coeff=0.0, S_coeff=0.0), MV(lat_E))
    add(8, "D + 0.75L + 0.75S + 0.75E",
        P(L_coeff=0.75, S_coeff=0.75), MV(lat_E, 0.75))
    add(9, "0.6D + W_uplift",
        P(D_coeff=0.6, L_coeff=0.0, S_coeff=0.0, uplift_coeff=1.0), MV(L["W_mwfw"]))
    add(10, "0.6D + W_cc_l", P(D_coeff=0.6, L_coeff=0.0, S_coeff=0.0), MV(L["W_cc_l"]))
    return combos
