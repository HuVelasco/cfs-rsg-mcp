# CFS-RSG MCP — Agent Guide

**Cold-formed steel wall-stud design over the RSG CFS 15 Automation Module (COM).**

**CRITICAL: This is structural engineering. Lives depend on accuracy.**

## 🚨 Non-negotiable rules
1. **Real CFS only.** Every result comes from the CFS15 engine via COM. Never
   approximate a section property or capacity in Python.
2. **No false success.** If a section won't load or a check errors, return the
   honest error. Never fabricate a passing unity check.
3. **Report flags.** CFS `NoteFlags` (slender element, KL/r limits, etc.) are
   surfaced — pass them through; don't hide them.

## 🛠️ The tools (keep them simple & powerful)
```
connect_cfs()               # connection + license check (call first if errors)
parse_wall_specification()  # "10ft, 600S200-43 @ 16 O.C." -> fields
list_standard_profiles()    # SSMA/SFIA/AISI/HUD/LGSI catalog
list_custom_profiles()      # user's .cfss files
get_section_properties()    # gross props + fully-braced strength
check_stud()                # one stud, explicit forces OR ASD combos
design_wall()               # pick lightest passing standard/custom section
design_stacked_wall()       # multi-level stack: accumulation, LLR, patterns, Simpson summary
find_equivalent_profiles()  # customs >= a standard spec at fixed spacing
find_wall_substitutions()   # HEADLINE: MSBS single/double @ varied spacing, lightest safe
check_members_batch()       # code-check many members (ETABS/SAP truss output)
export_section_for_analysis() # gross props -> SAP2000/ETABS PropFrame.SetGeneral
```
New tools must be as thin and reliable as these.

## 🧱 Architecture
- `cfs_engine.py` — the **only** module that touches COM. `cfs_session()` context
  manager creates a fresh, licensed `RSG.CFS15.Calculation` per tool call
  (STA-safe), releases the license on exit. Late-binding (`dynamic.Dispatch`) —
  never the gen_py cache. Also: designation parsing, SSMA library-path
  resolution, spec enums, unit-safe getters.
- `cfs_loads.py` — load-path helpers, unbraced-length rules, support conditions,
  deflection demand, and ASD combos as FACTOR TABLES over per-stud load effects
  (`stud_effects` → `combos_from_effects`). Built-in sets and user combos share
  one code path. Pure, no COM.
- `stacked.py` — pure accumulation for `design_stacked_wall` (top-down, walls
  above, ASCE 7-22 §4.7 LLR, AB/A/B patterns, alignment flags).
- `watchdog.py` — runs COM jobs in a killable child process (timeout, partials,
  hung-engine restart). `jobs.py` — the COM job bodies (module-level, picklable;
  never import FastMCP there).
- `catalog.py` — enumerate standard designations from `.cfsl`, list custom
  `.cfss`, parse free-text wall specs.
- `substitution.py` — pure (no COM) decision logic for `find_wall_substitutions`:
  depth matching, spacing sweep, efficiency selection, alignment, gauge ceiling,
  ambiguous-Mx flags, plain-language status. Unit-tested off-Windows.
- `server.py` — FastMCP tool layer. Returns JSON strings.

## ⚙️ Key facts
- ProgID: `RSG.CFS15.Calculation` (+ `.MemberParams`, `.SectionForces`,
  `.SectionData`). Helper classes via `RSG.CFS15.<Name>`.
- Default spec: **AISI2016USASD = 27** (AISI S100-16 (2020) w/S2-20 = code of
  record for IBC 2024/2021, BCNYS 2025). NOT 2024: S100-24 overstated Pa by 36%
  in production. `spec_code_check` warns on a spec/ASCE mismatch vs
  `building_code` (default IBC2024) and ERRORS on LRFD spec + ASD combos.
- **Load combos** (verified T2–T7): combo 2a "D + L" has NO roof load (L_lat stays
  — it is part of L). Lr/S/R/uplift use trib_A + trib_B (exterior: trib_A only).
  Each combo takes ONE of "Lr or 0.7S or R" (governing, `roof_term`), never the sum.
  Seismic §2.4.5: Eq 8 D+0.7Ev+0.7Eh, Eq 9 D+0.525Ev+0.525Eh+0.75L+0.1S, Eq 10
  0.6D−0.7Ev+0.7Eh; Eh×D_wall lateral, Ev×total D axial (strength level). Defaults
  wind_level/snow_level = 'strength' (0.6W, 0.7S, 0.1S); 'asd' → 0.1/0.7 = 0.143 in
  Eq 9. MWFRS combines with axial; C&C = bending only (P=0) + deflection.
  User combos apply factors EXACTLY (no hidden 0.6/0.7).
- **Watchdog**: CFS15.Calculation is IN-PROCESS .NET COM (InprocServer32 =
  mscoree.dll) → a hung call can't be interrupted in-thread; that is why COM tools
  run via `watchdog.run(jobs.X, ...)` in a spawned child (≈2 s/call incl. license).
  Child stdout is redirected to stderr (stdout is the MCP channel).
- **Limit-state strengths** (Pa-Dist, Ma-Brc, Ma-Dist, buckling mode) are parsed
  from CFS's own MemberCheck trace (`parse_strength_trace`) and reconciled with
  CFS member P/Mx — a mismatch becomes a warning. Web crippling = CFS `WebCheck`
  at the max lateral end reaction; deflection uses CFS `EffectiveProperties` Ix at
  the service moment (only the beam formula is Python).
- SSMA library: `C:\Program Files (x86)\RSG Software\CFS 15\SSMA.cfsl`.
  Internal folders: `Structural Studs 33/50 ksi`, `Structural Tracks 33/50 ksi`,
  `Non-Structural Studs 33/50 ksi`, `U-Channels`, `Furring Channels`.
- LoadSection path form: `<lib>.cfsl|<Folder>\<name>.cfss`. Studs ≤43 mil default
  to 33 ksi, ≥54 mil to 50 ksi; the loader tries the alternate folder on failure.
- **Member strengths** for equivalency/design come from `MemberCheck` with zero
  applied force (`_Session.member_strengths`) — CFS returns the available P, Mx,
  My, Vy already accounting for global/distortional buckling at Lx/Ly/Lt/Lm.
- Custom `.cfss` descriptions are unreliable (stale template copies) → label
  custom sections by **filename** (`section_label()`), library sections by
  description.
- MSBS custom sections live under `Info-and-Examples/Sections/{SINGLE,B2B,BOX,
  TRUSS}`. `catalog.list_msbs_sections()` tags each: SINGLE=single (1 member),
  B2B/BOX=double (2 members, built-up capacity is in the .cfss already). B2B and
  BOX share filenames → qualify by `[folder]` in output.
- **Substitution logic** (`find_wall_substitutions`): member strength does NOT
  depend on spacing, only on section + unbraced length. So evaluate each section
  ONCE, then sweep spacings analytically — every ratio scales by
  `(ref_spacing / s)`. Phase-1 rewrite (logic in `substitution.py`, pure/testable):
  - `wall_type` ('interior'|'exterior') is **required** — no silent 'exterior'
    default anywhere in the call chain (also enforced in `design_wall` and
    `build_load_combinations`).
  - **Depth matching** is computed from the library's ACTUAL available MSBS depths
    (`nearest_depth`) — no hardcoded 4→3⅝ table. `depth_filter_in` overrides.
  - **Spacings** default to `[original spacing_in, 12, 16, 24]`; override with
    `allowed_spacings` (used exactly as given). The sweep is **exhaustive** — every
    spacing × category × gauge is scored; it never stops at the first pass.
  - **Selection** = lightest weight per ft² of wall (`weight_plf × 12/spacing`),
    preferring **single over double** within a 5% weight band (`select_most_efficient`).
    The chosen row is `most_efficient: true` in the audit table and `selected` on
    its profile.
  - **Categories**: single tried first; double defaults to **B2B only** (BOX only
    when `include_boxed=True`).
  - **No auto-accept**: a row that passes P but fails Mx is returned **flagged**
    (`ambiguous_mx_flag`) for BOTH wall types — the engineer decides. `governing`
    ('both'|'axial'|'flexural') still controls what counts as a pass for selection.
  - **`reference_n`** (default 1) multiplies ONLY the **reference** side by N plies
    (P, Mx, Ix, weight) — an idealized stacked model, flagged as such. `brace_n`
    (renamed from `custom_n`) is bracing-only and never multiplies capacity.
  - **Cross-floor alignment**: pass `adjacent_spacing_in` + `load_transfer_present`;
    if the selected spacing doesn't stack and there's no transfer member, an
    alignment flag fires (`alignment_flag`).
  - **Gauge ceiling + fallback**: if the reference gauge exceeds the heaviest MSBS
    gauge at the matched depth, an explicit flag is returned; if NO MSBS section
    passes at any spacing, the search extends to **standard** sections at the
    matched depth (`category: 'standard'`) so there's never a silent empty result.
  - **Output**: `substitution_table` (ALWAYS — one plain-language row, GC/PM-readable),
    `selected` (with actual P/Mx capacities + required values), `profiles` (per-
    candidate with `by_spacing`), `audit_table` (only when `include_all_configs=True`
    — every tried row), and CFS calc trace per section when `include_trace=True`.
    `stiffness_ratio` (Ix) is reported for the engineer's deflection judgment.
- **ETABS/SAP round-trip**: `check_members_batch` groups members by section so
  each loads once (fast for trusses); `export_section_for_analysis` maps gross
  props to CSI `PropFrame.SetGeneral` arg order (t3,t2,Area,As2,As3,J,I33,I22,
  S33,S22,Z33,Z22,R33,R22). Shear areas 0 (CFS governs shear itself).
  - **Sign/units** (critical): CFS uses compression-POSITIVE; ETABS reports axial
    compression NEGATIVE → flip sign before feeding in. Forces are kip / kip-in;
    the batch tool warns if P>500k or M>10000 (likely lb passed as kip).
  - **Axial sign safety** (`resolve_axial_force` in `cfs_loads.py`): every tool
    that takes a raw `P_k` (`check_stud`, per-member in `check_members_batch`)
    accepts `force_type` = 'compression'|'tension'|'auto'. 'compression'/'tension'
    sign the magnitude for you; 'auto' (default) uses the sign as given and returns
    a WARNING if `P_k<0` (silently ran as tension). The PSF `loads` path never
    needs this — `build_load_combinations` assigns each combo's axial sign
    explicitly (gravity compression +, wind uplift −).
- **NoteFlags** are ENGINEERING NOTES, not automatic fails. `FLAG_DESCRIPTIONS`
  in `cfs_engine.py` gives plain-English text; every check returns `flag_notes`.
  `BendRadius` (R/t over limit) makes CFS return Vy=0 for some webs (e.g. the
  244SLW truss chords) → the MxVy/MyVx interaction hits the CFS sentinel
  `UC_SENTINEL=9.999`. `member_check` handles this honestly: if there is NO
  applied shear it EXCLUDES that interaction (with a warning) and governs on
  PMxMy; if shear IS applied with zero capacity it reports a real FAIL.
- **`structural_only`**: SSMA non-structural drywall studs are the S125 (1-1/4"
  flange) series. `design_wall` defaults `structural_only=True` so a load-bearing
  wall is never designed with a partition gauge; `list_standard_profiles` exposes
  the same flag plus a `flange_in` filter.
- **Wall-spec parsing** handles compound feet-inch for BOTH height and spacing
  (`2'-6"` → 30 in, not 6 in), simple height arithmetic (`10'-8" minus 4.5"`),
  and returns a `warnings` list for anything it could not fully interpret (never
  silently drops a term).

## 🧪 Testing protocol
- Requires a live, licensed CFS 15 with the Automation option. `connect_cfs`
  must report `success: true` before trusting anything else.
- **Restart the MCP server** after editing any `.py` — Claude won't see changes
  until reload.
- Validated live during development: `600S200-43` gross props match the SSMA
  table exactly (A=0.492, Ix=2.683, Sx=0.894); equivalency and design tools run
  end-to-end against the bundled custom library.

## 📋 Versioning
- `0.x.0.y` = developed, not MCP-tested (don't trust yet).
- `0.x.y.0` = MCP-tester validated.
- `0.x.y.z` + user validation = production ready.
- Current: **0.1.0.3** (developed; production-issue fix pass — combo 1 = D+L only,
  snow/roof over trib_A+trib_B, Ev + ASCE 7-22 §2.4.5 seismic, default spec
  AISI2016USASD + code-mismatch warning, watchdog/timeout/partials, Lr/R inputs,
  load levels, 7-22/7-16/IBC-alt combo sets, user combos, design_stacked_wall with
  LLR/patterns/alignment, deflection, cantilever support, line/point loads,
  limit-state strength breakdown, web crippling. Acceptance tests T1–T9 pass incl.
  live CFS; **re-test via MCP before trusting**).
- Previous: 0.1.0.2 (developed; Phase 1 fix pass — `reference_n` multiplies the
  reference wall, `force_type` + negative-`P_k` warning close the sign trap,
  `custom_n`→`brace_n` rename, `wall_type` required, `find_wall_substitutions`
  rewritten over `substitution.py`: library-driven depth match, exhaustive
  spacing/category/gauge matrix, lightest-per-ft selection, cross-floor alignment
  check, gauge ceiling + standard-section fallback, audit + plain-language tables,
  optional CFS trace. Pure logic unit-tested; **re-test live before trusting**).

## Author
Hugo Velasco (Hugo VelP).
