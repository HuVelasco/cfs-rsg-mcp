# FILE: server.py
# PURPOSE: RSG CFS15 MCP server - cold-formed steel wall-stud design + profile
#          equivalency, driving the CFS15 Automation Module over COM.
# STATUS: Version 0.1.0.1 - Developed (bug-fix pass from tester report); not yet re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# ============================================================================
#                              MCP AGENT GUIDE
# ============================================================================
# This server performs REAL cold-formed-steel design calculations by driving the
# RSG CFS 15 calculation engine (the same engine as the CFS desktop app). Every
# number comes from CFS, not an approximation. Lives depend on accuracy - if a
# section fails to load or a check errors, the tool returns an honest error, it
# never fabricates a passing result.
#
# UNITS: kips, kip-inches, inches, ksi. Spacing/height as noted per tool.
#
# WHAT A "SECTION" CAN BE (accepted by every tool's `section` argument):
#   * An SSMA designation ...... "600S200-43"   (resolved from the bundled SSMA
#                                                 library; 33/50 ksi auto-picked)
#   * A custom .cfss file ....... "C:\...\Sections\SINGLE\600SLS250-43_18_GA.cfss"
#   * A full library path ....... "SSMA.cfsl|Structural Studs 33 ksi\600S200-43.cfss"
#
# TYPICAL WORKFLOWS
#   Design a wall with standard studs:
#     design_wall(height_ft=10, spacing_in=16, D_A=..., L_A=..., W_cc_w=...)
#   Find custom profiles equivalent to a standard spec (the headline feature):
#     find_equivalent_profiles(standard="600S200-43", height_ft=10, spacing_in=16)
#   Inspect one profile:
#     get_section_properties("600S200-43")
#
# SPEC: defaults to AISI2024USASD (enum 42). Pass spec="AISI2022USASD" or an int.
# ============================================================================

from __future__ import annotations

import json
import os
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from . import catalog
from .cfs_engine import (
    CFSError, DEFAULT_SPEC, SPEC_NAME_BY_ID, cfs_install_dir, cfs_session,
    describe_flags, parse_designation, resolve_spec, section_label,
)
from .cfs_loads import build_load_combinations, unbraced_lengths

mcp = FastMCP("cfs-rsg-mcp")


def _ok(**kw) -> str:
    return json.dumps({"success": True, **kw}, indent=2, default=str)


def _err(msg, **kw) -> str:
    return json.dumps({"success": False, "error": msg, **kw}, indent=2, default=str)


# ---------------------------------------------------------------------------
# Internal: evaluate one section inside an open session
# ---------------------------------------------------------------------------
def _evaluate(session, identifier, spec, lengths, grade=None, fy=None):
    """Load a section and gather gross props + member strengths at `lengths`.

    Returns a result dict; on failure returns {'error': ...} instead of raising,
    so a batch loop can continue past a bad section.
    """
    label = os.path.splitext(os.path.basename(identifier))[0] if identifier else identifier
    try:
        path = session.load(identifier, grade=grade, fy=fy)
    except CFSError as e:
        return {"section": label, "error": str(e)}
    try:
        sd = session.section_data()
        gp = session.gross_properties()
        ms = session.member_strengths(spec, lengths)
        return {
            "section": section_label(path, sd.get("description")),
            "identifier": identifier,
            "load_path": path,
            "section_type": sd.get("section_type_name"),
            "Fy_ksi": sd.get("Fy_ksi"),
            "gross": gp,
            "member_strength": ms,
        }
    except Exception as e:  # noqa: BLE001
        return {"section": label, "error": f"Calculation failed: {e}"}


# ---------------------------------------------------------------------------
# 1. Connection / status
# ---------------------------------------------------------------------------
@mcp.tool()
def connect_cfs() -> str:
    """Check the CFS15 Automation Module connection and license.

    Returns license status, the CFS install directory, the default design
    specification, and the available section libraries. Call this first if
    anything else errors, to isolate connection/license problems.
    """
    try:
        with cfs_session():
            install = cfs_install_dir()
            libs = []
            if install:
                libs = [os.path.basename(p) for p in
                        __import__("glob").glob(os.path.join(install, "*.cfsl"))]
            return _ok(
                message="Connected to RSG CFS15. License is active.",
                install_dir=install,
                default_spec=f"{SPEC_NAME_BY_ID.get(DEFAULT_SPEC)} (enum {DEFAULT_SPEC})",
                libraries=libs,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Unexpected error connecting to CFS: {e}")


# ---------------------------------------------------------------------------
# 2. Parse a free-text wall spec
# ---------------------------------------------------------------------------
@mcp.tool()
def parse_wall_specification(text: str) -> str:
    """Parse a free-text wall spec into structured fields.

    Example: parse_wall_specification("10ft tall, 600S200-43 @ 16 O.C.")
      -> {height_ft: 10, spacing_in: 16, designation: "600S200-43"}
    Understands feet-inch heights like "9'-6\"". Use this to turn a natural
    request into arguments for design_wall / find_equivalent_profiles.
    """
    spec = catalog.parse_wall_spec(text)
    desig = spec.get("designation")
    if desig:
        spec["designation_parsed"] = parse_designation(desig)
    return _ok(**spec)


# ---------------------------------------------------------------------------
# 3. List standard profiles
# ---------------------------------------------------------------------------
@mcp.tool()
def list_standard_profiles(type_letter: Optional[str] = None,
                           depth_in: Optional[float] = None,
                           flange_in: Optional[float] = None,
                           structural_only: bool = False,
                           library: str = "SSMA") -> str:
    """List standard profile designations available in a bundled CFS library.

    type_letter:     filter by 'S' (stud), 'T' (track), 'U' (channel), 'F' (furring).
    depth_in:        filter by nominal web depth in inches (e.g. 6.0 for 600-series).
    flange_in:       filter by nominal flange width in inches (e.g. 2.5).
    structural_only: drop non-structural drywall studs (S125 / 1-1/4" flange).
    library:         'SSMA' (default), 'SFIA', 'AISI', 'HUD', or 'LGSI'.
    """
    try:
        names = catalog.list_standard_designations(
            library, type_letter, depth_in, flange_in, structural_only)
        return _ok(library=library, count=len(names), profiles=names)
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


# ---------------------------------------------------------------------------
# 4. List custom profiles
# ---------------------------------------------------------------------------
@mcp.tool()
def list_custom_profiles(directory: Optional[str] = None,
                         recursive: bool = False) -> str:
    """List the user's custom .cfss profiles, with their parent folder.

    directory: folder of .cfss files. Defaults to the bundled
               Info-and-Examples/Sections ROOT, scanned recursively, so ALL
               sibling libraries (SINGLE / B2B / BOX / TRUSS) are discovered in
               one call. Pass a specific folder to scope the search.
    recursive: force recursion when scoping to a specific directory.
    Each entry includes `folder` (immediate parent) so builds are distinguishable.
    """
    try:
        profiles = catalog.list_custom_profiles(directory, recursive)
        folders = sorted({p["folder"] for p in profiles})
        return _ok(
            directory=directory or catalog._SECTIONS_ROOT,
            folders_found=folders,
            count=len(profiles), profiles=profiles,
        )
    except Exception as e:  # noqa: BLE001
        return _err(str(e))


# ---------------------------------------------------------------------------
# 5. Section properties + fully-braced strength
# ---------------------------------------------------------------------------
@mcp.tool()
def get_section_properties(section: str, spec=None,
                           fy: Optional[float] = None,
                           grade: Optional[int] = None) -> str:
    """Get gross section properties and fully-braced strengths for one section.

    section: SSMA designation ('600S200-43'), .cfss file path, or library path.
    spec:    AISI spec (name or int enum). Default AISI2024USASD.
    fy:      override yield strength (ksi) after loading (forces carbon steel).
    grade:   for standard designations, force 33 or 50 ksi library folder.
    """
    spec_id = resolve_spec(spec)
    try:
        with cfs_session() as s:
            path = s.load(section, grade=grade, fy=fy)
            sd = s.section_data()
            gp = s.gross_properties()
            st = s.fully_braced_strength(spec_id)
            return _ok(
                section=section_label(path, sd.get("description")),
                load_path=path,
                spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
                section_data=sd,
                gross_properties=gp,
                fully_braced_strength=st,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Failed to get properties for '{section}': {e}")


# ---------------------------------------------------------------------------
# 6. Check a single stud
# ---------------------------------------------------------------------------
@mcp.tool()
def check_stud(section: str, height_ft: float, spacing_in: float = 16.0,
               brace_config: str = "none", custom_n: Optional[int] = None,
               spec=None, grade: Optional[int] = None, fy: Optional[float] = None,
               # explicit forces (override load-combo generation if any is set):
               P_k: Optional[float] = None, Mx_kin: Optional[float] = None,
               Vy_k: Optional[float] = None,
               # OR provide PSF loads to auto-build ASD combos:
               loads: Optional[dict] = None,
               trib_A_ft: float = 0.0, trib_B_ft: float = 0.0,
               wall_type: str = "exterior") -> str:
    """Run an AISI member check on ONE stud section.

    Two modes:
      * Explicit forces - pass any of P_k / Mx_kin / Vy_k for a single check.
      * Load combos     - pass a `loads` PSF dict (keys like D_A, L_A, S,
                          W_cc_w, W_mwfw, Eh...) plus tributary widths; the tool
                          builds the 10 ASD combos and reports the governing one.

    brace_config: none|midpoint|third|quarter|48in|60in|72in|custom (needs custom_n).
    Returns per-combo unity checks and the governing UC (PASS if <= 1.0).
    """
    spec_id = resolve_spec(spec)
    try:
        lengths = unbraced_lengths(height_ft, brace_config, custom_n)
    except ValueError as e:
        return _err(str(e))

    explicit = any(v is not None for v in (P_k, Mx_kin, Vy_k))
    if explicit:
        combos = [{"combo": 1, "label": "explicit forces",
                   "P_k": P_k or 0.0, "Mx_kin": Mx_kin or 0.0, "Vy_k": Vy_k or 0.0}]
    else:
        geom = {"height_ft": height_ft, "spacing_in": spacing_in,
                "trib_A_ft": trib_A_ft, "trib_B_ft": trib_B_ft, "wall_type": wall_type}
        combos = build_load_combinations(loads or {}, geom)

    try:
        with cfs_session() as s:
            path = s.load(section, grade=grade, fy=fy)
            sd = s.section_data()
            rows, gov_uc, gov = [], -1.0, None
            for c in combos:
                chk = s.member_check(spec_id, lengths, c)
                row = {"combo": c["combo"], "label": c["label"],
                       "P_k": c["P_k"], "Mx_kin": c["Mx_kin"], "Vy_k": c["Vy_k"],
                       "PMxMy1": chk["PMxMy1"], "PMxMy2": chk["PMxMy2"],
                       "MxVy": chk["MxVy"], "UC": chk["UC"], "status": chk["status"]}
                rows.append(row)
                if chk["UC"] > gov_uc:
                    gov_uc, gov = chk["UC"], row
            return _ok(
                section=section_label(path, sd.get("description")), load_path=path,
                spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
                height_ft=height_ft, spacing_in=spacing_in,
                brace_config=brace_config, unbraced_lengths_in=lengths,
                passed=gov_uc <= 1.0, governing_UC=round(gov_uc, 4),
                governing_combo=gov, combos=rows,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Check failed for '{section}': {e}")


# ---------------------------------------------------------------------------
# 7. Design a wall - pick the lightest passing section
# ---------------------------------------------------------------------------
@mcp.tool()
def design_wall(height_ft: float = 10.0, spacing_in: float = 16.0,
                brace_config: str = "third", custom_n: Optional[int] = None,
                wall_type: str = "exterior",
                trib_A_ft: float = 0.0, trib_B_ft: float = 0.0,
                # candidate sections: explicit list, custom folder, or SSMA depth
                sections: Optional[List[str]] = None,
                custom_directory: Optional[str] = None,
                ssma_depth_in: Optional[float] = None,
                structural_only: bool = True,
                spec=None,
                # ASD loads (PSF) - anything omitted defaults to 0 (D_wall=12):
                D_wall: float = 12.0, D_A: float = 0.0, L_A: float = 0.0,
                D_B: float = 0.0, L_B: float = 0.0, S: float = 0.0,
                W_uplift: float = 0.0, W_mwfw: float = 0.0, W_mwfl: float = 0.0,
                W_cc_w: float = 0.0, W_cc_l: float = 0.0, L_lat: float = 0.0,
                Eh: float = 0.0, Ev: float = 0.0) -> str:
    """Design a wall stud: try candidate sections, return the lightest that passes.

    Candidate sections (choose one source):
      * sections          - explicit list of designations / .cfss paths (in order)
      * custom_directory   - a folder of custom .cfss files
      * ssma_depth_in      - auto-build SSMA structural studs of this depth,
                             ordered thinnest->thickest (e.g. 6.0)
    If none is given, defaults to SSMA 6" structural studs.

    structural_only (default True): when auto-building from SSMA, skip
    non-structural drywall studs (S125 / 1-1/4" flange) so a load-bearing wall is
    never designed with a partition-only gauge. Set False to include them.

    Loads are ASD PSF values; tributary widths in feet. The tool builds the 10
    ASCE-7 ASD combos and checks every combo. Returns the first passing section
    plus a summary of all sections tried.
    """
    spec_id = resolve_spec(spec)
    try:
        lengths = unbraced_lengths(height_ft, brace_config, custom_n)
    except ValueError as e:
        return _err(str(e))

    # Resolve candidate list
    try:
        if sections:
            candidates = list(sections)
        elif custom_directory:
            candidates = [p["path"] for p in catalog.list_custom_profiles(custom_directory)]
        else:
            depth = ssma_depth_in if ssma_depth_in is not None else 6.0
            candidates = catalog.list_standard_designations(
                "SSMA", "S", depth, structural_only=structural_only)
        if not candidates:
            return _err("No candidate sections found for the given source.")
    except Exception as e:  # noqa: BLE001
        return _err(f"Could not build candidate list: {e}")

    geom = {"height_ft": height_ft, "spacing_in": spacing_in,
            "trib_A_ft": trib_A_ft, "trib_B_ft": trib_B_ft, "wall_type": wall_type}
    loads = {"D_wall": D_wall, "D_A": D_A, "L_A": L_A, "D_B": D_B, "L_B": L_B,
             "S": S, "W_uplift": W_uplift, "W_mwfw": W_mwfw, "W_mwfl": W_mwfl,
             "W_cc_w": W_cc_w, "W_cc_l": W_cc_l, "L_lat": L_lat, "Eh": Eh, "Ev": Ev}
    combos = build_load_combinations(loads, geom)

    tried, selected = [], None
    try:
        with cfs_session() as s:
            for ident in candidates:
                try:
                    path = s.load(ident)
                    sd = s.section_data()
                    gp = s.gross_properties()
                    gov_uc, gov = -1.0, {"combo": None, "label": None,
                                         "P_k": None, "Mx_kin": None, "Vy_k": None}
                    for c in combos:
                        chk = s.member_check(spec_id, lengths, c)
                        if chk["UC"] > gov_uc:
                            gov_uc, gov = chk["UC"], {**c, **chk}
                    rec = {"section": section_label(path, sd.get("description")),
                           "weight_plf": gp["weight_plf"],
                           "governing_UC": round(gov_uc, 4),
                           "governing_combo": gov["combo"], "governing_label": gov["label"],
                           "passed": gov_uc <= 1.0}
                    tried.append(rec)
                    if gov_uc <= 1.0:
                        selected = {**rec, "load_path": path,
                                    "governing_forces": {"P_k": gov["P_k"],
                                                         "Mx_kin": gov["Mx_kin"],
                                                         "Vy_k": gov["Vy_k"]}}
                        break
                except CFSError as e:
                    tried.append({"section": ident, "error": str(e)})
                    continue
    except CFSError as e:
        return _err(str(e))

    return _ok(
        height_ft=height_ft, spacing_in=spacing_in, brace_config=brace_config,
        unbraced_lengths_in=lengths,
        spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
        load_combinations=combos,
        selected=selected,
        passed=selected is not None,
        message=None if selected else "No candidate section passes; increase size or reduce loads.",
        sections_tried=tried,
    )


# ---------------------------------------------------------------------------
# 8. HEADLINE: find custom profiles equivalent to a standard spec
# ---------------------------------------------------------------------------
@mcp.tool()
def find_equivalent_profiles(standard: str, height_ft: float,
                             spacing_in: float = 16.0,
                             custom_profiles: Optional[List[str]] = None,
                             custom_directory: Optional[str] = None,
                             recursive: bool = False,
                             brace_config: str = "none",
                             custom_n: Optional[int] = None,
                             spec=None, tolerance: float = 0.0,
                             standard_grade: Optional[int] = None,
                             standard_fy: Optional[float] = None) -> str:
    """Find which of your custom profiles are structurally equivalent to a
    standard profile for a given wall condition.

    Example (your request):
      find_equivalent_profiles(standard="600S200-43", height_ft=10, spacing_in=16)

    Method: at the wall's unbraced condition (from height_ft + brace_config), CFS
    computes the standard profile's member AXIAL strength (P), FLEXURAL strength
    (Mx) and gross stiffness (Ix). Each custom profile is evaluated the same way.
    A custom profile is 'equivalent' when it meets or exceeds the standard on
    BOTH axial and flexural strength (within `tolerance`, e.g. 0.02 = allow 2%
    under). Stiffness ratio (Ix, drives deflection) is reported for judgment.

    custom_profiles:  explicit list of .cfss paths / designations.
    custom_directory: folder of .cfss files (default: bundled SINGLE library).
    recursive:        include subfolders of custom_directory.
    tolerance:        fractional shortfall allowed (0.0 = strict >=).
    brace_config:     bracing for BOTH standard and customs (kept identical so
                      the comparison is fair). Default 'none' = full-height
                      unbraced (conservative).

    Returns the reference strengths, and every custom profile ranked with its
    axial / flexural / stiffness ratios, an 'equivalent' flag, its weight, and
    'max_spacing_in' (the spacing at which the custom matches the standard's
    per-foot capacity when used at the reference spacing).
    """
    spec_id = resolve_spec(spec)
    try:
        lengths = unbraced_lengths(height_ft, brace_config, custom_n)
    except ValueError as e:
        return _err(str(e))

    # Build custom candidate list
    try:
        if custom_profiles:
            customs = list(custom_profiles)
        else:
            # keep this tool's documented default = the SINGLE library
            cdir = custom_directory or catalog.DEFAULT_CUSTOM_DIR
            customs = [p["path"] for p in
                       catalog.list_custom_profiles(cdir, recursive)]
        if not customs:
            return _err("No custom profiles found. Provide custom_profiles or a "
                        "valid custom_directory.")
    except Exception as e:  # noqa: BLE001
        return _err(f"Could not build custom profile list: {e}")

    try:
        with cfs_session() as s:
            ref = _evaluate(s, standard, spec_id, lengths,
                            grade=standard_grade, fy=standard_fy)
            if "error" in ref:
                return _err(f"Could not evaluate standard profile '{standard}': "
                            f"{ref['error']}")
            ref_P = ref["member_strength"]["P_k"] or 0.0
            ref_Mx = ref["member_strength"]["Mx_kin"] or 0.0
            ref_Ix = ref["gross"]["Ix_in4"] or 0.0

            results = []
            for ident in customs:
                r = _evaluate(s, ident, spec_id, lengths)
                if "error" in r:
                    results.append({"section": r["section"], "error": r["error"],
                                    "equivalent": False})
                    continue
                cP = r["member_strength"]["P_k"] or 0.0
                cMx = r["member_strength"]["Mx_kin"] or 0.0
                cIx = r["gross"]["Ix_in4"] or 0.0
                pr = _ratio(cP, ref_P)
                mr = _ratio(cMx, ref_Mx)
                ir = _ratio(cIx, ref_Ix)
                gov = min(pr, mr)
                equivalent = (pr >= 1.0 - tolerance) and (mr >= 1.0 - tolerance)
                results.append({
                    "section": r["section"],
                    "identifier": ident,
                    "section_type": r["section_type"],
                    "Fy_ksi": r["Fy_ksi"],
                    "weight_plf": r["gross"]["weight_plf"],
                    "axial_P_k": cP, "flexural_Mx_kin": cMx, "Ix_in4": cIx,
                    "axial_ratio": round(pr, 3),
                    "flexural_ratio": round(mr, 3),
                    "stiffness_ratio": round(ir, 3),
                    "governing_ratio": round(gov, 3),
                    "equivalent": equivalent,
                    "max_spacing_in": round(spacing_in * gov, 2),
                })

            # equivalents first (lightest first), then the rest by governing ratio
            results.sort(key=lambda x: (
                not x.get("equivalent", False),
                x.get("weight_plf") if x.get("equivalent") else -x.get("governing_ratio", 0),
            ))

            equiv = [r for r in results if r.get("equivalent")]
            return _ok(
                message=(f"{len(equiv)} of {len(customs)} custom profiles are "
                         f"equivalent to {ref['section']} for a {height_ft} ft wall "
                         f"@ {spacing_in}\" o.c."),
                spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
                brace_config=brace_config, unbraced_lengths_in=lengths,
                reference={
                    "section": ref["section"], "load_path": ref["load_path"],
                    "Fy_ksi": ref["Fy_ksi"], "spacing_in": spacing_in,
                    "axial_P_k": ref_P, "flexural_Mx_kin": ref_Mx,
                    "Ix_in4": ref_Ix, "weight_plf": ref["gross"]["weight_plf"],
                },
                equivalent_count=len(equiv),
                total_evaluated=len(customs),
                profiles=results,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Equivalency evaluation failed: {e}")


def _ratio(val, ref):
    if not ref:
        return float("inf") if val else 1.0
    return val / ref


def _msbs_depth_in(name: str) -> float:
    """Nominal depth (in) from an MSBS filename, e.g. '600SLS250-43_18_GA' -> 6.0."""
    import re as _re
    m = _re.match(r"\s*(\d{2,4})", name or "")
    return int(m.group(1)) / 100.0 if m else -1.0


# ---------------------------------------------------------------------------
# 9. HEADLINE: substitute a standard wall spec with MSBS sections
#    (considers single/double sections AND different stud spacings)
# ---------------------------------------------------------------------------
DEFAULT_SPACINGS = [12.0, 16.0, 19.2, 24.0]


@mcp.tool()
def find_wall_substitutions(standard: str, height_ft: float,
                            spacing_in: float = 16.0,
                            brace_config: str = "none",
                            custom_n: Optional[int] = None,
                            msbs_directory: Optional[str] = None,
                            categories: Optional[List[str]] = None,
                            allowed_spacings: Optional[List[float]] = None,
                            depth_filter_in: Optional[List[float]] = None,
                            governing: str = "both",
                            spec=None, tolerance: float = 0.0,
                            standard_grade: Optional[int] = None,
                            standard_fy: Optional[float] = None) -> str:
    """Evaluate EVERY MSBS profile as a substitute for a standard wall spec and
    return the full ratio table — passing AND failing — for the engineer to judge.

    The tool computes; it does NOT hide options. A profile may pass axial (P) but
    fail flexural (Mx); on an interior wall with no wind, Mx demand is zero and the
    Mx shortfall is irrelevant — that call belongs to the engineer, so BOTH ratios
    are always reported.

    Example (single profiles, sheet-width spacing then half-sheet fallback):
      find_wall_substitutions(standard="400S300-68", height_ft=10, spacing_in=30,
          brace_config="quarter", allowed_spacings=[30, 15],
          categories=["single"], governing="axial")   # interior, no wind

    METHOD — capacity per foot of wall. A wall is a section AT a spacing. Member
    strength depends only on the section + unbraced length (NOT spacing), so each
    profile is evaluated by CFS once, then spacings are swept analytically. At
    spacing s a profile's per-foot capacity is (P/s, Mx/s); the reference is
    (P_ref/spacing_in, Mx_ref/spacing_in). Every ratio scales by
    (spacing_in / s).

    PARAMETERS
      spacing_in       reference wall's stud spacing (defines the demand basis).
      allowed_spacings ORDERED spacings (in) to try for substitutes, most-preferred
                       first, e.g. [30, 15] (30" = sheet width, 15" = fallback).
                       Used EXACTLY as given — the reference spacing is NOT auto-
                       added. Default [12, 16, 19.2, 24].
      categories       ['single'] and/or ['double'] (default both). Use ['single']
                       when double back-to-back / box build-ups are not allowed.
      depth_filter_in  restrict candidates to these nominal depths (e.g. [4.0]).
      governing        which strength must pass for a spacing to be 'ok':
                         'both'     - P AND Mx (default; exterior / wind walls)
                         'axial'    - P only  (interior wall, no lateral/wind)
                         'flexural' - Mx only
                       Ratios for BOTH P and Mx are always reported regardless.
      tolerance        fractional shortfall allowed on the governing check(s),
                       e.g. 0.02 lets a 0.98 ratio count as passing.

    RETURNS, per profile (ALL of them, ranked passing-first then by closeness):
      axial_ratio / flexural_ratio / stiffness_ratio / weight_ratio at the best
      (widest passing) spacing, max continuous spacing for P-only, Mx-only and
      combined, and a `by_spacing` breakdown giving each ratio + axial_ok /
      flexural_ok / passes at every allowed spacing. `best_spacing_in` is the
      widest allowed spacing meeting `governing` (null if none). Plus flags/notes.
    """
    spec_id = resolve_spec(spec)
    governing = (governing or "both").lower()
    if governing not in ("both", "axial", "flexural"):
        return _err("governing must be 'both', 'axial', or 'flexural'.")
    spacings = sorted({float(s) for s in (allowed_spacings or DEFAULT_SPACINGS)},
                      reverse=True)  # widest (most preferred) first
    try:
        lengths = unbraced_lengths(height_ft, brace_config, custom_n)
    except ValueError as e:
        return _err(str(e))

    try:
        msbs = catalog.list_msbs_sections(msbs_directory, categories)
        if depth_filter_in:
            depths = [float(d) for d in depth_filter_in]
            msbs = [m for m in msbs
                    if any(abs(_msbs_depth_in(m["name"]) - d) <= 1e-6 for d in depths)]
        if not msbs:
            return _err("No MSBS sections found for the given filters. Check "
                        "msbs_directory / categories / depth_filter_in, or the "
                        "bundled Info-and-Examples/Sections folders.")
    except Exception as e:  # noqa: BLE001
        return _err(f"Could not list MSBS sections: {e}")

    def _passes(axial_ok, flex_ok):
        if governing == "axial":
            return axial_ok
        if governing == "flexural":
            return flex_ok
        return axial_ok and flex_ok

    try:
        with cfs_session() as s:
            ref = _evaluate(s, standard, spec_id, lengths,
                            grade=standard_grade, fy=standard_fy)
            if "error" in ref:
                return _err(f"Could not evaluate standard '{standard}': {ref['error']}")
            ref_P = ref["member_strength"]["P_k"] or 0.0
            ref_Mx = ref["member_strength"]["Mx_kin"] or 0.0
            ref_Ix = ref["gross"]["Ix_in4"] or 0.0
            ref_w = ref["gross"]["weight_plf"] or 0.0
            ref_w_psf = ref_w * 12.0 / spacing_in
            tol = 1.0 - tolerance

            profiles = []
            for m in msbs:
                disp = m["name"] if m["folder"] == "SINGLE" \
                    else f"{m['name']} [{m['folder']}]"
                r = _evaluate(s, m["path"], spec_id, lengths)
                if "error" in r:
                    profiles.append({"section": disp, "category": m["category"],
                                     "error": r["error"], "passes": False,
                                     "best_spacing_in": None})
                    continue
                cP = r["member_strength"]["P_k"] or 0.0
                cMx = r["member_strength"]["Mx_kin"] or 0.0
                cIx = r["gross"]["Ix_in4"] or 0.0
                cw = r["gross"]["weight_plf"] or 0.0
                flags = r["member_strength"]["flags"]

                # capacity ratios (independent of spacing): substitute vs reference
                cap_P = _ratio(cP, ref_P)
                cap_Mx = _ratio(cMx, ref_Mx)
                cap_Ix = _ratio(cIx, ref_Ix)

                # max continuous spacings = ref spacing scaled by capacity ratio
                max_axial = spacing_in * cap_P
                max_flex = spacing_in * cap_Mx
                max_comb = min(max_axial, max_flex)

                by_spacing, best = [], None
                for sp in spacings:  # widest first
                    scale = spacing_in / sp   # ratio at this spacing
                    aP, aMx, aIx = cap_P * scale, cap_Mx * scale, cap_Ix * scale
                    aW = _ratio(cw, ref_w) * scale
                    a_ok, m_ok = aP >= tol, aMx >= tol
                    passes_here = _passes(a_ok, m_ok)
                    by_spacing.append({
                        "spacing_in": sp,
                        "axial_ratio": round(aP, 3), "flexural_ratio": round(aMx, 3),
                        "stiffness_ratio": round(aIx, 3), "weight_ratio": round(aW, 3),
                        "weight_psf": round(cw * 12.0 / sp, 4),
                        "axial_ok": a_ok, "flexural_ok": m_ok, "passes": passes_here,
                    })
                    if passes_here and best is None:
                        best = sp  # spacings are widest-first, so first pass = widest

                rec = {
                    "section": disp, "category": m["category"], "folder": m["folder"],
                    "members_per_stud": m["members_per_stud"],
                    "section_type": r["section_type"],
                    "member_weight_plf": cw,
                    "axial_P_k": cP, "flexural_Mx_kin": cMx, "Ix_in4": cIx,
                    "capacity_axial_ratio": round(cap_P, 3),
                    "capacity_flexural_ratio": round(cap_Mx, 3),
                    "capacity_stiffness_ratio": round(cap_Ix, 3),
                    "max_continuous_spacing_axial_in": round(max_axial, 2),
                    "max_continuous_spacing_flexural_in": round(max_flex, 2),
                    "max_continuous_spacing_combined_in": round(max_comb, 2),
                    "flags": flags, "flag_notes": describe_flags(flags),
                    "by_spacing": by_spacing,
                    "best_spacing_in": best,
                    "passes": best is not None,
                }
                if best is not None:
                    rec["axial_ratio"] = round(cap_P * spacing_in / best, 3)
                    rec["flexural_ratio"] = round(cap_Mx * spacing_in / best, 3)
                    rec["stiffness_ratio"] = round(cap_Ix * spacing_in / best, 3)
                    rec["weight_psf"] = round(cw * 12.0 / best, 4)
                    rec["lighter_than_original"] = rec["weight_psf"] < ref_w_psf
                profiles.append(rec)

            # rank: passing first (lightest at best spacing), then failing by how
            # close they get on the governing check at the tightest allowed spacing
            tightest = spacings[-1] if spacings else spacing_in

            def close_score(p):
                if p.get("error"):
                    return -1.0
                sc = spacing_in / tightest
                aP = p["capacity_axial_ratio"] * sc
                aMx = p["capacity_flexural_ratio"] * sc
                if governing == "axial":
                    return aP
                if governing == "flexural":
                    return aMx
                return min(aP, aMx)

            profiles.sort(key=lambda p: (
                not p.get("passes", False),
                p["weight_psf"] if p.get("passes") else -close_score(p),
            ))

            passing = [p for p in profiles if p.get("passes")]
            recommended = passing[0] if passing else None
            if recommended:
                msg = (f"{len(passing)} of {len(profiles)} profiles pass on "
                       f"'{governing}'. Lightest: {recommended['section']} @ "
                       f"{recommended['best_spacing_in']}\" o.c. "
                       f"({recommended['weight_psf']} psf vs {round(ref_w_psf,4)} psf "
                       f"original). Full ratio table returned for all profiles.")
            else:
                closest = profiles[0] if profiles and not profiles[0].get("error") else None
                extra = ""
                if closest:
                    extra = (f" Closest: {closest['section']} reaches "
                             f"{closest['max_continuous_spacing_combined_in']}\" "
                             f"combined / {closest['max_continuous_spacing_axial_in']}\" "
                             f"axial-only max spacing.")
                msg = (f"No profile passes on '{governing}' at any allowed spacing "
                       f"{spacings}.{extra} Full ratio table returned so you can see "
                       f"how far each is and judge (e.g. axial-only for interior walls).")

            return _ok(
                message=msg,
                spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
                governing=governing,
                brace_config=brace_config, unbraced_lengths_in=lengths,
                allowed_spacings_in=spacings,
                reference={
                    "section": ref["section"], "spacing_in": spacing_in,
                    "axial_P_k": ref_P, "flexural_Mx_kin": ref_Mx, "Ix_in4": ref_Ix,
                    "member_weight_plf": ref_w, "weight_psf": round(ref_w_psf, 4),
                },
                recommended=recommended,
                passing_count=len(passing),
                total_evaluated=len(profiles),
                profiles=profiles,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Substitution search failed: {e}")


# ---------------------------------------------------------------------------
# 10. Batch code-check members (ETABS / SAP2000 truss or frame integration)
# ---------------------------------------------------------------------------
def _member_lengths(m: dict, spec_default_brace: str = "none") -> dict:
    """Resolve a member's unbraced lengths from its dict.

    Length keys may sit at the TOP LEVEL of the member dict or inside a nested
    `lengths` sub-dict — both are accepted. Priority order:
      * explicit Lx_in/Ly_in/Lt_in/Lm_in
      * Lu_in (applied to all four)
      * length_ft (+ optional brace_config / custom_n)
    """
    src = dict(m)
    if isinstance(m.get("lengths"), dict):
        src = {**m, **m["lengths"]}  # nested `lengths` keys override / fill in
    if any(k in src for k in ("Lx_in", "Ly_in", "Lt_in", "Lm_in")):
        base = src.get("Lu_in")
        def g(k):
            v = src.get(k, base)
            if v is None:
                raise ValueError(f"Member '{m.get('id')}' missing {k} (or Lu_in).")
            return float(v)
        return {"Lx": g("Lx_in"), "Ly": g("Ly_in"), "Lt": g("Lt_in"), "Lm": g("Lm_in")}
    if src.get("Lu_in") is not None:
        L = float(src["Lu_in"])
        return {"Lx": L, "Ly": L, "Lt": L, "Lm": L}
    if src.get("length_ft") is not None:
        return unbraced_lengths(float(src["length_ft"]),
                                src.get("brace_config", spec_default_brace),
                                src.get("custom_n"))
    raise ValueError(
        f"Member '{m.get('id')}' has no length info. Provide length keys either at "
        f"the top level or inside a 'lengths' object: Lx_in/Ly_in/Lt_in/Lm_in, or "
        f"Lu_in, or length_ft (+brace_config)."
    )


# Rough sanity bounds for a single CFS member (kip / kip-inch). Values beyond
# these usually mean the caller passed pounds instead of kips.
_P_SANITY_K = 500.0
_M_SANITY_KIN = 10000.0


def _force_unit_warnings(mid, forces: dict) -> List[str]:
    w = []
    P = abs(forces.get("P_k", 0.0) or 0.0)
    Mx = abs(forces.get("Mx_kin", 0.0) or 0.0)
    My = abs(forces.get("My_kin", 0.0) or 0.0)
    if P > _P_SANITY_K:
        w.append(f"Member '{mid}': P_k={P} looks large for kips — did you pass "
                 f"POUNDS? CFS expects kips (divide lb by 1000).")
    if max(Mx, My) > _M_SANITY_KIN:
        w.append(f"Member '{mid}': moment {max(Mx, My)} looks large for kip-in — "
                 f"did you pass lb-in? CFS expects kip-inches.")
    return w


@mcp.tool()
def check_members_batch(members: List[dict], spec=None,
                        brace_config: str = "none") -> str:
    """Code-check many members against applied forces — for ETABS/SAP2000 output.

    Feed the results of a frame/truss analysis (e.g. from the ETABS MCP) straight
    in: each member carries its section, its unbraced lengths, and its forces.

    members: list of dicts. Working example (length keys at TOP LEVEL):
        {"id": "B7", "section": "335SLC-54_16_GA.cfss",
         "forces": {"P_k": 4.26, "Mx_kin": 1.98, "Vy_k": 0.0},
         "Lu_in": 12.0}
      Fields:
        id       - label (e.g. frame name from ETABS)
        section  - designation / .cfss path / library path
        forces   - {P_k, Mx_kin, My_kin, Vy_k, Vx_k}
        lengths  - ONE of: Lx_in/Ly_in/Lt_in/Lm_in, or Lu_in, or length_ft(+brace_config).
                   These may be TOP-LEVEL keys OR inside a nested "lengths" object;
                   both work.
        grade,fy - optional overrides

    SIGN CONVENTION (important): CFS uses structural convention — compression P is
    POSITIVE, tension NEGATIVE. ETABS reports axial compression as NEGATIVE, so
    flip the sign of ETABS axial forces before passing them in. Mx is k-in with
    compression-on-top positive.

    UNITS: kips and kip-inches. If your model is in lb / lb-in, convert first
    (divide by 1000). The tool emits a warning if a force looks like wrong units.

    Zero-shear-capacity sections (e.g. a BendRadius geometry flag) are handled
    honestly: if there is no applied shear the shear-interaction check is excluded
    (with a warning) instead of reporting a bogus UC=9.999; if shear IS applied it
    is reported as a real failure. Read each member's `flags`/`flag_notes`.

    Returns per-member unity check (UC, governing_check, PASS/FAIL, member
    strengths, NoteFlags with plain-English notes, warnings) and an overall
    summary. Sections are loaded once and reused across members that share them.
    """
    spec_id = resolve_spec(spec)
    if not members:
        return _err("No members provided.")

    # group by (section, grade, fy) so each unique section loads once
    order = {id(m): i for i, m in enumerate(members)}
    groups: dict = {}
    try:
        for m in members:
            key = (str(m.get("section")), m.get("grade"), m.get("fy"))
            groups.setdefault(key, []).append(m)
    except Exception as e:  # noqa: BLE001
        return _err(f"Bad members input: {e}")

    results = []
    try:
        with cfs_session() as s:
            for (section, grade, fy), grp in groups.items():
                try:
                    path = s.load(section, grade=grade, fy=fy)
                    sd = s.section_data()
                    label = section_label(path, sd.get("description"))
                except CFSError as e:
                    for m in grp:
                        results.append({"id": m.get("id"), "section": section,
                                        "error": str(e), "status": "ERROR"})
                    continue
                for m in grp:
                    try:
                        lengths = _member_lengths(m, brace_config)
                    except ValueError as e:
                        results.append({"id": m.get("id"), "section": label,
                                        "error": str(e), "status": "ERROR"})
                        continue
                    fc = m.get("forces", {})
                    chk = s.member_check(spec_id, lengths, fc)
                    warns = list(chk.get("warnings", [])) + _force_unit_warnings(m.get("id"), fc)
                    results.append({
                        "id": m.get("id"), "section": label,
                        "unbraced_lengths_in": lengths,
                        "forces": fc,
                        "PMxMy1": chk["PMxMy1"], "PMxMy2": chk["PMxMy2"],
                        "MxVy": chk["MxVy"], "MyVx": chk["MyVx"],
                        "UC": chk["UC"], "governing_check": chk["governing_check"],
                        "status": chk["status"], "flags": chk["flags"],
                        "flag_notes": chk["flag_notes"],
                        "strengths": chk["strengths"],
                        "warnings": warns,
                    })
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Batch check failed: {e}")

    results.sort(key=lambda r: order.get(
        next((id(m) for m in members if m.get("id") == r.get("id")), 0), 0))
    checked = [r for r in results if r.get("status") in ("PASS", "FAIL")]
    failed = [r for r in checked if r["status"] == "FAIL"]
    errored = [r for r in results if r.get("status") == "ERROR"]
    worst = max((r["UC"] for r in checked), default=None)
    return _ok(
        spec=f"{SPEC_NAME_BY_ID.get(spec_id)} (enum {spec_id})",
        total=len(members), passed=len(checked) - len(failed),
        failed=len(failed), errored=len(errored),
        all_passed=len(failed) == 0 and len(errored) == 0,
        governing_UC=round(worst, 4) if worst is not None else None,
        members=results,
    )


# ---------------------------------------------------------------------------
# 11. Export section properties for ETABS / SAP2000 frame definition
# ---------------------------------------------------------------------------
@mcp.tool()
def export_section_for_analysis(section: str, fy: Optional[float] = None,
                                grade: Optional[int] = None) -> str:
    """Export a section's properties for defining a frame section in ETABS/SAP2000.

    Returns the gross properties in the exact argument order used by the CSI API
    PropFrame.SetGeneral (SAP2000 / ETABS share this), so the ETABS MCP can create
    a matching general frame section, plus a plain property dict. Units are
    kip-inch (in, in^2, in^4). Shear areas As2/As3 are returned as 0 (rigid) —
    CFS governs shear via its own web-crippling / shear checks, so let the CFS
    tools handle shear rather than the analysis model.

    Use this to round-trip: CFS designs/verifies the CFS member, then hands ETABS
    the properties it needs to represent that member in the global model.
    """
    try:
        with cfs_session() as s:
            path = s.load(section, grade=grade, fy=fy)
            sd = s.section_data()
            gp = s.gross_properties()
            setgeneral = {
                "t3_depth_in": sd["depth_in"],
                "t2_width_in": sd["flange_in"],
                "Area_in2": gp["area_in2"],
                "As2_shear_in2": 0.0,
                "As3_shear_in2": 0.0,
                "Torsion_J_in4": gp["J_in4"],
                "I33_Ix_in4": gp["Ix_in4"],
                "I22_Iy_in4": gp["Iy_in4"],
                "S33_Sx_in3": gp["Sx_in3"],
                "S22_Sy_in3": gp["Sy_in3"],
                "Z33_Zx_in3": gp["Zx_in3"],
                "Z22_Zy_in3": gp["Zy_in3"],
                "R33_rx_in": gp["rx_in"],
                "R22_ry_in": gp["ry_in"],
                "_As2_As3_note": ("As2=As3=0 sets RIGID shear in the analysis "
                                  "model — the model will NOT reduce stiffness or "
                                  "check shear. This is intentional: CFS governs "
                                  "shear/web-crippling. Do NOT copy these zeros "
                                  "into a model where you expect it to check shear."),
            }
            return _ok(
                section=section_label(path, sd.get("description")),
                load_path=path,
                units="kip, inch",
                shear_assumption=("As2=As3=0 (rigid shear). Shear is designed in "
                                  "CFS via check_stud / check_members_batch, not in "
                                  "the analysis model."),
                note=("Feed sap2000_setgeneral values to PropFrame.SetGeneral "
                      "(same API for ETABS). Warping Cw and shear center Xo are "
                      "included for torsion-sensitive modeling but are not "
                      "SetGeneral arguments."),
                material={"Fy_ksi": sd["Fy_ksi"], "Fu_ksi": sd["Fu_ksi"],
                          "E_ksi": 29500.0},
                sap2000_setgeneral=setgeneral,
                extra_properties={"Cw_in6": gp["Cw_in6"], "Xo_in": gp["Xo_in"],
                                  "Ro_in": gp["Ro_in"], "weight_plf": gp["weight_plf"]},
                gross_properties=gp,
                section_data=sd,
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Export failed for '{section}': {e}")


# ============================================================================
#                               RUN SERVER
# ============================================================================
def main() -> None:
    """Console-script / module entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
