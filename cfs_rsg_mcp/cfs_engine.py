# FILE: cfs_engine.py
# PURPOSE: COM connection + core calculation wrapper for the RSG CFS15 Automation Module
# STATUS: Version 0.1.0.1 - Developed (bug-fix pass from tester report); not yet re-validated
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

import pythoncom
from win32com.client import dynamic

PROGID = "RSG.CFS15.Calculation"
_NS = "RSG.CFS15."  # namespace prefix for helper classes (MemberParams, SectionForces...)

# ---------------------------------------------------------------------------
# Specifications (AISI). Default matches the user's wall-stud notebook.
# ---------------------------------------------------------------------------
DEFAULT_SPEC = 42  # AISI2024USASD

SPEC_BY_NAME: Dict[str, int] = {
    "AISI2016USASD": 27, "AISI2016USLRFD": 28,
    "AISI2018USASD": 32, "AISI2018USLRFD": 33,
    "AISI2022USASD": 37, "AISI2022USLRFD": 38,
    "AISI2024USASD": 42, "AISI2024USLRFD": 43,
    "AISI2024CanLSD": 46,
}
SPEC_NAME_BY_ID: Dict[int, str] = {v: k for k, v in SPEC_BY_NAME.items()}


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
        f.P = float(P); f.Mx = float(Mx); f.My = float(My)
        f.Vx = float(Vx); f.Vy = float(Vy)
        f.Dx = float(Dx); f.Dy = float(Dy)
        return f

    def member_strengths(self, spec: int, lengths: Dict, **overrides) -> Dict:
        """Member design strengths (P, Mx, My, Vy) at the given unbraced lengths.

        Runs MemberCheck with zero applied force; the returned MemberUnityCheck
        exposes the available member strengths (already accounting for global /
        distortional buckling at Lx/Ly/Lt/Lm).
        """
        mp = self._member_params(spec, lengths, **overrides)
        f = self._forces()
        chk = self.calc.MemberCheck(mp, f)
        return {
            "P_k": _f(chk.P),
            "Mx_kin": _f(chk.Mx),
            "My_kin": _f(chk.My),
            "Vy_k": _f(chk.Vy),
            "Vx_k": _f(chk.Vx),
            "flags": _decode_flags(int(chk.Flags)),
        }

    def member_check(self, spec: int, lengths: Dict, forces: Dict, **overrides) -> Dict:
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
        return {
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
