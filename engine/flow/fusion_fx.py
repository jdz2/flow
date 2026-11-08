"""Flow's effects set, built as Fusion node graphs.

Image effects (need the picture):  zoom/push_in, shake, glow, halation, rgb_split, blur_in, blur_bg
   -> one Fusion comp per V1 clip: MediaIn -> chain -> MediaOut
Overlay effects (drawn on top):     flash, letterbox, vignette, spotlight, highlight_box,
                                    circle_reveal, grain, light_leak
   -> a Fusion Composition clip on an FX track (some use a composite mode)

All frame numbers handed in are local to the clip (0 = first frame of the clip).
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List

from flow.fusion_util import (animate, animate_point, comp_start, fit_comp_range, media_in, media_out,
                               set_inputs, set_point)

IMAGE_FX = {"zoom", "push_in", "shake", "glow", "halation", "rgb_split", "blur_in", "blur_bg"}
OVERLAY_FX = {"flash", "letterbox", "vignette", "spotlight", "highlight_box", "circle_reveal", "grain",
              "light_leak"}
# Overlays that should blend rather than cover.
OVERLAY_COMPOSITE = {"grain": ("COMPOSITE_OVERLAY", 35.0), "light_leak": ("COMPOSITE_SCREEN", 70.0)}


def _env(f0: float, f1: float, ramp: int = 3):
    """Blend envelope keys: fade in and out over `ramp` frames."""
    ramp = max(1, min(ramp, int((f1 - f0) / 2) or 1))
    return [(f0, 0.0), (f0 + ramp, 1.0), (f1 - ramp, 1.0), (f1, 0.0)]


# ---------------------------------------------------------------- image effects
def _transform(comp: Any) -> Any:
    return comp.AddTool("Transform", -32768, -32768)


def fx_zoom(comp: Any, inp: Any, fx: dict, s: float) -> Any:
    p = fx["params"]
    tr = _transform(comp)
    tr.ConnectInput("Input", inp)
    cx, cy = p.get("center", [0.5, 0.6])
    set_point(tr, "Pivot", cx, cy)
    set_point(tr, "Center", cx, cy)
    a = float(p.get("zoom_from", p.get("from", 1.0)))
    b = float(p.get("zoom_to", p.get("to", p.get("zoom", 1.12))))
    f0, f1 = s + fx.get("uf0", fx["lf0"]), s + fx.get("uf1", fx["lf1"])
    n = 6
    keys = []
    for k in range(n + 1):  # ease-in-out sampled into keyframes
        t = k / n
        e = t * t * (3 - 2 * t)
        keys.append((f0 + (f1 - f0) * t, a + (b - a) * e))
    animate(tr, "Size", keys)
    return tr


def fx_shake(comp: Any, inp: Any, fx: dict, s: float) -> Any:
    p = fx["params"]
    amt = float(p.get("intensity", 1.0))
    tr = _transform(comp)
    tr.ConnectInput("Input", inp)
    f0, f1 = s + fx.get("uf0", fx["lf0"]), s + fx.get("uf1", fx["lf1"])
    rnd = random.Random(int(f0 * 7919 + f1))
    length = max(2, int(f1 - f0))
    angle_keys, size_keys, pos_keys = [(f0 - 1, 0.0)], [(f0 - 1, 1.0)], [(f0 - 1, 0.5, 0.5)]
    for k in range(length + 1):
        decay = 1.0 - k / float(length)
        f = f0 + k
        angle_keys.append((f, rnd.uniform(-2.2, 2.2) * amt * decay))
        size_keys.append((f, 1.06 + rnd.uniform(-0.02, 0.025) * amt * decay))
        pos_keys.append((f, 0.5 + rnd.uniform(-0.012, 0.012) * amt * decay,
                         0.5 + rnd.uniform(-0.012, 0.012) * amt * decay))
    angle_keys.append((f1 + 1, 0.0))
    size_keys.append((f1 + 1, 1.0))
    pos_keys.append((f1 + 1, 0.5, 0.5))
    animate(tr, "Angle", angle_keys)
    animate(tr, "Size", size_keys)
    animate_point(tr, "Center", pos_keys)
    return tr


def fx_glow(comp: Any, inp: Any, fx: dict, s: float, halation: bool = False) -> Any:
    p = fx["params"]
    amt = float(p.get("intensity", 1.0))
    g = comp.AddTool("SoftGlow", -32768, -32768)
    g.ConnectInput("Input", inp)
    if halation:
        set_inputs(g, {"Threshold": 0.72, "Gain": 1.2 * amt, "XGlowSize": 18.0,
                       "Red": 1.0, "Green": 0.45, "Blue": 0.25})
    else:
        set_inputs(g, {"Threshold": 0.55, "Gain": 1.6 * amt, "XGlowSize": 12.0})
    if not p.get("_global"):
        animate(g, "Blend", _env(s + fx.get("uf0", fx["lf0"]), s + fx.get("uf1", fx["lf1"]), 6))
    return g


def fx_rgb_split(comp: Any, inp: Any, fx: dict, s: float) -> Any:
    """Channel-limited transforms: shift only red one way and only blue the other."""
    amt = float(fx["params"].get("intensity", 1.0)) * 0.012
    f0, f1 = s + fx.get("uf0", fx["lf0"]), s + fx.get("uf1", fx["lf1"])
    red = _transform(comp)
    red.ConnectInput("Input", inp)
    set_inputs(red, {"ProcessGreen": 0.0, "ProcessBlue": 0.0, "ProcessAlpha": 0.0})
    blue = _transform(comp)
    blue.ConnectInput("Input", red)
    set_inputs(blue, {"ProcessRed": 0.0, "ProcessGreen": 0.0, "ProcessAlpha": 0.0})
    mid = (f0 + f1) / 2
    animate_point(red, "Center", [(f0 - 1, 0.5, 0.5), (f0, 0.5 + amt, 0.5), (mid, 0.5 + amt * 0.4, 0.5),
                                  (f1, 0.5, 0.5)])
    animate_point(blue, "Center", [(f0 - 1, 0.5, 0.5), (f0, 0.5 - amt, 0.5), (mid, 0.5 - amt * 0.4, 0.5),
                                   (f1, 0.5, 0.5)])
    return blue


def fx_blur_in(comp: Any, inp: Any, fx: dict, s: float) -> Any:
    b = comp.AddTool("Blur", -32768, -32768)
    b.ConnectInput("Input", inp)
    amt = float(fx["params"].get("intensity", 1.0))
    animate(b, "XBlurSize", [(s + fx.get("uf0", fx["lf0"]), 25.0 * amt), (s + fx.get("uf1", fx["lf1"]), 0.0)])
    return b


def fx_blur_bg(comp: Any, inp: Any, fx: dict, s: float) -> Any:
    p = fx["params"]
    b = comp.AddTool("Blur", -32768, -32768)
    b.ConnectInput("Input", inp)
    set_inputs(b, {"XBlurSize": 8.0 * float(p.get("intensity", 1.0))})
    m = comp.AddTool("EllipseMask", -32768, -32768)
    cx, cy = p.get("center", [0.5, 0.6])
    set_point(m, "Center", cx, cy)
    size = float(p.get("size", 0.6))
    set_inputs(m, {"Width": size, "Height": size * 0.75, "SoftEdge": 0.12, "Invert": 1.0})
    b.ConnectInput("EffectMask", m)
    animate(b, "Blend", _env(s + fx.get("uf0", fx["lf0"]), s + fx.get("uf1", fx["lf1"]), 8))
    return b


IMAGE_BUILDERS = {
    "zoom": fx_zoom, "push_in": fx_zoom, "shake": fx_shake, "glow": fx_glow,
    "halation": lambda c, i, f, s: fx_glow(c, i, f, s, halation=True),
    "rgb_split": fx_rgb_split, "blur_in": fx_blur_in, "blur_bg": fx_blur_bg,
}


def clear_comp(comp: Any) -> None:
    """Remove every effect tool from a comp and wire MediaIn straight to MediaOut again.
    (Resolve won't delete a clip's only Fusion comp, so effects are cleared inside it instead.)"""
    comp.Lock()
    try:
        mi, mo = media_in(comp), media_out(comp)
        for t in list((comp.GetToolList(False) or {}).values()):
            if t is None or t in (mi, mo):
                continue
            try:
                if t.GetAttrs().get("TOOLS_RegID") in ("MediaIn", "MediaOut"):
                    continue
            except Exception:
                pass
            t.Delete()
        if mi is not None and mo is not None:
            mo.ConnectInput("Input", mi)
    finally:
        comp.Unlock()


def apply_image_fx(item: Any, fxs: List[dict], reuse: bool = False) -> Dict[str, Any]:
    """Build one Fusion comp on a timeline clip with all its image effects chained.
    reuse=True clears and rebuilds the clip's existing comp instead of adding a new one."""
    done, failed = [], []
    comp = None
    if reuse and int(item.GetFusionCompCount() or 0) > 0:
        comp = item.GetFusionCompByIndex(1)
        if comp is not None:
            clear_comp(comp)
    if not fxs:
        return {"done": done, "failed": failed}
    if comp is None:
        comp = item.AddFusionComp()
    if comp is None:
        return {"done": done, "failed": [f["type"] for f in fxs]}
    comp.Lock()
    try:
        mi, mo = media_in(comp), media_out(comp)
        s = comp_start(comp)
        cur = mi
        for fx in sorted(fxs, key=lambda f: ("zoom", "push_in", "blur_bg", "shake", "rgb_split",
                                             "glow", "halation", "blur_in").index(f["type"])
                         if f["type"] in IMAGE_BUILDERS else 99):
            builder = IMAGE_BUILDERS.get(fx["type"])
            if builder is None:
                failed.append(fx["type"])
                continue
            try:
                cur = builder(comp, cur, fx, s) or cur
                done.append(fx["type"])
            except Exception as exc:
                failed.append(f"{fx['type']}: {exc}")
        if mo is not None and cur is not mi:
            mo.ConnectInput("Input", cur)
    finally:
        comp.Unlock()
    return {"done": done, "failed": failed}


# ---------------------------------------------------------------- overlays
def _background(comp: Any, rgb=(0.0, 0.0, 0.0), alpha: float = 1.0) -> Any:
    bg = comp.AddTool("Background", -32768, -32768)
    set_inputs(bg, {"TopLeftRed": rgb[0], "TopLeftGreen": rgb[1], "TopLeftBlue": rgb[2],
                    "TopLeftAlpha": alpha})
    return bg


def ov_flash(comp, fx, s, dur):
    bg = _background(comp, (1.0, 1.0, 1.0))
    mid = s + dur * 0.35
    animate(bg, "Blend", [(s, 0.0), (mid, float(fx["params"].get("intensity", 1.0))), (s + dur, 0.0)])
    return bg


def ov_letterbox(comp, fx, s, dur):
    p = fx["params"]
    bar = p.get("bar")
    ratio = p.get("ratio")
    w, h = float(p.get("_w", 1080)), float(p.get("_h", 1920))
    if ratio:
        visible = min(1.0, (w / float(ratio)) / h)
    else:
        visible = 1.0 - 2 * float(bar if bar is not None else 0.1)
    bg = _background(comp)
    m = comp.AddTool("RectangleMask", -32768, -32768)
    set_point(m, "Center", 0.5, 0.5)
    set_inputs(m, {"Width": 2.0, "Height": max(0.05, visible), "Invert": 1.0, "SoftEdge": 0.0})
    bg.ConnectInput("EffectMask", m)
    return bg


def _ellipse_overlay(comp, fx, s, dur, size, soft, strength):
    p = fx["params"]
    bg = _background(comp)
    m = comp.AddTool("EllipseMask", -32768, -32768)
    cx, cy = p.get("center", [0.5, 0.6])
    set_point(m, "Center", cx, cy)
    set_inputs(m, {"Width": size, "Height": size * 0.62, "SoftEdge": soft, "Invert": 1.0})
    bg.ConnectInput("EffectMask", m)
    if p.get("_global"):
        set_inputs(bg, {"Blend": strength})
    else:
        animate(bg, "Blend", [(k, v * strength) for k, v in _env(s, s + dur, 8)])
    return bg


def ov_vignette(comp, fx, s, dur):
    amt = float(fx["params"].get("intensity", 1.0))
    return _ellipse_overlay(comp, fx, s, dur, size=1.25, soft=0.45, strength=0.45 * amt)


def ov_spotlight(comp, fx, s, dur):
    p = fx["params"]
    return _ellipse_overlay(comp, fx, s, dur, size=float(p.get("size", 0.6)), soft=0.18,
                            strength=float(p.get("strength", 0.6)) * float(p.get("intensity", 1.0)))


def ov_highlight_box(comp, fx, s, dur):
    p = fx["params"]
    bg = _background(comp, (1.0, 1.0, 1.0))
    cx, cy = p.get("center", [0.5, 0.6])
    w, h = float(p.get("width", 0.6)), float(p.get("height", 0.3))
    outer = comp.AddTool("RectangleMask", -32768, -32768)
    set_point(outer, "Center", cx, cy)
    set_inputs(outer, {"Width": w, "Height": h, "CornerRadius": 0.08})
    inner = comp.AddTool("RectangleMask", -32768, -32768)
    set_point(inner, "Center", cx, cy)
    t = float(p.get("thickness", 0.012))
    set_inputs(inner, {"Width": w - t, "Height": h - t * 0.56, "CornerRadius": 0.08, "PaintMode": "Subtract"})
    inner.ConnectInput("EffectMask", outer)
    bg.ConnectInput("EffectMask", inner)
    animate(bg, "Blend", _env(s, s + dur, 4))
    return bg


def ov_circle_reveal(comp, fx, s, dur):
    p = fx["params"]
    bg = _background(comp)
    m = comp.AddTool("EllipseMask", -32768, -32768)
    cx, cy = p.get("center", [0.5, 0.6])
    set_point(m, "Center", cx, cy)
    set_inputs(m, {"SoftEdge": 0.02, "Invert": 1.0})
    keys = []
    for k in range(7):
        t = k / 6.0
        e = 1 - (1 - t) ** 3
        keys.append((s + dur * t, 0.001 + 2.4 * e))
    animate(m, "Width", keys)
    animate(m, "Height", [(f, v * 0.5625) for f, v in keys])
    bg.ConnectInput("EffectMask", m)
    return bg


def ov_grain(comp, fx, s, dur):
    n = comp.AddTool("FastNoise", -32768, -32768)
    set_inputs(n, {"Detail": 10.0, "Contrast": 2.2, "Brightness": 0.0, "XScale": 0.6,
                   "SeetheRate": 4.0, "Discontinuous": 1.0})
    return n


def ov_light_leak(comp, fx, s, dur):
    p = fx["params"]
    bg = _background(comp, (1.0, 0.42, 0.08))
    m = comp.AddTool("EllipseMask", -32768, -32768)
    set_inputs(m, {"Width": 1.1, "Height": 0.9, "SoftEdge": 0.5})
    animate_point(m, "Center", [(s, 1.05, 0.9), (s + dur, 0.75, 0.6)])
    bg.ConnectInput("EffectMask", m)
    if not p.get("_global"):
        animate(bg, "Blend", _env(s, s + dur, 10))
    return bg


OVERLAY_BUILDERS = {
    "flash": ov_flash, "letterbox": ov_letterbox, "vignette": ov_vignette, "spotlight": ov_spotlight,
    "highlight_box": ov_highlight_box, "circle_reveal": ov_circle_reveal, "grain": ov_grain,
    "light_leak": ov_light_leak,
}


def build_overlay(item: Any, fx: dict, dur: int) -> bool:
    comp = item.GetFusionCompByIndex(1) or item.AddFusionComp()
    if comp is None:
        return False
    builder = OVERLAY_BUILDERS.get(fx["type"])
    if builder is None:
        return False
    fx["_range"] = fit_comp_range(comp, dur)
    comp.Lock()
    try:
        s = comp_start(comp)
        node = builder(comp, fx, s, dur)
        out = media_out(comp)
        if out is not None and node is not None:
            out.ConnectInput("Input", node)
    finally:
        comp.Unlock()
    return True


def apply_lut_comp(item, lut_path: str) -> bool:
    """Look on an adjustment clip: MediaIn (everything below) -> FileLUT -> MediaOut."""
    comp = item.AddFusionComp()
    if comp is None:
        return False
    comp.Lock()
    try:
        mi, mo = media_in(comp), media_out(comp)
        fl = comp.AddTool("FileLUT", -32768, -32768)
        if fl is None or mi is None or mo is None:
            return False
        fl.SetInput("LUTFile", str(lut_path))
        fl.ConnectInput("Input", mi)
        mo.ConnectInput("Input", fl)
        return True
    finally:
        comp.Unlock()
