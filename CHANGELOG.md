# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Maturity note: this package also carries an internal 4-part engineering
> maturity marker (see `CLAUDE.md`): `0.x.0.y` = developed, `0.x.y.0` =
> MCP-tester validated, `0.x.y.z` + user validation = production. The public
> SemVer below tracks releases; the maturity marker tracks trust.

## [Unreleased]

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
