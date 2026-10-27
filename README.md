# RSG CFS MCP Server

An MCP server for **cold-formed steel (CFS) wall-stud design** that drives the
**RSG CFS 15 Automation Module** over COM. Every number comes from the real CFS
calculation engine (the same one behind the CFS desktop app) — nothing is
approximated in Python.

It gives an AI agent (Claude Desktop / Claude Code) tools to:

- Design stud walls using **standard SSMA profiles** (e.g. `600S200-43`).
- Find which of **your custom profiles** are structurally **equivalent** to a
  standard wall spec — e.g. *"which of my custom sections match a 10 ft wall of
  600S200-43 @ 16″ o.c.?"*

Built in the same spirit as the ETABS and Tekla MCP servers: a thin, honest
wrapper over a real engineering engine.

---

## Requirements

- **Windows** (COM).
- **Python 3.10+** with `pywin32` and `mcp`.
- **RSG CFS 15** installed and **licensed with the Automation option**
  (Single-User + Automation, or Network + Automation).
- The **CFS15 Automation Module** registered (`cfs15dllinst.exe`, run as admin).

Verify the connection any time with the `connect_cfs` tool.

---

## Install

```bat
git clone https://github.com/HuVelasco/cold-formed-steel-cfs-rsg-mcp.git
cd cold-formed-steel-cfs-rsg-mcp
pip install -e .            REM or: pip install -r requirements.txt
```

Run it (stdio MCP server):

```bat
python -m cfs_rsg_mcp       REM or the console script: cfs-rsg-mcp
```

Register with **Claude Desktop** — edit `claude_desktop_config.json` (see
`claude_desktop_config.example.json`), pointing `PYTHONPATH` at your clone and,
optionally, `CFS_MCP_SECTIONS_DIR` at your custom `.cfss` folder:

```json
{
  "mcpServers": {
    "cfs-rsg": {
      "command": "python",
      "args": ["-m", "cfs_rsg_mcp"],
      "env": {
        "PYTHONPATH": "C:\\path\\to\\cold-formed-steel-cfs-rsg-mcp",
        "PYTHONIOENCODING": "utf-8",
        "CFS_MCP_SECTIONS_DIR": "C:\\path\\to\\your\\sections"
      }
    }
  }
}
```

Restart Claude Desktop. For **Claude Code**:
`claude mcp add cfs-rsg -e PYTHONPATH=C:\path\to\repo -- python -m cfs_rsg_mcp`.

> **Sections:** standard SSMA designations need no setup. To use your own custom
> `.cfss` profiles, set `CFS_MCP_SECTIONS_DIR` or pass a path per call — see
> [`examples/README.md`](examples/README.md). The development section library is
> project IP and is **not** included in this repo.

---

## Tools

| Tool | Purpose |
|------|---------|
| `connect_cfs` | Check the COM connection, license, and available libraries. |
| `parse_wall_specification` | Parse `"10ft tall, 600S200-43 @ 16 O.C."` into fields. |
| `list_standard_profiles` | List SSMA/SFIA/AISI/HUD/LGSI designations (filter by type/depth). |
| `list_custom_profiles` | List your `.cfss` files in a folder. |
| `get_section_properties` | Gross section properties + fully-braced strengths for one section. |
| `check_stud` | AISI member check on one stud (explicit forces or ASD load combos); every combo, limit-state strengths, web crippling, deflection; pinned or cantilever. |
| `design_wall` | Try candidate sections, return the **lightest that passes** all ASD combos (and the deflection limit, if given). |
| `design_stacked_wall` | Multi-storey stacked wall: loads accumulated down the stack, ASCE 7-22 §4.7 live load reduction, AB/A/B live patterns, stud alignment; Simpson-style per-level summary. |
| `find_equivalent_profiles` | Which custom profiles ≥ a standard spec at a fixed spacing. |
| `find_wall_substitutions` | **Headline:** full ratio table of MSBS substitutions across spacings (pass & fail) for engineer judgment. |
| `check_members_batch` | Code-check many members with forces (ETABS/SAP2000 truss output). |
| `export_section_for_analysis` | Hand gross properties to ETABS/SAP2000 to define a frame section. |

### A "section" can be any of:

- an SSMA designation — `600S200-43` (33/50 ksi folder auto-selected);
- a custom `.cfss` file path;
- a full library path — `SSMA.cfsl|Structural Studs 33 ksi\600S200-43.cfss`.

**Specification** defaults to `AISI2016USASD` (enum `27`) = AISI S100-16 (2020)
w/S2-20, the edition referenced by IBC 2024 / IBC 2021 / BCNYS 2025. Every tool
echoes the spec it used (`spec_used`) and warns when it does not match
`building_code` (default `IBC2024`). Override with `spec="AISI2024USASD"` or an
int — but note S100-24 can overstate capacity against an S100-16 code of record
(362SLS155-43 single at 48" bracing: Pa 3.564 k vs 2.658 k).

**Watchdog.** `connect_cfs`, `check_stud`, `design_wall`, `design_stacked_wall`,
`find_wall_substitutions` and `check_members_batch` run CFS in an isolated child
process with a per-call timeout (`timeout_s`, default 60 s, or the
`CFS_MCP_TIMEOUT_S` env var). The CFS COM server is in-process .NET, so a hung call
cannot be interrupted in-thread; the watchdog kills the hung engine (the next call
gets a fresh one), returns the work finished so far as `partial_results`, and names
the section/combo that hung. `CFS_MCP_WATCHDOG=0` runs jobs inline for debugging.

---

## Example: the equivalency request

> *"Give me the equivalent profiles from my custom list for a wall that is 10 ft
> tall and 600S200-43 @ 16 O.C."*

```
find_equivalent_profiles(standard="600S200-43", height_ft=10, spacing_in=16)
```

CFS computes the standard stud's member **axial** strength (P), **flexural**
strength (Mx), and **stiffness** (Ix) at the wall's unbraced condition, then
evaluates each custom profile the same way. A profile is **equivalent** when it
meets or exceeds the standard on both axial and flexural strength. Output is
ranked (equivalents lightest-first) with per-metric ratios, weight, and the
`max_spacing_in` at which each custom would match the standard's per-foot
capacity.

Custom profiles come from your configured sections folder (`CFS_MCP_SECTIONS_DIR`,
its `SINGLE` subfolder by default); point elsewhere with `custom_directory=...`
(add `recursive=True` to include `B2B / BOX / TRUSS`) or pass an explicit
`custom_profiles=[...]` list. See [`examples/README.md`](examples/README.md).

---

## Example: MSBS wall substitutions (single/double + spacing)

> *"I have a wall 10 ft tall, 600S162-43 @ 16, give me possible substitutions
> using MSBS sections."*

```
find_wall_substitutions(standard="600S162-43", height_ft=10, wall_type="exterior",
    spacing_in=16)
```

It tries **single** studs (`SINGLE`) and **back-to-back doubles** (`B2B`), each at
every candidate **stud spacing** and gauge at the matched depth, builds the full
matrix, and **selects the most efficient passing wall** — the lightest weight per
ft² (`weight_plf × 12/spacing`), preferring a single over a double at comparable
weight. Member strength depends only on the section and unbraced length (not
spacing), so each section is evaluated by CFS once and spacings are swept
analytically (every ratio scales by `ref_spacing / s`).

`wall_type` (`"interior"` / `"exterior"`) is **required** — it is never silently
defaulted. Key controls, matching how walls are actually detailed:

- **Spacings** default to `[spacing_in, 12, 16, 24]`. Override with an *exact*
  ordered `allowed_spacings` list — e.g. `[30, 15]` for a 30″ sheet width with a
  15″ fallback. The whole list is always evaluated (no early stop).
- **`reference_n=2`** — the wall being replaced is a *double* standard build-up;
  the reference P/Mx/Ix/weight are multiplied by N plies (idealized, flagged).
- **`include_boxed=True`** — also consider `BOX` doubles (B2B only by default).
- **`categories=["single"]`** — when doubles are not allowed at all.
- **`adjacent_spacing_in` + `load_transfer_present`** — cross-floor stud-alignment
  check: if the selected spacing doesn't stack with the level above/below and there
  is no load-transfer member, the result is flagged.
- **`governing`** — what counts as a pass for selection: `"both"` (P **and** Mx,
  default), `"axial"` (P only — interior/no-wind), or `"flexural"`. A row that
  passes P but fails Mx is **never auto-accepted** — it is returned flagged for
  both wall types so you judge it.

```
# Interior wall, no wind, single sections, sheet-friendly spacing:
find_wall_substitutions(standard="400S300-68", height_ft=10, wall_type="interior",
    spacing_in=30, brace_config="quarter", allowed_spacings=[30, 15],
    categories=["single"], depth_filter_in=[3.62], governing="axial")
```

Output always includes a plain-language **`substitution_table`** (original →
substitute, spacing, category, P/Mx ratios, and a readable status), the
**`selected`** wall with actual capacities and required values, and **`profiles`**
(per-candidate `by_spacing` ratios + `NoteFlags`). Pass `include_all_configs=True`
for the full **audit table** of every tried row (with the single `most_efficient`
row marked), and `include_trace=True` for CFS's calculation trace. If no MSBS
section passes at any spacing, the search **falls back to standard sections** at
the matched depth (`category: "standard"`) — never a silent empty result.

---

## Example: ETABS / SAP2000 integration

Two directions, so CFS and a global analysis model can work together:

**1. Code-check analysis output** — feed frame/truss forces (e.g. from the ETABS
MCP) straight into CFS:

```
check_members_batch(members=[
  {"id": "T1", "section": "600S200-43",
   "forces": {"P_k": 8.0, "Mx_kin": 2.0, "Vy_k": 0.1}, "Lu_in": 48},
  {"id": "D1", "section": "362S162-54",
   "forces": {"P_k": -2.0, "Mx_kin": 1.0}, "length_ft": 6},
  ...
])
```

Each member carries its section, forces, and unbraced lengths (explicit
`Lx_in/Ly_in/Lt_in/Lm_in`, a single `Lu_in`, or `length_ft` + `brace_config`).
Sections load once and are reused across members that share them, so a large
truss checks fast. Returns per-member unity check + PASS/FAIL + `NoteFlags`, and
an overall governing UC.

**2. Define the section in ETABS/SAP2000** — hand the analysis model the
properties it needs:

```
export_section_for_analysis(section="600S200-43")
```

Returns gross properties in the exact argument order of the CSI API
`PropFrame.SetGeneral` (SAP2000 and ETABS share it): `t3, t2, Area, As2, As3, J,
I33, I22, S33, S22, Z33, Z22, R33, R22`. Shear areas are `0` (rigid) because CFS
governs shear/web-crippling through its own checks — keep shear design in CFS,
not the analysis model. Use kip-inch units in the model to match.

---

## How it maps loads to a stud

Adapted from a validated wall-stud design workflow. A stud is a
simply-supported beam-column:

- **Axial:** `P_stud (k) = PSF × trib_ft × spacing_ft / 1000` — for D, L **and**
  the roof loads Lr / S / R (over `trib_A + trib_B`; exterior walls use `trib_A`).
  Line loads (plf) and point loads (k/stud) at the top of the wall are supported.
- **Lateral:** `w = PSF × spacing_ft`; pinned-pinned `Mx = w·H²/8`, `Vy = w·H/2`;
  cantilever (parapet) `Mx = w·H²/2`, `Vy = w·H`, `Kx = 2.1`.
- **Unbraced lengths:** `Lx` = full height always; `Ly = Lt = Lm` set by bridging
  (`none / midpoint / third / quarter / 48in / 60in / 72in / custom`).
- **Combos:** built-in `ASCE7-22_ASD` (default; §2.4.1 Eqs. 1a–7a + §2.4.5 Eqs.
  8–10), `ASCE7-16_ASD`, or `IBC2024_ALT_ASD` (IBC §1605.2 Eqs. 16-1…16-6), each
  row labelled with its code reference, plus optional `user_combos`
  (`combo_mode = builtin | user | append`). Every combo is returned with P, M, V, UC.
- **Load levels:** wind and snow default to **strength level** (the combos apply
  0.6W, 0.7S and 0.1S); pass `wind_level="asd"` / `snow_level="asd"` if your inputs
  are already ASD. Eh / Ev are strength level (Eh × D_wall lateral, Ev × D axial).
  `load_level_factors` and each combo's `factors` echo exactly what was applied.
- **Wind:** MWFRS pressures (`W_mwfw`, `W_mwfl`) combine with axial load; C&C
  (`W_cc_w`, `W_cc_l`) are checked in bending alone and used for deflection.
- **Roof:** each combo uses **one** of Lr / 0.7S / R — whichever governs — never
  their sum.

---

## Notes & honesty

- This tool reports exactly what CFS returns. If a section fails to load or a
  check errors, you get an honest error — never a fabricated pass.
- **Deflection** (`check_stud`, `design_wall`, `design_stacked_wall`) uses the
  CFS effective Ix at the service moment; C&C wind is taken as
  `wind_defl_modifier × 0.6W` (default 0.7 → 0.42W, IBC Table 1604.3 note f) and
  interior L_lat at 1.0. It is always reported (Δ and L/x); pass/fail only when
  `defl_limit` is given. `find_equivalent_profiles` still reports only the Ix ratio.
- **Limit-state strengths** (Pa-Dist, Ma-Brc, Ma-Dist, governing axial buckling
  mode) are read from CFS's own calculation trace and reconciled against CFS's
  member strength; a mismatch is flagged, never hidden.
- **Web crippling** at the stud end uses CFS `WebCheck` with the maximum lateral
  end reaction (`bearing_length_in`, default 1.0 in, fastened); UC > 1 is reported
  as *stiffener required*.
- A `.cfss` file with a **corrupt embedded date field** cannot be loaded by CFS —
  that is a data problem in the file, not the server; re-export it from CFS. The
  server reports the honest load error rather than skipping silently.
- Custom `.cfss` files often carry a **stale embedded description**; the server
  therefore labels custom sections by **filename**, not the embedded description.
- **Project structure**: server code lives in the `cfs_rsg_mcp/` package
  (`server.py` tools · `jobs.py` COM job bodies · `watchdog.py` process isolation ·
  `cfs_engine.py` COM layer · `cfs_loads.py` loads/combos · `stacked.py` stacked
  walls · `catalog.py` catalogs/parsing). Tests in `tests/` (`pytest -m "not live"` runs
  without CFS; `pytest -m live` needs a licensed CFS 15).

---

*Author: Hugo Velasco (Hugo VelP). Version 0.1.0 (maturity 0.1.0.3) — developed; pending MCP-tester
and user validation before production use.*
