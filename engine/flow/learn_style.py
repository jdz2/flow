"""Teach Flow your style: measure a timeline you cut by hand and save it as a custom style.

Measures on the current Resolve timeline:
  * pacing: average shot length -> how tight silences get cut
  * punch-ins: share of clips with zoom > 1 and the typical zoom
  * speed: typical clip speed
  * captions: Text+ clips (font, style, size, color, position, words per caption, letter case)
  * hook: a Text+ clip at the very start on the top track
  * look: LUT on node 1 of the first clips
  * transitions present or not
The result extends a built-in style, so anything not measured keeps sane defaults.
"""
from __future__ import annotations

import statistics
from typing import Any, List, Optional

from flow import resolve_bridge as rb
from flow.notes import text_of_item
from flow.styles import get_style, save_user_style
from flow.util import FlowError


def _rgb_hex(r: float, g: float, b: float) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(round(v * 255)))) for v in (r, g, b)))


def _text_props(item: Any) -> Optional[dict]:
    try:
        comp = item.GetFusionCompByIndex(1)
        tool = list((comp.GetToolList(False, "TextPlus") or {}).values())[0]
    except Exception:
        return None
    try:
        # read at the comp's last frame so intro animations (pop, fade) have settled
        end = comp.GetAttrs().get("COMPN_RenderEnd") or 0
        center = tool.GetInput("Center", end) or {}
        txt = str(tool.GetInput("StyledText") or "")
        return {
            "text": txt, "font": tool.GetInput("Font"), "font_style": tool.GetInput("Style"),
            "size": float(tool.GetInput("Size", end) or 0.07),
            "color": _rgb_hex(*(1.0 if v is None else v for v in
                                (tool.GetInput("Red1"), tool.GetInput("Green1"), tool.GetInput("Blue1")))),
            "outline": bool(tool.GetInput("Enabled2")), "box": bool(tool.GetInput("Enabled4")),
            "position": [float(center.get(1, 0.5)), float(center.get(2, 0.3))] if isinstance(center, dict) else None,
        }
    except Exception:
        return None


def learn_from_timeline(name: str, base: str = "talking-head", save: bool = True) -> dict:
    resolve = rb.connect()
    project, _, tl = rb.current(resolve)
    if tl is None:
        raise FlowError("Open the timeline you edited by hand, then run this again.")
    fps = float(tl.GetSetting("timelineFrameRate") or 30)
    v1 = [i for i in tl.GetItemListInTrack("video", 1) or [] if i.GetType() == "video"]
    if len(v1) < 3:
        raise FlowError("That timeline has fewer than 3 clips on V1; not enough to learn a style from.")
    durs = [i.GetDuration() / fps for i in v1]
    avg = statistics.mean(durs)
    zooms = []
    for i in v1:
        try:
            z = float(i.GetProperty("ZoomX") or 1.0)
        except Exception:
            z = 1.0
        zooms.append(z)
    punched = [z for z in zooms if z > 1.02]
    speeds = []
    for i in v1:
        try:
            speeds.append(float((i.GetSpeed() or {}).get("Percentage", 100.0)))
        except Exception:
            pass
    lut = ""
    try:
        lut = v1[0].GetNodeGraph().GetLUT(1) or ""
    except Exception:
        pass

    texts: List[dict] = []
    hook = None
    start = tl.GetStartFrame()
    for k in range(2, (tl.GetTrackCount("video") or 1) + 1):
        for it in tl.GetItemListInTrack("video", k) or []:
            if not text_of_item(it):
                continue
            props = _text_props(it)
            if not props:
                continue
            props["dur"] = it.GetDuration() / fps
            props["t0"] = (it.GetStart() - start) / fps
            props["track"] = k
            texts.append(props)
    if texts:
        top = max(t["track"] for t in texts)
        first = [t for t in texts if t["track"] == top and t["t0"] < 0.5 and t["dur"] >= 1.2]
        if first and len([t for t in texts if t["track"] == top]) <= 2:
            hook = first[0]
    caps = [t for t in texts if t is not hook and t["dur"] < 4.0]

    style: dict = {"name": name, "description": f"Learned from “{tl.GetName()}”", "learned_from": tl.GetName()}
    tight = 0.12 if avg < 1.5 else 0.18 if avg < 2.5 else 0.25 if avg < 4 else 0.32
    style["pacing"] = {"max_gap": tight}
    if speeds:
        sp = statistics.median(speeds)
        if 90 <= sp <= 130 and abs(sp - 100) > 2:
            style["pacing"]["speed"] = round(sp / 100.0, 3)
    ratio = len(punched) / len(v1)
    if ratio > 0.15:
        style["zoom"] = {"mode": "auto", "on_cuts": round(statistics.median(punched), 3),
                         "every": 0.0 if ratio < 0.4 else 3.5, "on_emphasis": 0.0, "max": 1.4}
    else:
        style["zoom"] = {"mode": "none"}
    if caps:
        words = [len(c["text"].split()) for c in caps]
        upper = sum(1 for c in caps if c["text"].isupper()) / len(caps)
        lower = sum(1 for c in caps if c["text"].islower()) / len(caps)
        sample = caps[len(caps) // 2]
        style["captions"] = {
            "enabled": True, "max_words": max(1, int(round(statistics.median(words)))),
            "case": "upper" if upper > 0.6 else "lower" if lower > 0.6 else "none",
            "font": sample.get("font") or "Inter", "font_style": sample.get("font_style") or "Bold",
            "size": round(statistics.median(c["size"] for c in caps), 4), "color": sample["color"],
        }
        if sample.get("position"):
            style["captions"]["position"] = sample["position"]
        if not sample["outline"]:
            style["captions"]["outline"] = None
        if sample["box"]:
            style["captions"]["box"] = {"color": "#000000", "opacity": 0.6}
    else:
        style["captions"] = {"enabled": False}
    if hook:
        style["hook"] = {"enabled": True, "duration": round(hook["dur"], 2), "font": hook.get("font") or "Inter",
                         "font_style": hook.get("font_style") or "Bold", "size": round(hook["size"], 4),
                         "color": hook["color"]}
        if hook.get("position"):
            style["hook"]["position"] = hook["position"]
    if lut:
        style["grade"] = {"lut": lut, "look": None, "intensity": 1.0, "balance": True}
    style["_measured"] = {"clips": len(v1), "avg_shot_s": round(avg, 2), "punch_in_ratio": round(ratio, 2),
                          "captions_found": len(caps), "hook_found": bool(hook), "lut": lut or None}
    if save:
        p = save_user_style({k: v for k, v in style.items() if not k.startswith("_")}, base=base)
        style["_saved"] = str(p)
        style["id"] = p.stem
        get_style(p.stem)
    return style


def format_style(st: dict) -> str:
    m = st.get("_measured", {})
    lines = [f"Style “{st['name']}” · {st.get('description', '')}",
             f"  {m.get('clips')} clips, avg shot {m.get('avg_shot_s')}s → max gap {st['pacing']['max_gap']}s",
             f"  punch-ins: {st['zoom'].get('mode')} (ratio {m.get('punch_in_ratio')})",
             f"  captions: {st['captions']}",
             f"  hook: {st.get('hook', 'none')}",
             f"  LUT: {m.get('lut')}"]
    if st.get("_saved"):
        lines.append(f"saved → {st['_saved']} (id: {st.get('id')})")
    return "\n".join(lines)
