# FILE: catalog.py
# PURPOSE: Enumerate standard (SSMA) profiles, list custom .cfss libraries, and
#          parse free-text wall specifications like "10ft tall, 600S200-43 @ 16 O.C."
# STATUS: Version 0.1.0.2 - Developed (Phase 1 fix pass); not yet MCP re-validated
# AUTHOR: Hugo Velasco (Hugo VelP)

from __future__ import annotations

import glob
import os
import re
from typing import Dict, List, Optional

from .cfs_engine import parse_designation, standard_library_path

# Location of the custom profile library. Override with the CFS_MCP_SECTIONS_DIR
# environment variable (recommended for a fresh clone — the bundled example
# sections are NOT shipped in the public repo). Falls back to the repo-root
# `Info-and-Examples/Sections` folder used during development.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECTIONS_ROOT = os.environ.get(
    "CFS_MCP_SECTIONS_DIR",
    os.path.join(_REPO_ROOT, "Info-and-Examples", "Sections"),
)
DEFAULT_CUSTOM_DIR = os.path.join(_SECTIONS_ROOT, "SINGLE")

# MSBS (the user's custom system) organizes sections by build-up type into
# subfolders. Category tells the design tools how many members share the load
# at each stud location: 'single' = 1, 'double' (back-to-back / box) = 2.
MSBS_CATEGORY_BY_FOLDER = {
    "SINGLE": ("single", 1),
    "B2B": ("double", 2),
    "BOX": ("double", 2),
    "TRUSS": ("truss", 1),
}

_CFSS_RE = re.compile(rb"([0-9]{2,4}[STUF][0-9]{2,4}-[0-9]{2,3})\.cfss", re.IGNORECASE)


# SSMA non-structural (drywall) studs use a 1-1/4" flange (S125 series);
# structural load-bearing studs use flanges >= 1-3/8" (137, 162, 200, 250, 300,
# 350). structural_only filters out the S125 non-structural series.
_MIN_STRUCTURAL_FLANGE_1_100IN = 137


def list_standard_designations(library: str = "SSMA",
                               type_letter: Optional[str] = None,
                               depth_in: Optional[float] = None,
                               flange_in: Optional[float] = None,
                               structural_only: bool = False) -> List[str]:
    """Enumerate SSMA-style designations found inside the .cfsl library file.

    We read the binary library and pull out every '<designation>.cfss' token.
    Optional filters:
      type_letter     'S','T','U','F'
      depth_in        nominal web depth in inches (e.g. 6.0)
      flange_in       nominal flange width in inches (e.g. 2.5)
      structural_only drop non-structural drywall studs (S125 / 1-1/4" flange)
    """
    path = standard_library_path(library)
    data = open(path, "rb").read()
    found = {m.group(1).decode("ascii").upper() for m in _CFSS_RE.finditer(data)}

    out = []
    for d in found:
        p = parse_designation(d)
        if not p:
            continue
        if type_letter and p["type_letter"] != type_letter.upper():
            continue
        if depth_in is not None and abs(p["depth_in"] - depth_in) > 1e-6:
            continue
        if flange_in is not None and abs(p["flange_in"] - flange_in) > 1e-6:
            continue
        if structural_only and p["type_letter"] == "S" \
                and p["flange_1_100in"] < _MIN_STRUCTURAL_FLANGE_1_100IN:
            continue
        out.append(d)

    def sort_key(d):
        p = parse_designation(d)
        return (p["depth_in"], p["type_letter"], p["flange_1_100in"], p["mils"])

    return sorted(out, key=sort_key)


def list_custom_profiles(directory: Optional[str] = None,
                         recursive: bool = False) -> List[Dict]:
    """List .cfss files in a directory (the user's custom profiles).

    Returns [{name, path, folder}] sorted by path. `folder` is the immediate
    parent folder name, so sibling libraries (SINGLE / B2B / BOX / TRUSS) are
    distinguishable in one listing.

    `directory` defaults to the bundled Info-and-Examples/Sections ROOT (so a
    recursive scan surfaces every sibling library, not just SINGLE). Pass an
    explicit directory to scope the search.
    """
    directory = directory or _SECTIONS_ROOT
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Custom profile directory not found: {directory}")
    # Default root has sub-libraries → scan recursively so nothing is hidden.
    scan_recursive = recursive or (directory == _SECTIONS_ROOT)
    pattern = os.path.join(directory, "**", "*.cfss") if scan_recursive \
        else os.path.join(directory, "*.cfss")
    files = sorted(glob.glob(pattern, recursive=scan_recursive))
    return [{"name": os.path.splitext(os.path.basename(f))[0], "path": f,
             "folder": os.path.basename(os.path.dirname(f))} for f in files]


def list_msbs_sections(base_dir: Optional[str] = None,
                       categories: Optional[List[str]] = None) -> List[Dict]:
    """List MSBS custom sections across their build-up subfolders.

    Returns [{name, path, folder, category, members_per_stud}] where category is
    'single' | 'double' | 'truss'. `categories` filters by category name
    (default: single + double, i.e. wall studs; excludes truss chords).
    base_dir defaults to the bundled Info-and-Examples/Sections root.
    """
    base_dir = base_dir or _SECTIONS_ROOT
    if not os.path.isdir(base_dir):
        raise FileNotFoundError(f"MSBS sections root not found: {base_dir}")
    wanted = categories or ["single", "double"]
    out: List[Dict] = []
    for folder, (cat, n) in MSBS_CATEGORY_BY_FOLDER.items():
        if cat not in wanted:
            continue
        d = os.path.join(base_dir, folder)
        if not os.path.isdir(d):
            continue
        for f in sorted(glob.glob(os.path.join(d, "*.cfss"))):
            out.append({
                "name": os.path.splitext(os.path.basename(f))[0],
                "path": f, "folder": folder,
                "category": cat, "members_per_stud": n,
            })
    return out


# ---------------------------------------------------------------------------
# Free-text wall spec parsing
# ---------------------------------------------------------------------------
_DESIG_TOKEN_RE = re.compile(r"\d{2,4}[STUF]\d{2,4}-\d{2,3}", re.IGNORECASE)

# A feet-and-inches measurement, e.g. 10'-8", 2'-6", 9' 6", 16", 30, 6 ft
_MEAS_RE = re.compile(
    r"""(?:(?P<ft>\d+(?:\.\d+)?)\s*(?:'|ft\b|feet\b|foot\b))?      # optional feet
        \s*[-\s]?\s*
        (?:(?P<in>\d+(?:\.\d+)?)\s*(?:\"|in\b|inch(?:es)?\b))?     # optional inches
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Spacing anchored on "o.c." / "on center"; capture the measurement in front of it
_SPACING_RE = re.compile(
    r"""@?\s*
        (?:(?P<ft>\d+(?:\.\d+)?)\s*(?:'|ft\b|feet\b)\s*[-\s]?\s*)?   # optional feet
        (?P<in>\d+(?:\.\d+)?)\s*(?:\"|in\b|inch(?:es)?)?             # inches (or bare)
        \s*(?:o\.?\s*c\.?|on[-\s]?center)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Leading feet(-inch) height, e.g. "10'-8"", "10 ft", "9'"
_HEIGHT_RE = re.compile(
    r"(?P<ft>\d+(?:\.\d+)?)\s*(?:'|ft\b|feet\b|foot\b)"
    r"(?:\s*[-\s]\s*(?P<in>\d+(?:\.\d+)?)\s*(?:\"|in\b|inch(?:es)?)?)?",
    re.IGNORECASE,
)
# Arithmetic adjustment after a height, e.g. "minus 4.5\"", "less 3 in", "+ 2\""
_ADJ_RE = re.compile(
    r"(?P<op>minus|less|plus|\+|-)\s*(?P<val>\d+(?:\.\d+)?)\s*(?:\"|in\b|inch(?:es)?)?",
    re.IGNORECASE,
)


def parse_wall_spec(text: str) -> Dict:
    """Parse a spec like "10'-8\" minus 4.5\", 400S300-68 @ 2'-6\" O.C." into fields.

    Returns {height_ft, spacing_in, designation, raw, warnings}. Handles:
      * feet-inch heights and spacings (2'-6" -> 30 in, not 6 in);
      * simple height arithmetic ("10'-8\" minus 4.5\"");
      * a `warnings` list flagging anything it could not fully interpret (so an
        upstream design tool never silently uses wrong geometry).
    """
    text = text or ""
    result = {"height_ft": None, "spacing_in": None, "designation": None,
              "raw": text, "warnings": []}
    consumed = [False] * len(text)  # track spans we understood

    def mark(a, b):
        for i in range(a, min(b, len(consumed))):
            consumed[i] = True

    m = _DESIG_TOKEN_RE.search(text)
    if m:
        result["designation"] = m.group(0).upper()
        mark(*m.span())

    # -- Spacing (anchored on o.c.) --
    ms = _SPACING_RE.search(text)
    if ms:
        inches = float(ms.group("in"))
        if ms.group("ft"):
            inches += float(ms.group("ft")) * 12.0
        result["spacing_in"] = round(inches, 4)
        mark(*ms.span())

    # -- Height (ignore the spacing span) --
    search_text = text
    if ms:
        search_text = text[:ms.start()] + " " * (ms.end() - ms.start()) + text[ms.end():]
    mh = _HEIGHT_RE.search(search_text)
    if mh:
        inches = float(mh.group("ft")) * 12.0
        if mh.group("in"):
            inches += float(mh.group("in"))
        mark(*mh.span())
        # optional arithmetic adjustment immediately following the height
        tail = search_text[mh.end():]
        ma = _ADJ_RE.match(tail.lstrip())
        if ma:
            val = float(ma.group("val"))
            op = ma.group("op").lower()
            inches += -val if op in ("minus", "less", "-") else val
            # mark the adjustment span in the original text
            off = mh.end() + (len(tail) - len(tail.lstrip()))
            mark(off, off + ma.end())
        result["height_ft"] = round(inches / 12.0, 4)

    # -- Warn about leftover numeric/arith tokens we did not consume --
    for tok in re.finditer(r"(?:minus|less|plus)\b|\d+(?:\.\d+)?\s*(?:'|\"|ft|in|inch(?:es)?)\b",
                            text, re.IGNORECASE):
        a, b = tok.span()
        if not any(consumed[a:b]):
            result["warnings"].append(
                f"Unparsed token '{tok.group(0).strip()}' at position {a}; "
                f"verify height/spacing manually."
            )
    return result
