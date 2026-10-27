# FILE: server.py
# PURPOSE: RSG CFS15 MCP server - cold-formed steel wall-stud design + profile
#          equivalency, driving the CFS15 Automation Module over COM.
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
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
# UNITS: kips, kip-inches, inches, ksi; area loads psf, line loads plf.
#
# WHAT A "SECTION" CAN BE (accepted by every tool's `section` argument):
#   * An SSMA designation ...... "600S200-43"   (resolved from the bundled SSMA
#                                                 library; 33/50 ksi auto-picked)
#   * A custom .cfss file ....... "C:\...\Sections\SINGLE\600SLS250-43_18_GA.cfss"
#   * A full library path ....... "SSMA.cfsl|Structural Studs 33 ksi\600S200-43.cfss"
#
# TYPICAL WORKFLOWS
#   Design a wall with standard studs:
#     design_wall(height_ft=10, spacing_in=16, wall_type="exterior", D_A=..., W_cc_w=...)
#   Design a multi-storey stacked wall:
#     design_stacked_wall(levels=[{...top...}, {...}, {...bottom...}], wall_type=...)
#   Find custom profiles equivalent to a standard spec:
#     find_equivalent_profiles(standard="600S200-43", height_ft=10, spacing_in=16)
#
# SPEC: defaults to AISI2016USASD (enum 27) = AISI S100-16 (2020) w/S2-20, the
# edition referenced by IBC 2024 / IBC 2021 / BCNYS 2025. Every tool echoes the
# spec used and warns when it does not match `building_code`.
#
# WATCHDOG: the COM-heavy tools run CFS in an isolated child process with a
# per-call timeout (timeout_s, default 60 s / CFS_MCP_TIMEOUT_S). A hung engine
# is killed and restarted on the next call; partial results are returned.
# ============================================================================

from __future__ import annotations

import json
from typing import Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from . import catalog, jobs, stacked, substitution, watchdog
from .cfs_engine import (
    DEFAULT_BUILDING_CODE,
    DEFAULT_SPEC,
    SPEC_NAME_BY_ID,
    CFSError,
    cfs_session,
    parse_designation,
    resolve_spec,
    section_label,
    spec_code_check,
)
from .cfs_loads import (
    build_combo_table,
    combo_defs_for,
    combos_from_effects,
    deflection_cases,
    effective_length_factors,
    load_level_factors,
    require_wall_type,
    resolve_axial_force,
    resolve_combo_options,
    unbraced_lengths,
)

mcp = FastMCP("cfs-rsg-mcp")

# Back-compat aliases (these helpers now live in jobs.py so the watchdog's child
# process can import them without FastMCP).
_evaluate = jobs.evaluate_section
_ratio = jobs.ratio
_member_lengths = jobs.member_lengths
_force_unit_warnings = jobs.force_unit_warnings
_P_SANITY_K = jobs.P_SANITY_K
_M_SANITY_KIN = jobs.M_SANITY_KIN


def _ok(**kw) -> str:
    return json.dumps({"success": True, **kw}, indent=2, default=str)


def _err(msg, **kw) -> str:
    return json.dumps({"success": False, "error": msg, **kw}, indent=2, default=str)


def _spec_str(spec_id: int) -> str:
    return f"{SPEC_NAME_BY_ID.get(spec_id, 'unknown')} (enum {spec_id})"


def _job_error(jr: "watchdog.JobResult", **context) -> str:
    """Honest failure from a watchdog job, with whatever finished before it."""
    return _err(jr.error or "CFS job failed.", timed_out=jr.timed_out,
                partial_results=jr.partial, watchdog=jr.diagnostics(), **context)


def _doc(extra: str):
    """Append a shared documentation block to a tool docstring (before @mcp.tool)."""
    def deco(fn):
        fn.__doc__ = (fn.__doc__ or "") + extra
        return fn
    return deco


LOAD_CONVENTIONS_DOC = """
    LOAD INPUTS (unsigned magnitudes; the combos assign every sign)
      D_wall            wall self-weight, psf of wall face
      D_A / D_B         dead load, side A / side B, psf (side B ignored for exterior)
      L_A / L_B         floor live load, side A / side B, psf
      L_lat             interior lateral live load, IBC 1607.15, psf. Part of L ->
                        it is in the "D + L" combo and factored 0.75 with L.
      Lr / S / R        roof live / snow / rain, psf over trib_A + trib_B (exterior:
                        trib_A only) - the same tributary rule as D and L. They are
                        NEVER summed: each combo uses "Lr or 0.7S or R", whichever
                        governs, and says which one it used (roof_term).
      W_mwfw / W_mwfl   MWFRS windward / leeward pressure, psf. COMBINED with axial
                        load: Eqs. 5a, 6a, 7a.
      W_cc_w / W_cc_l   C&C pressure, psf. BENDING ALONE (P = 0) and DEFLECTION.
      W_uplift          MWFRS roof uplift, psf over the roof tributary; subtracts
                        axial load in 0.6D + 0.6W (Eq. 7a) only.
      Eh                seismic out-of-plane coefficient (strength level):
                        lateral = Eh x D_wall (Fp/Ww, ASCE 7-22 12.11.1).
      Ev                seismic vertical coefficient 0.2*SDS (strength level):
                        axial = Ev x (total dead axial load).
      line_loads        {D, L, Lr, S, R} plf at the top of the wall (e.g. parapet D).
      point_loads       {D, L, Lr, S, R} kips PER STUD at the top of the wall.

    LOAD LEVELS
      wind_level  'strength' (default) -> combos apply 0.6W.  'asd' -> W used at 1.0.
      snow_level  'strength' (default for 7-22) -> 0.7S in Eqs. 3a/4a/6a, 0.1S in
                  Eq. 9.  'asd' -> 0.7 already applied; Eq. 9 uses 0.1/0.7 = 0.143.
                  (ASCE 7-16 snow is nominal -> default 'asd' for 7-16.)
      Eh, Ev      always strength level; combos apply 0.7 / 0.525.
      Output `load_level_factors` + each combo's `factors` echo exactly what
      multiplied each input.

    COMBINATIONS
      combo_set   'ASCE7-22_ASD' (default; Eqs. 1a-7a of 2.4.1 + Eqs. 8-10 of 2.4.5),
                  'ASCE7-16_ASD', 'IBC2024_ALT_ASD' (IBC 1605.2 Eqs. 16-1..16-6).
      combo_mode  'builtin' (default) | 'user' | 'append' (user combos after built-in).
      user_combos [{"label": str, "factors": {"D": 1, "L": 1, "L_lat": 1, ...}}]
                  keys D, L, L_lat, Lr, S, R, W_mwfw, W_mwfl, W_cc_w, W_cc_l,
                  W_uplift, Eh, Ev; missing keys = 0; unknown keys are rejected.
                  Factors multiply the inputs EXACTLY - no hidden 0.6 or 0.7.
      asce_edition '7-22' (default) | '7-16'.
      axial_at    'base' (default, full wall self-weight) | 'mid_height' (half of
                  the wall's own weight subtracted, as Simpson does).

    SUPPORT / SERVICEABILITY
      support     'pinned-pinned' (default: M = wH^2/8, V = wH/2, K = 1) |
                  'cantilever' (fixed-free parapet: M = wH^2/2, V = wH, Kx = 2.1).
      defl_limit  L/x limit for lateral deflection (e.g. 240, 360, 600). Deflection
                  is always reported; pass/fail only when a limit is given.
      wind_defl_modifier  factor on the ASD C&C pressure for deflection (default
                  0.7 -> 0.7 x 0.6W = 0.42W, IBC Table 1604.3 note f).
      bearing_length_in   stud end bearing length for web crippling (default 1.0).

    SPEC / CODE
      spec        default AISI2016USASD (= AISI S100-16 (2020) w/S2-20).
      building_code  'IBC2024' (default) | 'BCNYS2025' | 'IBC2021' | 'IBC2018' | 'none'.
                  A spec or ASCE edition that does not match the code -> WARNING.
                  An LRFD spec with these ASD combos -> ERROR.
      timeout_s   watchdog timeout per call (default 60 s).
"""


# ---------------------------------------------------------------------------
# Shared preparation
# ---------------------------------------------------------------------------
def _prep_spec(spec, building_code, asce_edition, asd_combos):
    """-> (spec_id, check) or raises ValueError."""
    spec_id = resolve_spec(spec)
    chk = spec_code_check(spec_id, building_code, asce_edition, asd_combos)
    if chk["errors"]:
        raise ValueError(" ".join(chk["errors"]))
    return spec_id, chk


def _spec_fields(spec_id, chk) -> Dict:
    return {"spec": _spec_str(spec_id), "spec_used": chk["spec"],
            "building_code": chk["building_code"]}


def _member_ctx(spec_id, lengths, kf, combos, height_ft, spacing_in, support,
                defl, defl_limit, bearing_length_in, include_trace=False,
                grade=None, fy=None, emit_combo_partials=False) -> Dict:
    return {"spec_id": spec_id, "lengths": lengths, "k_factors": kf, "combos": combos,
            "height_ft": height_ft, "spacing_in": spacing_in, "support": support,
            "defl_cases": defl, "defl_limit": defl_limit,
            "bearing_length_in": bearing_length_in, "web_fastened": True,
            "include_trace": include_trace, "grade": grade, "fy": fy,
            "emit_combo_partials": emit_combo_partials}


def _support_notes(support, brace_config, kf) -> List[str]:
    if support != "cantilever":
        return []
    if kf["Ky"] == 1.0:
        why = (f"Ky = Kt = 1.0 assumes the bracing ({brace_config}) restrains weak-axis "
               f"and torsional buckling of each braced segment - confirm for the parapet")
    else:
        why = "no intermediate bracing: the full free height uses K = 2.1 on all axes"
    return [f"Cantilever (fixed-free) stud: Kx = {kf['Kx']}, Ky = Kt = {kf['Ky']} ({why}). "
            f"Moment and axial are taken at the fixed base."]


# ---------------------------------------------------------------------------
# 1. Connection / status
# ---------------------------------------------------------------------------
@mcp.tool()
def connect_cfs(timeout_s: Optional[float] = None) -> str:
    """Check the CFS15 Automation Module connection and license.

    Returns license status, the CFS install directory, the default design
    specification, and the available section libraries. Call this first if
    anything else errors, to isolate connection/license problems. Runs under the
    watchdog, so a hung engine is reported instead of hanging the server.
    """
    jr = watchdog.run(jobs.connect_job, {}, timeout_s, "connect_cfs")
    if not jr.ok:
        return _job_error(jr)
    return _ok(message="Connected to RSG CFS15. License is active.",
               install_dir=jr.result["install_dir"],
               default_spec=f"{_spec_str(DEFAULT_SPEC)} = AISI S100-16 (2020) w/S2-20",
               default_building_code=DEFAULT_BUILDING_CODE,
               libraries=jr.result["libraries"], watchdog=jr.diagnostics())


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
                           grade: Optional[int] = None,
                           building_code: Optional[str] = DEFAULT_BUILDING_CODE) -> str:
    """Get gross section properties and fully-braced strengths for one section.

    section: SSMA designation ('600S200-43'), .cfss file path, or library path.
    spec:    AISI spec (name or int enum). Default AISI2016USASD
             (= AISI S100-16 (2020) w/S2-20, referenced by IBC 2024).
    fy:      override yield strength (ksi) after loading (forces carbon steel).
    grade:   for standard designations, force 33 or 50 ksi library folder.
    building_code: warns if the spec does not match ('none' to skip).
    """
    try:
        spec_id, chk = _prep_spec(spec, building_code, None, False)
    except ValueError as e:
        return _err(str(e))
    try:
        with cfs_session() as s:
            path = s.load(section, grade=grade, fy=fy)
            sd = s.section_data()
            gp = s.gross_properties()
            st = s.fully_braced_strength(spec_id)
            return _ok(
                section=section_label(path, sd.get("description")),
                load_path=path,
                **_spec_fields(spec_id, chk),
                section_data=sd,
                gross_properties=gp,
                fully_braced_strength=st,
                warnings=chk["warnings"],
            )
    except CFSError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"Failed to get properties for '{section}': {e}")


# ---------------------------------------------------------------------------
# 6. Check a single stud
# ---------------------------------------------------------------------------
@mcp.tool()
@_doc(LOAD_CONVENTIONS_DOC)
def check_stud(section: str, height_ft: float, spacing_in: float = 16.0,
               brace_config: str = "none", brace_n: Optional[int] = None,
               spec=None, grade: Optional[int] = None, fy: Optional[float] = None,
               # explicit forces (override load-combo generation if any is set):
               P_k: Optional[float] = None, Mx_kin: Optional[float] = None,
               Vy_k: Optional[float] = None, force_type: str = "auto",
               # OR provide PSF loads to auto-build ASD combos:
               loads: Optional[dict] = None,
               trib_A_ft: float = 0.0, trib_B_ft: float = 0.0,
               wall_type: Optional[str] = None, include_trace: bool = False,
               asce_edition: str = "7-22", snow_level: Optional[str] = None,
               wind_level: str = "strength", combo_set: Optional[str] = None,
               combo_mode: str = "builtin", user_combos: Optional[List[dict]] = None,
               axial_at: str = "base", support: str = "pinned-pinned",
               line_loads: Optional[dict] = None, point_loads: Optional[dict] = None,
               defl_limit: Optional[float] = None, wind_defl_modifier: float = 0.7,
               bearing_length_in: float = 1.0,
               building_code: Optional[str] = DEFAULT_BUILDING_CODE,
               timeout_s: Optional[float] = None) -> str:
    """Run an AISI member check on ONE stud section.

    Two modes:
      * Explicit forces - pass any of P_k / Mx_kin / Vy_k (kips, kip-in) for a
                          single check. No combos, no load-level factors.
      * Load combos     - pass a `loads` PSF dict (keys D_wall, D_A, D_B, L_A, L_B,
                          L_lat, Lr, S, R, W_mwfw, W_mwfl, W_cc_w, W_cc_l, W_uplift,
                          Eh, Ev) plus tributary widths and wall_type (REQUIRED in
                          this mode). Unknown keys are rejected, never ignored.

    AXIAL SIGN CONVENTION (explicit-force mode): CFS15 is compression-POSITIVE.
      force_type='compression' -> the P_k MAGNITUDE is compression.
      force_type='tension'     -> the P_k magnitude is tension.
      force_type='auto'        -> sign as given; P_k<0 returns a WARNING.

    brace_config: none|midpoint|third|quarter|48in|60in|72in|custom (custom needs
    brace_n = number of equal unbraced segments; bracing only, never capacity).

    OUTPUT: every combo with P, M, V and UC (not only the governing one); the
    governing combo; section properties (A, Ix, Sx, Ae); strengths by limit state
    (Pa, Pa-Dist, Ma-Fy, Ma-Brc, Ma-Dist, Va) and the governing axial buckling
    mode, read from CFS's own trace; web crippling at the stud end (stiffener
    required?); lateral deflection with L/x; the spec used; load-level factors.
    """
    try:
        spec_id, chk = _prep_spec(spec, building_code, asce_edition, P_k is None
                                  and Mx_kin is None and Vy_k is None)
        lengths = unbraced_lengths(height_ft, brace_config, brace_n)
        kf = effective_length_factors(support, brace_config)
    except ValueError as e:
        return _err(str(e))

    warnings: List[str] = list(chk["warnings"])
    explicit = any(v is not None for v in (P_k, Mx_kin, Vy_k))
    table = None
    defl = []
    if explicit:
        try:
            P_signed, w = resolve_axial_force(P_k, force_type)
        except ValueError as e:
            return _err(str(e))
        warnings += w
        if loads:
            warnings.append("Explicit forces were given, so `loads` was NOT used.")
        combos = [{"combo": "explicit", "label": "explicit forces",
                   "P_k": P_signed or 0.0, "Mx_kin": Mx_kin or 0.0, "Vy_k": Vy_k or 0.0}]
    else:
        geom = {"height_ft": height_ft, "spacing_in": spacing_in, "trib_A_ft": trib_A_ft,
                "trib_B_ft": trib_B_ft, "wall_type": wall_type, "support": support}
        try:
            table = build_combo_table(
                loads or {}, geom, asce_edition=asce_edition, snow_level=snow_level,
                wind_level=wind_level, combo_set=combo_set, combo_mode=combo_mode,
                user_combos=user_combos, axial_at=axial_at, line_loads=line_loads,
                point_loads=point_loads)
        except ValueError as e:
            return _err(str(e))
        combos = table["combos"]
        warnings += table["warnings"]
        defl = deflection_cases(table["unfactored_effects"]["lateral_psf"],
                                table["options"]["wind_level"], wind_defl_modifier)
    warnings += _support_notes(support, brace_config, kf)

    ctx = _member_ctx(spec_id, lengths, kf, combos, height_ft, spacing_in, support,
                      defl, defl_limit, bearing_length_in, include_trace, grade, fy,
                      emit_combo_partials=True)
    jr = watchdog.run(jobs.check_sections_job, {"sections": [section], "ctx": ctx},
                      timeout_s, "check_stud")
    common = dict(**_spec_fields(spec_id, chk), height_ft=height_ft, spacing_in=spacing_in,
                  support=support, brace_config=brace_config, unbraced_lengths_in=lengths,
                  effective_length_factors=kf)
    if not jr.ok:
        return _job_error(jr, **common, warnings=warnings)
    rec = jr.result["records"][0]
    if rec.get("error"):
        return _err(rec["error"], **common)
    gov = next((r for r in rec["combos"] if r["combo"] == rec["governing_combo"]), None)
    return _ok(
        section=rec["section"], load_path=rec["load_path"], **common,
        force_type=force_type if explicit else None,
        passed=rec["passed"], passed_strength=rec["passed_strength"],
        governing_UC=rec["governing_UC"], governing_combo=gov,
        combos=rec["combos"],
        properties=rec["properties"], strengths=rec["strengths"],
        web_crippling=rec["web_crippling"], deflection=rec["deflection"],
        flags=rec["flags"], flag_notes=rec["flag_notes"],
        load_level_factors=table["load_level_factors"] if table else
        {"note": "explicit forces - applied exactly as given, no load factors"},
        unfactored_effects=table["unfactored_effects"] if table else None,
        warnings=warnings + rec["warnings"], watchdog=jr.diagnostics(),
    )


# ---------------------------------------------------------------------------
# 7. Design a wall - pick the lightest passing section
# ---------------------------------------------------------------------------
@mcp.tool()
@_doc(LOAD_CONVENTIONS_DOC)
def design_wall(height_ft: float, spacing_in: float, wall_type: str,
                brace_config: str = "third", brace_n: Optional[int] = None,
                trib_A_ft: float = 0.0, trib_B_ft: float = 0.0,
                # candidate sections: explicit list, custom folder, or SSMA depth
                sections: Optional[List[str]] = None,
                custom_directory: Optional[str] = None,
                ssma_depth_in: Optional[float] = None,
                structural_only: bool = True,
                spec=None,
                # loads (PSF) - anything omitted defaults to 0 (D_wall=12):
                D_wall: float = 12.0, D_A: float = 0.0, L_A: float = 0.0,
                D_B: float = 0.0, L_B: float = 0.0, S: float = 0.0,
                W_uplift: float = 0.0, W_mwfw: float = 0.0, W_mwfl: float = 0.0,
                W_cc_w: float = 0.0, W_cc_l: float = 0.0, L_lat: float = 0.0,
                Eh: float = 0.0, Ev: float = 0.0,
                Lr: float = 0.0, R: float = 0.0,
                asce_edition: str = "7-22", snow_level: Optional[str] = None,
                wind_level: str = "strength", combo_set: Optional[str] = None,
                combo_mode: str = "builtin", user_combos: Optional[List[dict]] = None,
                axial_at: str = "base", support: str = "pinned-pinned",
                line_loads: Optional[dict] = None, point_loads: Optional[dict] = None,
                defl_limit: Optional[float] = None, wind_defl_modifier: float = 0.7,
                bearing_length_in: float = 1.0,
                building_code: Optional[str] = DEFAULT_BUILDING_CODE,
                timeout_s: Optional[float] = None) -> str:
    """Design a wall stud: try candidate sections in order, return the first
    (lightest, for SSMA auto-lists) that passes strength AND deflection (when
    defl_limit is set).

    Candidate sections (choose one source):
      * sections          - explicit list of designations / .cfss paths (in order)
      * custom_directory   - a folder of custom .cfss files
      * ssma_depth_in      - auto-build SSMA structural studs of this depth,
                             ordered thinnest->thickest (e.g. 6.0)
    If none is given, defaults to SSMA 6" structural studs.

    wall_type ('interior' | 'exterior') is REQUIRED - never silently defaulted.
    structural_only (default True): skip SSMA non-structural (S125) studs.

    OUTPUT: `selected` carries the full check of the chosen section (every combo
    with P, M, V, UC; properties; limit-state strengths; web crippling;
    deflection); `sections_tried` summarizes every section evaluated;
    `load_combinations` is the full combo table with its factors. If the watchdog
    times out, the sections finished so far are returned as partial_results.
    """
    try:
        require_wall_type(wall_type)
        spec_id, chk = _prep_spec(spec, building_code, asce_edition, True)
        lengths = unbraced_lengths(height_ft, brace_config, brace_n)
        kf = effective_length_factors(support, brace_config)
    except ValueError as e:
        return _err(str(e))

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

    geom = {"height_ft": height_ft, "spacing_in": spacing_in, "trib_A_ft": trib_A_ft,
            "trib_B_ft": trib_B_ft, "wall_type": wall_type, "support": support}
    loads = {"D_wall": D_wall, "D_A": D_A, "L_A": L_A, "D_B": D_B, "L_B": L_B,
             "Lr": Lr, "S": S, "R": R, "W_uplift": W_uplift, "W_mwfw": W_mwfw,
             "W_mwfl": W_mwfl, "W_cc_w": W_cc_w, "W_cc_l": W_cc_l, "L_lat": L_lat,
             "Eh": Eh, "Ev": Ev}
    try:
        table = build_combo_table(
            loads, geom, asce_edition=asce_edition, snow_level=snow_level,
            wind_level=wind_level, combo_set=combo_set, combo_mode=combo_mode,
            user_combos=user_combos, axial_at=axial_at, line_loads=line_loads,
            point_loads=point_loads)
    except ValueError as e:
        return _err(str(e))
    defl = deflection_cases(table["unfactored_effects"]["lateral_psf"],
                            table["options"]["wind_level"], wind_defl_modifier)
    warnings = list(chk["warnings"]) + table["warnings"] + \
        _support_notes(support, brace_config, kf)

    ctx = _member_ctx(spec_id, lengths, kf, table["combos"], height_ft, spacing_in,
                      support, defl, defl_limit, bearing_length_in)
    jr = watchdog.run(jobs.check_sections_job,
                      {"sections": candidates, "ctx": ctx, "stop_at_first_pass": True},
                      timeout_s, "design_wall")
    common = dict(**_spec_fields(spec_id, chk), height_ft=height_ft,
                  spacing_in=spacing_in, wall_type=wall_type, support=support,
                  brace_config=brace_config, unbraced_lengths_in=lengths,
                  effective_length_factors=kf,
                  load_level_factors=table["load_level_factors"],
                  load_combinations=table["combos"],
                  unfactored_effects=table["unfactored_effects"])
    if not jr.ok:
        return _job_error(jr, **common, warnings=warnings)

    records = jr.result["records"]
    selected = next((r for r in records if r.get("passed")), None)
    tried = [jobs._summary(r) for r in records]
    return _ok(
        **common,
        selected=selected,
        passed=selected is not None,
        message=None if selected else "No candidate section passes; increase size or reduce loads.",
        sections_tried=tried,
        warnings=warnings + (selected["warnings"] if selected else []),
        watchdog=jr.diagnostics(),
    )


# ---------------------------------------------------------------------------
# 7b. Stacked multi-level wall
# ---------------------------------------------------------------------------
@mcp.tool()
@_doc(LOAD_CONVENTIONS_DOC)
def design_stacked_wall(levels: List[dict], wall_type: str,
                        level_defaults: Optional[dict] = None,
                        axial_at: str = "base",
                        live_load_reduction: bool = True, KLL: float = 1.0,
                        live_load_patterns: Optional[List[str]] = None,
                        check_alignment: bool = True,
                        spec=None,
                        asce_edition: str = "7-22", snow_level: Optional[str] = None,
                        wind_level: str = "strength", combo_set: Optional[str] = None,
                        combo_mode: str = "builtin",
                        user_combos: Optional[List[dict]] = None,
                        defl_limit: Optional[float] = None,
                        wind_defl_modifier: float = 0.7,
                        bearing_length_in: float = 1.0,
                        include_all_combos: bool = True,
                        building_code: Optional[str] = DEFAULT_BUILDING_CODE,
                        timeout_s: Optional[float] = None) -> str:
    """Design / check a STACKED multi-storey stud wall, level by level.

    levels: list of level objects, TOP DOWN (first = the level under the roof).
      Each level:
        height_ft, spacing_in (required); trib_A_ft, trib_B_ft
        D / L (both sides) or D_A, D_B, L_A, L_B - the roof or floor load at the
          TOP of this level, psf
        Lr, S, R, W_uplift - roof loads at the top of this level (usually only the
          top level), psf over trib_A + trib_B
        D_wall, L_lat, W_mwfw, W_mwfl, W_cc_w, W_cc_l, Eh, Ev - this level's wall
        brace_config / brace_n (default '48in')
        section (check one) OR sections (candidates in order; first that passes)
        grade, fy; L_reducible (default True; False for assembly/garage floors);
        load_transfer_present (a transfer track/header between this level and the
          next); line_loads (plf, carried down); point_loads (k/stud, this level)
      level_defaults: keys merged into every level (e.g. D_wall, L_lat, Ev, brace).

    ACCUMULATION: D, L, Lr, S, R and uplift are carried down through the stack,
    plus the self-weight of every wall above. Loads arrive as plf along the track
    and are picked up per stud at each level's own spacing. axial_at='mid_height'
    subtracts half of the level's own wall weight (Simpson's convention).

    LIVE LOAD REDUCTION (ASCE 7-22 §4.7): per stud, AT = spacing x (trib_A +
    trib_B) summed over the floors supported; L = Lo(0.25 + 15/sqrt(KLL*AT)) for
    KLL*AT >= 400 ft^2 (min 0.50Lo one floor / 0.40Lo two+ floors); Lo > 100 psf
    is not reduced except max 20% for members supporting two+ floors (§4.7.3).
    KLL defaults to 1.0 (conservative). AT, KLL*AT and the factor per supported
    floor are reported for every level. Roof live load is not reduced.

    LIVE LOAD PATTERNS: 'AB' both sides, 'A' side A only, 'B' side B only
    (Simpson Case AB / A / B). Default: all three for interior walls with unequal
    sides, else AB. Without load eccentricity AB governs axial; A/B are reported.

    ALIGNMENT: consecutive levels whose stud spacings do not stack are flagged
    (unless load_transfer_present).

    OUTPUT per level (Simpson Stacked Wall Summary columns): section, governing
    combo, P, M, V, UC, Pa, Pa-Dist, Ma-Fy, Ma-Dist, Ma-Brc, Va, web crippling UC,
    stiffener required, deflection and L/x; plus the full combo table, LLR and
    accumulated loads. Levels are pinned-pinned (check a parapet separately with
    check_stud(support='cantilever')).
    """
    try:
        wt = require_wall_type(wall_type)
        spec_id, chk = _prep_spec(spec, building_code, asce_edition, True)
        opts = resolve_combo_options(asce_edition, snow_level, wind_level, combo_set,
                                     combo_mode, user_combos)
        if not levels:
            raise ValueError("levels must be a non-empty list (top level first).")
        norm = [stacked.normalize_level(lv, i, level_defaults) for i, lv in enumerate(levels)]
        if live_load_patterns:
            pats = list(live_load_patterns)
        else:
            unequal = wt == "interior" and any(
                lv["loads"]["L_A"] != lv["loads"]["L_B"] or lv["trib_A_ft"] != lv["trib_B_ft"]
                for lv in norm)
            pats = ["AB", "A", "B"] if unequal else ["AB"]
        if "AB" not in pats:
            pats = ["AB"] + pats
        acc = stacked.accumulate(norm, wt, axial_at, live_load_reduction, KLL, pats)
    except ValueError as e:
        return _err(str(e))

    defs = combo_defs_for(opts)
    level_ctxs, level_meta = [], []
    for lv in acc:
        try:
            lengths = unbraced_lengths(lv["height_ft"], lv["brace_config"], lv["brace_n"])
        except ValueError as e:
            return _err(f"{lv['name']}: {e}")
        cands = [lv["section"]] if lv.get("section") else list(lv.get("sections") or [])
        if not cands:
            return _err(f"{lv['name']}: give 'section' or 'sections'.")
        geom = {"height_ft": lv["height_ft"], "spacing_in": lv["spacing_in"],
                "support": "pinned-pinned"}
        combos = []
        for pat in pats:
            rows = combos_from_effects(defs, lv["effects_by_pattern"][pat], geom)
            for r in rows:
                if pat != "AB":
                    if not r["factors"].get("L"):
                        continue          # A/B patterns only differ where L acts
                    r["combo"] = f"{r['combo']} [{pat}]"
                    r["label"] = f"{r['label']} | live pattern {pat}"
                r["live_pattern"] = pat
                combos.append(r)
        defl = deflection_cases(lv["effects_by_pattern"]["AB"]["lat_psf"],
                                opts["wind_level"], wind_defl_modifier)
        ctx = _member_ctx(spec_id, lengths, {"Kx": 1.0, "Ky": 1.0, "Kt": 1.0}, combos,
                          lv["height_ft"], lv["spacing_in"], "pinned-pinned", defl,
                          defl_limit, bearing_length_in, grade=lv.get("grade"),
                          fy=lv.get("fy"))
        level_ctxs.append({"name": lv["name"], "candidates": cands, "ctx": ctx})
        level_meta.append({"lv": lv, "lengths": lengths, "combos": combos})

    align = stacked.alignment_flags(norm) if check_alignment else []
    warnings = list(chk["warnings"]) + list(opts["warnings"]) + [a["flag"] for a in align]
    common = dict(**_spec_fields(spec_id, chk), wall_type=wt, axial_at=axial_at,
                  live_load_patterns=pats,
                  load_level_factors=load_level_factors(opts),
                  alignment_flags=align)

    jr = watchdog.run(jobs.stacked_job, {"level_ctxs": level_ctxs}, timeout_s,
                      "design_stacked_wall")
    if not jr.ok:
        return _job_error(jr, **common, warnings=warnings)

    summary, details = [], []
    for meta, res in zip(level_meta, jr.result["levels"]):
        lv, sel = meta["lv"], res["selected"]
        if not sel or sel.get("error"):
            summary.append({"level": lv["name"], "section": None, "passed": False,
                            "error": (sel or {}).get("error", "no section evaluated")})
            details.append({"level": lv["name"], "sections_tried": res["sections_tried"]})
            continue
        st = sel["strengths"]
        gf = sel["governing_forces"] or {}
        web = sel["web_crippling"]
        dctrl = (sel["deflection"] or {}).get("controlling") or {}
        summary.append({
            "level": lv["name"], "section": sel["section"],
            "height_ft": lv["height_ft"], "spacing_in": lv["spacing_in"],
            "governing_combo": sel["governing_label"],
            "P_k": gf.get("P_k"), "M_kin": gf.get("Mx_kin"), "V_k": gf.get("Vy_k"),
            "UC": sel["governing_UC"],
            "Pa_k": st["Pa_k"], "Pa_dist_k": st["Pa_dist_k"],
            "Ma_Fy_kin": st["Ma_Fy_kin"], "Ma_dist_kin": st["Ma_dist_kin"],
            "Ma_brc_kin": st["Ma_brc_kin"], "Va_k": st["Va_k"],
            "axial_buckling_mode": st["axial_buckling_mode"],
            "web_crippling_UC": web.get("UC"),
            "stiffener_required": web.get("stiffener_required"),
            "deflection_in": dctrl.get("deflection_in"), "L_over": dctrl.get("L_over"),
            "deflection_case": dctrl.get("case"),
            "passed": sel["passed"],
        })
        det = {"level": lv["name"], "unbraced_lengths_in": meta["lengths"],
               "accumulated_plf": lv["accumulated_plf"], "live_load_reduction": lv["llr"],
               "sections_tried": res["sections_tried"],
               "selected": sel if include_all_combos else
               {k: v for k, v in sel.items() if k != "combos"}}
        details.append(det)

    all_pass = all(r.get("passed") for r in summary)
    return _ok(
        **common,
        all_levels_pass=all_pass,
        summary=summary,
        levels=details,
        warnings=warnings,
        watchdog=jr.diagnostics(),
    )


# ---------------------------------------------------------------------------
# 8. Find custom profiles equivalent to a standard spec
# ---------------------------------------------------------------------------
@mcp.tool()
def find_equivalent_profiles(standard: str, height_ft: float,
                             spacing_in: float = 16.0,
                             custom_profiles: Optional[List[str]] = None,
                             custom_directory: Optional[str] = None,
                             recursive: bool = False,
                             brace_config: str = "none",
                             brace_n: Optional[int] = None,
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
    spec:             default AISI2016USASD.

    Returns the reference strengths, and every custom profile ranked with its
    axial / flexural / stiffness ratios, an 'equivalent' flag, its weight, and
    'max_spacing_in' (the spacing at which the custom matches the standard's
    per-foot capacity when used at the reference spacing).
    """
    spec_id = resolve_spec(spec)
    try:
        lengths = unbraced_lengths(height_ft, brace_config, brace_n)
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
                spec=_spec_str(spec_id),
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


# ---------------------------------------------------------------------------
# 9. HEADLINE: substitute a standard wall spec with MSBS (or standard) sections
#    Depth-matched, exhaustive spacing/category/gauge matrix, lightest-per-ft
#    selection, cross-floor alignment check, gauge ceiling + standard fallback.
# ---------------------------------------------------------------------------
@mcp.tool()
def find_wall_substitutions(standard: str, height_ft: float, wall_type: str,
                            spacing_in: float = 16.0,
                            brace_config: str = "48in",
                            brace_n: Optional[int] = None,
                            reference_n: int = 1,
                            msbs_directory: Optional[str] = None,
                            categories: Optional[List[str]] = None,
                            include_boxed: bool = False,
                            allowed_spacings: Optional[List[float]] = None,
                            depth_filter_in: Optional[List[float]] = None,
                            adjacent_spacing_in: Optional[float] = None,
                            load_transfer_present: bool = False,
                            governing: str = "both",
                            include_all_configs: bool = False,
                            include_trace: bool = False,
                            spec=None, tolerance: float = 0.0,
                            standard_grade: Optional[int] = None,
                            standard_fy: Optional[float] = None,
                            building_code: Optional[str] = DEFAULT_BUILDING_CODE,
                            timeout_s: Optional[float] = None) -> str:
    """Substitute a standard wall spec with the most efficient MSBS section.

    Evaluates a FULL matrix (every candidate x every spacing x every gauge at the
    matched depth), then selects the lightest-per-ft passing wall, preferring a
    single section over a double at comparable weight. Nothing is auto-accepted:
    a candidate that passes axial (P) but fails moment (Mx) is returned flagged
    for the engineer to judge - for interior AND exterior walls alike.

    REQUIRED
      standard    the section being replaced (e.g. '600S200-43').
      height_ft   wall height.
      wall_type   'interior' | 'exterior'. Required - never silently defaulted.

    REFERENCE MULTIPLIER
      reference_n number of PLIES in the reference wall being replaced (default 1).
                  reference_n=2 models a back-to-back DOUBLE standard wall: the
                  reference P, Mx, Ix and weight are all multiplied by N (an
                  idealized stacked-ply approximation that ignores built-up
                  slenderness reduction - noted in flags). This multiplies ONLY the
                  reference side of the comparison. brace_n (bracing segments) does
                  NOT affect capacity and is a separate parameter.

    DEPTH MATCHING
      Candidates are matched to the nearest AVAILABLE MSBS depth to the reference's
      actual depth (computed from the library, no hardcoded table). depth_filter_in
      overrides with explicit depths.

    SPACING SWEEP (exhaustive - never stops at the first pass)
      Default spacings = [original spacing_in, 12, 16, 24]. Override with
      allowed_spacings (used exactly as given). Member strength is spacing-
      independent, so each section is evaluated once and spacings swept
      analytically; every spacing is scored against every category and gauge.

    CATEGORIES
      Default single + double; single is always tried first. Double defaults to
      back-to-back (B2B) ONLY. Set include_boxed=True to also consider BOX builds.

    CROSS-FLOOR ALIGNMENT
      Pass adjacent_spacing_in (the stud spacing of the level above/below) and
      load_transfer_present=False when that level has no top-track/transfer member.
      If the selected spacing does not stack with the adjacent studs, an alignment
      flag is returned (point loads may not land on a stud).

    GAUGE CEILING + FALLBACK
      If the reference gauge exceeds the heaviest MSBS gauge at the matched depth,
      an explicit ceiling flag is returned. If NO MSBS single/double passes at any
      spacing, the search extends to standard (non-MSBS) sections at the matched
      depth, labeled category='standard', so you always get an answer.

    SPEC / WATCHDOG
      spec defaults to AISI2016USASD (S100-16 w/S2-20); a spec that does not match
      building_code returns a warning. Runs under the watchdog (timeout_s, default
      60 s): on a hang, the profiles evaluated so far are returned as partials.

    OUTPUT
      * substitution_table (ALWAYS): one plain-language row - original -> substitute,
        spacing, category, P ratio, Mx ratio, and a status column readable by a GC/PM.
      * selected: the chosen substitution with actual capacities and required values.
      * profiles: per-candidate detail incl. by_spacing ratios (always).
      * audit_table (include_all_configs=True): EVERY tried (section,spacing,gauge)
        row - pass and fail - with a single most_efficient=true row.
      * include_trace=True attaches CFS's calculation trace per evaluated section.
    """
    if wall_type not in ("interior", "exterior"):
        return _err("wall_type is required and must be 'interior' or 'exterior'.")
    governing = (governing or "both").lower()
    if governing not in ("both", "axial", "flexural"):
        return _err("governing must be 'both', 'axial', or 'flexural'.")
    try:
        spec_id, chk = _prep_spec(spec, building_code, None, False)
    except ValueError as e:
        return _err(str(e))
    reference_n = max(1, int(reference_n or 1))
    tol_factor = 1.0 - tolerance

    try:
        lengths = unbraced_lengths(height_ft, brace_config, brace_n)
    except ValueError as e:
        return _err(str(e))

    ref_parsed = parse_designation(standard)
    ref_depth = ref_parsed["depth_in"] if ref_parsed else substitution.depth_from_name(standard)
    ref_mils = ref_parsed["mils"] if ref_parsed else substitution.mils_from_name(standard)

    if allowed_spacings:
        spacings, seen = [], set()
        for sp in allowed_spacings:
            sp = round(float(sp), 4)
            if sp > 0 and sp not in seen:
                seen.add(sp)
                spacings.append(sp)
    else:
        spacings = substitution.default_spacings(spacing_in)

    try:
        msbs = catalog.list_msbs_sections(msbs_directory, categories)
        if not include_boxed:
            msbs = [m for m in msbs if m["folder"] != "BOX"]
        if not msbs:
            return _err("No MSBS sections found for the given filters. Check "
                        "msbs_directory / categories, or the bundled "
                        "Info-and-Examples/Sections folders.")
    except Exception as e:  # noqa: BLE001
        return _err(f"Could not list MSBS sections: {e}")

    all_depths = [substitution.depth_from_name(m["name"]) for m in msbs]
    if depth_filter_in:
        target_depths = {round(float(d), 4) for d in depth_filter_in}
        matched_depth = None
    else:
        matched_depth = substitution.nearest_depth(ref_depth, all_depths)
        target_depths = {matched_depth} if matched_depth is not None else set()
    msbs = [m for m in msbs
            if round(substitution.depth_from_name(m["name"]), 4) in target_depths]
    if not msbs:
        return _err(f"No MSBS candidates at the matched depth "
                    f"{sorted(target_depths)} (reference depth {ref_depth}\").")

    candidate_mils = [substitution.mils_from_name(m["name"]) for m in msbs]
    ceiling_depth = matched_depth if matched_depth is not None else ref_depth
    gauge_flag = substitution.gauge_ceiling_flag(ref_mils, candidate_mils, ceiling_depth)

    top_flags: List[str] = list(chk["warnings"])
    if gauge_flag:
        top_flags.append(gauge_flag)
    if reference_n > 1:
        top_flags.append(
            f"reference_n={reference_n}: the reference capacity (P, Mx, Ix) and "
            f"weight are the single-ply values multiplied by {reference_n}. This is "
            f"an idealized stacked-ply model; a real built-up section has reduced "
            f"slenderness capacity - verify the built-up reference explicitly.")

    params = {
        "standard": standard, "standard_grade": standard_grade,
        "standard_fy": standard_fy, "spec_id": spec_id, "lengths": lengths,
        "spacing_in": spacing_in, "spacings": spacings, "reference_n": reference_n,
        "governing": governing, "tol_factor": tol_factor,
        "include_trace": include_trace, "include_all_configs": include_all_configs,
        "msbs": msbs, "target_depths": sorted(target_depths), "ref_depth": ref_depth,
        "top_flags": top_flags, "adjacent_spacing_in": adjacent_spacing_in,
        "load_transfer_present": load_transfer_present,
    }
    common = dict(**_spec_fields(spec_id, chk), wall_type=wall_type, governing=governing,
                  brace_config=brace_config, brace_n=brace_n,
                  unbraced_lengths_in=lengths, reference_n=reference_n,
                  matched_depth_in=matched_depth, allowed_spacings_in=spacings)
    jr = watchdog.run(jobs.substitution_job, {"p": params}, timeout_s,
                      "find_wall_substitutions")
    if not jr.ok:
        return _job_error(jr, **common, flags=top_flags)
    return _ok(**common, **jr.result, watchdog=jr.diagnostics())


# ---------------------------------------------------------------------------
# 10. Batch code-check members (ETABS / SAP2000 truss or frame integration)
# ---------------------------------------------------------------------------
@mcp.tool()
def check_members_batch(members: List[dict], spec=None,
                        brace_config: str = "none",
                        building_code: Optional[str] = DEFAULT_BUILDING_CODE,
                        timeout_s: Optional[float] = None) -> str:
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
        lengths  - ONE of: Lx_in/Ly_in/Lt_in[/Lm_in], or Lu_in, or length_ft(+brace_config).
                   Lm_in (distortional) is optional: if omitted, Lm = Lt and a
                   warning says so.
                   These may be TOP-LEVEL keys OR inside a nested "lengths" object;
                   both work.
        grade,fy - optional overrides

    SIGN CONVENTION (important): CFS uses structural convention — compression P is
    POSITIVE, tension NEGATIVE. ETABS reports axial compression as NEGATIVE, so
    flip the sign of ETABS axial forces before passing them in. Mx is k-in with
    compression-on-top positive.

    To avoid the silent tension-vs-compression trap, each member may carry a
    "force_type": "compression" | "tension" | "auto" (default "auto"). With
    "compression"/"tension" the P_k MAGNITUDE is signed for you; with "auto" the
    sign is used as given and a negative P_k returns a warning that it was read as
    tension. This lets you pass compression as a positive magnitude without
    remembering CFS's rule.

    UNITS: kips and kip-inches. If your model is in lb / lb-in, convert first
    (divide by 1000). The tool emits a warning if a force looks like wrong units.
    Forces are applied exactly as given - they must already be ASD-level for an
    ASD spec (default AISI2016USASD).

    Zero-shear-capacity sections (e.g. a BendRadius geometry flag) are handled
    honestly: if there is no applied shear the shear-interaction check is excluded
    (with a warning) instead of reporting a bogus UC=9.999; if shear IS applied it
    is reported as a real failure. Read each member's `flags`/`flag_notes`.

    Returns per-member unity check (UC, governing_check, PASS/FAIL, member
    strengths, NoteFlags with plain-English notes, warnings) and an overall
    summary. Sections are loaded once and reused across members that share them.
    Runs under the watchdog (timeout_s, default 60 s): on a hang, the members
    checked so far are returned as partial_results.
    """
    try:
        spec_id, chk = _prep_spec(spec, building_code, None, False)
    except ValueError as e:
        return _err(str(e))
    if not members:
        return _err("No members provided.")
    try:
        members = [dict(m) for m in members]
    except Exception as e:  # noqa: BLE001
        return _err(f"Bad members input: {e}")

    jr = watchdog.run(jobs.batch_job, {"members": members, "spec_id": spec_id,
                                       "brace_config": brace_config},
                      timeout_s, "check_members_batch")
    if not jr.ok:
        return _job_error(jr, **_spec_fields(spec_id, chk), total=len(members),
                          warnings=chk["warnings"])
    results = jr.result["members"]
    checked = [r for r in results if r.get("status") in ("PASS", "FAIL")]
    failed = [r for r in checked if r["status"] == "FAIL"]
    errored = [r for r in results if r.get("status") == "ERROR"]
    worst = max((r["UC"] for r in checked), default=None)
    return _ok(
        **_spec_fields(spec_id, chk),
        total=len(members), passed=len(checked) - len(failed),
        failed=len(failed), errored=len(errored),
        all_passed=len(failed) == 0 and len(errored) == 0,
        governing_UC=round(worst, 4) if worst is not None else None,
        members=results, warnings=chk["warnings"], watchdog=jr.diagnostics(),
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
