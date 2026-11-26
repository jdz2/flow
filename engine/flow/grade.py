"""Per-clip grading: look at every clip, measure its color problems, grade it accordingly.

For each footage clip:
  1. export the clip's own frame (with its current grade) via TimelineItem.ExportFrame
  2. measure: share of harsh blue/violet, green, magenta, warm/skin pixels, brightness
  3. pick a look strength (more correction where club lights are harsh)
  4. put the look on node 1. If node 1 already holds a LUT (log conversion, highlight fix…),
     the look is COMPOSED after it into one new LUT, so the existing transform is kept.
"""
from __future__ import annotations

import colorsys
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow import luts, paths
from flow.util import which

# bump when a look's math changes, so older timelines keep their own LUT files
LOOK_REV = {"nightlife": "5"}
SOFT_REV = "3"   # bump when the soft variant's math changes

LUT_ROOTS = [paths.RESOLVE_LUT_DIR,
             Path.home() / "Library" / "Application Support" / "Blackmagic Design" / "DaVinci Resolve" / "LUT"]


def find_lut_file(name: str) -> Optional[Path]:
    """Resolve reports node LUTs relative to its LUT folders (or absolute)."""
    if not name:
        return None
    p = Path(name)
    if p.is_absolute() and p.exists():
        return p
    for root in LUT_ROOTS:
        cand = root / name
        if cand.exists():
            return cand
    for root in LUT_ROOTS:
        if root.exists():
            hits = list(root.rglob(Path(name).name))
            if hits:
                return hits[0]
    return None


def split_composed(name: str) -> Optional[Path]:
    """The clip's own LUT behind a node-1 LUT: Flow's per-clip LUTs bake the clip's conversion
    LUT and the look into one file named <base stem>_<hash>__<look>.cube. Returns the base LUT
    (None when there is none, e.g. Flow_<look>.cube); a LUT Flow didn't make is its own base."""
    if not name:
        return None
    stem = Path(name).name
    if stem.startswith("Flow_") or stem.startswith("Flow "):
        return None
    m = re.match(r"^(.+)_([0-9a-f]{4})__[A-Za-z]+\d*_\d+.*\.cube$", stem)
    if not m:
        return find_lut_file(name)
    want_stem, want_hash = m.group(1), m.group(2)
    for root in LUT_ROOTS + [Path("/Volumes")]:
        if not root.exists() or root == Path("/Volumes"):
            continue
        for cand in root.rglob("*.cube"):
            if (re.sub(r"[^A-Za-z0-9]+", "_", cand.stem)[:40] == want_stem
                    and hashlib.sha1(str(cand).encode()).hexdigest()[:4] == want_hash):
                return cand
    return None


def analyze_image(path: Path, width: int = 64) -> Dict[str, float]:
    import subprocess
    data = subprocess.run([which("ffmpeg"), "-v", "error", "-i", str(path), "-vf", f"scale={width}:-2",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    n = len(data) // 3
    if n == 0:
        return {}
    bands = {"blue_violet": 0, "green": 0, "magenta": 0, "teal": 0, "warm": 0, "skin": 0}
    luma_sum = 0.0
    mid = [0.0, 0.0, 0.0, 0]
    for k in range(n):
        r, g, b = data[3 * k] / 255.0, data[3 * k + 1] / 255.0, data[3 * k + 2] / 255.0
        luma_sum += 0.2126 * r + 0.7152 * g + 0.0722 * b
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        deg = h * 360
        if 0.12 < l < 0.88:
            mid[0] += r; mid[1] += g; mid[2] += b; mid[3] += 1
        if 0.15 < l < 0.85 and 0.12 < s < 0.6 and 8 <= deg <= 45:
            bands["skin"] += 1
        if s < 0.3 or l < 0.08 or l > 0.92:
            continue
        if 200 <= deg < 320:
            bands["blue_violet"] += 1
        elif 80 <= deg < 170:
            bands["green"] += 1
        elif 320 <= deg < 345:
            bands["magenta"] += 1
        elif 170 <= deg < 200:
            bands["teal"] += 1
        elif deg < 60 or deg >= 345:
            bands["warm"] += 1
    out = {k: round(v / n, 4) for k, v in bands.items()}
    out["luma"] = round(luma_sum / n, 4)
    out["harsh"] = round(out["blue_violet"] + out["green"] + 0.6 * out["magenta"], 4)
    if mid[3]:
        out["cast_rb"] = round((mid[0] - mid[2]) / mid[3], 4)  # >0 = warm cast in the mid-tones
    return out


def params_for(stats: Dict[str, float], lo: float = 0.6, hi: float = 1.0, keep_green: bool = False) -> dict:
    """Per-clip look settings from its measurements.

    strength  how far the nightlife look goes (more where club light is harsh)
    warm      warm-light correction for shots dominated by red/orange/yellow light
    """
    if not stats:
        return {"strength": lo, "warm": 0.0, "keep_green": keep_green}
    warm_amt = max(0.0, min(1.0, (stats.get("warm", 0.0) - 0.25) / 0.45))
    warm_amt = max(warm_amt, max(0.0, min(1.0, (stats.get("cast_rb", 0.0) - 0.08) / 0.2)))
    harsh = stats.get("harsh", 0.0) - (stats.get("green", 0.0) if keep_green else 0.0)
    strength = max(lo + 1.4 * harsh, 0.6 + 0.4 * warm_amt)
    return {"strength": round(max(lo, min(hi, strength)), 2), "warm": round(warm_amt, 2), "keep_green": keep_green}


def strength_for(stats: Dict[str, float], lo: float = 0.6, hi: float = 1.0) -> float:
    return params_for(stats, lo, hi)["strength"]


def lut_for(look: str, intensity: float, base_lut: Optional[Path], out_dir: Path,
            warm: float = 0.0, keep_green: bool = False, soft: float = 0.0) -> Path:
    """Cached LUT: (existing LUT ->) look with per-clip settings, quantized so similar clips share a LUT."""
    q = round(round(intensity / 0.05) * 0.05, 2)
    wq = round(round(warm / 0.1) * 0.1, 1)
    sq = round(round(soft / 0.1) * 0.1, 1)
    fn = luts.nightlife_transform(keep_green=keep_green, warm=wq, soft=sq) if look == "nightlife" else None
    tf = luts.build_transform(look, q, fn=fn)
    tag = (f"{look}{LOOK_REV.get(look, '')}_{int(q * 100)}" + (f"_w{int(wq * 10)}" if wq else "")
           + ("_g" if keep_green else "") + (f"_soft{int(sq * 10)}r{SOFT_REV}" if sq else ""))
    if base_lut is not None:
        stem = re.sub(r"[^A-Za-z0-9]+", "_", base_lut.stem)[:40]
        h = hashlib.sha1(str(base_lut).encode()).hexdigest()[:4]
        path = out_dir / f"{stem}_{h}__{tag}.cube"
        if not path.exists():
            luts.compose_cube(base_lut, tf, path, f"{base_lut.stem} + Flow {tag}")
    else:
        path = out_dir / f"Flow_{tag}.cube"
        if not path.exists():
            luts.write_cube(path, tf, f"Flow {tag}")
    return path


def measure_clip(item: Any, out_dir: Path, label: str) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{label}.jpg"
    try:
        res = item.ExportFrame(int(item.GetDuration() // 2),
                               {"outputType": "file", "filePath": str(p), "maxDimension": 320})
    except Exception as exc:
        return {"error": str(exc)}
    if not (res and res.get("success") and p.exists()):
        return {"error": "frame export failed"}
    st = analyze_image(p)
    st["frame"] = str(p)
    return st


def grade_clips(project: Any, items: List[tuple], look: str, out_dir: Path, lo: float = 0.6, hi: float = 1.0,
                dry_run: bool = False, match: Optional[Dict[str, str]] = None,
                keep_green: Optional[set] = None, on_clip: Any = None, nodes: bool = True,
                vignette: float = 0.0, soft: float = 0.0) -> List[dict]:
    """items: [(label, TimelineItem)]. `match` maps follower -> leader label: the follower gets the
    leader's strength (e.g. a cut-out layer that must match its plate). Returns one record per clip."""
    key = luts.normalize_look(look)
    lut_dir = paths.FLOW_LUT_DIR / "Grades"
    match = match or {}
    items = sorted(items, key=lambda x: x[0] in match)  # leaders first
    strengths: Dict[str, dict] = {}
    keep_green = keep_green or set()
    rows = []
    refreshed = False
    seen_luts: set = set()
    for label, it in items:
        if on_clip:
            on_clip(label, it)
        row: Dict[str, Any] = {"clip": label, "name": it.GetName()}
        g = it.GetNodeGraph()
        if g is None:
            row["status"] = "no color graph"
            rows.append(row)
            continue
        base_name = g.GetLUT(1) or ""
        base = find_lut_file(base_name) if base_name else None
        row["base_lut"] = base_name or None
        if base_name and base is None:
            row["status"] = f"existing LUT {base_name} not found on disk; left as is"
            rows.append(row)
            continue
        if label in match and match[label] in strengths:
            prm = dict(strengths[match[label]])
            row["matched_to"] = match[label]
        else:
            stats = measure_clip(it, out_dir / "frames", re.sub(r"[^A-Za-z0-9#]+", "_", label))
            row["stats"] = stats
            prm = params_for(stats, lo, hi, keep_green=label in keep_green)
        strengths[label] = prm
        strength = prm["strength"]
        row.update(prm)
        if dry_run:
            row["status"] = "measured"
            rows.append(row)
            continue
        from flow import nodetree
        if nodes and nodetree.available() and nodetree.can_apply(it)[0]:
            # node tree: the clip's own LUT stays on 01 Input, the per-clip look goes on 04 Look
            try:
                look_path = lut_for(key, strength, None, lut_dir, warm=prm["warm"], keep_green=prm["keep_green"],
                                    soft=soft)
            except ValueError as exc:
                row["status"] = f"skipped: {exc}"
                rows.append(row)
                continue
            if not refreshed or str(look_path) not in seen_luts:
                # every LUT file written during this run must be in Resolve's list before SetLUT
                project.RefreshLUTList()
                refreshed = True
            seen_luts.add(str(look_path))
            res = nodetree.apply_tree(it, input_lut=str(base) if base else None, look_lut=str(look_path),
                                      vignette=vignette)
            if res["ok"]:
                row["lut"] = look_path.name
                row["nodes"] = "tree"
                row["status"] = "graded"
                rows.append(row)
                continue
            row["tree_failed"] = res["failed"]
        try:
            path = lut_for(key, strength, base, lut_dir, warm=prm["warm"], keep_green=prm["keep_green"], soft=soft)
        except ValueError as exc:
            row["status"] = f"skipped: {exc}"
            rows.append(row)
            continue
        if not refreshed or str(path) not in seen_luts:
            project.RefreshLUTList()
            refreshed = True
        seen_luts.add(str(path))
        ok = g.SetLUT(1, str(path))
        if not ok:
            project.RefreshLUTList()
            ok = g.SetLUT(1, str(path))
        row["lut"] = path.name
        row["status"] = "graded" if ok else "SetLUT failed"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- reference look + mask tree
def measure_neutral(item: Any, out_dir: Path, label: str) -> Dict[str, Any]:
    """Like measure_clip, but with every node after 01 Input switched off while the frame is exported,
    so a clip that already carries a Flow grade is measured as converted footage, not as graded."""
    g = item.GetNodeGraph()
    n = int(g.GetNumNodes() or 0) if g is not None else 0
    off = []
    for i in range(2, n + 1):
        try:
            if g.SetNodeEnabled(i, False):
                off.append(i)
        except Exception:
            pass
    try:
        return measure_clip(item, out_dir, label)
    finally:
        g = item.GetNodeGraph()
        for i in off:
            try:
                g.SetNodeEnabled(i, True)
            except Exception:
                pass


def grade_clips_masked(project: Any, items: List[tuple], out_dir: Path, look: Optional[str] = None,
                       reference: Optional[str] = None, masks_cfg: Any = True,
                       subjects: Optional[Dict[str, List[float]]] = None, vignette: Optional[float] = None,
                       ref_strength: Optional[dict] = None, replace: bool = False, on_clip: Any = None,
                       lo: float = 0.6, hi: float = 1.0) -> Dict[str, Any]:
    """Grade every clip with the mask tree (flow.masks): 01 Input keeps the clip's own conversion LUT,
    04 Look gets the reference match (one LUT for the whole timeline, built from all clips) or the named
    Flow look, and the rule set fills Skin / Greens / Highlights / Sky / Subject / Vignette per clip.
    Returns {"rows": [...], "reference": {...} | None}."""
    import re as _re
    from flow import drx, masks, nodetree, refmatch
    from flow.util import FlowError
    if not drx.zstd_available():
        raise FlowError("Masks need zstd to write Resolve grade files. Fix: brew install zstd")
    rules =masks.rules_with(masks_cfg if isinstance(masks_cfg, dict) else None)
    if masks_cfg is False:
        rules = {k: {**v, "on": False} for k, v in rules.items()}
    if vignette is not None:
        rules["vignette"]["amount"] = float(vignette)
    subjects = subjects or {}
    lut_dir = paths.FLOW_LUT_DIR / "Grades"
    mask_lut_dir = paths.FLOW_LUT_DIR / "Masks"
    drx_dir = out_dir / "masks"
    rows: List[Dict[str, Any]] = []
    todo = []
    # pass 1: measure everything (neutral frames), so a reference match can pool all clips
    for label, it in items:
        row: Dict[str, Any] = {"clip": label, "name": it.GetName()}
        g = it.GetNodeGraph()
        if g is None:
            row["status"] = "no color graph"
            rows.append(row)
            continue
        ok, why = nodetree.can_apply(it, replace)
        if not ok:
            row["status"] = f"left as graded: {why}"
            rows.append(row)
            continue
        base_name = g.GetLUT(1) or ""
        base = find_lut_file(base_name) if base_name else None
        row["base_lut"] = base_name or None
        slug = _re.sub(r"[^A-Za-z0-9#]+", "_", label)
        stats = measure_neutral(it, out_dir / "frames", slug)
        row["stats"] = stats
        todo.append((label, it, row, base, slug))
        rows.append(row)
    ref_info = None
    ref_lut = None
    if reference:
        frames = [Path(r["stats"]["frame"]) for _, _, r, _, _ in todo if r["stats"].get("frame")]
        kw = dict(ref_strength or {})
        tag = hashlib.sha1(json.dumps([str(reference), sorted(map(str, frames)), kw], sort_keys=True)
                           .encode()).hexdigest()[:6]
        ref_lut = lut_dir / f"Flow_refmatch_{_re.sub(r'[^A-Za-z0-9]+', '_', Path(reference).stem)[:30]}_{tag}.cube"
        ref_info = refmatch.write_match_lut(Path(reference), frames, ref_lut, **kw)
    project.RefreshLUTList()
    sky_target = (ref_info or {}).get("sky")
    key = luts.normalize_look(look) if look and look != "reference" else None
    seen_luts: set = set()
    for label, it, row, base, slug in todo:
        if on_clip:
            on_clip(label, it)
        stats = row["stats"]
        if ref_lut is not None:
            look_path = ref_lut
        elif key:
            prm = params_for(stats, lo, hi)
            row.update(prm)
            look_path = lut_for(key, prm["strength"], None, lut_dir, warm=prm["warm"], keep_green=prm["keep_green"])
        else:
            look_path = None
        reg = masks.regions(masks.load_rgb(Path(stats["frame"]))) if stats.get("frame") else {}
        dec = masks.decide(reg, rules, subject_hint=subjects.get(label), sky_target=sky_target)
        for lab in ("Skin", "Greens", "Highlights"):           # write every new qualifier LUT before applying
            op = dec["ops"].get(lab)
            if op:
                p = masks.qualifier_lut(op["qualifier"], op["params"], mask_lut_dir)
                if str(p) not in seen_luts:
                    seen_luts.add(str(p))
                    project.RefreshLUTList()
        if look_path is not None and str(look_path) not in seen_luts:
            seen_luts.add(str(look_path))
            project.RefreshLUTList()
        res = masks.apply(it, project, nodetree.TEMPLATE, dec["ops"], drx_dir / f"{slug}.drx", mask_lut_dir,
                          input_lut=str(base) if base else None, look_lut=str(look_path) if look_path else None)
        row["regions"] = reg
        row["masks"] = {k: v for k, v in dec["why"].items()}
        row["mask_nodes_on"] = sorted(dec["ops"])
        row["lut"] = look_path.name if look_path else None
        if stats.get("frame"):
            prev = masks.overlay_preview(Path(stats["frame"]), dec["ops"], drx_dir / f"{slug}_windows.jpg")
            if prev:
                row["windows_preview"] = str(prev)
        row["status"] = "graded" if res["ok"] else f"mask tree failed: {', '.join(res['failed'])}"
    return {"rows": rows, "reference": ref_info}
