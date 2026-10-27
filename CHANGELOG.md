# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Maturity note: this package also carries an internal 4-part engineering
> maturity marker (see `CLAUDE.md`): `0.x.0.y` = developed, `0.x.y.0` =
> MCP-tester validated, `0.x.y.z` + user validation = production. The public
> SemVer below tracks releases; the maturity marker tracks trust.

## [Unreleased]

### Production-issue fix pass (internal maturity marker `0.1.0.3`)

Issues found on a real project (2025 BCNYS → IBC 2024 → ASCE 7-22, AISI S100-16
w/S2-20) and checked against Simpson Strong-Tie CFS Designer 6.2.2. Affected
tools: `design_wall`, `check_stud`, `check_members_batch`,
`find_wall_substitutions`. Developed and verified against live CFS 15 (T1, T2, T9
reproduce the reference numbers); not yet MCP-tester validated.

#### Behavior changes (read these before re-running old calculations)
- **BREAKING — combo "D + L" no longer includes roof load.** It previously
  computed D + L + S (e.g. 3.351 + 5.000 + 0.800 = 9.151 k instead of 8.351 k),
  which flipped a passing design (UC 0.974) to FAIL (1.064). It is now ASCE 7-22
  Eq. 2a, D + L only; lateral live load L_lat stays in it because it is part of L.
  Roof loads (Lr, S, R) appear only in the combos where ASCE places them.
- **BREAKING — roof snow over both tributaries.** Snow (and the new Lr, R, and
  roof uplift) is applied over `trib_A + trib_B`, the same rule as D and L
  (exterior walls: `trib_A` only). It previously used `trib_A` only, which
  understated the snow load by half on interior walls.
- **BREAKING — default spec is now `AISI2016USASD` (enum 27)**, i.e. AISI S100-16
  (2020) w/S2-20, the code of record for IBC 2024 / IBC 2021 / BCNYS 2025. The old
  default `AISI2024USASD` overstated axial capacity by ~36% (362SLS155-43 single,
  Ly = Lt = 48": Pa 3.564 k vs 2.658 k; Simpson 2.629 k). Every tool echoes
  `spec_used`, and a spec that does not match `building_code` returns a warning.
- **BREAKING — seismic combos per ASCE 7-22 §2.4.5, with Ev.** Ev was ignored and
  E was used at 1.0 (and with 0.75S). Now: Eq. 8 D + 0.7Ev + 0.7Eh; Eq. 9
  D + 0.525Ev + 0.525Eh + 0.75L + 0.1S; Eq. 10 0.6D − 0.7Ev + 0.7Eh. Eh × D_wall is
  the out-of-plane load; Ev × (total dead) is the vertical effect. Both are
  strength level.
- **BREAKING — load levels are explicit, and default to strength level.** Wind
  inputs are strength level by default (the combos apply 0.6W), and ASCE 7-22 snow
  is strength level (0.7S in Eqs. 3a/4a/6a, 0.1S in Eq. 9). Callers that were
  passing ASD-level wind or snow must now set `wind_level="asd"` /
  `snow_level="asd"`, or the loads will be reduced a second time. Every response
  carries `load_level_factors`, and each combo carries the `factors` applied to
  each input.
- **BREAKING — built-in combo set restructured.** The old 10 combos are replaced
  by ASCE 7-22 ASD Eqs. 1a–7a (§2.4.1) + Eqs. 8–10 (§2.4.5): 15 rows, since MWFRS
  windward and leeward each get 5a/6a/7a, plus C&C windward and leeward. Combo ids
  are now strings (`"2a"`, `"6a-mw"`, `"9"`) and every label carries its code
  reference (e.g. `"ASCE 7-22 Eq. 4a: D + 0.75L + 0.75(Lr or 0.7S or R)"`).
- **C&C pressures are checked in bending alone (P = 0)** and used for deflection;
  MWFRS pressures are combined with axial load. Previously C&C was combined with D.
- **Roof uplift** is applied over the roof tributary and only in 0.6D + 0.6W.
  It was previously applied with MWFRS windward at 1.0.
- `L_lat` is now applied whenever it is given. It was previously dropped silently
  on exterior walls.
- **`loads` keys are validated**: unknown keys and negative magnitudes are
  rejected. They were previously ignored silently.
- `check_stud` no longer defaults `wall_type` to `"exterior"`. It is required in
  load-combination mode (explicit-force mode is unchanged).
- An LRFD/LSD spec combined with the ASD load combinations is now an error.

#### Added
- **Watchdog** (`watchdog.py`): the COM tools run in an isolated, killable child
  process with a per-call timeout (`timeout_s`, default 60 s; `CFS_MCP_TIMEOUT_S`).
  This is needed because the CFS COM server is in-process .NET (`mscoree.dll`), so
  a hung call cannot be interrupted in-thread. On a hang the engine process is
  terminated and restarted on the next call. Work finished so far is returned as
  `partial_results`, and the section/combo being evaluated is reported and logged.
  A crashed engine is reported the same way. `CFS_MCP_WATCHDOG=0` runs inline.
- **`Lr` and `R` inputs.** Each combo takes "Lr or 0.7S or R" separately and uses
  the one that governs (reported in `roof_term`); they are never summed.
- `asce_edition` (`7-22` | `7-16`), `snow_level`, `wind_level`, `combo_set`
  (`ASCE7-22_ASD` | `ASCE7-16_ASD` | `IBC2024_ALT_ASD`), `combo_mode`
  (`builtin` | `user` | `append`), `user_combos`, and `building_code`.
- **User-defined combos.** Factors are applied to the inputs exactly as given (no
  hidden 0.6/0.7). Unknown keys are rejected, a negative D factor is flagged, and
  the full table is echoed.
- **`design_stacked_wall`** (new tool):
  - accumulates loads top-down, including the walls above;
  - `axial_at` `base` | `mid_height`;
  - ASCE 7-22 §4.7 live load reduction per stud (AT, KLL, the > 100 psf rule),
    reported per level;
  - AB/A/B live load patterns and a stud-alignment check between levels;
  - a Simpson Stacked Wall Summary-style row per level.
- **Deflection**: `defl_limit`, `wind_defl_modifier` (default 0.7 → 0.42W). The
  deflection uses the CFS effective Ix at the service moment and is reported as Δ
  and L/x with the controlling case.
- **Support conditions**: `support="cantilever"` (fixed-free parapet: wH²/2, wH,
  Kx = 2.1, deflection wL⁴/8EI). Also `line_loads` (plf) and `point_loads`
  (k/stud) at the top of the wall.
- **Output completeness**: every combo is returned with P, M, V and UC. The output
  adds section properties (A, Ix, Sx, Ae); limit-state strengths (Pa, Pa-Dist,
  Ma-Fy, Ma-Brc, Ma-Dist, Va); and the governing axial buckling mode (global /
  local / distortional). These are read from CFS's own trace (`parse_strength_trace`)
  and reconciled against the CFS member strength. The output also adds web
  crippling at the stud end (CFS `WebCheck`, *stiffener required*).
- Tests: `tests/test_acceptance.py` (acceptance tests T1–T9, live parts
  auto-skip) and `tests/test_combos_and_features.py`.

#### Changed
- The COM job bodies moved to `jobs.py`. The old private helpers remain importable
  from `server.py` as aliases.

---

Phase 1 fix pass (internal maturity marker `0.1.0.2`; public SemVer unchanged —
developed, not yet MCP-tester validated). Scope limited to bugs/gaps blocking
`find_wall_substitutions`; opening tool, built-up builder, PyNite, diagrams and
report export remain Phase 2+.

### Added
- `substitution.py`: pure, COM-free decision logic for `find_wall_substitutions`
  (depth matching, spacing sweep, efficiency selection, cross-floor alignment,
  gauge ceiling, ambiguous-Mx flagging, plain-language status) with a dedicated
  off-Windows test module.
- `cfs_loads.resolve_axial_force`: CFS axial sign-convention helper.
  `check_stud` and `check_members_batch` gain a `force_type`
  (`compression`/`tension`/`auto`) so a compression load can be passed as a
  positive magnitude; a negative `P_k` under `auto` now returns a warning that it
  was interpreted as tension.
- `find_wall_substitutions`: `reference_n` (multiplies ONLY the reference wall by
  N plies for a back-to-back replacement), `include_boxed`, `adjacent_spacing_in`
  + `load_transfer_present` (cross-floor stud-alignment flag), `include_all_configs`
  (full audit table), and `include_trace` (CFS calculation trace). Always returns a
  plain-language `substitution_table` and a standard-section fallback when no MSBS
  section passes.
- `include_trace` support in `cfs_engine` member strength/check readers.

### Changed
- **BREAKING:** `custom_n` renamed to `brace_n` across `unbraced_lengths`,
  `check_stud`, `design_wall`, `find_equivalent_profiles`, and batch member dicts
  (legacy `custom_n` still accepted in batch member dicts). It is a bracing-only
  parameter and never multiplies capacity.
- **BREAKING:** `wall_type` is now required for `find_wall_substitutions` and
  `design_wall`; `build_load_combinations` raises if it is missing. No silent
  `exterior` default anywhere in the call chain.
- `find_wall_substitutions` rewritten: library-driven nearest-depth matching (no
  hardcoded table), spacings default to `[original, 12, 16, 24]`, exhaustive
  matrix (no early stop), lightest-weight-per-ft selection preferring single over
  double, B2B-only doubles by default, and no auto-accept of P-passes/Mx-fails
  rows (returned flagged for both wall types).

### Fixed
- The `custom_n`/reference-capacity confusion: doubling the reference wall now has
  a real, tested effect via `reference_n` (≈2× capacity), instead of a parameter
  that silently only affected candidate bracing.
- The silent compression-entered-as-negative tension trap on `P_k`.

## [0.1.0] — 2026-07-10

Initial public release. Eleven tools driving the RSG CFS 15 Automation Module
(COM) for cold-formed steel wall-stud design and analysis integration.

### Added
- **Core engine** (`cfs_engine.py`): STA-safe `cfs_session()` per-call COM
  context manager (late-binding), spec/enum handling, SSMA library-path
  resolution, gross/effective properties, member strengths and unity checks,
  and a documented `NoteFlags` reference.
- **Tools**: `connect_cfs`, `parse_wall_specification`, `list_standard_profiles`,
  `list_custom_profiles`, `get_section_properties`, `check_stud`, `design_wall`,
  `find_equivalent_profiles`, `find_wall_substitutions`, `check_members_batch`,
  `export_section_for_analysis`.
- **`find_wall_substitutions`**: evaluates every MSBS profile (single + double)
  across a caller-ordered spacing sweep and returns a full ratio table (passing
  AND failing) with per-spacing `axial_ratio`/`flexural_ratio`/`stiffness_ratio`,
  P-only / Mx-only / combined max continuous spacings, and a `governing`
  (`both`/`axial`/`flexural`) selector for interior (no-wind) walls.
- **ETABS/SAP2000 integration**: `check_members_batch` (section-cached batch code
  check with honest zero-shear-capacity handling and unit/sign guards) and
  `export_section_for_analysis` (properties mapped to CSI `PropFrame.SetGeneral`).
- Packaging: installable `cfs_rsg_mcp` package, `cfs-rsg-mcp` console script,
  `python -m cfs_rsg_mcp` entry point, pytest suite, and MIT license.

### Fixed (from MCP-tester report)
- `parse_wall_specification`: compound feet-inch for both height and spacing
  (`2'-6"` → 30 in), height arithmetic (`10'-8" minus 4.5"`), and a `warnings`
  list for anything not fully parsed (no silent term-dropping).
- `check_members_batch`: accepts length keys nested in `lengths` OR at top level;
  `BendRadius`/zero-shear sentinel (UC 9.999) handled honestly.
- `find_wall_substitutions`: no longer auto-adds the reference spacing to the
  sweep; returns all profiles for engineer judgment.
- `design_wall` / `list_standard_profiles`: `structural_only` filter (skip S125
  non-structural drywall studs) and `flange_in` filter.
- `list_custom_profiles`: root-level folder discovery (SINGLE/B2B/BOX/TRUSS).
