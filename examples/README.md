# Section libraries

This server designs with **standard** profiles (SSMA/SFIA/AISI/HUD/LGSI — read
directly from the `.cfsl` libraries that ship with your CFS 15 install) and with
your own **custom `.cfss`** profiles.

The custom sections used during development (an MSBS single / back-to-back / box /
truss library) are **not distributed** with this repository — they are project
IP. Point the server at your own sections instead.

## Pointing the tools at your sections

Two ways:

1. **Per call** — pass a path to any tool that takes one:
   - `list_custom_profiles(directory="D:\\MySections")`
   - `find_equivalent_profiles(..., custom_directory="D:\\MySections")`
   - `find_wall_substitutions(..., msbs_directory="D:\\MySections")`
   - `check_members_batch` / `get_section_properties` accept full `.cfss` paths.

2. **Default folder** — set the `CFS_MCP_SECTIONS_DIR` environment variable to a
   folder that contains these subfolders, and the tools will discover them:

   ```
   <your sections dir>/
     SINGLE/   # single studs        (category: single, 1 member)
     B2B/      # back-to-back built-up (category: double, 2 members)
     BOX/      # box built-up         (category: double, 2 members)
     TRUSS/    # truss chords/webs    (category: truss)
   ```

   In a Claude Desktop / Qwen MCP config:

   ```json
   "env": { "CFS_MCP_SECTIONS_DIR": "D:\\MySections" }
   ```

Standard SSMA designations (e.g. `600S200-43`) need no configuration — they are
resolved from the CFS 15 install automatically.
