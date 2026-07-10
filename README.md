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
| `check_stud` | AISI member check on one stud (explicit forces or ASD load combos). |
| `design_wall` | Try candidate sections, return the **lightest that passes** all ASD combos. |
| `find_equivalent_profiles` | Which custom profiles ≥ a standard spec at a fixed spacing. |
| `find_wall_substitutions` | **Headline:** full ratio table of MSBS substitutions across spacings (pass & fail) for engineer judgment. |
| `check_members_batch` | Code-check many members with forces (ETABS/SAP2000 truss output). |
| `export_section_for_analysis` | Hand gross properties to ETABS/SAP2000 to define a frame section. |

### A "section" can be any of:

- an SSMA designation — `600S200-43` (33/50 ksi folder auto-selected);
- a custom `.cfss` file path;
- a full library path — `SSMA.cfsl|Structural Studs 33 ksi\600S200-43.cfss`.

**Specification** defaults to `AISI2024USASD` (enum `42`). Override with
`spec="AISI2022USASD"` or an int on any design tool.

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
find_wall_substitutions(standard="600S162-43", height_ft=10, spacing_in=16)
```

It tries **single** studs (`SINGLE`) and **double** built-ups (`B2B` back-to-back,
`BOX`), each at every allowed **stud spacing**, and returns **every profile with
its full ratio table — passing and failing** — because the engineer, not the tool,
owns the judgment call. Member strength depends only on the section and unbraced
length (not spacing), so each section is evaluated by CFS once and spacings are
swept analytically (every ratio scales by `ref_spacing / s`).

Key controls, matching how walls are actually detailed:

- **`allowed_spacings`** — an *ordered* list, widest first, used **exactly as
  given** (the reference spacing is never auto-added). For sheet-driven layouts:
  `allowed_spacings=[30, 15]` — 30″ (sheet width) preferred, 15″ as the fallback.
- **`categories=["single"]`** — when back-to-back / box doubles are not allowed.
- **`governing`** — which check must pass for a spacing to count:
  `"both"` (P **and** Mx, default — exterior / wind walls), `"axial"` (P only —
  **interior wall with no wind**, where Mx demand is zero), or `"flexural"`.
  Both `axial_ratio` and `flexural_ratio` are **always** reported regardless, so a
  case where P passes but Mx doesn't is visible for you to judge.

```
# Interior wall, no wind, single sections, sheet-friendly spacing:
find_wall_substitutions(standard="400S300-68", height_ft=10, spacing_in=30,
    brace_config="quarter", allowed_spacings=[30, 15],
    categories=["single"], depth_filter_in=[3.62], governing="axial")
```

Each profile returns: capacity ratios, `max_continuous_spacing_axial/flexural/
combined_in`, a `by_spacing` breakdown (P/Mx/Ix/weight ratio + `axial_ok`/
`flexural_ok`/`passes` at every spacing), `best_spacing_in` (widest passing under
`governing`, or null), weight vs. original, and `NoteFlags` with plain-English
notes. Profiles are ranked passing-first (lightest), then failing by how close.
When nothing passes, the message reports the **closest** section and its max
reachable spacing — never a bare "unsuitable".

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

- **Axial:** `P_stud (k) = PSF × trib_ft × spacing_ft / 1000`
- **Lateral (wind/seismic):** `w = PSF × spacing_ft`; `Mx = w·H²/8`, `Vy = w·H/2`
- **Unbraced lengths:** `Lx` = full height always; `Ly = Lt = Lm` set by bridging
  (`none / midpoint / third / quarter / 48in / 60in / 72in / custom`).
- **Combos:** the 10 ASCE-7 ASD combinations; the governing unity check wins.

---

## Notes & honesty

- This tool reports exactly what CFS returns. If a section fails to load or a
  check errors, you get an honest error — never a fabricated pass.
- **Serviceability (deflection) is not auto-checked** by the strength unity
  checks. `find_equivalent_profiles` reports the stiffness (Ix) ratio so you can
  judge deflection; run `check_stud` with `DxMax/DyMax`-style limits in CFS for a
  formal L/240 etc. check.
- A `.cfss` file with a **corrupt embedded date field** cannot be loaded by CFS —
  that is a data problem in the file, not the server; re-export it from CFS. The
  server reports the honest load error rather than skipping silently.
- Custom `.cfss` files often carry a **stale embedded description**; the server
  therefore labels custom sections by **filename**, not the embedded description.
- **Project structure**: server code lives in the `cfs_rsg_mcp/` package
  (`server.py` tools · `cfs_engine.py` COM layer · `cfs_loads.py` load paths ·
  `catalog.py` catalogs/parsing). Tests in `tests/` (`pytest -m "not live"` runs
  without CFS; `pytest -m live` needs a licensed CFS 15).

---

*Author: Hugo Velasco (Hugo VelP). Version 0.1.0 — developed; pending MCP-tester
and user validation before production use.*
