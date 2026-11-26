"""Flow masks: read a clip's frame, run a small colorist ruleset, and put real secondaries on the Color page.

Each graded clip gets the mask tree (an extension of nodetree's six-node tree):

    01 Input · 02 Balance · 03 Exposure · 04 Look        (as before)
    05 Skin        qualifier  skin hues pulled to the skin line            (keyed LUT)
    06 Greens      qualifier  foliage hue + saturation tamed toward the look (keyed LUT)
    07 Highlights  qualifier  soft roll-off + tint of the brightest values   (keyed LUT)
    08 Sky         window     graduated, wide ellipse from the top down to the horizon (CDL)
    09 Subject     window     soft ellipse on the subject: pulled forward     (CDL)
    10 Vignette    window     inverted ellipse, centred on the subject        (CDL)

Rules decide per clip whether a node is used and how hard (see RULES and skills/flow/reference/masks.md).
Windows are written into a per-clip .drx (flow.drx) because Resolve's API cannot add or move windows;
qualifiers are baked into soft-keyed 3D LUTs, which is exactly what a HSL qualifier + correction does
to a pixel, but stays a plain LUT node the colorist can swap for a real qualifier. Nodes whose rule does
not fire stay in the tree, disabled, so every clip has the same layout.
"""
from __future__ import annotations

import colorsys
import copy
import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flow import drx, luts
from flow.util import which

TREE = ["Input", "Balance", "Exposure", "Look", "Skin", "Greens", "Highlights", "Sky", "Subject", "Vignette"]
IDX = {name: i + 1 for i, name in enumerate(TREE)}
COLORS = {"Look": "Orange", "Skin": "Apricot", "Greens": "Green", "Highlights": "Yellow", "Sky": "Blue",
          "Subject": "Pink", "Vignette": "Navy"}

# Window param ids inside a node (found with tests/drx_lab.py on Resolve 21)
P_RADIUS, P_SOFT = 139460613, 139460614            # radius (see _u), size/softness multiplier (1.0 = calibrated)
P_ENABLED, P_INVERT, P_MODE, P_MATRIX = 2286944264, 2286944265, 2286944266, 2286944272

# ---------------------------------------------------------------- the ruleset
RULES: Dict[str, Dict[str, Any]] = {
    # Sky: a graduated ND from the top edge to the horizon. Fires when enough bright, smooth, non-green
    # area touches the top of the frame. Darkens (density), adds the target sky colour, a bit of saturation.
    "sky": {"on": True, "min_area": 0.08, "density": 0.18, "tint": 0.5, "sat": 1.12, "strength": 1.0},
    # Subject: a soft ellipse on what the eye should go to (saliency + centre prior, or the plan's point).
    # Lifts it a little so it separates from a darker, denser background.
    "subject": {"on": True, "min_conf": 0.2, "lift": 0.14, "sat": 1.04, "size": 2.4, "strength": 1.0},
    # Vignette: inverted ellipse that follows the subject, so the darkening never sits on the subject.
    "vignette": {"on": True, "amount": 0.22, "follow": 0.6, "radius": 0.62},
    # Greens: foliage pulled toward the look's olive and desaturated (the classic "make it come together").
    "greens": {"on": True, "min_area": 0.04, "hue": 55.0, "sat": 0.62, "luma": 0.94, "strength": 0.85},
    # Skin: skin hues pulled to the skin line (~22°) with saturation kept in a natural band.
    # (above max_area the "skin" is warm light on road/grass/walls, not people)
    "skin": {"on": True, "min_area": 0.012, "max_area": 0.15, "hue": 22.0, "sat_lo": 0.22, "sat_hi": 0.55, "strength": 0.6},
    # Highlights: soft knee on the top end + warm tint, so bright skies and headlights don't clip harshly.
    "highlights": {"on": True, "min_area": 0.015, "knee": 0.8, "tint": [1.03, 1.0, 0.95], "strength": 0.7},
}


def rules_with(overrides: Optional[dict]) -> Dict[str, Dict[str, Any]]:
    """Defaults + overrides ({"greens": false} turns a rule off, {"sky": {"density": 0.3}} tunes it)."""
    out = copy.deepcopy(RULES)
    for k, v in (overrides or {}).items():
        if k not in out:
            continue
        if v is False:
            out[k]["on"] = False
        elif isinstance(v, dict):
            out[k].update(v)
    return out


# ---------------------------------------------------------------- frame analysis
AW, AH = 96, 54


def load_rgb(path: Path, w: int = AW, h: int = AH) -> List[List[Tuple[float, float, float]]]:
    data = subprocess.run([which("ffmpeg"), "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}",
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    if len(data) < w * h * 3:
        return []
    return [[(data[3 * (y * w + x)] / 255, data[3 * (y * w + x) + 1] / 255, data[3 * (y * w + x) + 2] / 255)
             for x in range(w)] for y in range(h)]


def _hls(c):
    h, l, s = colorsys.rgb_to_hls(*c)
    return h * 360.0, l, s


def _is_green(h: float, s: float, l: float) -> bool:
    return 70 <= h <= 170 and s > 0.18 and 0.08 < l < 0.9


def _box_blur(grid: List[List[float]], r: int) -> List[List[float]]:
    h, w = len(grid), len(grid[0])
    integ = [[0.0] * (w + 1) for _ in range(h + 1)]
    for y in range(h):
        acc = 0.0
        for x in range(w):
            acc += grid[y][x]
            integ[y + 1][x + 1] = integ[y][x + 1] + acc
    out = [[0.0] * w for _ in range(h)]
    for y in range(h):
        y0, y1 = max(0, y - r), min(h, y + r + 1)
        for x in range(w):
            x0, x1 = max(0, x - r), min(w, x + r + 1)
            s = integ[y1][x1] - integ[y0][x1] - integ[y1][x0] + integ[y0][x0]
            out[y][x] = s / ((y1 - y0) * (x1 - x0))
    return out


def regions(img: List[List[Tuple[float, float, float]]]) -> Dict[str, Any]:
    """Sky, subject, greens, skin and highlight measurements of one frame (coords 0..1, y from the top)."""
    if not img:
        return {"error": "no frame"}
    h, w = len(img), len(img[0])
    n = w * h
    L = [[luts.luma(*img[y][x]) for x in range(w)] for y in range(h)]
    HLS = [[_hls(img[y][x]) for x in range(w)] for y in range(h)]
    flat = sorted(v for row in L for v in row)
    p75 = flat[int(0.75 * (n - 1))]
    mean_l = sum(flat) / n

    # sky: per column, a run of bright, smooth, non-green pixels from the top edge
    bright = max(0.42, min(0.8, p75 * 0.92))
    runs, sky_px, sky_rgb, sky_x = [], 0, [0.0, 0.0, 0.0], 0.0
    for x in range(w):
        run = 0
        for y in range(h):
            hh, ll, ss = HLS[y][x]
            nb = L[y][min(w - 1, x + 1)]
            dn = L[min(h - 1, y + 1)][x]
            smooth = abs(L[y][x] - nb) + abs(L[y][x] - dn) < 0.09
            if L[y][x] >= bright and smooth and not _is_green(hh, ss, ll):
                run += 1
            else:
                break
        runs.append(run)
        if run >= 2:
            sky_px += run
            sky_x += x * run
            for y in range(run):
                for c in range(3):
                    sky_rgb[c] += img[y][x][c]
    sky_cols = [r for r in runs if r >= 2]
    sky = {"area": round(sky_px / n, 4), "cols": round(len(sky_cols) / w, 3)}
    if sky_px:
        sc = sorted(sky_cols)
        sky.update({"horizon": round(sc[len(sc) // 2] / h, 3), "x": round(sky_x / sky_px / (w - 1), 3),
                    "rgb": [round(v / sky_px, 4) for v in sky_rgb]})
    sky_mask = [[1 if y < runs[x] and runs[x] >= 2 else 0 for x in range(w)] for y in range(h)]

    # subject: local contrast vs a blurred background, centre-weighted, sky excluded
    bg = _box_blur(L, max(3, w // 10))
    sal = [[0.0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            if sky_mask[y][x]:
                continue
            dx, dy = (x / (w - 1) - 0.5) / 0.32, (y / (h - 1) - 0.52) / 0.38
            prior = math.exp(-0.5 * (dx * dx + dy * dy))
            sal[y][x] = abs(L[y][x] - bg[y][x]) * (0.35 + prior)
    vals = sorted((sal[y][x], x, y) for y in range(h) for x in range(w))
    top = vals[int(0.93 * n):]
    s_all = sum(v[0] for v in vals) / n + 1e-6
    s_top = sum(v[0] for v in top) / max(1, len(top))
    wsum = sum(v[0] for v in top) + 1e-9
    cx = sum(v[0] * v[1] for v in top) / wsum / (w - 1)
    cy = sum(v[0] * v[2] for v in top) / wsum / (h - 1)
    var = sum(v[0] * (((v[1] / (w - 1)) - cx) ** 2 + ((v[2] / (h - 1)) - cy) ** 2) for v in top) / wsum
    subject = {"x": round(cx, 3), "y": round(cy, 3), "spread": round(math.sqrt(var), 3),
               "conf": round(max(0.0, min(1.0, (s_top / s_all - 2.0) / 4.0)), 3)}

    green = skin = hi = 0
    for y in range(h):
        for x in range(w):
            hh, ll, ss = HLS[y][x]
            if _is_green(hh, ss, ll):
                green += 1
            if 5 <= hh <= 45 and 0.15 < ll < 0.8 and 0.15 < ss < 0.65 and not sky_mask[y][x]:
                skin += 1
            if L[y][x] > 0.9:
                hi += 1
    return {"luma": round(mean_l, 4), "p75": round(p75, 4), "sky": sky, "subject": subject,
            "greens": round(green / n, 4), "skin": round(skin / n, 4), "highlights": round(hi / n, 4)}


# ---------------------------------------------------------------- rules -> mask ops
def decide(reg: Dict[str, Any], rules: Dict[str, Dict[str, Any]], subject_hint: Optional[List[float]] = None,
           sky_target: Optional[List[float]] = None, aspect: float = 16 / 9) -> Dict[str, Any]:
    """One clip's measurements + the ruleset -> what every mask node does (pure; no Resolve)."""
    ops: Dict[str, Any] = {}
    why: Dict[str, str] = {}
    sky = reg.get("sky") or {}

    r = rules["sky"]
    if r["on"] and sky.get("area", 0) >= r["min_area"] and sky.get("horizon"):
        k = r["strength"]
        dens = 1.0 - r["density"] * k
        tint = [1.0, 1.0, 1.0]
        if sky_target:
            m = sum(sky_target) / 3 or 1.0
            tint = [1.0 + r["tint"] * k * (c / m - 1.0) for c in sky_target]
        hz = sky["horizon"]
        r50y = max(0.08, 0.85 * hz)                              # 50% at ~85 % of the way to the horizon
        ops["Sky"] = {"window": {"cx": sky.get("x", 0.5), "cy": 0.0, "ry": r50y, "rx": 1.3 * aspect, "invert": False},
                      "cdl": {"slope": [round(dens * t, 4) for t in tint], "offset": [0, 0, 0], "power": [1, 1, 1],
                              "sat": round(1 + (r["sat"] - 1) * k, 3)}}
        why["Sky"] = f"sky {sky['area']:.0%} of frame, horizon at {hz:.0%}"
    else:
        why["Sky"] = f"no sky (area {sky.get('area', 0):.0%})" if r["on"] else "rule off"

    subj = reg.get("subject") or {}
    sx, sy = (subject_hint or [subj.get("x", 0.5), subj.get("y", 0.5)])[:2]
    r = rules["subject"]
    conf = 1.0 if subject_hint else subj.get("conf", 0)
    if r["on"] and conf >= r["min_conf"]:
        k = r["strength"] * (1.0 if subject_hint else min(1.0, 0.5 + conf))
        # darker frames get a little more lift, bright ones less
        lift = r["lift"] * k * (1.25 if reg.get("luma", 0.4) < 0.25 else 0.8 if reg.get("luma", 0.4) > 0.6 else 1.0)
        size = max(0.1, min(0.38, r["size"] * max(0.06, subj.get("spread", 0.12))))
        ops["Subject"] = {"window": {"cx": sx, "cy": sy, "ry": size, "rx": size * 1.15, "invert": False},
                          "cdl": {"slope": [round(1 + lift, 4)] * 3, "offset": [0, 0, 0], "power": [1, 1, 1],
                                  "sat": round(1 + (r["sat"] - 1) * k, 3)}}
        why["Subject"] = ("subject from plan" if subject_hint else f"subject conf {conf:.2f}") + f" at ({sx:.2f}, {sy:.2f})"
    else:
        why["Subject"] = f"no clear subject (conf {conf:.2f})" if r["on"] else "rule off"

    r = rules["vignette"]
    if r["on"] and r["amount"] > 0:
        f = r["follow"] if "Subject" in ops else 0.0
        vx, vy = 0.5 + (sx - 0.5) * f, 0.5 + (sy - 0.5) * f
        ops["Vignette"] = {"window": {"cx": vx, "cy": vy, "ry": r["radius"] * 0.62, "rx": r["radius"] * 0.62 * aspect * 0.8,
                                      "invert": True},
                           "cdl": {"slope": [round(1 - r["amount"], 4)] * 3, "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 0.95}}
        why["Vignette"] = f"vignette {r['amount']:.2f}" + (" following the subject" if f else "")

    for name, key, measure in (("Greens", "greens", "greens"), ("Skin", "skin", "skin"),
                               ("Highlights", "highlights", "highlights")):
        r = rules[key]
        area = reg.get(measure, 0)
        if r["on"] and area >= r["min_area"] and area <= r.get("max_area", 1.0):
            ops[name] = {"qualifier": key, "params": {k: v for k, v in r.items()
                                                      if k not in ("on", "min_area", "max_area")}}
            why[name] = f"{key} {area:.1%} of frame"
        elif r["on"] and area > r.get("max_area", 1.0):
            why[name] = f"{key} {area:.1%} > {r['max_area']:.0%}: warm scene light, not {key}"
        else:
            why[name] = f"{key} {area:.1%} < {r['min_area']:.1%}" if r["on"] else "rule off"
    return {"ops": ops, "why": why}


# ---------------------------------------------------------------- qualifier LUTs (soft keys)
def _key(h: float, lo: float, hi: float, soft: float = 12.0) -> float:
    """Soft hue window lo..hi (degrees) with `soft` degrees of feather on each side."""
    def d(a, b):
        return (a - b + 180) % 360 - 180
    if lo <= h <= hi:
        return 1.0
    dist = min(abs(d(h, lo)), abs(d(h, hi)))
    return max(0.0, 1.0 - dist / soft)


def qualifier_transform(kind: str, p: Dict[str, Any]):
    k = float(p.get("strength", 0.8))

    def greens(c):
        h, l, s = _hls(c)
        m = _key(h, 70, 165, 18) * luts.smoothstep(0.08, 0.22, s) * luts.smoothstep(0.04, 0.12, l) * k
        if m <= 0:
            return c
        nh =h + ((p["hue"] - h + 180) % 360 - 180) * 0.75 * m
        ns = s * (1 + (p["sat"] - 1) * m)
        nl = l * (1 + (p["luma"] - 1) * m)
        r, g, b = colorsys.hls_to_rgb((nh % 360) / 360, luts.clamp(nl), luts.clamp(ns))
        return (r, g, b)

    def skin(c):
        h, l, s = _hls(c)
        m = _key(h, 8, 40, 10) * luts.smoothstep(0.08, 0.18, s) * luts.smoothstep(0.1, 0.2, l) * \
            (1 - luts.smoothstep(0.82, 0.95, l)) * k
        if m <= 0:
            return c
        nh = h + ((p["hue"] - h + 180) % 360 - 180) * 0.6 * m
        ns = s + (min(max(s, p["sat_lo"]), p["sat_hi"]) - s) * m
        r, g, b = colorsys.hls_to_rgb((nh % 360) / 360, l, luts.clamp(ns))
        return (r, g, b)

    def highlights(c):
        y = luts.luma(*c)
        knee = float(p["knee"])
        m = luts.smoothstep(knee - 0.1, 1.0, y) * k
        if m <= 0:
            return c
        # soft shoulder: compress above the knee
        def sh(v):
            if v <= knee:
                return v
            t = (v - knee) / (1 - knee)
            return knee + (1 - knee) * (1 - (1 - t) ** 2) * 0.92
        t = p.get("tint", [1, 1, 1])
        return tuple(luts.clamp(luts.lerp(v, sh(v) * t[i], m)) for i, v in enumerate(c))

    return {"greens": greens, "skin": skin, "highlights": highlights}[kind]


def qualifier_lut(kind: str, params: Dict[str, Any], out_dir: Path) -> Path:
    tag = hashlib.sha1(json.dumps([kind, params], sort_keys=True).encode()).hexdigest()[:8]
    path = out_dir / f"Flow_mask_{kind}_{tag}.cube"
    if not path.exists():
        luts.write_cube(path, qualifier_transform(kind, params), f"Flow mask {kind} {tag}", size=33)
    return path


# ---------------------------------------------------------------- the mask tree (.drx)
def _u_params(win: Dict[str, Any], aspect: float) -> Dict[int, Any]:
    """Frame-relative ellipse -> Resolve window params. Units (measured): u = frame width / 1080;
    the 50 % edge of the soft circle sits at radius/2 · u; the matrix scales about the centre and
    translates in u, +y up. cx, cy are 0..1 (y from the top); rx, ry are 50 % radii as a fraction of
    the frame HEIGHT."""
    hu = 1080.0 / aspect                     # frame height in u
    ry = max(1e-3, float(win["ry"])) * hu
    rx = max(1e-3, float(win["rx"])) * hu
    radius = 2.0 * ry
    tx = (float(win["cx"]) - 0.5) * 1080.0
    ty = (0.5 - float(win["cy"])) * hu
    return {P_RADIUS: radius, P_SOFT: 1.0, P_ENABLED: 2, P_INVERT: 2 if win.get("invert") else 0,
            P_MATRIX: [rx / ry, 0.0, tx, 0.0, 1.0, ty, 0.0, 0.0, 1.0]}


def _node_msg(root: drx.Msg, label: str) -> drx.Msg:
    for n in drx.nodes(root):
        if drx.node_label(n) == label:
            return n
    raise ValueError(f"template has no node {label}")


def _set(msg: drx.Msg, fn: int, wt: int, val: Any) -> None:
    for f in msg:
        if f[0] == fn:
            f[1], f[2] = wt, val
            return
    msg.append([fn, wt, val])


def build_tree_root(template_grade: bytes) -> drx.Msg:
    """Base six-node template -> ten-node mask tree (serial chain, labelled, laid out left to right)."""
    root = drx.grade_root(template_grade)
    proto = {lab: _node_msg(root, lab) for lab in ("Input", "Balance", "Exposure", "Look", "Skin", "Vignette")}
    base_ids = {lab: drx.first(m, 1)[2] for lab, m in proto.items()}
    next_id = max(base_ids.values()) + 1
    built: List[drx.Msg] = []
    ids: List[int] = []
    for i, lab in enumerate(TREE):
        if lab in proto:
            m = copy.deepcopy(proto[lab])
            nid = base_ids[lab]
        else:
            src = "Vignette" if lab in ("Sky", "Subject") else "Skin"
            m = copy.deepcopy(proto[src])
            nid = next_id
            next_id += 1
        _set(m, 1, 0, nid)
        _set(m, 2, 0, i)
        _set(m, 4, 0, 150 + 190 * i)
        _set(m, 5, 0, 180)
        _set(m, 6, 2, lab.encode())
        built.append(m)
        ids.append(nid)
    link_proto = drx.decode(drx.get(root, 8)[0][2])
    out: drx.Msg = []
    placed = False
    for f in root:
        if f[0] in (7, 8):
            if not placed:
                out += [[7, 2, drx.encode(m)] for m in built]
                for k in range(len(ids) - 1):
                    link = copy.deepcopy(link_proto)
                    _set(link, 1, 0, ids[k])
                    _set(link, 3, 0, ids[k + 1])
                    _set(link, 7, 0, k + 1)
                    out.append([8, 2, drx.encode(link)])
                placed = True
            continue
        out.append(f)
    _patch_io(out, first_id=ids[0], last_id=ids[-1])
    for f in out:
        if f[0] == 1 and f[1] == 0:
            f[2] = max(ids)
    return out


def _patch_io(root: drx.Msg, first_id: int, last_id: int) -> None:
    """Field 9 = clip input -> first node, field 10 = last node -> output (sub-field 3.4 is the node id)."""
    for f in root:
        if f[0] in (9, 10) and f[1] == 2:
            m = drx.decode(f[2])
            for g in m:
                if g[0] == 3 and g[1] == 2:
                    inner = drx.decode(g[2])
                    _set(inner, 4, 0, first_id if f[0] == 9 else last_id)
                    g[2] = drx.encode(inner)
            f[2] = drx.encode(m)


def clip_drx(template: Path, ops: Dict[str, Any], out: Path, aspect: float = 16 / 9) -> Path:
    """Write the mask tree with this clip's windows into its own .drx."""
    xml, grade = drx.read_grade(template)
    root = build_tree_root(grade)
    for lab in ("Sky", "Subject", "Vignette"):
        win = (ops.get(lab) or {}).get("window")
        if win is None:
            continue
        params = _u_params(win, aspect)
        root = drx.edit_window(root, lab, lambda cur: [(pid, params.get(pid, v)) for pid, v in cur])
    return drx.write_grade(xml, drx.rebuild_grade(grade, root), out)


def _cdl_strings(c: Dict[str, Any]) -> Dict[str, str]:
    f = lambda v: " ".join(f"{float(x):.4f}" for x in v)  # noqa: E731
    return {"Slope": f(c["slope"]), "Offset": f(c["offset"]), "Power": f(c["power"]), "Saturation": f"{float(c['sat']):.4f}"}


def apply(item: Any, project: Any, template: Path, ops: Dict[str, Any], drx_out: Path, lut_dir: Path,
          input_lut: Optional[str] = None, balance: Optional[dict] = None, exposure: Optional[dict] = None,
          look_lut: Optional[str] = None, aspect: float = 16 / 9) -> Dict[str, Any]:
    """Put the mask tree on one clip and fill every node. Returns {ok, done, failed, off}."""
    import time
    res: Dict[str, Any] = {"ok": False, "done": [], "failed": [], "off": []}
    path = clip_drx(template, ops, drx_out, aspect)
    g = item.GetNodeGraph()
    if g is None or not g.ApplyGradeFromDRX(str(path), 0):
        res["failed"].append("ApplyGradeFromDRX")
        return res
    time.sleep(0.05)
    g = item.GetNodeGraph()
    if int(g.GetNumNodes() or 0) != len(TREE):
        res["failed"].append(f"tree gave {g.GetNumNodes()} nodes")
        return res
    for lab, i in IDX.items():
        if (g.GetNodeLabel(i) or "") != lab:
            g.SetNodeLabel(i, lab)
        if lab in COLORS:
            try:
                g.SetNodeColor(i, COLORS[lab])
            except Exception:
                pass

    def set_lut(lab: str, p: Optional[str]) -> None:
        if not p:
            return
        ok = g.SetLUT(IDX[lab], str(p))
        if not ok:                      # a LUT written moments ago may not be in Resolve's list yet
            project.RefreshLUTList()
            time.sleep(0.2)
            ok = g.SetLUT(IDX[lab], str(p))
        (res["done"] if ok else res["failed"]).append(f"{lab}:lut")

    def set_cdl(lab: str, vals: Optional[dict]) -> None:
        if vals:
            ok = item.SetCDL({"NodeIndex": str(IDX[lab]), **vals})
            (res["done"] if ok else res["failed"]).append(f"{lab}:cdl")

    set_lut("Input", input_lut)
    if balance:
        set_cdl("Balance", {k: str(v) for k, v in balance.items()})
    if exposure:
        set_cdl("Exposure", {k: str(v) for k, v in exposure.items()})
    set_lut("Look", look_lut)
    for lab in ("Skin", "Greens", "Highlights"):
        op = ops.get(lab)
        if op:
            set_lut(lab, str(qualifier_lut(op["qualifier"], op["params"], lut_dir)))
        else:
            g.SetNodeEnabled(IDX[lab], False)
            res["off"].append(lab)
    for lab in ("Sky", "Subject", "Vignette"):
        op = ops.get(lab)
        if op:
            set_cdl(lab, _cdl_strings(op["cdl"]))
        else:
            g.SetNodeEnabled(IDX[lab], False)
            res["off"].append(lab)
    res["ok"] = not res["failed"]
    return res


def overlay_preview(frame: Path, ops: Dict[str, Any], out: Path) -> Optional[Path]:
    """Draw where the windows sit on the analysed frame (ellipses at their 50 % edge) for review."""
    try:
        from PIL import Image, ImageDraw  # optional; Flow runs without Pillow
    except Exception:
        return None
    im = Image.open(frame).convert("RGB")
    w, h = im.size
    d = ImageDraw.Draw(im)
    cols = {"Sky": (80, 160, 255), "Subject": (255, 90, 200), "Vignette": (40, 40, 160)}
    for lab, col in cols.items():
        win = (ops.get(lab) or {}).get("window")
        if not win:
            continue
        cx, cy, rx, ry = win["cx"] * w, win["cy"] * h, win["rx"] * h, win["ry"] * h
        d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=col, width=2)
        d.text((min(w - 60, max(2, cx - 20)), min(h - 12, max(2, cy - 6))), lab, fill=col)
    im.save(out)
    return out
