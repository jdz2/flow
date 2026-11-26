"""Style Text+ titles for captions, hooks and the titles system."""
from __future__ import annotations

from typing import Any, Optional

from flow.fusion_util import animate, comp_start, first_tool, fit_comp_range, hex_rgb, media_out, set_inputs, set_point

# Text+ shading elements: 1 fill, 2 outline, 3 shadow, 4 background box.
APPEARANCE_BORDER_FILL = 2
LEVEL_LINE = 1

TITLE_DEFAULTS = {
    "title": {"font_style": "Bold", "size": 0.085, "case": "upper", "color": "#FFFFFF", "position": [0.5, 0.55],
              "animation": "tracking", "tracking": 1.0, "duration": 3.0, "sub_size": 0.034, "shadow": True},
    "chapter": {"font_style": "Bold", "size": 0.06, "case": "upper", "color": "#FFFFFF", "position": [0.5, 0.5],
                "animation": "fade", "tracking": 1.0, "duration": 2.0, "sub_size": 0.03, "shadow": True},
    "lower_third": {"font_style": "Bold", "size": 0.055, "case": "none", "color": "#FFFFFF", "position": [0.07, 0.3],
                    "align": "left", "animation": "slide", "duration": 3.5, "sub_size": 0.042,
                    "box": {"color": "#000000", "opacity": 0.55}},
    "location": {"font_style": "SemiBold", "size": 0.046, "case": "upper", "color": "#FFFFFF", "position": [0.5, 0.82],
                 "animation": "tracking", "tracking": 1.0, "duration": 3.0, "sub_size": 0.032, "shadow": True},
    "end_card": {"font_style": "Bold", "size": 0.075, "case": "none", "color": "#FFFFFF", "position": [0.5, 0.52],
                 "animation": "fade", "duration": 3.0, "sub_size": 0.045, "sub_style": "SemiBold", "shadow": True},
    "quote": {"font_style": "Medium", "size": 0.052, "case": "none", "color": "#FFFFFF", "position": [0.5, 0.5],
              "animation": "fade", "duration": 3.0, "sub_size": 0.03, "shadow": True},
}


def text_tool(item: Any) -> tuple:
    comp = item.GetFusionCompByIndex(1)
    if comp is None:
        return None, None
    return comp, first_tool(comp, "TextPlus")


def _style_text(tool: Any, text: str, cfg: dict, accent: bool = False, size_scale: float = 1.0) -> None:
    font = cfg.get("font") or "Inter"
    style = cfg.get("font_style") or "Bold"
    fill = hex_rgb(cfg.get("accent") if accent and cfg.get("accent") else cfg.get("color", "#FFFFFF"))
    set_inputs(tool, {"StyledText": text, "Font": font, "Style": style,
                      "Size": float(cfg.get("size", 0.065)) * size_scale,
                      "Red1": fill[0], "Green1": fill[1], "Blue1": fill[2], "Alpha1": 1.0})
    if cfg.get("tracking"):
        set_inputs(tool, {"CharacterSpacing": float(cfg["tracking"])})
    if cfg.get("line_spacing"):
        set_inputs(tool, {"LineSpacing": float(cfg["line_spacing"])})
    align = cfg.get("align", "center")
    if align == "left":
        set_inputs(tool, {"HorizontalJustificationNew": 0.0, "HorizontalJustificationLeft": 1.0})
    pos = cfg.get("position") or [0.5, 0.3]
    set_point(tool, "Center", pos[0], pos[1])

    outline = cfg.get("outline")
    if outline:
        r, g, b = hex_rgb(outline.get("color", "#000000"))
        set_inputs(tool, {"Enabled2": 1.0, "Red2": r, "Green2": g, "Blue2": b, "Alpha2": 1.0,
                          "Thickness2": float(outline.get("width", 0.05))})
    else:
        set_inputs(tool, {"Enabled2": 0.0})

    shadow = cfg.get("shadow")
    if shadow:
        sh = shadow if isinstance(shadow, dict) else {"color": "#000000", "opacity": 0.55,
                                                      "offset": [0.0, -0.005], "softness": 0.03}
        r, g, b = hex_rgb(sh.get("color", "#000000"))
        set_inputs(tool, {"Enabled3": 1.0, "Red3": r, "Green3": g, "Blue3": b,
                          "Alpha3": float(sh.get("opacity", 0.5)), "Softness3": float(sh.get("softness", 0.03))})
        off = sh.get("offset", [0.0, -0.005])
        set_point(tool, "Offset3", off[0], off[1])
    else:
        set_inputs(tool, {"Enabled3": 0.0})

    box = cfg.get("box")
    if box:
        r, g, b = hex_rgb(box.get("color", "#000000"))
        set_inputs(tool, {"Enabled4": 1.0, "ElementShape4": APPEARANCE_BORDER_FILL, "Level4": LEVEL_LINE,
                          "Red4": r, "Green4": g, "Blue4": b, "Alpha4": float(box.get("opacity", 0.8)),
                          "ExtendHorizontal4": float(box.get("pad_x", 0.22)),
                          "ExtendVertical4": float(box.get("pad_y", 0.14)),
                          "Round4": float(box.get("round", 0.25))})
    else:
        set_inputs(tool, {"Enabled4": 0.0})


# Widest a centred line may get (share of frame width): clears the right-hand UI rail on Reels/TikTok.
MAX_LINE_WIDTH = 0.78


def fit_scale(text: str, cfg: dict) -> float:
    """Shrink factor so the longest line fits MAX_LINE_WIDTH. Text+ Size is roughly the em as a share
    of frame width; average glyph width is ~0.66 em for caps in heavy weights, ~0.56 em otherwise."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return 1.0
    longest = max(lines, key=len)
    heavy = (cfg.get("font_style") or "").lower() in ("black", "heavy", "extrabold", "extra bold")
    per = 0.66 if (longest.isupper() or heavy) else 0.56
    outline = float((cfg.get("outline") or {}).get("width", 0.0)) if isinstance(cfg.get("outline"), dict) else 0.0
    est = len(longest) * float(cfg.get("size", 0.065)) * per * (1.0 + outline)
    return 1.0 if est <= MAX_LINE_WIDTH else max(0.6, MAX_LINE_WIDTH / est)


def _animate(tool: Any, anim: str, size: float, start: float, dur: int, cfg: dict,
             fade_out: bool = True, fps: float = 30.0, accent: bool = False) -> None:
    """Keyframe the entrance. Timings are authored at 30 fps and scaled to the timeline rate.
    fade_out=False for a caption that hands straight over to the next one (no blink between them)."""
    k = max(0.5, float(fps or 30.0) / 30.0)

    def f(n: float) -> float:
        return start + max(1, int(round(n * k)))

    end = start + max(1, dur - 1)
    if anim == "pop":
        animate(tool, "Size", [(start, size * 0.86), (f(3), size * 1.03), (f(5), size)])
    elif anim == "pop_strong":
        # every caption pops; only an emphasised word gets the big one
        lo, hi = (0.65, 1.08) if accent else (0.8, 1.05)
        animate(tool, "Size", [(start, size * lo), (f(3), size * hi), (f(5), size * 0.99), (f(7), size)])
    elif anim in ("fade", "slide", "tracking"):
        fin = min(int(round(4 * k)), max(1, dur // 4))
        keys = [(start, 0.0), (start + fin, 1.0)]
        if fade_out and dur > 12 * k:
            keys += [(end - fin, 1.0), (end, 0.0)]
        animate(tool, "Blend", keys)
        if anim == "slide":
            pos = cfg.get("position") or [0.5, 0.3]
            dx = 0.04 if cfg.get("align") == "left" else 0.0
            dy = 0.0 if dx else -0.02
            try:
                from flow.fusion_util import animate_point
                animate_point(tool, "Center", [(start, pos[0] - dx, pos[1] + dy), (f(6), pos[0], pos[1])])
            except Exception:
                pass
        if anim == "tracking":
            base = float(cfg.get("tracking", 1.0))
            animate(tool, "CharacterSpacing", [(start, base + 0.22), (start + max(8, int(dur * 0.7)), base)])


def style_caption(item: Any, text: str, ccfg: dict, accent: bool, dur: int,
                  fade_out: bool = True, fps: float = 30.0) -> bool:
    comp, tool = text_tool(item)
    if tool is None:
        return False
    fit_comp_range(comp, dur)
    scale = fit_scale(text, ccfg)
    comp.Lock()
    try:
        _style_text(tool, text, ccfg, accent, size_scale=scale)
        _animate(tool, ccfg.get("animation", "none"), float(ccfg.get("size", 0.065)) * scale, comp_start(comp),
                 dur, ccfg, fade_out=fade_out, fps=fps, accent=accent)
    finally:
        comp.Unlock()
    return True


def style_title(item: Any, text: str, cfg: dict, dur: int, subtext: Optional[str] = None) -> bool:
    """Titles system: main line + optional smaller subline (second Text+ in the same comp)."""
    comp, tool = text_tool(item)
    if tool is None:
        return False
    fit_comp_range(comp, dur)
    comp.Lock()
    try:
        _style_text(tool, text, cfg)
        start = comp_start(comp)
        _animate(tool, cfg.get("animation", "fade"), float(cfg.get("size", 0.07)), start, dur, cfg)
        if subtext:
            sub = comp.AddTool("TextPlus", -32768, -32768)
            sub_cfg = dict(cfg)
            sub_cfg.update({"size": float(cfg.get("sub_size", 0.03)), "font_style": cfg.get("sub_style", "Regular"),
                            "box": None, "tracking": cfg.get("sub_tracking", 1.0)})
            pos = cfg.get("position") or [0.5, 0.5]
            gap = float(cfg.get("size", 0.07)) * 0.55 + float(sub_cfg["size"]) * 0.45
            sub_cfg["position"] = [pos[0], pos[1] - gap]
            _style_text(sub, subtext, sub_cfg)
            _animate(sub, "fade", float(sub_cfg["size"]), start + 4, max(2, dur - 4), sub_cfg)
            merge = comp.AddTool("Merge", -32768, -32768)
            merge.ConnectInput("Background", tool)
            merge.ConnectInput("Foreground", sub)
            out = media_out(comp)
            if out is not None:
                out.ConnectInput("Input", merge)
    finally:
        comp.Unlock()
    return True


def title_config(kind: str, style: dict, override: Optional[dict] = None) -> dict:
    """Title look: built-in kind defaults <- style['titles'][kind] <- per-title override."""
    base = dict(TITLE_DEFAULTS.get(kind, TITLE_DEFAULTS["title"]))
    base.setdefault("font", (style.get("captions") or {}).get("font", "Inter"))
    st = (style.get("titles") or {}).get(kind) or {}
    base.update(st)
    if override:
        base.update({k: v for k, v in override.items() if v is not None})
    if base.get("shadow") is True:
        base["shadow"] = {"color": "#000000", "opacity": 0.55, "offset": [0.0, -0.004], "softness": 0.04}
    return base
