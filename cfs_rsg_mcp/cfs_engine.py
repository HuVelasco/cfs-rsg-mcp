# FILE: cfs_engine.py
# PURPOSE: COM connection + core calculation wrapper for the RSG CFS15 Automation Module
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# This module is the ONLY place that talks to the CFS15 COM server. Everything
# above it (loads, designations, MCP tools) works with plain Python dicts.
#
# THREADING / COM NOTES
# ---------------------
# CFS15 is a single-threaded-apartment (STA) COM object. FastMCP runs each sync
# tool in a worker thread, and a COM object created in one thread cannot be used
# from another. To stay safe we create a fresh Calculation object *inside* every
# tool call using the `cfs_session()` context manager, which:
#   1. CoInitialize()s the calling thread,
#   2. Dispatches RSG.CFS15.Calculation with LATE binding (dynamic.Dispatch) so we
#      never touch the win32com gen_py cache (which is fragile for this DLL),
#   3. ReleaseLicense()s and CoUninitialize()s on exit - important for NETWORK
#      licenses so the seat is freed immediately.
#
# A single tool call may load and check hundreds of sections; that all happens in
# one session/thread, so it is fast and correct.

from __future__ import annotations

import glob
import os
import re
from contextlib import contextmanager
from typing import Dict, List, Optional

# NOTE: pywin32 (pythoncom / win32com) is imported LAZILY inside cfs_session()
# and _mk(), not at module load. This keeps the package importable on any OS
# (so parsing/load/catalog logic can be unit-tested off-Windows and in CI); the
# Windows-only COM dependency is only required when you actually talk to CFS.

PROGID = "RSG.CFS15.Calculation"
_NS = "RSG.CFS15."  # namespace prefix for helper classes (MemberParams, SectionForces...)

# ---------------------------------------------------------------------------
# Specifications (AISI).
# Default = the CODE OF RECORD, not the newest edition: IBC 2024 (and IBC 2021,
# BCNYS 2025) reference AISI S100-16 (2020) w/S2-20 -> CFS "AISI2016USASD".
# Using AISI2024USASD overstated axial capacity in production by ~36%
# (362SLS155-43 single, Ly=Lt=48": Pa 3.564 k vs 2.658 k; Simpson 2.629 k).
# ---------------------------------------------------------------------------
DEFAULT_SPEC = 27  # AISI2016USASD

SPEC_BY_NAME: Dict[str, int] = {
    "AISI2016USASD": 27, "AISI2016USLRFD": 28,
    "AISI2018USASD": 32, "AISI2018USLRFD": 33,
    "AISI2022USASD": 37, "AISI2022USLRFD": 38,
    "AISI2024USASD": 42, "AISI2024USLRFD": 43,
    "AISI2024CanLSD": 46,
}
SPEC_NAME_BY_ID: Dict[int, str] = {v: k for k, v in SPEC_BY_NAME.items()}


# Building code -> referenced ASCE 7 edition and AISI S100 edition.
BUILDING_CODES: Dict[str, Dict] = {
    "IBC2024": {"asce": "7-22", "aisi_year": "2016",
                "aisi_text": "AISI S100-16 (2020) w/S2-20"},
    "BCNYS2025": {"asce": "7-22", "aisi_year": "2016",
                  "aisi_text": "AISI S100-16 (2020) w/S2-20 (via IBC 2024)"},
    "IBC2021": {"asce": "7-16", "aisi_year": "2016",
                "aisi_text": "AISI S100-16 (2020) w/S2-20"},
    "IBC2018": {"asce": "7-16", "aisi_year": "2016", "aisi_text": "AISI S100-16"},
}
BUILDING_CODE_ALIASES = {"NYSBC2025": "BCNYS2025", "2025BCNYS": "BCNYS2025"}
DEFAULT_BUILDING_CODE = "IBC2024"


def spec_name(spec_id: int) -> str:
    return SPEC_NAME_BY_ID.get(int(spec_id), f"enum {spec_id}")


def spec_info(spec_id: int) -> Dict:
    """Echo of the specification actually used (always returned by the tools)."""
    name = SPEC_NAME_BY_ID.get(int(spec_id))
    m = re.match(r"AISI(\d{4})(US|Can)(ASD|LRFD|LSD)$", name or "")
    return {"name": name or f"unknown (enum {spec_id})", "enum": int(spec_id),
            "aisi_year": m.group(1) if m else None,
            "method": m.group(3) if m else None,
            "is_default": int(spec_id) == DEFAULT_SPEC}


def spec_code_check(spec_id: int, building_code: Optional[str] = DEFAULT_BUILDING_CODE,
                    asce_edition: Optional[str] = None,
                    asd_combos: bool = False) -> Dict:
    """Compare the spec (and ASCE edition) with the selected building code.

    Returns {'warnings': [...], 'errors': [...], 'building_code': {...}}. An ASD
    load combination set with an LRFD/LSD spec is an ERROR (the unity checks would
    compare ASD loads with factored resistance - unconservative).
    """
    info = spec_info(spec_id)
    warnings: List[str] = []
    errors: List[str] = []
    code = None
    if building_code and str(building_code).strip().lower() not in ("none", ""):
        key = str(building_code).strip().upper().replace(" ", "").replace("-", "")
        key = BUILDING_CODE_ALIASES.get(key, key)
        if key not in BUILDING_CODES:
            raise ValueError(f"Unknown building_code {building_code!r}. Use one of "
                             f"{sorted(BUILDING_CODES)} or 'none'.")
        code = {"name": key, **BUILDING_CODES[key]}
        if info["aisi_year"] != code["aisi_year"]:
            warnings.append(
                f"SPEC MISMATCH: {info['name']} is not the edition referenced by {key} "
                f"({code['aisi_text']} -> CFS 'AISI{code['aisi_year']}USASD'). A newer "
                f"edition can materially overstate capacity (e.g. 362SLS155-43 at "
                f"48\" bracing: Pa 3.564 k under S100-24 vs 2.658 k under S100-16).")
        if asce_edition and asce_edition != code["asce"]:
            warnings.append(f"ASCE MISMATCH: asce_edition {asce_edition} but {key} "
                            f"references ASCE 7-{code['asce'].split('-')[1]}.")
    if asd_combos and info["method"] not in (None, "ASD"):
        errors.append(f"The load combinations are ASD but the spec {info['name']} is "
                      f"{info['method']}. Use an ASD spec (e.g. AISI2016USASD).")
    return {"warnings": warnings, "errors": errors, "building_code": code, "spec": info}


def resolve_spec(spec) -> int:
    """Accept an int enum value or a name like 'AISI2024USASD'. Returns the int."""
    if spec is None:
        return DEFAULT_SPEC
    if isinstance(spec, int):
        return spec
    s = str(spec).strip()
    if s.isdigit():
        return int(s)
    if s in SPEC_BY_NAME:
        return SPEC_BY_NAME[s]
    raise ValueError(
        f"Unknown specification '{spec}'. Use an int enum or one of: "
        + ", ".join(SPEC_BY_NAME)
    )


# ---------------------------------------------------------------------------
# CFS install / standard library discovery
# ---------------------------------------------------------------------------
_INSTALL_CANDIDATES = [
    r"C:\Program Files (x86)\RSG Software\CFS 15",
    r"C:\Program Files\RSG Software\CFS 15",
]


def cfs_install_dir() -> Optional[str]:
    for d in _INSTALL_CANDIDATES:
        if os.path.isdir(d):
            return d
    return None


def standard_library_path(library: str = "SSMA") -> str:
    """Full path to a bundled CFS section library (SSMA, SFIA, AISI, HUD, LGSI)."""
    d = cfs_install_dir()
    if not d:
        raise FileNotFoundError(
            "Could not locate the CFS 15 install directory. "
            "Checked: " + "; ".join(_INSTALL_CANDIDATES)
        )
    p = os.path.join(d, f"{library}.cfsl")
    if not os.path.isfile(p):
        avail = [os.path.basename(x) for x in glob.glob(os.path.join(d, "*.cfsl"))]
        raise FileNotFoundError(
            f"Library '{library}.cfsl' not found in {d}. Available: {', '.join(avail)}"
        )
    return p


# Folder names *inside* SSMA.cfsl (discovered by inspecting the library file).
SSMA_FOLDERS = {
    "S_33": "Structural Studs 33 ksi",
    "S_50": "Structural Studs 50 ksi",
    "T_33": "Structural Tracks 33 ksi",
    "T_50": "Structural Tracks 50 ksi",
    "NS_33": "Non-Structural Studs 33 ksi",
    "NS_50": "Non-Structural Studs 50 ksi",
    "U": "U-Channels",
    "F": "Furring Channels",
}

# SSMA designation, e.g. 600S200-43  or  362S162-54  or  600T125-43
_DESIG_RE = re.compile(
    r"^\s*(\d{2,4})\s*([STUF])\s*(\d{2,4})\s*-\s*(\d{2,3})\s*$", re.IGNORECASE
)


def parse_designation(desig: str) -> Optional[Dict]:
    """Parse an SSMA designation like '600S200-43'.

    Returns dict {web_1_100in, type_letter, flange_1_100in, mils, depth_in,
    flange_in} or None if it does not look like a standard designation.
    """
    m = _DESIG_RE.match(desig or "")
    if not m:
        return None
    web, letter, flange, mils = m.group(1), m.group(2).upper(), m.group(3), int(m.group(4))
    return {
        "designation": f"{web}{letter}{flange}-{mils}",
        "web_1_100in": int(web),
        "type_letter": letter,
        "flange_1_100in": int(flange),
        "mils": mils,
        "depth_in": int(web) / 100.0,
        "flange_in": int(flange) / 100.0,
    }


def _ssma_folder_order(letter: str, mils: int, grade: Optional[int]) -> List[str]:
    """Priority-ordered list of SSMA folders to try for a designation.

    SSMA convention: thin gauges (<=43 mil) are typically 33 ksi, heavier gauges
    (>=54 mil) are 50 ksi. A section may exist in both; we try the likely folder
    first and fall back to the other.
    """
    if grade in (33, 50):
        grades = [grade, 50 if grade == 33 else 33]
    elif mils <= 43:
        grades = [33, 50]
    else:
        grades = [50, 33]

    if letter == "S":
        base = [f"Structural Studs {g} ksi" for g in grades]
        base += [f"Non-Structural Studs {g} ksi" for g in grades]
    elif letter == "T":
        base = [f"Structural Tracks {g} ksi" for g in grades]
    elif letter == "U":
        base = ["U-Channels"]
    elif letter == "F":
        base = ["Furring Channels"]
    else:
        base = [f"Structural Studs {g} ksi" for g in grades]
    return base


def resolve_section(identifier: str, library: str = "SSMA",
                    grade: Optional[int] = None) -> str:
    """Turn a user-supplied section identifier into a CFS LoadSection string.

    Accepts:
      * A path to a custom '.cfss' file           -> returned as-is
      * A full library path 'X.cfsl|Folder\\Y.cfss' -> returned as-is
      * An SSMA designation like '600S200-43'     -> 'SSMA.cfsl|Folder\\600S200-43.cfss'

    For designations, returns the FIRST candidate path; the engine's loader will
    try the alternates if the first fails (see load_section).
    """
    ident = (identifier or "").strip().strip('"')
    if "|" in ident:
        return ident
    if ident.lower().endswith(".cfss"):
        return ident  # explicit file path (custom section)

    parsed = parse_designation(ident)
    if parsed:
        libpath = standard_library_path(library)
        folder = _ssma_folder_order(parsed["type_letter"], parsed["mils"], grade)[0]
        return f"{libpath}|{folder}\\{parsed['designation']}.cfss"

    raise ValueError(
        f"Could not interpret section identifier '{identifier}'. Provide a .cfss "
        f"file path, a full 'lib.cfsl|Folder\\name.cfss' path, or an SSMA "
        f"designation like '600S200-43'."
    )


def candidate_load_paths(identifier: str, library: str = "SSMA",
                         grade: Optional[int] = None) -> List[str]:
    """All LoadSection strings to try, in priority order (for standard designations)."""
    ident = (identifier or "").strip().strip('"')
    if "|" in ident or ident.lower().endswith(".cfss"):
        return [ident]
    parsed = parse_designation(ident)
    if not parsed:
        return [resolve_section(identifier, library, grade)]
    libpath = standard_library_path(library)
    return [
        f"{libpath}|{folder}\\{parsed['designation']}.cfss"
        for folder in _ssma_folder_order(parsed["type_letter"], parsed["mils"], grade)
    ]


# ---------------------------------------------------------------------------
# COM session
# ---------------------------------------------------------------------------
class CFSError(RuntimeError):
    pass


@contextmanager
def cfs_session():
    """Context manager yielding a licensed CFS15 Calculation object.

    Usage:
        with cfs_session() as s:
            s.load("600S200-43")
            props = s.gross_properties()
    """
    try:
        import pythoncom
        from win32com.client import dynamic
    except ImportError as e:  # noqa: BLE001
        raise CFSError(
            "pywin32 is required to talk to CFS (Windows only). "
            f"Install it with 'pip install pywin32'. Underlying error: {e}"
        )
    pythoncom.CoInitialize()
    calc = None
    session = None
    try:
        try:
            calc = dynamic.Dispatch(PROGID)
        except Exception as e:  # noqa: BLE001
            raise CFSError(
                f"Could not create COM object '{PROGID}'. Is the CFS15 Automation "
                f"Module installed and registered? Underlying error: {e}"
            )
        if not calc.HasLicense:
            raise CFSError(
                "CFS reports no available license. Ensure CFS 15 is licensed with "
                "the Automation option (Single-User + Automation or Network + "
                "Automation) and is not already using all network seats."
            )
        session = _Session(calc)
        yield session
    finally:
        try:
            if calc is not None:
                calc.ReleaseLicense()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


def _mk(name: str):
    """Create a CFS15 helper COM object (MemberParams, SectionForces, SectionData)."""
    from win32com.client import dynamic
    return dynamic.Dispatch(_NS + name)


class _Session:
    """Thin wrapper over a licensed Calculation object. Not created directly."""

    def __init__(self, calc):
        self.calc = calc
        self._loaded: Optional[str] = None

    # -- loading -----------------------------------------------------------
    def load(self, identifier: str, library: str = "SSMA",
             grade: Optional[int] = None, fy: Optional[float] = None) -> str:
        """Load a section by designation / library path / .cfss file.

        Returns the load path that actually succeeded. Raises CFSError with the
        collected messages if nothing loads.
        """
        errors = []
        try:
            paths = candidate_load_paths(identifier, library, grade)
        except (ValueError, FileNotFoundError) as e:
            raise CFSError(str(e))
        for path in paths:
            try:
                self.calc.LoadSection(path)
                self._loaded = path
                if fy is not None:
                    self.calc.Fy = float(fy)
                return path
            except Exception as e:  # noqa: BLE001
                errors.append(f"  {path}\n     -> {_com_msg(e)}")
        raise CFSError(
            f"Failed to load section '{identifier}'. Tried:\n" + "\n".join(errors)
        )

    @property
    def loaded_path(self) -> Optional[str]:
        return self._loaded

    # -- properties / strength --------------------------------------------
    def section_data(self) -> Dict:
        sd = self.calc.SectionData()
        return {
            "description": _s(sd.Description),
            "section_type": int(sd.SectionType),
            "section_type_name": SECTION_TYPE_NAMES.get(int(sd.SectionType), "Unknown"),
            "depth_in": _f(sd.Depth),
            "flange_in": _f(sd.Flange),
            "lip_in": _f(sd.Lip),
            "thickness_in": _f(sd.Thickness),
            "radius_in": _f(sd.Radius),
            "Fy_ksi": _f(sd.Fy),
            "Fu_ksi": _f(sd.Fu),
            "num_parts": int(sd.NumParts),
        }

    def gross_properties(self) -> Dict:
        p = self.calc.GrossProperties()
        area = _f(p.Area)
        return {
            "area_in2": area,
            "weight_plf": round(area * 0.283 * 12, 4) if area else None,  # steel 0.283 lb/in^3
            "Ix_in4": _f(p.Ix),
            "Sx_in3": round(min(_f(p.Sxt), _f(p.Sxb)), 5),
            "Sxt_in3": _f(p.Sxt),
            "Sxb_in3": _f(p.Sxb),
            "rx_in": _f(p.Rx),
            "Iy_in4": _f(p.Iy),
            "Sy_in3": round(min(_f(p.Syl), _f(p.Syr)), 5),
            "ry_in": _f(p.Ry),
            "J_in4": _f(p.J),
            "Cw_in6": _f(p.Cw),
            "Xo_in": _f(p.Xo),
            "Ro_in": _f(p.Ro),
            "Zx_in3": _f(p.Zx),
            "Zy_in3": _f(p.Zy),
        }

    def fully_braced_strength(self, spec: int) -> Dict:
        st = self.calc.Strength(int(spec))
        flags = _decode_flags(int(st.Flags))
        return {
            "Pn_k": _f(st.P),          # nominal/allowable compressive strength (fully braced)
            "Tn_k": _f(st.T),          # tension strength
            "Mnx_pos_kin": _f(st.Mxp),
            "Mnx_neg_kin": _f(st.Mxn),
            "Mny_pos_kin": _f(st.Myp),
            "Mny_neg_kin": _f(st.Myn),
            "Vy_k": _f(st.Vy),
            "Vx_k": _f(st.Vx),
            "Ae_in2": _f(st.Ae),       # effective area at nominal compressive strength
            # effective section moduli at nominal moment (slender-element governed)
            "Sxe_pos_in3": round(min(_f(st.Sxtep), _f(st.Sxbep)), 5),
            "Sxe_neg_in3": round(min(_f(st.Sxten), _f(st.Sxben)), 5),
            "Sye_pos_in3": round(min(_f(st.Sylep), _f(st.Syrep)), 5),
            "flags": flags,
            "flag_notes": describe_flags(flags),
        }

    def _member_params(self, spec: int, lengths: Dict, **overrides):
        mp = _mk("MemberParams")
        mp.Spec = int(spec)
        mp.Lx = float(lengths["Lx"])
        mp.Ly = float(lengths["Ly"])
        mp.Lt = float(lengths["Lt"])
        mp.Lm = float(lengths["Lm"])
        mp.Kx = float(overrides.get("Kx", 1.0))
        mp.Ky = float(overrides.get("Ky", 1.0))
        mp.Kt = float(overrides.get("Kt", 1.0))
        mp.Cbx = float(overrides.get("Cbx", 1.0))
        mp.Cby = float(overrides.get("Cby", 1.0))
        mp.Cmx = float(overrides.get("Cmx", 1.0))
        mp.Cmy = float(overrides.get("Cmy", 1.0))
        mp.BracedFlange = int(overrides.get("BracedFlange", 0))
        mp.R = float(overrides.get("R", 0.0))
        mp.Kf = float(overrides.get("Kf", 0.0))
        mp.Pdelta = bool(overrides.get("Pdelta", False))
        return mp

    def _forces(self, P=0.0, Mx=0.0, My=0.0, Vx=0.0, Vy=0.0, Dx=0.0, Dy=0.0):
        f = _mk("SectionForces")
        f.P = float(P)
        f.Mx = float(Mx)
        f.My = float(My)
        f.Vx = float(Vx)
        f.Vy = float(Vy)
        f.Dx = float(Dx)
        f.Dy = float(Dy)
        return f

    def member_strengths(self, spec: int, lengths: Dict,
                         include_trace: bool = False, **overrides) -> Dict:
        """Member design strengths (P, Mx, My, Vy) at the given unbraced lengths.

        Runs MemberCheck with zero applied force; the returned MemberUnityCheck
        exposes the available member strengths (already accounting for global /
        distortional buckling at Lx/Ly/Lt/Lm).

        include_trace: attach CFS's full calculation trace string under 'trace'.
        """
        mp = self._member_params(spec, lengths, **overrides)
        f = self._forces()
        chk = self.calc.MemberCheck(mp, f)
        out = {
            "P_k": _f(chk.P),
            "Mx_kin": _f(chk.Mx),
            "My_kin": _f(chk.My),
            "Vy_k": _f(chk.Vy),
            "Vx_k": _f(chk.Vx),
            "flags": _decode_flags(int(chk.Flags)),
        }
        if include_trace:
            out["trace"] = _trace(chk)
        return out

    def member_check(self, spec: int, lengths: Dict, forces: Dict,
                     include_trace: bool = False, **overrides) -> Dict:
        """Full unity check for one set of applied forces.

        Honesty rules around CFS's UC sentinel (9.999):
          * A shear-interaction check (MxVy / MyVx) can hit the sentinel when CFS
            cannot compute the shear strength (e.g. a BendRadius geometry flag
            zeroes Vy). If there is NO applied shear in that direction, the
            interaction is meaningless — we EXCLUDE it from the governing UC and
            record a warning that shear was not evaluated. If there IS applied
            shear with zero capacity, that is a genuine FAIL and we keep it.
        """
        applied = {
            "P_k": forces.get("P_k", 0.0), "Mx_kin": forces.get("Mx_kin", 0.0),
            "My_kin": forces.get("My_kin", 0.0), "Vx_k": forces.get("Vx_k", 0.0),
            "Vy_k": forces.get("Vy_k", 0.0),
        }
        mp = self._member_params(spec, lengths, **overrides)
        f = self._forces(P=applied["P_k"], Mx=applied["Mx_kin"], My=applied["My_kin"],
                         Vx=applied["Vx_k"], Vy=applied["Vy_k"])
        chk = self.calc.MemberCheck(mp, f)

        uc1 = _f(chk.PMxMy1) or 0.0
        uc2 = _f(chk.PMxMy2) or 0.0
        mxvy = _f(chk.MxVy) or 0.0
        try:
            myvx = _f(chk.MyVx) or 0.0
        except Exception:  # noqa: BLE001  (older builds may lack MyVx)
            myvx = 0.0
        vy_cap = _f(chk.Vy)
        vx_cap = _f(chk.Vx) if hasattr(chk, "Vx") else None
        flags = _decode_flags(int(chk.Flags))

        governing = [("PMxMy1", uc1), ("PMxMy2", uc2)]
        warnings: List[str] = []

        def _handle_shear(label, uc_val, cap, applied_v):
            nonlocal warnings
            unavailable = uc_val >= UC_SENTINEL or (cap is not None and cap == 0)
            if unavailable and abs(applied_v) < 1e-6:
                warnings.append(
                    f"{label} shear check not evaluated: CFS reports zero shear "
                    f"capacity (see flags) but there is no applied shear in this "
                    f"direction, so it does not govern."
                )
                return  # excluded from governing UC
            if unavailable and abs(applied_v) >= 1e-6:
                warnings.append(
                    f"{label} FAILS: applied shear {applied_v} k with zero available "
                    f"shear capacity (see flags). Section cannot carry this shear."
                )
            governing.append((label, uc_val))

        _handle_shear("MxVy", mxvy, vy_cap, applied["Vy_k"])
        _handle_shear("MyVx", myvx, vx_cap, applied["Vx_k"])

        gov_label, gov_uc = max(governing, key=lambda kv: kv[1])
        out = {
            "PMxMy1": round(uc1, 4),
            "PMxMy2": round(uc2, 4),
            "MxVy": round(mxvy, 4),
            "MyVx": round(myvx, 4),
            "UC": round(gov_uc, 4),
            "governing_check": gov_label,
            "status": "PASS" if gov_uc <= 1.0 else "FAIL",
            "strengths": {
                "P_k": _f(chk.P), "Mx_kin": _f(chk.Mx),
                "My_kin": _f(chk.My), "Vy_k": vy_cap, "Vx_k": vx_cap,
            },
            "flags": flags,
            "flag_notes": describe_flags(flags),
            "warnings": warnings,
        }
        if include_trace:
            out["trace"] = _trace(chk)
        return out

    # -- detailed strengths / serviceability / web crippling ---------------
    def strength_detail(self, spec: int, lengths: Dict, **overrides) -> Dict:
        """Member strengths broken down by limit state, all from CFS.

        The COM API exposes only the governing member P / Mx. The individual
        limit states (global Pne, local Pnl, distortional Pnd, Mnl, Mnd) and the
        safety factors are read from CFS's OWN calculation trace for the member
        check - nothing is computed in Python. The parsed governing values are
        reconciled against CFS's reported member P / Mx; any mismatch is flagged.

        Returns Pa, Pa_dist, Ma, Ma_Fy (fully braced), Ma_brc (at the member's
        bracing), Ma_dist, Va, Ae, the governing axial buckling mode and notes.
        """
        ms = self.member_strengths(spec, lengths, include_trace=True, **overrides)
        fb = self.fully_braced_strength(spec)
        parsed = parse_strength_trace(ms.get("trace"))
        lrfd = spec_info(spec)["method"] in ("LRFD", "LSD")
        notes: List[str] = []       # informational
        warnings: List[str] = []    # the breakdown could not be verified

        def avail(nominal, omega, phi):
            if nominal is None:
                return None
            f = phi if lrfd else (1.0 / omega if omega else None)
            return round(nominal * f, 5) if f else None

        ax, fx = parsed.get("axial", {}), parsed.get("flex_x", {})
        Pa_lg = avail(ax.get("Pnl"), ax.get("omega"), ax.get("phi"))
        Pa_dist = avail(ax.get("Pnd"), ax.get("omega"), ax.get("phi"))
        Pa_glob = avail(ax.get("Pne"), ax.get("omega"), ax.get("phi"))
        Ma_brc = avail(fx.get("Mnl"), fx.get("omega"), fx.get("phi"))
        Ma_dist = avail(fx.get("Mnd"), fx.get("omega"), fx.get("phi"))
        if Ma_brc is None and fx.get("Mnl_is_fully_braced"):
            Ma_brc = fb["Mnx_pos_kin"]
            notes.append("CFS reports Fn = Fy for X-axis flexure (no global LTB "
                         "reduction at this bracing): Ma-Brc = fully braced Ma-Fy.")

        mode = None
        if ax.get("Pnl") is not None and ax.get("Pnd") is not None:
            if ax["Pnd"] < ax["Pnl"]:
                mode = "distortional"
            elif ax.get("Pne") is not None and ax["Pnl"] < 0.995 * ax["Pne"]:
                mode = "local (local-global interaction, Pnl < Pne)"
            else:
                mode = "global (flexural / torsional / flexural-torsional)"

        # Reconcile parsed limit states with CFS's governing member strengths.
        def reconcile(label, parsed_vals, cfs_val):
            vals = [v for v in parsed_vals if v is not None]
            if not vals or not cfs_val:
                warnings.append(f"{label}: limit-state breakdown not available from the "
                                f"CFS trace; only the governing value is reported.")
                return
            if abs(min(vals) - cfs_val) > 0.01 * abs(cfs_val):
                warnings.append(f"{label}: parsed CFS trace ({round(min(vals), 4)}) does "
                                f"not reconcile with CFS member strength ({cfs_val}); "
                                f"the member strength is authoritative - breakdown "
                                f"shown for reference only.")
        reconcile("Pa", [Pa_lg, Pa_dist], ms["P_k"])
        reconcile("Ma", [Ma_brc, Ma_dist], ms["Mx_kin"])

        return {
            "Pa_k": ms["P_k"], "Pa_local_global_k": Pa_lg, "Pa_global_k": Pa_glob,
            "Pa_dist_k": Pa_dist,
            "Ma_kin": ms["Mx_kin"], "Ma_Fy_kin": fb["Mnx_pos_kin"],
            "Ma_brc_kin": Ma_brc, "Ma_dist_kin": Ma_dist,
            "Va_k": ms["Vy_k"], "My_a_kin": ms["My_kin"],
            "Pa_fully_braced_k": fb["Pn_k"],
            "Ae_fully_braced_in2": fb["Ae_in2"], "Ae_member_in2": ax.get("Ae"),
            "axial_buckling_mode": mode,
            "method": "LRFD (phi*Rn)" if lrfd else "ASD (Rn/Omega)",
            "flags": sorted(set(ms["flags"]) | set(fb["flags"])),
            "notes": notes,
            "warnings": warnings,
        }

    def elastic_modulus(self) -> Optional[float]:
        try:
            return _f(self.calc.SectionData().E)
        except Exception:  # noqa: BLE001
            return None

    def effective_Ix(self, spec: int, Mx_kin: float) -> Optional[float]:
        """CFS effective Ix (in^4) of the loaded section at the moment Mx_kin."""
        ep = self.calc.EffectiveProperties(self._forces(Mx=float(Mx_kin)), int(spec))
        return _f(ep.Ix)

    def web_crippling(self, spec: int, P_k: float, bearing_length_in: float = 1.0,
                      fastened: bool = True, Lend_in: float = 0.0,
                      Lload_in: float = 10000.0, M_kin: float = 0.0) -> Dict:
        """CFS WebCheck for a concentrated load / end reaction on the flange.

        For a wall stud this is the lateral END REACTION bearing on the stud
        flange at the track (Dir = 1: load along the web). Allowable reaction is
        derived from CFS's own unity check (Pa = P / UC).
        """
        wp = _mk("WebParams")
        wp.Spec = int(spec)
        wp.Dir = 1
        wp.Fastened = bool(fastened)
        wp.Lend = float(Lend_in)
        wp.Lload = float(Lload_in)
        wp.M = float(M_kin)
        wp.N = float(bearing_length_in)
        wp.P = float(abs(P_k))
        w = self.calc.WebCheck(wp)
        uc = _f(w.P)
        flags = _decode_flags(int(w.Flags))
        return {"R_k": round(abs(P_k), 5), "UC": uc,
                "UC_with_bending": _f(w.PM),
                "Ra_k": round(abs(P_k) / uc, 4) if uc else None,
                "stiffener_required": bool(uc is not None and uc > 1.0),
                "bearing_length_in": bearing_length_in, "fastened": fastened,
                "flags": flags, "flag_notes": describe_flags(flags)}


# ---------------------------------------------------------------------------
# CFS trace parsing (read CFS's own limit-state results; no calculation here)
# ---------------------------------------------------------------------------
_NUM = r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
_TRACE_KEYS = ("Pne", "Pnl", "Pnd", "Mne", "Mnl", "Mnd", "Ae")


def parse_strength_trace(trace: Optional[str]) -> Dict:
    """Pull the limit-state strengths out of a CFS MemberCheck trace.

    Returns {'axial': {...}, 'flex_x': {...}, 'flex_y': {...}} with the first
    Pne/Pnl/Pnd (axial) or Mne/Mnl/Mnd (flexure), Ae, and omega/phi of each
    block. Missing values are simply absent - callers must not invent them.
    """
    out: Dict[str, Dict] = {}
    if not trace:
        return out
    blocks: Dict[str, List[str]] = {}
    current = None
    for line in str(trace).splitlines():
        if line and not line[0].isspace():
            head = line.strip()
            if head.startswith("Axial Compression Strength"):
                current = "axial"
            elif head.startswith("Flexural Strength about X"):
                current = "flex_x"
            elif head.startswith("Flexural Strength about Y"):
                current = "flex_y"
            else:
                current = None
            if current:
                blocks[current] = []
            continue
        if current:
            blocks[current].append(line)
    for name, lines in blocks.items():
        text = "\n".join(lines)
        d: Dict = {}
        for key in _TRACE_KEYS:
            m = re.search(rf"\b{key}={_NUM}", text)
            if m:
                d[key] = float(m.group(1))
        m = re.search(r"Ω[cb]=" + _NUM + r",\s*φ[cb]=" + _NUM, text)
        if m:
            d["omega"], d["phi"] = float(m.group(1)), float(m.group(2))
        if "Fn = Fy, see fully braced strength" in text and "Mnl" not in d:
            d["Mnl_is_fully_braced"] = True
        out[name] = d
    return out


# ---------------------------------------------------------------------------
# Enums / helpers
# ---------------------------------------------------------------------------
SECTION_TYPE_NAMES = {
    0: "Unknown", 1: "Cee", 2: "CeeStiffened", 3: "Zee", 4: "ZeeStiffened",
    5: "DoubleCee", 6: "DoubleCeeStiffened", 7: "Box", 8: "Tube",
    9: "Angle", 10: "AngleStiffened", 11: "Hat", 12: "Cylinder",
}

_NOTE_FLAGS = {
    1: "ThinTubeWall", 2: "SlenderElement", 4: "LongEdgeStiffener",
    8: "EdgeStiffenerAngle", 16: "SlenderColumn", 32: "SheathedColumnLimits",
    64: "SheathedColumnFastener", 128: "ConnectorSpacing", 256: "BendRadius",
    512: "HoleDepthRatio", 1024: "HoleDepth", 2048: "HoleLength",
    4096: "HoleSpacing", 8192: "YieldStress", 16384: "TorsionCheck",
    32768: "Stiffeners",
}

# Plain-English meaning of each CFS NoteFlag (per the CFS15 Automation Module
# reference). These are engineering NOTES, not automatic failures — read them.
FLAG_DESCRIPTIONS = {
    "ThinTubeWall": "D/t exceeds the maximum for a cylindrical tube wall.",
    "SlenderElement": "w/t exceeds the slenderness limit for an element; effective "
                      "width provisions govern (strength reduced accordingly).",
    "LongEdgeStiffener": "D/w (or do/bo) exceeds the maximum for an edge stiffener.",
    "EdgeStiffenerAngle": "Edge stiffener (lip) angle is outside code limits.",
    "SlenderColumn": "KL/r exceeds the maximum slenderness for a column member.",
    "SheathedColumnLimits": "Section does not meet all requirements for the sheathed "
                            "column provisions.",
    "SheathedColumnFastener": "Sheathed-column fastener assumed at the middle of the flange.",
    "ConnectorSpacing": "Built-up connector spacing a/ri exceeds 0.5·KL/r.",
    "BendRadius": "Inside bend radius / thickness (R/t) exceeds the code limit. CFS "
                  "cannot compute some strengths for this geometry (often shear Vy=0). "
                  "Re-model with a smaller bend radius or verify shear by hand.",
    "HoleDepthRatio": "Hole depth / web depth (dh/h) exceeds the limit.",
    "HoleDepth": "Hole depth exceeds the limit.",
    "HoleLength": "Hole length exceeds the limit.",
    "HoleSpacing": "Clear distance between holes is less than the limit.",
    "YieldStress": "Yield stress exceeds the Specification limit (Fy capped for design).",
    "TorsionCheck": "Unable to check torsion because Cw was overridden.",
    "Stiffeners": "Number of intermediate stiffeners exceeds the limit.",
}

# CFS returns this sentinel for a unity check it cannot compute (e.g. an
# interaction that would divide by a zero strength). Treat >= this as "n/a".
UC_SENTINEL = 9.999


def describe_flags(flag_names: List[str]) -> List[Dict]:
    """Turn a list of flag codes into [{code, meaning}] for user-facing output."""
    return [{"code": n, "meaning": FLAG_DESCRIPTIONS.get(n, "See CFS documentation.")}
            for n in flag_names]


def section_label(load_path: Optional[str], description: Optional[str]) -> str:
    """Best human label for a section.

    Custom .cfss files often carry a stale embedded Description (copied from a
    template), so for a real file we trust the FILENAME. For a bundled library
    path ('lib.cfsl|Folder\\name.cfss') the description is authoritative.
    """
    desc = (description or "").strip()
    if load_path and "|" not in load_path and load_path.lower().endswith(".cfss"):
        return os.path.splitext(os.path.basename(load_path))[0]
    return desc or (os.path.splitext(os.path.basename(load_path))[0] if load_path else "")


def _decode_flags(flags: int) -> List[str]:
    if not flags:
        return []
    return [name for bit, name in _NOTE_FLAGS.items() if flags & bit]


def _trace(chk) -> Optional[str]:
    """Best-effort read of a CFS result object's Trace (detailed calc report).

    Returns None if the build/object does not expose a Trace property so callers
    that opt into include_trace never crash on an older CFS.
    """
    try:
        t = chk.Trace
        return str(t) if t is not None else None
    except Exception:  # noqa: BLE001
        return None


def _f(v):
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def _s(v):
    return str(v).strip() if v is not None else ""


def _com_msg(e: Exception) -> str:
    """Extract the human-readable message from a pywin32 COM exception."""
    try:
        # com_error args: (hresult, strerror, excepinfo, argerr)
        exc = e.args[2]
        if exc and len(exc) > 2 and exc[2]:
            return str(exc[2])
    except Exception:  # noqa: BLE001
        pass
    return str(e)
