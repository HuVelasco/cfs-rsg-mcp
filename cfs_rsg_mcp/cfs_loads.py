# FILE: cfs_loads.py
# PURPOSE: Load-path helpers, unbraced-length rules, axial sign convention,
#          support conditions, deflection demand and ASD load combinations
#          (ASCE 7-22 / 7-16 / IBC alternative + user-defined) for cold-formed
#          steel wall-stud design.
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# Pure Python - no COM. Units: forces in kips, moments in kip-inches, lengths in
# inches unless noted, area loads in psf, line loads in plf.
#
# DESIGN: every load combination is a FACTOR TABLE applied to the unfactored
# per-stud load EFFECTS of each load type (stud_effects). Built-in code sets and
# user-defined combos go through the same code path, so a user combo gets
# exactly the factors it asks for and nothing else.

from __future__ import annotations

from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Load-path helpers (a stud is analyzed as a beam-column)
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
    """Lateral PSF -> Vy (kips) at stud ends (simply supported)."""
    w = psf * (spacing_in / 12.0) / 1000.0
    return w * height_ft / 2.0


# ---------------------------------------------------------------------------
# Axial sign convention (CFS15: compression POSITIVE, tension NEGATIVE)
# ---------------------------------------------------------------------------
def resolve_axial_force(P_k: Optional[float],
                        force_type: str = "auto") -> tuple:
    """Apply the CFS15 axial sign convention and return (P_signed, warnings).

    CFS15 uses compression-POSITIVE / tension-NEGATIVE. A caller who passes a
    compression load as a NEGATIVE number (e.g. copying an ETABS axial force
    without flipping the sign) would silently get a TENSION check and a wrong,
    usually much lower, unity check. This helper removes that trap.

    force_type:
      'compression' - treat the MAGNITUDE as compression -> +|P_k|.
      'tension'     - treat the MAGNITUDE as tension     -> -|P_k|.
      'auto'        - use the sign exactly as given (positive=compression,
                      negative=tension). If P_k is negative, a warning is
                      returned because the intent is ambiguous.
    """
    warnings: List[str] = []
    if P_k is None:
        return None, warnings
    ft = (force_type or "auto").lower()
    if ft == "compression":
        return abs(P_k), warnings
    if ft == "tension":
        return -abs(P_k), warnings
    if ft == "auto":
        if P_k < 0:
            warnings.append(
                "Negative P_k interpreted as TENSION per CFS15 convention "
                "(compression is POSITIVE). If this load is compression, pass a "
                "positive value or set force_type='compression'."
            )
        return P_k, warnings
    raise ValueError("force_type must be 'auto', 'compression', or 'tension'.")


# ---------------------------------------------------------------------------
# Unbraced lengths
# ---------------------------------------------------------------------------
BRACE_CONFIGS = ("none", "midpoint", "third", "quarter", "48in", "60in", "72in", "custom")


def unbraced_lengths(height_ft: float, brace_config: str = "none",
                     brace_n: Optional[int] = None) -> Dict[str, float]:
    """Return {Lx, Ly, Lt, Lm} in inches.

    Lx (strong-axis buckling) is ALWAYS the full height - wall sheathing/bridging
    does not brace strong-axis flexural buckling. Ly, Lt, Lm are set by the
    bridging/bracing configuration.

    brace_config: 'none' | 'midpoint' | 'third' | 'quarter' |
                  '48in' | '60in' | '72in' | 'custom' (needs brace_n)
    brace_n: number of EQUAL UNBRACED SEGMENTS when brace_config='custom'
             (e.g. 3 = braces at third points). This is a BRACING parameter
             only - it never multiplies plies, capacity, or weight.
             (Renamed from the ambiguous 'custom_n'.)
    """
    H = height_ft * 12.0
    fixed = {
        "none": H, "midpoint": H / 2.0, "third": H / 3.0, "quarter": H / 4.0,
        "48in": 48.0, "60in": 60.0, "72in": 72.0,
    }
    if brace_config == "custom":
        if not brace_n or brace_n < 1:
            raise ValueError("brace_n must be >= 1 when brace_config='custom'")
        Lb = H / brace_n
    elif brace_config in fixed:
        Lb = fixed[brace_config]
    else:
        raise ValueError(
            f"Unknown brace_config '{brace_config}'. Use one of: {', '.join(BRACE_CONFIGS)}"
        )
    Lb = min(Lb, H)
    return {"Lx": H, "Ly": Lb, "Lt": Lb, "Lm": Lb}


# ---------------------------------------------------------------------------
# Support conditions
# ---------------------------------------------------------------------------
SUPPORTS = ("pinned-pinned", "cantilever")

# Effective-length factor for a fixed-free (cantilever / parapet) stud. 2.0 is the
# theoretical value; 2.1 is the recommended design value (AISC Commentary Table
# C-A-7.1) because a real base is never perfectly fixed.
CANTILEVER_K = 2.1


def check_support(support: Optional[str]) -> str:
    s = (support or "pinned-pinned").lower()
    if s not in SUPPORTS:
        raise ValueError(f"support must be one of {SUPPORTS} (got {support!r}).")
    return s


def lateral_effects(psf: float, spacing_in: float, height_ft: float,
                    support: str = "pinned-pinned") -> tuple:
    """Lateral PSF -> (Mx k-in, Vy k) at the critical section for the support.

    pinned-pinned: M = wH^2/8 at mid-height, V = wH/2 at the ends.
    cantilever   : M = wH^2/2 at the fixed base, V = wH at the base.
    """
    s = check_support(support)
    w = psf * (spacing_in / 12.0) / 1000.0   # k/ft on the stud
    if s == "cantilever":
        return (w * height_ft ** 2 / 2.0) * 12.0, w * height_ft
    return (w * height_ft ** 2 / 8.0) * 12.0, w * height_ft / 2.0


def lateral_deflection_in(psf: float, spacing_in: float, height_ft: float,
                          E_ksi: float, I_in4: float,
                          support: str = "pinned-pinned") -> Optional[float]:
    """Beam-formula lateral deflection (in) for a uniform lateral load.

    This is ANALYSIS (the demand), not a section property: E and I come from CFS
    (I is CFS's effective Ix at the service moment).
    pinned-pinned: 5wL^4/(384EI); cantilever: wL^4/(8EI).
    """
    s = check_support(support)
    if not I_in4 or not E_ksi:
        return None
    w = psf * (spacing_in / 12.0) / 1000.0 / 12.0   # k/in
    L = height_ft * 12.0
    if s == "cantilever":
        return w * L ** 4 / (8.0 * E_ksi * I_in4)
    return 5.0 * w * L ** 4 / (384.0 * E_ksi * I_in4)


def effective_length_factors(support: str, brace_config: str = "none") -> Dict[str, float]:
    """K factors for MemberParams by support condition.

    pinned-pinned: Kx = Ky = Kt = 1.0.
    cantilever:    Kx = 2.1 (fixed-free, recommended design value). Ky/Kt = 2.1
                   only when the stud has NO intermediate bracing (full-height
                   free segment); with bridging/sheathing bracing the braced
                   segments are taken as Ky = Kt = 1.0 - an assumption returned
                   in the output notes for the engineer to confirm.
    """
    s = check_support(support)
    if s == "cantilever":
        kyt = CANTILEVER_K if (brace_config or "none") == "none" else 1.0
        return {"Kx": CANTILEVER_K, "Ky": kyt, "Kt": kyt}
    return {"Kx": 1.0, "Ky": 1.0, "Kt": 1.0}


# ---------------------------------------------------------------------------
# LOAD INPUT CONVENTIONS
# ---------------------------------------------------------------------------
# Every load is an UNSIGNED magnitude. Units: psf unless noted.
#   D_wall        wall self-weight (psf of wall face)
#   D_A, D_B      floor/roof dead load on side A / side B (psf)
#   L_A, L_B      floor live load on side A / side B (psf)
#   L_lat         lateral live load on interior walls, IBC 1607.15 (psf). It is
#                 part of L, so it is factored with L in every combo.
#   Lr, S, R      roof live / roof snow / rain (psf) - applied over trib_A +
#                 trib_B (trib_B is 0 for exterior walls), the same rule as D, L.
#                 Never added together: each combo takes "Lr or 0.7S or R"
#                 separately and uses the one that governs.
#   W_mwfw/W_mwfl MWFRS windward / leeward wall pressure. Used COMBINED with
#                 axial load (ASCE Eqs. 5a/6a/7a).
#   W_cc_w/W_cc_l Components & Cladding pressure. Used for BENDING ALONE (P = 0)
#                 and for DEFLECTION - never combined with axial load.
#   W_uplift      MWFRS roof uplift (psf over the roof tributary). Reduces axial
#                 compression; included only where it is unfavourable (0.6D+0.6W).
#   Eh            seismic horizontal coefficient (strength level). Out-of-plane
#                 lateral load = Eh x D_wall (Fp / Ww per ASCE 7-22 12.11.1).
#   Ev            seismic vertical coefficient (strength level) = 0.2*SDS.
#                 Vertical load effect = Ev x (total dead axial load).
# LOAD LEVELS
#   wind_level 'strength' (default): W is strength level; built-in combos apply
#              0.6W. 'asd': W already includes 0.6; combos apply W at 1.0.
#   snow_level 'strength' (default for ASCE 7-22): combos apply 0.7S (Eqs. 3a,
#              4a, 6a) and 0.1S (Eq. 9). 'asd': the 0.7 is already applied; combos
#              scale by 1/0.7 where needed (Eq. 9 -> 0.1/0.7 = 0.143, as Simpson
#              does). ASCE 7-16 snow is nominal (ASD) -> default 'asd' for 7-16.
#   Eh, Ev are ALWAYS strength level (ASCE 12.4); the combos apply 0.7 / 0.525.
LOAD_KEYS = ("D", "L", "L_lat", "Lr", "S", "R", "W_mwfw", "W_mwfl",
             "W_cc_w", "W_cc_l", "W_uplift", "Eh", "Ev")

DEFAULT_LOADS = {
    "D_wall": 12.0,
    "D_A": 0.0, "L_A": 0.0,
    "D_B": 0.0, "L_B": 0.0,
    "Lr": 0.0, "S": 0.0, "R": 0.0,
    "W_uplift": 0.0,
    "W_mwfw": 0.0, "W_mwfl": 0.0,
    "W_cc_w": 0.0, "W_cc_l": 0.0,
    "L_lat": 0.0,
    "Eh": 0.0, "Ev": 0.0,
}
LINE_LOAD_KEYS = ("D", "L", "Lr", "S", "R")

ASCE_EDITIONS = ("7-16", "7-22")
COMBO_SETS = ("ASCE7-22_ASD", "ASCE7-16_ASD", "IBC2024_ALT_ASD")
COMBO_MODES = ("builtin", "user", "append")
LIVE_PATTERNS = ("AB", "A", "B")
AXIAL_AT = ("base", "mid_height")

# IBC alternative ASD combinations: omega applied to 0.6W (IBC 1605.2).
IBC_ALT_OMEGA = 1.3


def validate_loads(loads: Optional[Dict]) -> Dict:
    """Merge with defaults; reject unknown keys and negative magnitudes."""
    loads = dict(loads or {})
    unknown = sorted(set(loads) - set(DEFAULT_LOADS))
    if unknown:
        raise ValueError(
            f"Unknown load key(s) {unknown}. Allowed: {sorted(DEFAULT_LOADS)}. "
            f"(Loads are never silently ignored.)")
    out = {**DEFAULT_LOADS}
    for k, v in loads.items():
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"Load '{k}' must be a number (got {v!r}).")
        if fv < 0:
            raise ValueError(
                f"Load '{k}' = {fv} is negative. Enter unsigned magnitudes; the "
                f"combinations assign every sign explicitly.")
        out[k] = fv
    return out


def require_wall_type(wtype):
    if wtype not in ("interior", "exterior"):
        raise ValueError(
            f"wall_type must be 'interior' or 'exterior' (got {wtype!r}). It is "
            f"required so side-B loads and interior lateral live load are handled "
            f"correctly - it is never silently defaulted."
        )
    return wtype


def validate_line_loads(line_loads: Optional[Dict], what: str = "line_loads") -> Dict[str, float]:
    ll = dict(line_loads or {})
    bad = sorted(set(ll) - set(LINE_LOAD_KEYS))
    if bad:
        raise ValueError(f"Unknown {what} key(s) {bad}. Allowed: {list(LINE_LOAD_KEYS)}.")
    out = {}
    for k, v in ll.items():
        try:
            fv = float(v or 0.0)
        except (TypeError, ValueError):
            raise ValueError(f"{what} '{k}' must be a number (got {v!r}).")
        if fv < 0:
            raise ValueError(f"{what} '{k}' must be an unsigned magnitude.")
        out[k] = fv
    return out


def stud_effects(loads: Dict, geometry: Dict, *, axial_at: str = "base",
                 line_loads: Optional[Dict] = None,
                 point_loads: Optional[Dict] = None,
                 live_pattern: str = "AB") -> Dict:
    """Unfactored per-stud load EFFECTS for one wall, by load type.

    Returns {'P': {key: P_k}, 'lat_psf': {key: psf}, 'breakdown': {...}}.
    Axial P is compression-positive; W_uplift is returned NEGATIVE.

    line_loads:  {'D','L','Lr','S','R'} in plf at the top of the wall (e.g. a
                 parapet dead load on an exterior wall).
    point_loads: {'D','L','Lr','S','R'} in kips PER STUD at the top of the wall.
    axial_at:    'base' (full wall self-weight, default) or 'mid_height' (half the
                 wall's own weight, as Simpson does).
    live_pattern 'AB' (both sides loaded), 'A' (side A only), 'B' (side B only).
    """
    L = validate_loads(loads)
    H = float(geometry["height_ft"])
    sp = float(geometry["spacing_in"])
    tA = float(geometry.get("trib_A_ft", 0.0) or 0.0)
    tB = float(geometry.get("trib_B_ft", 0.0) or 0.0)
    wtype = require_wall_type(geometry.get("wall_type"))
    if axial_at not in AXIAL_AT:
        raise ValueError(f"axial_at must be one of {AXIAL_AT} (got {axial_at!r}).")
    if live_pattern not in LIVE_PATTERNS:
        raise ValueError(f"live_pattern must be one of {LIVE_PATTERNS}.")
    tB_eff = tB if wtype == "interior" else 0.0
    lines = validate_line_loads(line_loads, "line_loads")
    points = validate_line_loads(point_loads, "point_loads")

    def k(plf):
        return axial_stud(plf, sp)

    wall_h = H / 2.0 if axial_at == "mid_height" else H
    P_wall = k(L["D_wall"] * wall_h)
    P_DA = k(axial_plf(L["D_A"], tA))
    P_DB = k(axial_plf(L["D_B"], tB_eff))
    lA = 1.0 if live_pattern in ("AB", "A") else 0.0
    lB = 1.0 if live_pattern in ("AB", "B") else 0.0
    P_LA = lA * k(axial_plf(L["L_A"], tA))
    P_LB = lB * k(axial_plf(L["L_B"], tB_eff))
    roof_trib = tA + tB_eff

    P = {key: 0.0 for key in LOAD_KEYS}
    P["D"] = P_wall + P_DA + P_DB + k(lines.get("D", 0.0)) + points.get("D", 0.0)
    P["L"] = P_LA + P_LB + k(lines.get("L", 0.0)) + points.get("L", 0.0)
    for rk in ("Lr", "S", "R"):
        P[rk] = k(axial_plf(L[rk], roof_trib)) + k(lines.get(rk, 0.0)) + points.get(rk, 0.0)
    P["W_uplift"] = -k(axial_plf(L["W_uplift"], roof_trib))
    P["Ev"] = L["Ev"] * P["D"]

    lat = {key: 0.0 for key in LOAD_KEYS}
    for lk in ("L_lat", "W_mwfw", "W_mwfl", "W_cc_w", "W_cc_l"):
        lat[lk] = L[lk]
    lat["Eh"] = L["Eh"] * L["D_wall"]

    return {
        "P": P, "lat_psf": lat,
        "breakdown": {
            "P_wall_k": round(P_wall, 5), "P_D_sideA_k": round(P_DA, 5),
            "P_D_sideB_k": round(P_DB, 5), "P_L_sideA_k": round(P_LA, 5),
            "P_L_sideB_k": round(P_LB, 5), "roof_trib_ft": roof_trib,
            "axial_at": axial_at, "live_pattern": live_pattern,
            "line_loads_plf": lines, "point_loads_k": points,
        },
    }


# ---------------------------------------------------------------------------
# Combination options + definitions
# ---------------------------------------------------------------------------
def default_snow_level(asce_edition: str) -> str:
    return "strength" if asce_edition == "7-22" else "asd"


def resolve_combo_options(asce_edition: str = "7-22", snow_level: Optional[str] = None,
                          wind_level: str = "strength", combo_set: Optional[str] = None,
                          combo_mode: str = "builtin",
                          user_combos: Optional[List[Dict]] = None) -> Dict:
    """Validate the combination options. Returns a dict incl. 'warnings'."""
    warnings: List[str] = []
    ed = str(asce_edition or "7-22").strip()
    if ed not in ASCE_EDITIONS:
        raise ValueError(f"asce_edition must be one of {ASCE_EDITIONS} (got {asce_edition!r}).")
    sl = (snow_level or default_snow_level(ed)).lower()
    if sl not in ("strength", "asd"):
        raise ValueError("snow_level must be 'strength' or 'asd'.")
    wl = (wind_level or "strength").lower()
    if wl not in ("strength", "asd"):
        raise ValueError("wind_level must be 'strength' or 'asd'.")
    cs = combo_set or ("ASCE7-22_ASD" if ed == "7-22" else "ASCE7-16_ASD")
    if cs not in COMBO_SETS:
        raise ValueError(f"combo_set must be one of {COMBO_SETS} (got {combo_set!r}).")
    mode = (combo_mode or "builtin").lower()
    if mode not in COMBO_MODES:
        raise ValueError(f"combo_mode must be one of {COMBO_MODES}.")
    if mode in ("user", "append") and not user_combos:
        raise ValueError(f"combo_mode='{mode}' requires a non-empty user_combos list.")
    if user_combos and mode == "builtin":
        warnings.append("user_combos were given but combo_mode='builtin', so they "
                        "were NOT evaluated. Use combo_mode='user' or 'append'.")
    if cs == "ASCE7-16_ASD" and ed == "7-22":
        warnings.append("combo_set ASCE7-16_ASD does not match asce_edition 7-22.")
    if cs == "ASCE7-22_ASD" and ed == "7-16":
        warnings.append("combo_set ASCE7-22_ASD does not match asce_edition 7-16.")
    if cs == "IBC2024_ALT_ASD" and ed != "7-22":
        warnings.append("IBC2024_ALT_ASD is written for ASCE 7-22 (strength-level snow).")
    if ed == "7-16" and sl == "strength":
        warnings.append("ASCE 7-16 snow loads are nominal (ASD level). snow_level="
                        "'strength' converts S to 0.7S before applying 7-16 factors.")
    parsed = validate_user_combos(user_combos) if (user_combos and mode != "builtin") \
        else {"combos": [], "warnings": []}
    warnings += parsed["warnings"]
    return {"asce_edition": ed, "snow_level": sl, "wind_level": wl, "combo_set": cs,
            "combo_mode": mode, "user_combos": parsed["combos"],
            "warnings": warnings}


def validate_user_combos(user_combos: List[Dict]) -> Dict:
    """Validate user-defined combos. Unknown keys are rejected; missing keys = 0."""
    if not isinstance(user_combos, list):
        raise ValueError("user_combos must be a list of {label, factors} objects.")
    out, warnings = [], []
    for i, uc in enumerate(user_combos, 1):
        if not isinstance(uc, dict):
            raise ValueError(f"user_combos[{i}] must be an object with 'label' and 'factors'.")
        extra = sorted(set(uc) - {"label", "factors"})
        if extra:
            raise ValueError(f"user_combos[{i}] has unknown field(s) {extra}; "
                             f"use only 'label' and 'factors'.")
        label = str(uc.get("label") or f"user {i}")
        fac = uc.get("factors")
        if not isinstance(fac, dict) or not fac:
            raise ValueError(f"user combo '{label}' needs a non-empty 'factors' object.")
        unknown = sorted(set(fac) - set(LOAD_KEYS))
        if unknown:
            raise ValueError(f"user combo '{label}': unknown factor key(s) {unknown}. "
                             f"Allowed: {list(LOAD_KEYS)}.")
        clean = {}
        for kk, v in fac.items():
            try:
                clean[kk] = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"user combo '{label}': factor {kk}={v!r} is not a number.")
        if clean.get("D", 0.0) < 0:
            warnings.append(f"user combo '{label}': negative factor on D ({clean['D']}). "
                            f"This is only appropriate for uplift / overturning checks.")
        out.append({"id": f"U{i}", "code_ref": "User-defined", "expr": label,
                    "label": label, "factors": clean, "roof": None})
    return {"combos": out, "warnings": warnings}


def _roof_alternatives(c: float, s_to_asd: float) -> Dict[str, float]:
    """'c(Lr or 0.7S or R)': the factor on each INPUT magnitude."""
    return {"Lr": c, "S": c * s_to_asd, "R": c}


def builtin_combo_defs(opts: Dict) -> List[Dict]:
    """Built-in combos as factor tables applied to the INPUT magnitudes.

    Each def: {id, code_ref, expr, factors, roof}. `roof` is the coefficient c on
    '(Lr or 0.7S or R)' - resolved per combo to the single governing roof load.
    Wind and snow load-level conversions are folded into the factors here (and
    echoed), so the factor table is exactly what multiplies what you entered.
    """
    cs = opts["combo_set"]
    fw = 0.6 if opts["wind_level"] == "strength" else 1.0        # -> 0.6W
    s_asd = 0.7 if opts["snow_level"] == "strength" else 1.0     # -> ASD-level snow

    def d(id_, ref, expr, factors, roof=None):
        return {"id": id_, "code_ref": ref, "expr": expr,
                "factors": {k: v for k, v in factors.items() if v != 0.0}, "roof": roof}

    defs: List[Dict] = []
    if cs in ("ASCE7-22_ASD", "ASCE7-16_ASD"):
        is22 = cs == "ASCE7-22_ASD"
        tag = "ASCE 7-22 Eq. " if is22 else "ASCE 7-16 Comb. "
        a = "a" if is22 else ""
        roof_txt = "(Lr or 0.7S or R)" if is22 else "(Lr or S or R)"
        if is22:
            s9 = 0.1 if opts["snow_level"] == "strength" else 0.1 / 0.7
            s9_txt = "0.1S"
        else:
            s9 = 0.75 * s_asd
            s9_txt = "0.75S"
        defs += [
            d("1" + a, f"{tag}1{a}", "D", {"D": 1.0}),
            d("2" + a, f"{tag}2{a}", "D + L", {"D": 1.0, "L": 1.0, "L_lat": 1.0}),
            d("3" + a, f"{tag}3{a}", f"D + {roof_txt}", {"D": 1.0}, roof=1.0),
            d("4" + a, f"{tag}4{a}", f"D + 0.75L + 0.75{roof_txt}",
              {"D": 1.0, "L": 0.75, "L_lat": 0.75}, roof=0.75),
        ]
        for side, key in (("windward", "W_mwfw"), ("leeward", "W_mwfl")):
            sfx = "mw" if side == "windward" else "ml"
            defs += [
                d(f"5{a}-{sfx}", f"{tag}5{a}", f"D + 0.6W (MWFRS {side})",
                  {"D": 1.0, key: fw}),
                d(f"6{a}-{sfx}", f"{tag}6{a}",
                  f"D + 0.75L + 0.75(0.6W) + 0.75{roof_txt} (MWFRS {side})",
                  {"D": 1.0, "L": 0.75, "L_lat": 0.75, key: 0.75 * fw}, roof=0.75),
                d(f"7{a}-{sfx}", f"{tag}7{a}", f"0.6D + 0.6W (MWFRS {side} + roof uplift)",
                  {"D": 0.6, key: fw, "W_uplift": fw}),
            ]
        for side, key in (("windward", "W_cc_w"), ("leeward", "W_cc_l")):
            sfx = "cw" if side == "windward" else "cl"
            defs.append(d(f"5{a}-{sfx}", f"{tag}5{a} (C&C, bending only)",
                          f"0.6W (C&C {side}), P = 0", {key: fw}))
        defs += [
            d("8", f"{tag}8", "D + 0.7Ev + 0.7Eh", {"D": 1.0, "Ev": 0.7, "Eh": 0.7}),
            d("9", f"{tag}9", f"D + 0.525Ev + 0.525Eh + 0.75L + {s9_txt}",
              {"D": 1.0, "Ev": 0.525, "Eh": 0.525, "L": 0.75, "L_lat": 0.75, "S": s9}),
            d("10", f"{tag}10", "0.6D - 0.7Ev + 0.7Eh", {"D": 0.6, "Ev": -0.7, "Eh": 0.7}),
        ]
    else:
        # IBC 2024 §1605.2 alternative ASD combinations (Eqs. 16-1 .. 16-6).
        tag = "IBC 2024 Eq. "
        fwo = fw * IBC_ALT_OMEGA
        L1 = {"D": 1.0, "L": 1.0, "L_lat": 1.0}
        defs.append(d("16-1", f"{tag}16-1", "D + L + (Lr or 0.7S or R)", dict(L1), roof=1.0))
        for side, key in (("windward", "W_mwfw"), ("leeward", "W_mwfl")):
            sfx = "mw" if side == "windward" else "ml"
            defs += [
                d(f"16-2-{sfx}", f"{tag}16-2", f"D + L + 0.6ωW (MWFRS {side})",
                  {**L1, key: fwo}),
                d(f"16-3-{sfx}", f"{tag}16-3",
                  f"D + L + 0.6ωW + 0.7S/2 (MWFRS {side})",
                  {**L1, key: fwo, "S": 0.5 * s_asd}),
                d(f"16-4-{sfx}", f"{tag}16-4",
                  f"D + L + 0.7S + 0.6ωW/2 (MWFRS {side})",
                  {**L1, key: fwo / 2.0, "S": s_asd}),
                d(f"16-2D-{sfx}", "IBC 2024 §1605.2 (2/3 D counteracting wind)",
                  f"(2/3)D + 0.6ωW (MWFRS {side} + roof uplift)",
                  {"D": 2.0 / 3.0, key: fwo, "W_uplift": fwo}),
            ]
        for side, key in (("windward", "W_cc_w"), ("leeward", "W_cc_l")):
            sfx = "cw" if side == "windward" else "cl"
            defs.append(d(f"16-2-{sfx}", f"{tag}16-2 (C&C, bending only)",
                          f"0.6ωW (C&C {side}), P = 0", {key: fwo}))
        defs += [
            d("16-5", f"{tag}16-5", "D + L + 0.7S + E/1.4",
              {**L1, "S": s_asd, "Eh": 1 / 1.4, "Ev": 1 / 1.4}),
            d("16-6", f"{tag}16-6", "0.9D + E/1.4 (Ev counteracting)",
              {"D": 0.9, "Eh": 1 / 1.4, "Ev": -1 / 1.4}),
        ]
    for c in defs:
        if c["roof"] is not None:
            c["roof_alternatives"] = _roof_alternatives(c["roof"], s_asd)
    return defs


def load_level_factors(opts: Dict) -> Dict:
    """Echo of which conversion factors the built-in combos applied to each input."""
    fw = 0.6 if opts["wind_level"] == "strength" else 1.0
    s_asd = 0.7 if opts["snow_level"] == "strength" else 1.0
    out = {
        "asce_edition": opts["asce_edition"], "combo_set": opts["combo_set"],
        "combo_mode": opts["combo_mode"],
        "wind_level": opts["wind_level"],
        "wind_factor_on_input": fw,
        "snow_level": opts["snow_level"],
        "snow_to_asd_factor_on_input": s_asd,
        "seismic_level": "strength (Eh, Ev always strength level; combos apply 0.7 / 0.525)",
        "roof_rule": "Each combo uses ONE of Lr / S / R (whichever governs); never summed.",
        "note": ("Each combo's 'factors' are the multipliers applied to the load "
                 "magnitudes exactly as you entered them (load-level conversions "
                 "included)."),
    }
    if opts["combo_set"] == "IBC2024_ALT_ASD":
        out["ibc_omega"] = IBC_ALT_OMEGA
        out["ibc_note"] = ("IBC alternative ASD set transcribed from IBC 2024 §1605.2 "
                           "(Eqs. 16-1..16-6) with ω = 1.3 and snow at ASD level; "
                           "verify against the adopted code text.")
    if opts["combo_mode"] in ("user", "append"):
        out["user_combo_note"] = ("User combos: factors are applied to the input "
                                  "magnitudes EXACTLY as given - no hidden 0.6 (wind) "
                                  "or 0.7 (snow/seismic) is applied.")
    return out


def _resolve_roof(cdef: Dict, P: Dict[str, float]) -> tuple:
    """Pick the single governing roof load for 'c(Lr or 0.7S or R)'."""
    alts = cdef.get("roof_alternatives")
    if not alts:
        return {}, None
    cands = [(k, f, f * P.get(k, 0.0)) for k, f in alts.items()]
    nonzero = [c for c in cands if abs(c[2]) > 0]
    if not nonzero:
        return {}, None
    gk, gf, gP = max(nonzero, key=lambda c: c[2])
    info = {"governing": gk, "factor": round(gf, 5), "P_k": round(gP, 5),
            "alternatives_P_k": {k: round(p, 5) for k, _, p in cands}}
    return {gk: gf}, info


def combos_from_effects(defs: List[Dict], effects: Dict, geometry: Dict) -> List[Dict]:
    """Apply factor tables to unfactored effects -> per-stud P, Mx, Vy per combo."""
    H = float(geometry["height_ft"])
    sp = float(geometry["spacing_in"])
    support = check_support(geometry.get("support", "pinned-pinned"))
    P, lat = effects["P"], effects["lat_psf"]
    rows = []
    for c in defs:
        factors = dict(c["factors"])
        roof_f, roof_info = _resolve_roof(c, P)
        for k, v in roof_f.items():
            factors[k] = factors.get(k, 0.0) + v
        Pk = sum(f * P.get(k, 0.0) for k, f in factors.items())
        lat_psf = sum(f * lat.get(k, 0.0) for k, f in factors.items())
        Mx, Vy = lateral_effects(lat_psf, sp, H, support)
        row = {"combo": c["id"], "code_ref": c["code_ref"], "expression": c["expr"],
               "label": c.get("label") or f"{c['code_ref']}: {c['expr']}",
               "factors": {k: round(v, 5) for k, v in factors.items()},
               "P_k": round(Pk, 5), "Mx_kin": round(Mx, 5), "Vy_k": round(Vy, 5),
               "lateral_psf": round(lat_psf, 4)}
        if roof_info:
            row["roof_term"] = roof_info
        rows.append(row)
    return rows


def combo_defs_for(opts: Dict) -> List[Dict]:
    if opts["combo_mode"] == "user":
        return list(opts["user_combos"])
    defs = builtin_combo_defs(opts)
    if opts["combo_mode"] == "append":
        defs += list(opts["user_combos"])
    return defs


def summarize_effects(eff: Dict) -> Dict:
    return {"P_k": {k: round(v, 5) for k, v in eff["P"].items() if v},
            "lateral_psf": {k: round(v, 4) for k, v in eff["lat_psf"].items() if v},
            **eff.get("breakdown", {})}


def build_combo_table(loads: Dict, geometry: Dict, *, asce_edition: str = "7-22",
                      snow_level: Optional[str] = None, wind_level: str = "strength",
                      combo_set: Optional[str] = None, combo_mode: str = "builtin",
                      user_combos: Optional[List[Dict]] = None,
                      axial_at: str = "base", line_loads: Optional[Dict] = None,
                      point_loads: Optional[Dict] = None,
                      live_pattern: str = "AB") -> Dict:
    """Full combination table for one wall: combos + echo of factors/effects."""
    opts = resolve_combo_options(asce_edition, snow_level, wind_level, combo_set,
                                 combo_mode, user_combos)
    eff = stud_effects(loads, geometry, axial_at=axial_at, line_loads=line_loads,
                       point_loads=point_loads, live_pattern=live_pattern)
    combos = combos_from_effects(combo_defs_for(opts), eff, geometry)
    return {"combos": combos, "options": opts,
            "load_level_factors": load_level_factors(opts),
            "unfactored_effects": summarize_effects(eff),
            "warnings": list(opts["warnings"])}


def build_load_combinations(loads: Dict, geometry: Dict, **options) -> List[Dict]:
    """Back-compatible entry point: list of {'combo','label','P_k','Mx_kin','Vy_k',...}.

    Defaults to the ASCE 7-22 ASD set with strength-level wind and snow. See
    build_combo_table for the options (asce_edition, snow_level, wind_level,
    combo_set, combo_mode, user_combos, axial_at, line_loads, point_loads).
    """
    return build_combo_table(loads, geometry, **options)["combos"]


# ---------------------------------------------------------------------------
# Deflection cases (service level)
# ---------------------------------------------------------------------------
def deflection_cases(lat_psf: Dict[str, float], wind_level: str = "strength",
                     wind_defl_modifier: float = 0.7) -> List[Dict]:
    """Service lateral loads for deflection, from unfactored lateral pressures.

    C&C wind: w = wind_defl_modifier x (ASD-level W). With strength-level input
    and the default 0.7 that is 0.7 x 0.6W = 0.42W (IBC Table 1604.3 note f).
    L_lat (interior partitions, IBC 1607.15): service value at 1.0.
    MWFRS pressures are not used for stud deflection (C&C is the component load).
    """
    fw = 0.6 if (wind_level or "strength") == "strength" else 1.0
    out = []
    for key, name in (("W_cc_w", "C&C windward"), ("W_cc_l", "C&C leeward")):
        v = lat_psf.get(key, 0.0)
        if v > 0:
            f = float(wind_defl_modifier) * fw
            out.append({"case": f"{round(f, 4)} x {key} ({name})", "load": key,
                        "factor": round(f, 5), "psf": v * f})
    v = lat_psf.get("L_lat", 0.0)
    if v > 0:
        out.append({"case": "1.0 x L_lat (interior lateral live)", "load": "L_lat",
                    "factor": 1.0, "psf": v})
    return out
