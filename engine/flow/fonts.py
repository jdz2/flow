"""Fonts Resolve's Text+ can use (from Fusion's font manager, or the font folders)."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

RECOMMENDED = ["Inter", "Helvetica Neue", "Montserrat", "Avenir Next", "Futura", "SF Pro Display",
               "Poppins", "Bebas Neue", "Anton", "Manrope", "DM Sans", "Space Grotesk", "Playfair Display",
               "Archivo", "Oswald", "Gilroy", "PP Mori", "Milligram"]
FONT_DIRS = [Path.home() / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
             Path("/System/Library/Fonts/Supplemental")]


def _from_files() -> Dict[str, List[str]]:
    fams: Dict[str, List[str]] = {}
    for d in FONT_DIRS:
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            stem = p.stem
            fam, _, sty = stem.partition("-")
            fams.setdefault(fam.replace("_", " "), []).append(sty or "Regular")
    return fams


def list_fonts(connect: bool = True) -> dict:
    fams: Dict[str, List[str]] = {}
    source = "files"
    if connect:
        try:
            from flow import resolve_bridge as rb
            fu = rb.connect().Fusion()
            fl = fu.FontManager.GetFontList() if fu else None
            if fl:
                fams = {k: sorted(v.keys()) if isinstance(v, dict) else [] for k, v in fl.items()}
                source = "resolve"
        except Exception:
            pass
    if not fams:
        fams = _from_files()
    lower = {k.lower().replace(" ", ""): k for k in fams}
    rec = []
    for name in RECOMMENDED:
        hit = lower.get(name.lower().replace(" ", ""))
        rec.append({"family": hit or name, "installed": bool(hit), "styles": fams.get(hit, []) if hit else []})
    return {"source": source, "recommended": rec, "families": sorted(fams)}


def format_fonts(data: dict, show_all: bool = False) -> str:
    lines = [f"fonts from {data['source']} · {len(data['families'])} families"]
    for r in data["recommended"]:
        mark = "✓" if r["installed"] else "·"
        styles = ", ".join(r["styles"][:8])
        lines.append(f"  {mark} {r['family']}" + (f"  ({styles})" if styles else "  (not installed)"))
    if show_all:
        lines.append("all: " + ", ".join(data["families"]))
    return "\n".join(lines)
