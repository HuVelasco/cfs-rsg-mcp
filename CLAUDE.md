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
- `cfs_loads.py` — load-path helpers, unbraced-length rules, ASD load combos.
  Adapted verbatim from the user's validated wall-stud notebook.
- `catalog.py` — enumerate standard designations from `.cfsl`, list custom
  `.cfss`, parse free-text wall specs.
- `server.py` — FastMCP tool layer. Returns JSON strings.

## ⚙️ Key facts
- ProgID: `RSG.CFS15.Calculation` (+ `.MemberParams`, `.SectionForces`,
  `.SectionData`). Helper classes via `RSG.CFS15.<Name>`.
- Default spec: **AISI2024USASD = 42**. Others in `SPEC_BY_NAME`.
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
  `(ref_spacing / s)`. The tool RETURNS ALL PROFILES (pass and fail) with both
  `axial_ratio` and `flexural_ratio` at each allowed spacing, plus P-only / Mx-only
  / combined max continuous spacings — the engineer judges (e.g. interior wall →
  Mx demand is 0). `governing` ('both'|'axial'|'flexural') sets which check must
  pass for `best_spacing_in`; 'axial' is the interior-no-wind case. `allowed_spacings`
  is used EXACTLY as given, widest-first (e.g. `[30,15]`: 30" sheet width preferred,
  15" fallback) — the reference spacing is NOT auto-added. No hard stiffness gate;
  `stiffness_ratio` (Ix) is reported for the engineer's deflection judgment.
- **ETABS/SAP round-trip**: `check_members_batch` groups members by section so
  each loads once (fast for trusses); `export_section_for_analysis` maps gross
  props to CSI `PropFrame.SetGeneral` arg order (t3,t2,Area,As2,As3,J,I33,I22,
  S33,S22,Z33,Z22,R33,R22). Shear areas 0 (CFS governs shear itself).
  - **Sign/units** (critical): CFS uses compression-POSITIVE; ETABS reports axial
    compression NEGATIVE → flip sign before feeding in. Forces are kip / kip-in;
    the batch tool warns if P>500k or M>10000 (likely lb passed as kip).
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
- Current: **0.1.0.1** (developed; bug-fix pass from the tester report — parse
  compound units, batch nested-`lengths` schema, BendRadius/zero-shear honesty,
  structural-only, folder discovery, unit/sign guards. Re-test before trusting).

## Author
Hugo Velasco (Hugo VelP).
