"""Thin helpers over Fusion's scripting objects (via Resolve's fusionscript)."""
from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence, Tuple


def hex_rgb(color: Optional[str]) -> Tuple[float, float, float]:
    if not color:
        return (1.0, 1.0, 1.0)
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0)


def set_point(tool: Any, name: str, x: float, y: float, frame: Optional[float] = None) -> bool:
    for val in ({1: float(x), 2: float(y)}, {1.0: float(x), 2.0: float(y)}, [float(x), float(y)]):
        try:
            ok = tool.SetInput(name, val, frame) if frame is not None else tool.SetInput(name, val)
            if ok is not False:
                return True
        except Exception:
            continue
    return False


def set_inputs(tool: Any, values: dict) -> None:
    for k, v in values.items():
        try:
            tool.SetInput(k, v)
        except Exception:
            pass


def animate(tool: Any, name: str, keys: Iterable[Tuple[float, float]]) -> bool:
    """Keyframe a numeric input. keys = [(frame, value), ...]."""
    keys = list(keys)
    if not keys:
        return False
    try:
        tool.AddModifier(name, "BezierSpline")
        for f, v in keys:
            tool.SetInput(name, float(v), float(f))
        return True
    except Exception:
        try:
            tool.SetInput(name, float(keys[-1][1]))
        except Exception:
            pass
        return False


def animate_point(tool: Any, name: str, keys: Sequence[Tuple[float, float, float]]) -> bool:
    """Keyframe a point input via an XY Path modifier: keys = [(frame, x, y)]."""
    try:
        tool.AddModifier(name, "XYPath")
        path = None
        try:
            path = tool[name].GetConnectedOutput().GetTool()
        except Exception:
            path = None
        if path is None:
            for f, x, y in keys:
                set_point(tool, name, x, y, f)
            return True
        path.AddModifier("X", "BezierSpline")
        path.AddModifier("Y", "BezierSpline")
        for f, x, y in keys:
            path.SetInput("X", float(x), float(f))
            path.SetInput("Y", float(y), float(f))
        return True
    except Exception:
        return False


def comp_start(comp: Any) -> float:
    try:
        return float(comp.GetAttrs().get("COMPN_RenderStart") or 0.0)
    except Exception:
        return 0.0


def first_tool(comp: Any, reg_id: str) -> Any:
    try:
        tools = comp.GetToolList(False, reg_id) or {}
        return list(tools.values())[0] if tools else None
    except Exception:
        return None


def media_out(comp: Any) -> Any:
    return comp.FindTool("MediaOut1") or first_tool(comp, "MediaOut")


def media_in(comp: Any) -> Any:
    return comp.FindTool("MediaIn1") or first_tool(comp, "MediaIn")


def fit_comp_range(comp: Any, frames: int) -> dict:
    """Clips inserted longer than Resolve's default generator length keep a 150-frame Fusion range
    until the project reloads. Stretch the comp's global and render range to the clip length."""
    try:
        a = comp.GetAttrs() or {}
        g0 = float(a.get("COMPN_GlobalStart") or 0.0)
        r0 = float(a.get("COMPN_RenderStart") or 0.0)
        end = r0 + max(1, int(frames)) - 1
        if float(a.get("COMPN_RenderEnd") or 0.0) < end or float(a.get("COMPN_GlobalEnd") or 0.0) < end:
            comp.SetAttrs({"COMPN_GlobalStart": min(g0, r0), "COMPN_GlobalEnd": max(end, float(a.get("COMPN_GlobalEnd") or 0.0)),
                           "COMPN_RenderStart": r0, "COMPN_RenderEnd": end})
        b = comp.GetAttrs() or {}
        return {"before": [a.get("COMPN_RenderStart"), a.get("COMPN_RenderEnd")],
                "after": [b.get("COMPN_RenderStart"), b.get("COMPN_RenderEnd")]}
    except Exception as exc:
        return {"error": str(exc)}
