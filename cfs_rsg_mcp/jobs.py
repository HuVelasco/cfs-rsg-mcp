# FILE: jobs.py
# PURPOSE: CFS job bodies - the COM-side work of the design tools, written as
#          module-level functions so the watchdog can run them in an isolated,
#          killable child process (see watchdog.py).
# STATUS: Version 0.1.0.3 - Developed (production-issue fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)
#
# Every job takes plain data, opens its own cfs_session(), reports what it is
# evaluating (watchdog.report) and hands back each finished unit of work
# (watchdog.partial) so a timeout still returns everything completed so far.
# Jobs return plain dicts and raise CFSError for honest failures.

from __future__ import annotations

import glob
import os
from typing import Dict, List, Optional

from . import catalog, substitution
from .cfs_engine import (
    CFSError,
    cfs_install_dir,
    cfs_session,
    describe_flags,
    section_label,
)
from .cfs_loads import (
    lateral_deflection_in,
    lateral_effects,
    resolve_axial_force,
    unbraced_lengths,
)
from .watchdog import partial, report


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def connect_job() -> Dict:
    with cfs_session():
        install = cfs_install_dir()
        libs = [os.path.basename(p) for p in glob.glob(os.path.join(install, "*.cfsl"))] \
            if install else []
        return {"install_dir": install, "libraries": libs}


# ---------------------------------------------------------------------------
# Full check of ONE section against a combo table (strength + web + deflection)
# ---------------------------------------------------------------------------
def check_one_section(s, ident: str, ctx: Dict) -> Dict:
    """Load `ident` and run every combo, web crippling and deflection.

    ctx keys: spec_id, lengths, k_factors, combos, height_ft, spacing_in,
    support, defl_cases, defl_limit, bearing_length_in, web_fastened,
    include_trace, grade, fy, emit_combo_partials.
    """
    spec = ctx["spec_id"]
    lengths = ctx["lengths"]
    kf = ctx.get("k_factors") or {}
    report(section=ident, step="loading section")
    path = s.load(ident, grade=ctx.get("grade"), fy=ctx.get("fy"))
    sd = s.section_data()
    gp = s.gross_properties()
    label = section_label(path, sd.get("description"))
    report(section=label, step="member strengths (limit-state breakdown)")
    sdet = s.strength_detail(spec, lengths, **kf)

    rows, gov, warnings = [], None, []
    for c in ctx["combos"]:
        report(section=label, combo=c["combo"], step="member check")
        chk = s.member_check(spec, lengths, c, include_trace=ctx.get("include_trace", False),
                             **kf)
        row = {"combo": c["combo"], "label": c["label"],
               "P_k": c["P_k"], "Mx_kin": c["Mx_kin"], "Vy_k": c["Vy_k"],
               "PMxMy1": chk["PMxMy1"], "PMxMy2": chk["PMxMy2"], "MxVy": chk["MxVy"],
               "UC": chk["UC"], "governing_check": chk["governing_check"],
               "status": chk["status"]}
        for extra in ("factors", "roof_term", "live_pattern"):
            if extra in c:
                row[extra] = c[extra]
        if chk.get("warnings"):
            row["warnings"] = chk["warnings"]
            warnings += [f"[{c['combo']}] {w}" for w in chk["warnings"]]
        if ctx.get("include_trace") and chk.get("trace"):
            row["trace"] = chk["trace"]
        rows.append(row)
        if ctx.get("emit_combo_partials"):
            partial({"section": label, **{k: v for k, v in row.items() if k != "trace"}})
        if gov is None or chk["UC"] > gov["UC"]:
            gov = row

    # Web crippling at the stud end (lateral end reaction on the flange)
    V_max = max((abs(r["Vy_k"] or 0.0) for r in rows), default=0.0)
    web = {"evaluated": False, "reason": "no lateral end reaction in any combo"}
    if V_max > 0:
        report(section=label, step="web crippling")
        try:
            web = {"evaluated": True, **s.web_crippling(
                spec, V_max, ctx.get("bearing_length_in", 1.0),
                ctx.get("web_fastened", True))}
            gcombo = max(rows, key=lambda r: abs(r["Vy_k"] or 0.0))["combo"]
            web["governing_combo"] = gcombo
            if web["stiffener_required"]:
                warnings.append(f"Web crippling UC {web['UC']} > 1.0 at the stud end "
                                f"(R = {web['R_k']} k): a web stiffener / clip is "
                                f"required, or increase the bearing length.")
        except Exception as e:  # noqa: BLE001
            web = {"evaluated": False,
                   "reason": f"CFS WebCheck could not evaluate this section: {e}"}

    # Deflection (service lateral load, CFS effective Ix at the service moment)
    defl = {"evaluated": False, "cases": [], "limit_L_over": ctx.get("defl_limit")}
    cases = ctx.get("defl_cases") or []
    if cases:
        E = s.elastic_modulus()
        H, sp = ctx["height_ft"], ctx["spacing_in"]
        L_in = H * 12.0
        out_cases = []
        for dc in cases:
            report(section=label, step=f"deflection {dc['case']}")
            M_serv, _ = lateral_effects(dc["psf"], sp, H, ctx.get("support", "pinned-pinned"))
            try:
                Ie = s.effective_Ix(spec, M_serv)
            except Exception as e:  # noqa: BLE001
                out_cases.append({**dc, "error": f"CFS effective Ix failed: {e}"})
                continue
            d = lateral_deflection_in(dc["psf"], sp, H, E, Ie,
                                      ctx.get("support", "pinned-pinned"))
            L_over = round(L_in / d, 1) if d else None
            lim = ctx.get("defl_limit")
            out_cases.append({**{k: dc[k] for k in ("case", "load", "factor")},
                              "service_psf": round(dc["psf"], 4),
                              "M_service_kin": round(M_serv, 4),
                              "Ix_eff_in4": Ie, "E_ksi": E,
                              "deflection_in": round(d, 4) if d is not None else None,
                              "L_over": L_over,
                              "passes": (L_over is not None and L_over >= lim) if lim else None})
        ok_cases = [c for c in out_cases if c.get("deflection_in") is not None]
        ctrl = max(ok_cases, key=lambda c: c["deflection_in"]) if ok_cases else None
        defl = {"evaluated": bool(ok_cases), "cases": out_cases,
                "limit_L_over": ctx.get("defl_limit"),
                "controlling": ctrl,
                "passes": (all(c["passes"] for c in ok_cases) if ctx.get("defl_limit")
                           and ok_cases else None)}

    gov_uc = gov["UC"] if gov else None
    passed_strength = gov_uc is not None and gov_uc <= 1.0
    passed = passed_strength and (defl.get("passes") is not False)
    flags = sorted(set(sdet.get("flags", [])))
    rec = {
        "section": label, "identifier": ident, "load_path": path,
        "section_type": sd.get("section_type_name"), "Fy_ksi": sd.get("Fy_ksi"),
        "weight_plf": gp["weight_plf"],
        "properties": {"A_in2": gp["area_in2"], "Ix_in4": gp["Ix_in4"],
                       "Sx_in3": gp["Sx_in3"], "Iy_in4": gp["Iy_in4"],
                       "rx_in": gp["rx_in"], "ry_in": gp["ry_in"],
                       "Ae_fully_braced_in2": sdet.get("Ae_fully_braced_in2"),
                       "Ae_member_in2": sdet.get("Ae_member_in2")},
        "strengths": sdet,
        "governing_UC": round(gov_uc, 4) if gov_uc is not None else None,
        "governing_combo": gov["combo"] if gov else None,
        "governing_label": gov["label"] if gov else None,
        "governing_forces": ({"P_k": gov["P_k"], "Mx_kin": gov["Mx_kin"],
                              "Vy_k": gov["Vy_k"]} if gov else None),
        "passed_strength": passed_strength,
        "passed": passed,
        "web_crippling": web,
        "deflection": defl,
        "combos": rows,
        "flags": flags, "flag_notes": describe_flags(flags),
        "warnings": warnings + [f"strength breakdown: {w}" for w in sdet.get("warnings", [])],
    }
    return rec


def _safe_check(s, ident, ctx) -> Dict:
    try:
        return check_one_section(s, ident, ctx)
    except CFSError as e:
        return {"section": ident, "identifier": ident, "error": str(e), "passed": False}
    except Exception as e:  # noqa: BLE001
        return {"section": ident, "identifier": ident,
                "error": f"Calculation failed: {e}", "passed": False}


def check_sections_job(sections: List[str], ctx: Dict,
                       stop_at_first_pass: bool = False) -> Dict:
    """Check a list of sections (in order). Used by check_stud and design_wall."""
    records = []
    with cfs_session() as s:
        for ident in sections:
            rec = _safe_check(s, ident, ctx)
            records.append(rec)
            partial(_summary(rec))
            if stop_at_first_pass and rec.get("passed"):
                break
    return {"records": records}


def _summary(rec: Dict) -> Dict:
    if rec.get("error"):
        return {"section": rec["section"], "error": rec["error"]}
    return {"section": rec["section"], "weight_plf": rec.get("weight_plf"),
            "governing_UC": rec.get("governing_UC"),
            "governing_combo": rec.get("governing_combo"),
            "governing_label": rec.get("governing_label"),
            "passed": rec.get("passed"),
            "deflection_L_over": ((rec.get("deflection") or {}).get("controlling") or {})
            .get("L_over"),
            "web_crippling_UC": (rec.get("web_crippling") or {}).get("UC")}


# ---------------------------------------------------------------------------
# Stacked wall: every level in ONE engine session
# ---------------------------------------------------------------------------
def stacked_job(level_ctxs: List[Dict]) -> Dict:
    """level_ctxs: [{'name', 'candidates': [...], 'ctx': {...}}] top-down."""
    results = []
    with cfs_session() as s:
        for lc in level_ctxs:
            report(level=lc["name"])
            tried = []
            chosen = None
            for ident in lc["candidates"]:
                rec = _safe_check(s, ident, lc["ctx"])
                tried.append(rec)
                if rec.get("passed"):
                    chosen = rec
                    break
            if chosen is None:
                ok = [r for r in tried if not r.get("error")]
                chosen = min(ok, key=lambda r: r["governing_UC"]) if ok else \
                    (tried[-1] if tried else None)
            res = {"level": lc["name"], "selected": chosen,
                   "sections_tried": [_summary(r) for r in tried]}
            results.append(res)
            partial({"level": lc["name"], "selected": _summary(chosen) if chosen else None})
    return {"levels": results}


# ---------------------------------------------------------------------------
# Batch member check (ETABS / SAP2000)
# ---------------------------------------------------------------------------
def member_lengths(m: dict, spec_default_brace: str = "none",
                   notes: Optional[List[str]] = None) -> dict:
    """Resolve a member's unbraced lengths from its dict.

    Length keys may sit at the TOP LEVEL of the member dict or inside a nested
    `lengths` sub-dict — both are accepted. Priority order:
      * explicit Lx_in/Ly_in/Lt_in[/Lm_in] - Lm_in is optional and defaults to
        Lt (distortional length = torsional brace spacing, the same rule as
        brace_config); the default is reported in `notes`
      * Lu_in (applied to all four)
      * length_ft (+ optional brace_config / brace_n)
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
        out = {"Lx": g("Lx_in"), "Ly": g("Ly_in"), "Lt": g("Lt_in")}
        if src.get("Lm_in") is None and base is None:
            out["Lm"] = out["Lt"]
            if notes is not None:
                notes.append(f"Member '{m.get('id')}': Lm_in not given - distortional "
                             f"unbraced length Lm set = Lt = {out['Lt']:g} in.")
        else:
            out["Lm"] = g("Lm_in")
        return out
    if src.get("Lu_in") is not None:
        L = float(src["Lu_in"])
        return {"Lx": L, "Ly": L, "Lt": L, "Lm": L}
    if src.get("length_ft") is not None:
        return unbraced_lengths(float(src["length_ft"]),
                                src.get("brace_config", spec_default_brace),
                                src.get("brace_n", src.get("custom_n")))
    raise ValueError(
        f"Member '{m.get('id')}' has no length info. Provide length keys either at "
        f"the top level or inside a 'lengths' object: Lx_in/Ly_in/Lt_in/Lm_in, or "
        f"Lu_in, or length_ft (+brace_config)."
    )


# Rough sanity bounds for a single CFS member (kip / kip-inch). Values beyond
# these usually mean the caller passed pounds instead of kips.
P_SANITY_K = 500.0
M_SANITY_KIN = 10000.0


def force_unit_warnings(mid, forces: dict) -> List[str]:
    w = []
    P = abs(forces.get("P_k", 0.0) or 0.0)
    Mx = abs(forces.get("Mx_kin", 0.0) or 0.0)
    My = abs(forces.get("My_kin", 0.0) or 0.0)
    if P > P_SANITY_K:
        w.append(f"Member '{mid}': P_k={P} looks large for kips — did you pass "
                 f"POUNDS? CFS expects kips (divide lb by 1000).")
    if max(Mx, My) > M_SANITY_KIN:
        w.append(f"Member '{mid}': moment {max(Mx, My)} looks large for kip-in — "
                 f"did you pass lb-in? CFS expects kip-inches.")
    return w


def batch_job(members: List[Dict], spec_id: int, brace_config: str) -> Dict:
    groups: Dict = {}
    for i, m in enumerate(members):
        key = (str(m.get("section")), m.get("grade"), m.get("fy"))
        groups.setdefault(key, []).append((i, m))
    results = []
    with cfs_session() as s:
        for (section, grade, fy), grp in groups.items():
            report(section=section, step="loading section")
            try:
                path = s.load(section, grade=grade, fy=fy)
                sd = s.section_data()
                label = section_label(path, sd.get("description"))
            except CFSError as e:
                for i, m in grp:
                    r = {"_i": i, "id": m.get("id"), "section": section,
                         "error": str(e), "status": "ERROR"}
                    results.append(r)
                    partial({k: v for k, v in r.items() if k != "_i"})
                continue
            for i, m in grp:
                report(section=label, member=m.get("id"))
                try:
                    len_w: List[str] = []
                    lengths = member_lengths(m, brace_config, len_w)
                    fc = dict(m.get("forces", {}))
                    P_signed, sign_w = resolve_axial_force(
                        fc.get("P_k"), m.get("force_type", "auto"))
                except ValueError as e:
                    r = {"_i": i, "id": m.get("id"), "section": label,
                         "error": str(e), "status": "ERROR"}
                    results.append(r)
                    partial({k: v for k, v in r.items() if k != "_i"})
                    continue
                if P_signed is not None:
                    fc["P_k"] = P_signed
                chk = s.member_check(spec_id, lengths, fc)
                warns = (list(chk.get("warnings", [])) + sign_w + len_w
                         + force_unit_warnings(m.get("id"), fc))
                r = {
                    "_i": i, "id": m.get("id"), "section": label,
                    "unbraced_lengths_in": lengths, "forces": fc,
                    "PMxMy1": chk["PMxMy1"], "PMxMy2": chk["PMxMy2"],
                    "MxVy": chk["MxVy"], "MyVx": chk["MyVx"],
                    "UC": chk["UC"], "governing_check": chk["governing_check"],
                    "status": chk["status"], "flags": chk["flags"],
                    "flag_notes": chk["flag_notes"], "strengths": chk["strengths"],
                    "warnings": warns,
                }
                results.append(r)
                partial({k: v for k, v in r.items() if k != "_i"})
    results.sort(key=lambda r: r["_i"])
    for r in results:
        r.pop("_i", None)
    return {"members": results}


# ---------------------------------------------------------------------------
# Wall substitution (MSBS) - evaluate each section ONCE, sweep spacings
# ---------------------------------------------------------------------------
def ratio(val, ref):
    if not ref:
        return float("inf") if val else 1.0
    return val / ref


def evaluate_section(session, identifier, spec, lengths, grade=None, fy=None,
                     include_trace=False):
    """Load a section and gather gross props + member strengths at `lengths`.

    Returns a result dict; on failure returns {'error': ...} instead of raising,
    so a batch loop can continue past a bad section.
    """
    label = os.path.splitext(os.path.basename(identifier))[0] if identifier else identifier
    report(section=label, step="member strengths")
    try:
        path = session.load(identifier, grade=grade, fy=fy)
    except CFSError as e:
        return {"section": label, "error": str(e)}
    try:
        sd = session.section_data()
        gp = session.gross_properties()
        ms = session.member_strengths(spec, lengths, include_trace=include_trace)
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


def _eval_sub_candidate(session, ident, disp, category, spec_id, lengths,
                        ref_P, ref_Mx, ref_Ix, ref_w_psf, spacings,
                        ref_spacing_in, tol_factor, governing, include_trace):
    r = evaluate_section(session, ident, spec_id, lengths, include_trace=include_trace)
    if "error" in r:
        return ({"section": disp, "category": category, "error": r["error"],
                 "passes": False, "by_spacing": []}, [])

    cP = r["member_strength"]["P_k"] or 0.0
    cMx = r["member_strength"]["Mx_kin"] or 0.0
    cIx = r["gross"]["Ix_in4"] or 0.0
    cw = r["gross"]["weight_plf"] or 0.0
    flags = r["member_strength"]["flags"]
    cap_P, cap_Mx, cap_Ix = ratio(cP, ref_P), ratio(cMx, ref_Mx), ratio(cIx, ref_Ix)

    by_spacing, flat_rows = [], []
    for sp in spacings:
        row = substitution.spacing_row(
            sp, ref_spacing_in, cap_P, cap_Mx, cap_Ix, cw, ref_w_psf, tol_factor, governing)
        amb = substitution.ambiguous_mx_flag(
            row["axial_ok"], row["flexural_ok"], row["flexural_ratio"])
        row["flags"] = [amb] if amb else []
        by_spacing.append(row)
        flat_rows.append({
            "section": disp, "category": category, "spacing_in": sp,
            "axial_ratio": row["axial_ratio"], "flexural_ratio": row["flexural_ratio"],
            "stiffness_ratio": row["stiffness_ratio"], "weight_psf": row["weight_psf"],
            "passes": row["passes"], "flags": row["flags"],
            "P_capacity_k": round(cP, 4), "P_required_k": round(ref_P, 4),
            "Mx_capacity_kin": round(cMx, 4), "Mx_required_kin": round(ref_Mx, 4),
            "most_efficient": False,
        })
    rec = {
        "section": disp, "category": category, "member_weight_plf": cw,
        "P_capacity_k": round(cP, 4), "P_required_k": round(ref_P, 4),
        "Mx_capacity_kin": round(cMx, 4), "Mx_required_kin": round(ref_Mx, 4),
        "Ix_in4": cIx,
        "capacity_axial_ratio": round(cap_P, 3),
        "capacity_flexural_ratio": round(cap_Mx, 3),
        "capacity_stiffness_ratio": round(cap_Ix, 3),
        "max_continuous_spacing_axial_in": round(ref_spacing_in * cap_P, 2),
        "max_continuous_spacing_flexural_in": round(ref_spacing_in * cap_Mx, 2),
        "max_continuous_spacing_combined_in": round(ref_spacing_in * min(cap_P, cap_Mx), 2),
        "flags": flags, "flag_notes": describe_flags(flags),
        "by_spacing": by_spacing,
        "passes": any(row["passes"] for row in by_spacing),
    }
    if include_trace:
        rec["trace"] = r["member_strength"].get("trace")
    return rec, flat_rows


def _closeness(row, governing):
    if governing == "axial":
        return row["axial_ratio"]
    if governing == "flexural":
        return row["flexural_ratio"]
    return min(row["axial_ratio"], row["flexural_ratio"])


def substitution_job(p: Dict) -> Dict:
    """COM body of find_wall_substitutions. `p` holds the validated inputs."""
    spec_id, lengths = p["spec_id"], p["lengths"]
    spacing_in, spacings = p["spacing_in"], p["spacings"]
    reference_n, governing = p["reference_n"], p["governing"]
    tol_factor, include_trace = p["tol_factor"], p["include_trace"]
    msbs, target_depths, ref_depth = p["msbs"], p["target_depths"], p["ref_depth"]
    top_flags = list(p["top_flags"])

    with cfs_session() as s:
        ref = evaluate_section(s, p["standard"], spec_id, lengths,
                               grade=p.get("standard_grade"), fy=p.get("standard_fy"),
                               include_trace=include_trace)
        if "error" in ref:
            raise CFSError(f"Could not evaluate standard '{p['standard']}': {ref['error']}")
        ref_P = (ref["member_strength"]["P_k"] or 0.0) * reference_n
        ref_Mx = (ref["member_strength"]["Mx_kin"] or 0.0) * reference_n
        ref_Ix = (ref["gross"]["Ix_in4"] or 0.0) * reference_n
        ref_w = (ref["gross"]["weight_plf"] or 0.0) * reference_n
        ref_w_psf = ref_w * 12.0 / spacing_in

        profiles, flat_rows = [], []

        def run(ident, disp, category):
            rec, rows = _eval_sub_candidate(
                s, ident, disp, category, spec_id, lengths, ref_P, ref_Mx, ref_Ix,
                ref_w_psf, spacings, spacing_in, tol_factor, governing, include_trace)
            profiles.append(rec)
            flat_rows.extend(rows)
            partial({k: v for k, v in rec.items() if k not in ("by_spacing", "trace")})

        for m in msbs:
            disp = m["name"] if m["folder"] == "SINGLE" else f"{m['name']} [{m['folder']}]"
            run(m["path"], disp, m["category"])

        passing_rows = [r for r in flat_rows if r["passes"]]
        fallback_used = False
        if not passing_rows:
            fallback_used = True
            depths = sorted(target_depths) if target_depths else [ref_depth]
            std_desigs: List[str] = []
            for d in depths:
                try:
                    std_desigs += catalog.list_standard_designations(
                        "SSMA", "S", d, structural_only=True)
                except Exception:  # noqa: BLE001
                    pass
            for desig in std_desigs:
                run(desig, desig, "standard")
            passing_rows = [r for r in flat_rows if r["passes"]]
            top_flags.append(
                "No MSBS single/double section passed at any spacing; the search was "
                "extended to standard (non-MSBS) sections at the matched depth "
                "(category='standard').")

        selected_row = substitution.select_most_efficient(passing_rows)
        selected, align_flag = None, None
        if selected_row:
            selected_row["most_efficient"] = True
            align_flag = substitution.alignment_flag(
                selected_row["spacing_in"], p.get("adjacent_spacing_in"),
                p.get("load_transfer_present", False))
            sel_flags = list(selected_row["flags"])
            if align_flag:
                sel_flags.append(align_flag)
                top_flags.append(align_flag)
            selected = {**{k: v for k, v in selected_row.items() if k != "most_efficient"},
                        "flags": sel_flags}
            for pr in profiles:
                if pr.get("section") == selected_row["section"]:
                    pr["selected"] = True
                    pr["selected_spacing_in"] = selected_row["spacing_in"]

        scored = [r for r in flat_rows if "axial_ratio" in r]
        closest = max(scored, key=lambda r: _closeness(r, governing)) if scored else None
        any_passing = bool(passing_rows)
        ref_disp = ref["section"] + (f" (x{reference_n} plies)" if reference_n > 1 else "")
        show = selected_row or closest
        if show:
            flagged = bool(show.get("flags")) or bool(align_flag)
            status = substitution.plain_status(
                passes=bool(selected_row), flagged=flagged, any_passing_in_set=any_passing)
            sub_row = {
                "original_section": ref_disp, "original_spacing_in": spacing_in,
                "substituted_section": show["section"], "category": show["category"],
                "spacing_in": show["spacing_in"], "axial_P_ratio": show["axial_ratio"],
                "flexural_Mx_ratio": show["flexural_ratio"],
                "weight_psf": show["weight_psf"], "status": status,
            }
        else:
            sub_row = {"original_section": ref_disp, "substituted_section": None,
                       "status": "No candidate could be evaluated."}

        def prank(pr):
            if pr.get("error"):
                return (2, 0.0)
            sc = min(pr["capacity_axial_ratio"], pr["capacity_flexural_ratio"]) \
                if governing == "both" else (
                    pr["capacity_axial_ratio"] if governing == "axial"
                    else pr["capacity_flexural_ratio"])
            return (0 if pr.get("passes") else 1, -sc)
        profiles.sort(key=prank)

        if selected:
            msg = (f"Selected {selected['section']} ({selected['category']}) @ "
                   f"{selected['spacing_in']}\" o.c. at {selected['weight_psf']} psf vs "
                   f"{round(ref_w_psf, 4)} psf original. {len(passing_rows)} passing "
                   f"(section,spacing) combos of {len(flat_rows)} evaluated"
                   + (" [standard-section fallback]" if fallback_used else "")
                   + ". See substitution_table for the plain-language summary.")
        else:
            extra = ""
            if closest:
                extra = (f" Closest: {closest['section']} @ {closest['spacing_in']}\" "
                         f"(P {closest['axial_ratio']}, Mx {closest['flexural_ratio']}).")
            msg = (f"No section passes on '{governing}' at any allowed spacing "
                   f"{spacings}.{extra} Full matrix returned for judgment.")

        return {
            "message": msg, "flags": top_flags,
            "reference": {
                "section": ref_disp, "spacing_in": spacing_in, "reference_n": reference_n,
                "axial_P_k": round(ref_P, 4), "flexural_Mx_kin": round(ref_Mx, 4),
                "Ix_in4": round(ref_Ix, 4), "member_weight_plf": round(ref_w, 4),
                "weight_psf": round(ref_w_psf, 4),
            },
            "selected": selected, "substitution_table": [sub_row],
            "passing_count": len(passing_rows), "total_evaluated": len(flat_rows),
            "profiles": profiles,
            "audit_table": (flat_rows if p.get("include_all_configs") else None),
        }
