# FILE: stacked.py
# PURPOSE: Pure (no COM) load accumulation for a STACKED multi-level stud wall:
#          axial loads carried down through the levels (D, L, Lr, S, R, uplift,
#          self-weight of walls above), ASCE 7-22 §4.7 live load reduction,
#          live load patterns (AB / A / B) and cross-level stud alignment.
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# Levels are given TOP DOWN (index 0 = top level, the one under the roof).
# The loads on a level are those applied at ITS top (the roof or floor it
# supports); every level below also carries them. Axial loads arrive at each
# level as plf along the top track and are picked up per stud at THAT level's
# spacing, so levels with different stud spacings are handled correctly.

from __future__ import annotations

import math
from typing import Dict, List, Optional

from .cfs_loads import (
    DEFAULT_LOADS,
    LIVE_PATTERNS,
    LOAD_KEYS,
    axial_stud,
    require_wall_type,
    validate_line_loads,
    validate_loads,
)

LEVEL_KEYS = {
    "name", "height_ft", "spacing_in", "trib_A_ft", "trib_B_ft",
    "D", "L", "brace_config", "brace_n", "section", "sections", "grade", "fy",
    "L_reducible", "load_transfer_present", "line_loads", "point_loads",
} | set(DEFAULT_LOADS)


def normalize_level(raw: Dict, index: int, defaults: Optional[Dict] = None) -> Dict:
    """Validate one level dict (defaults merged in). Unknown keys are rejected."""
    if not isinstance(raw, dict):
        raise ValueError(f"levels[{index}] must be an object.")
    lv = {**(defaults or {}), **raw}
    unknown = sorted(set(lv) - LEVEL_KEYS)
    if unknown:
        raise ValueError(f"levels[{index}] has unknown key(s) {unknown}. "
                         f"Allowed: {sorted(LEVEL_KEYS)}.")
    for req in ("height_ft", "spacing_in"):
        if lv.get(req) in (None, 0):
            raise ValueError(f"levels[{index}] needs '{req}' (> 0).")
    # 'D' / 'L' shorthand = the same psf on both sides
    for short, a, b in (("D", "D_A", "D_B"), ("L", "L_A", "L_B")):
        if short in raw and (a in raw or b in raw):
            raise ValueError(f"levels[{index}]: give either '{short}' (both sides) or "
                             f"'{a}'/'{b}', not both.")
        if short in lv and lv[short] is not None:
            if a not in raw:
                lv[a] = lv[short]
            if b not in raw:
                lv[b] = lv[short]
    loads = validate_loads({k: lv[k] for k in DEFAULT_LOADS if k in lv})
    return {
        "index": index,
        "name": str(lv.get("name") or f"Level {index + 1} (from top)"),
        "height_ft": float(lv["height_ft"]),
        "spacing_in": float(lv["spacing_in"]),
        "trib_A_ft": float(lv.get("trib_A_ft", 0.0) or 0.0),
        "trib_B_ft": float(lv.get("trib_B_ft", 0.0) or 0.0),
        "loads": loads,
        "brace_config": lv.get("brace_config", "48in"),
        "brace_n": lv.get("brace_n"),
        "section": lv.get("section"),
        "sections": lv.get("sections"),
        "grade": lv.get("grade"), "fy": lv.get("fy"),
        "L_reducible": bool(lv.get("L_reducible", True)),
        "load_transfer_present": bool(lv.get("load_transfer_present", False)),
        "line_loads": validate_line_loads(lv.get("line_loads"), f"levels[{index}].line_loads"),
        "point_loads": validate_line_loads(lv.get("point_loads"), f"levels[{index}].point_loads"),
    }


def llr_factor(KLL: float, AT_ft2: float, n_floors: int, Lo_psf: float,
               reducible: bool = True, enabled: bool = True) -> tuple:
    """ASCE 7-22 §4.7 live load reduction factor L/Lo for one supported floor.

    §4.7.2: L = Lo(0.25 + 15/sqrt(KLL*AT)) when KLL*AT >= 400 ft^2, not less
            than 0.50Lo (one floor) or 0.40Lo (two or more floors).
    §4.7.3: Lo > 100 psf is not reduced, except members supporting two or more
            floors may be reduced by at most 20% (and not below the 4.7.2 value).
    Returns (factor, reason).
    """
    if not enabled:
        return 1.0, "live load reduction disabled"
    if not reducible:
        return 1.0, "floor marked L_reducible=False (e.g. assembly / garage, §4.7.4-4.7.5)"
    if n_floors < 1 or Lo_psf <= 0:
        return 1.0, "no floor live load"
    KA = KLL * AT_ft2
    if KA < 400.0:
        std, why = 1.0, f"KLL*AT = {KA:.1f} ft^2 < 400 ft^2 -> no reduction (§4.7.2)"
    else:
        raw = 0.25 + 15.0 / math.sqrt(KA)
        lim = 0.50 if n_floors == 1 else 0.40
        std = min(1.0, max(raw, lim))
        why = (f"KLL*AT = {KA:.1f} ft^2: 0.25 + 15/sqrt(KLL*AT) = {raw:.3f}, "
               f"limit {lim:.2f} (§4.7.2)")
    if Lo_psf > 100.0:
        if n_floors >= 2:
            f = max(std, 0.80)
            return f, why + (f"; Lo = {Lo_psf:g} psf > 100 psf, two+ floors -> at most 20% "
                             f"reduction and not below the §4.7.2 value (§4.7.3) -> "
                             f"factor {f:.3f}")
        return 1.0, f"Lo = {Lo_psf:g} psf > 100 psf, one floor -> not reduced (§4.7.3)"
    return std, why


def _k(plf: float, spacing_in: float) -> float:
    return axial_stud(plf, spacing_in)


def accumulate(levels: List[Dict], wall_type: str, axial_at: str = "base",
               live_load_reduction: bool = True, KLL: float = 1.0,
               patterns: Optional[List[str]] = None) -> List[Dict]:
    """Per-level unfactored per-stud effects, accumulated top-down.

    `levels` are normalized level dicts (normalize_level). Returns one dict per
    level with 'effects_by_pattern' {pattern: {'P':{...}, 'lat_psf':{...}}},
    'llr' (AT, KLL, factor per supported floor) and 'accumulated_plf'.
    """
    wtype = require_wall_type(wall_type)
    pats = list(patterns or ["AB"])
    for p in pats:
        if p not in LIVE_PATTERNS:
            raise ValueError(f"live load pattern {p!r} not in {LIVE_PATTERNS}.")
    out = []
    above_D_plf = 0.0                       # dead from roofs/floors/walls above
    above_roof = {"Lr": 0.0, "S": 0.0, "R": 0.0, "W_uplift": 0.0}
    floors: List[Dict] = []                 # every floor with live load, so far
    for lv in levels:
        Ld = lv["loads"]
        tA = lv["trib_A_ft"]
        tB = lv["trib_B_ft"] if wtype == "interior" else 0.0
        H, sp = lv["height_ft"], lv["spacing_in"]
        lines, points = lv["line_loads"], lv["point_loads"]

        own_D_top = Ld["D_A"] * tA + Ld["D_B"] * tB + lines.get("D", 0.0)
        own_wall_full = Ld["D_wall"] * H
        own_wall_here = own_wall_full / 2.0 if axial_at == "mid_height" else own_wall_full
        D_plf = above_D_plf + own_D_top + own_wall_here
        roof_here = {rk: above_roof[rk] + Ld[rk] * (tA + tB) + lines.get(rk, 0.0)
                     for rk in ("Lr", "S", "R")}
        roof_here["W_uplift"] = above_roof["W_uplift"] + Ld["W_uplift"] * (tA + tB)

        if Ld["L_A"] > 0 or Ld["L_B"] > 0 or lines.get("L", 0.0) > 0:
            floors.append({"level": lv["name"], "LA_plf": Ld["L_A"] * tA,
                           "LB_plf": Ld["L_B"] * tB, "L_line_plf": lines.get("L", 0.0),
                           "Lo_psf": max(Ld["L_A"], Ld["L_B"]),
                           "trib_ft": tA + tB, "reducible": lv["L_reducible"]})
        n_fl = len(floors)
        AT = sum((sp / 12.0) * f["trib_ft"] for f in floors)
        llr_rows = []
        for f in floors:
            fac, why = llr_factor(KLL, AT, n_fl, f["Lo_psf"], f["reducible"],
                                  live_load_reduction)
            llr_rows.append({"floor": f["level"], "Lo_psf": f["Lo_psf"],
                             "factor": round(fac, 4), "reason": why})

        effects_by_pattern = {}
        L_unreduced = {}
        for pat in pats:
            sA = 1.0 if pat in ("AB", "A") else 0.0
            sB = 1.0 if pat in ("AB", "B") else 0.0
            L_plf = sum((sA * f["LA_plf"] + sB * f["LB_plf"] + f["L_line_plf"]) * r["factor"]
                        for f, r in zip(floors, llr_rows))
            L_unreduced[pat] = sum(sA * f["LA_plf"] + sB * f["LB_plf"] + f["L_line_plf"]
                                   for f in floors)
            P = {key: 0.0 for key in LOAD_KEYS}
            P["D"] = _k(D_plf, sp) + points.get("D", 0.0)
            P["L"] = _k(L_plf, sp) + points.get("L", 0.0)
            for rk in ("Lr", "S", "R"):
                P[rk] = _k(roof_here[rk], sp) + points.get(rk, 0.0)
            P["W_uplift"] = -_k(roof_here["W_uplift"], sp)
            P["Ev"] = Ld["Ev"] * P["D"]
            lat = {key: 0.0 for key in LOAD_KEYS}
            for lk in ("L_lat", "W_mwfw", "W_mwfl", "W_cc_w", "W_cc_l"):
                lat[lk] = Ld[lk]
            lat["Eh"] = Ld["Eh"] * Ld["D_wall"]
            effects_by_pattern[pat] = {"P": P, "lat_psf": lat}

        out.append({
            **lv,
            "wall_type": wtype,
            "effects_by_pattern": effects_by_pattern,
            "accumulated_plf": {"D": round(D_plf, 3),
                                **{k: round(v, 3) for k, v in roof_here.items()},
                                "L_unreduced": {p: round(v, 3) for p, v in L_unreduced.items()}},
            "llr": {"enabled": live_load_reduction, "KLL": KLL,
                    "AT_ft2": round(AT, 3), "KLL_AT_ft2": round(KLL * AT, 3),
                    "floors_supported": n_fl, "floors": llr_rows,
                    "L_unreduced_k": round(_k(L_unreduced.get("AB", 0.0), sp), 5)
                    if "AB" in L_unreduced else None,
                    "L_reduced_k": round(effects_by_pattern["AB"]["P"]["L"], 5)
                    if "AB" in effects_by_pattern else None},
        })
        # carry this level down
        above_D_plf += own_D_top + own_wall_full
        for rk in ("Lr", "S", "R", "W_uplift"):
            above_roof[rk] = roof_here[rk]
    return out


def alignment_flags(levels: List[Dict]) -> List[Dict]:
    """Stud alignment between consecutive levels (spacing mismatch -> flag).

    A level's studs stack on the level below when the spacings are equal or the
    upper spacing is an integer multiple of the lower one. Otherwise, unless the
    upper level declares load_transfer_present (a transfer track/header), the
    point loads may not land on a stud.
    """
    flags = []
    for up, low in zip(levels, levels[1:]):
        s_up, s_low = up["spacing_in"], low["spacing_in"]
        ratio = s_up / s_low if s_low else 0
        aligned = s_up >= s_low and abs(round(ratio) - ratio) < 1e-6
        if aligned:
            continue
        transfer = up.get("load_transfer_present") or low.get("load_transfer_present")
        flags.append({
            "between": [up["name"], low["name"]],
            "upper_spacing_in": s_up, "lower_spacing_in": s_low,
            "load_transfer_present": bool(transfer),
            "flag": (f"Studs of {up['name']} @ {s_up}\" do not stack on {low['name']} "
                     f"@ {s_low}\"" + (" - a load-transfer member is declared; verify it "
                                       "for the concentrated stud reactions."
                                       if transfer else
                                       " and no load-transfer member is declared - point "
                                       "loads may not land on a stud. Verify the load path.")),
        })
    return flags
