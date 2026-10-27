# cfs-rsg-mcp

> **MCP server connecting Claude (and any MCP client) to RSG CFS 15 — AI-driven cold-formed steel stud, track, and wall design.**

**By [Hugo Velasco](https://github.com/HuVelasco)** · [dualitum.com](https://dualitum.com)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-FastMCP-green)](https://github.com/jlowin/fastmcp)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)](https://www.rsgsoftware.com)

---

## What It Does

cfs-rsg-mcp lets Claude drive RSG CFS 15 for cold-formed steel design — stud selection, wall assembly, stacked walls, and section property lookups — all from natural language.

**Example prompt:** *"Find the lightest 600S stud that passes AISI S100 for a 12-foot wall at 16" o.c. with 500 lb/ft axial and 10 psf lateral."*

---

## Tools

### 🔗 Connection
| Tool | Description |
|---|---|
| `connect_cfs` | Connect to running RSG CFS 15 instance |

### 📐 Section Library
| Tool | Description |
|---|---|
| `list_standard_profiles` | All standard CFS profiles (studs, tracks, angles, channels) |
| `list_custom_profiles` | User-defined profiles in the current database |
| `get_section_properties` | Full properties for a section (Ix, Sx, weight, gauge, etc.) |
| `export_section_for_analysis` | Export section data for use in other calculations |

### ✅ Design Checks
| Tool | Description |
|---|---|
| `check_stud` | AISI S100 check for a single stud (axial + bending + combined) |
| `check_members_batch` | Batch check multiple studs in one call |
| `design_wall` | Full wall assembly design — find passing studs for a wall spec |
| `design_stacked_wall` | Design stacked wall with load transfer between stories |

### 🔍 Optimization
| Tool | Description |
|---|---|
| `parse_wall_specification` | Parse natural-language wall spec into structured parameters |
| `find_wall_substitutions` | Find lighter or cheaper alternatives to a current wall design |
| `find_equivalent_profiles` | Find profiles with equivalent structural properties |

---

## Quickstart

### Prerequisites
- RSG CFS 15 installed
- Python 3.10+

### Install

```bash
git clone https://github.com/HuVelasco/cfs-rsg-mcp.git
cd cfs-rsg-mcp
pip install -r requirements.txt
```

### Claude Desktop config

```json
{
  "mcpServers": {
    "cfs-rsg": {
      "command": "python",
      "args": ["C:/path/to/cfs-rsg-mcp/server.py"]
    }
  }
}
```

---

## Example Workflow

```
User: Design a 10-ft exterior wall, 16" o.c., 600 lb/ft axial, 12 psf lateral, deflection L/360

Claude: parse_wall_specification → design_wall → 600S162-54 passes (DCR=0.87)
        find_wall_substitutions  → 600S162-43 fails (DCR=1.12), keep -54
```

---

## License

MIT © 2026 Hugo Velasco. See [LICENSE](LICENSE).

All structural outputs must be reviewed by a licensed professional engineer.

---

*Part of the [Hugo Velasco](https://github.com/HuVelasco) AEC automation suite · [dualitum.com](https://dualitum.com)*
