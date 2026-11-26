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
        raise FlowError(f"Style inheritance loop at '{sid}' (check its \"extends\")")
    seen.add(sid)
    if sid not in styles:
        raise FlowError(f"Unknown style '{sid}'. Available: {', '.join(sorted(styles))}")
    raw = styles[sid]
    parent = raw.get("extends")
    if parent:
        try:
            pid = match_style_id(parent, styles)   # "Talking Head+" works as well as "talking-head-plus"
        except FlowError:
            raise FlowError(f"Style '{sid}' extends unknown style '{parent}'. "
                            f"Available: {', '.join(sorted(styles))}") from None
        if pid == sid:
            # a custom file that shadows a built-in and extends it: resolve the built-in underneath
            builtin = _load_dir(paths.BUILTIN_STYLES_DIR, "builtin")
            if sid not in builtin:
                raise FlowError(f"Style '{sid}' extends itself")
            return deep_merge(copy.deepcopy(builtin[sid]), {k: v for k, v in raw.items() if k != "extends"})
        base = _resolve(pid, styles, seen)
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


_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
ENUMS = {
    ("captions", "case"): ("none", "upper", "lower"),
    ("hook", "case"): ("none", "upper", "lower"),
    ("captions", "animation"): ("pop", "pop_strong", "fade", "slide", "tracking", "none"),
    ("hook", "animation"): ("pop", "pop_strong", "fade", "slide", "tracking", "none"),
    ("transitions", "type"): ("none", "smooth_cut", "dissolve"),
    ("framing", "reframe"): ("subject", "smart", "center"),
    ("split_edits", "type"): ("J", "L", "j", "l", "alternate"),
    ("zoom", "mode"): ("auto", "none"),
}


def validate_style(style: dict) -> List[str]:
    """Problems that would otherwise only show up mid-build in Resolve (or as an ungraded timeline)."""
    errs: List[str] = []
    for sec in REQUIRED_SECTIONS:
        if sec not in style:
            errs.append(f"missing section '{sec}'")
        elif not isinstance(style[sec], dict):
            errs.append(f"'{sec}' must be an object, got {type(style[sec]).__name__}")
    for sec in ("captions", "hook"):
        cfg = style.get(sec)
        if not isinstance(cfg, dict):
            continue
        for key in ("color", "accent"):
            v = cfg.get(key)
            if v is not None and not (isinstance(v, str) and _HEX.match(v)):
                errs.append(f"{sec}.{key} must be a hex colour like #FFFFFF, got {v!r}")
        for key in ("outline", "shadow", "box"):
            v = cfg.get(key)
            if isinstance(v, dict) and v.get("color") is not None and not _HEX.match(str(v["color"])):
                errs.append(f"{sec}.{key}.color must be a hex colour, got {v['color']!r}")
        pos = cfg.get("position")
        if pos is not None and not (isinstance(pos, (list, tuple)) and len(pos) == 2
                                    and all(isinstance(x, (int, float)) and 0.0 <= x <= 1.0 for x in pos)):
            errs.append(f"{sec}.position must be [x, y] with both in 0..1, got {pos!r}")
        size = cfg.get("size")
        if size is not None and not (isinstance(size, (int, float)) and 0.0 < size < 0.5):
            errs.append(f"{sec}.size must be a number between 0 and 0.5, got {size!r}")
    for (sec, key), allowed in ENUMS.items():
        cfg = style.get(sec)
        if isinstance(cfg, dict) and cfg.get(key) is not None and cfg[key] not in allowed:
            errs.append(f"{sec}.{key} must be one of {', '.join(allowed)}, got {cfg[key]!r}")
    g = style.get("grade")
    if isinstance(g, dict) and g.get("look") and not g.get("lut"):
        from flow import luts
        try:
            luts.normalize_look(str(g["look"]))
        except ValueError as exc:
            errs.append(f"grade.look: {exc}")
    return errs


def check_style(style: dict, name: str) -> dict:
    errs = validate_style(style)
    if errs:
        raise FlowError(f"Style '{name}' has problems: " + "; ".join(errs))
    return style


def get_style(name: str) -> dict:
    styles = all_styles()
    return check_style(_resolve(match_style_id(name, styles), styles), name)


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
    styles = all_styles()                 # validate before anything is written
    styles[sid] = dict(out, _source="custom")
    check_style(_resolve(sid, styles), name)
    path = paths.USER_STYLES_DIR / f"{sid}.json"
    write_json(path, out)
    return path
