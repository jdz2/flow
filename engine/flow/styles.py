"""Editing styles: built-in presets (styles/*.json) plus user styles (~/.flow/styles)."""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow import paths
from flow.util import FlowError, read_json, write_json

REQUIRED_SECTIONS = ("pacing", "framing", "captions", "hook", "zoom", "ramps", "effects",
                     "masks", "transitions", "grade", "audio")


def deep_merge(base: Any, over: Any) -> Any:
    """Merge `over` into a copy of `base`. Dicts merge, everything else replaces."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = copy.deepcopy(base)
        for k, v in over.items():
            out[k] = deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def slugify(name: str) -> str:
    s = name.lower().replace("+", " plus ").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "my-style"


def _load_dir(d: Path, source: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = read_json(p)
        except Exception:
            continue
        sid = data.get("id") or p.stem
        data["id"] = sid
        data["_source"] = source
        data["_path"] = str(p)
        out[sid] = data
    return out


def all_styles() -> Dict[str, dict]:
    styles = _load_dir(paths.BUILTIN_STYLES_DIR, "builtin")
    styles.update(_load_dir(paths.USER_STYLES_DIR, "custom"))
    return styles


def _resolve(sid: str, styles: Dict[str, dict], seen: Optional[set] = None) -> dict:
    seen = seen or set()
    if sid in seen:
        raise FlowError(f"Style inheritance loop at '{sid}'")
    seen.add(sid)
    if sid not in styles:
        raise FlowError(f"Unknown style '{sid}'. Available: {', '.join(sorted(styles))}")
    raw = styles[sid]
    parent = raw.get("extends")
    if parent:
        base = _resolve(parent, styles, seen)
        merged = deep_merge(base, {k: v for k, v in raw.items() if k != "extends"})
        merged["extends"] = parent
        return merged
    return copy.deepcopy(raw)


def match_style_id(name: str, styles: Optional[Dict[str, dict]] = None) -> str:
    styles = styles or all_styles()
    if name in styles:
        return name
    for sid, s in styles.items():  # exact display name first ("Talking Head+")
        if (s.get("name") or "").strip().lower() == name.strip().lower():
            return sid
    key = slugify(name)
    if key in styles:
        return key
    for sid, s in styles.items():
        if slugify(s.get("name", "")) == key:
            return sid
    aliases = {"simplicity": "clean", "minimal": "clean", "simple": "clean",
               "talking-head-pro": "talking-head-plus", "advanced-talking-head": "talking-head-plus",
               "simple-talking-head": "talking-head", "yt-shorts": "shorts", "youtube-shorts": "shorts",
               "tiktok": "shorts", "reels": "shorts", "brand": "business", "film": "cinematic"}
    if key in aliases and aliases[key] in styles:
        return aliases[key]
    raise FlowError(f"Unknown style '{name}'. Available: {', '.join(sorted(styles))}")


def get_style(name: str) -> dict:
    styles = all_styles()
    style = _resolve(match_style_id(name, styles), styles)
    missing = [s for s in REQUIRED_SECTIONS if s not in style]
    if missing:
        raise FlowError(f"Style '{name}' is missing sections: {', '.join(missing)}")
    return style


def list_styles() -> List[dict]:
    styles = all_styles()
    rows = []
    for sid, raw in styles.items():
        rows.append({"id": sid, "name": raw.get("name", sid), "description": raw.get("description", ""),
                     "source": raw.get("_source"), "order": raw.get("order", 100)})
    rows.sort(key=lambda r: (r["source"] != "builtin", r["order"], r["name"]))
    return rows


def save_user_style(data: dict, base: Optional[str] = None) -> Path:
    """Save a custom style. With `base`, only the differences need to be given."""
    name = data.get("name") or data.get("id")
    if not name:
        raise FlowError("A custom style needs a name.")
    sid = slugify(data.get("id") or name)
    builtin = _load_dir(paths.BUILTIN_STYLES_DIR, "builtin")
    if sid in builtin:
        sid = f"{sid}-custom"
    out = {k: v for k, v in data.items() if not k.startswith("_")}
    out["id"] = sid
    out["name"] = name
    if base:
        out["extends"] = match_style_id(base)
    out.setdefault("description", "Custom style")
    path = paths.USER_STYLES_DIR / f"{sid}.json"
    write_json(path, out)
    get_style(sid)  # validate it resolves
    return path
