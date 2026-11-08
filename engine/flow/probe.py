"""Capability probe: exercise every Resolve API behavior Flow depends on.

Runs ONLY inside a scratch project called "Flow Sandbox" (with --switch it saves
the current project, opens the sandbox, and switches back afterwards). Results go
to ~/.flow/capabilities.json and are used by the builder to pick working paths.
"""
from __future__ import annotations

import datetime as _dt
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict

from flow import paths, resolve_bridge as rb
from flow.util import FlowError, frames_to_tc, read_json, write_json

SANDBOX = "Flow Sandbox"
PROBE_DIR = paths.FLOW_HOME / "probe"


def _fixture() -> Path:
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    dst = PROBE_DIR / "probe_clip.mp4"
    if dst.exists():
        return dst
    src = paths.REPO_ROOT / "tests" / "fixtures" / "flow_test_clip.mp4"
    if not src.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("mkfix", paths.REPO_ROOT / "tests" / "make_fixture.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        mod.main(src)
    shutil.copy2(src, dst)
    return dst


def open_sandbox(resolve: Any, switch: bool) -> tuple:
    pm = resolve.GetProjectManager()
    cur = pm.GetCurrentProject()
    orig = cur.GetName() if cur else None
    if orig == SANDBOX:
        return cur, None
    if not switch:
        raise FlowError(f"The probe only runs in the '{SANDBOX}' project. Re-run with --switch: "
                        f"Flow saves '{orig}', opens the sandbox, and switches back when done.")
    pm.SaveProject()
    proj = pm.LoadProject(SANDBOX)
    if not proj:
        proj = pm.CreateProject(SANDBOX)
    if not proj:
        raise FlowError("Could not open or create the Flow Sandbox project.")
    return proj, orig


def return_to(resolve: Any, name: str) -> bool:
    pm = resolve.GetProjectManager()
    pm.SaveProject()
    return bool(pm.LoadProject(name))


def _tool_inputs(tool: Any) -> Dict[str, str]:
    out = {}
    try:
        for inp in (tool.GetInputList() or {}).values():
            a = inp.GetAttrs()
            out[a.get("INPS_ID")] = f"{a.get('INPS_Name')} [{a.get('INPS_DataType')}]"
    except Exception as exc:
        out["_error"] = str(exc)
    return out


def run(switch: bool = False, return_to_original: bool = True) -> dict:
    resolve = rb.connect()
    project, orig = open_sandbox(resolve, switch)
    res: Dict[str, Any] = {"ok": True, "tests": {}, "facts": {}, "product": resolve.GetProductName(),
                           "version": rb.version_string(resolve),
                           "date": _dt.datetime.now().isoformat(timespec="seconds")}
    T = res["tests"]
    F = res["facts"]

    def test(name: str, fn: Callable[[], Any]) -> Any:
        try:
            val = fn()
            ok = bool(val) if not isinstance(val, dict) else bool(val.get("ok", True))
            T[name] = {"ok": ok, "detail": val}
            return val
        except Exception as exc:
            T[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                       "trace": traceback.format_exc().splitlines()[-3:]}
            return None

    try:
        mp = project.GetMediaPool()
        clip_path = _fixture()
        from flow.find_clip import find_media_pool_item
        mpi = find_media_pool_item(mp, str(clip_path)) or (mp.ImportMedia([str(clip_path)]) or [None])[0]
        if mpi is None:
            raise FlowError("Probe could not import its test clip.")
        name = "Flow Probe " + time.strftime("%H%M%S")
        tl = mp.CreateEmptyTimeline(name)
        project.SetCurrentTimeline(tl)
        F["timeline_start"] = tl.GetStartFrame()
        fps = 30.0

        def settings():
            ok = tl.SetSettings({"useCustomSettings": "1", "timelineResolutionWidth": "1080",
                                 "timelineResolutionHeight": "1920", "timelineFrameRate": "30",
                                 "timelineInputResMismatchBehavior": "scaleToCrop",
                                 "timelineOutputResMatchTimelineRes": "1"})
            return {"ok": bool(ok), "w": tl.GetSetting("timelineResolutionWidth"),
                    "h": tl.GetSetting("timelineResolutionHeight"),
                    "mismatch": tl.GetSetting("timelineInputResMismatchBehavior")}
        test("timeline_settings", settings)
        start = tl.GetStartFrame()

        def append():
            items = mp.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": 30, "endFrame": 89,
                                          "trackIndex": 1}])
            vids = [i for i in items if i.GetType() == "video"]
            v = vids[0]
            F["append_returns"] = [i.GetType() for i in items]
            F["end_frame_inclusive"] = v.GetDuration() == 60
            F["append_duration"] = v.GetDuration()
            return {"ok": True, "duration": v.GetDuration(), "start": v.GetStart(),
                    "src": [v.GetSourceStartFrame(), v.GetSourceEndFrame()]}
        test("append_range", append)

        def append_seq():
            before_end = tl.GetEndFrame()
            items = mp.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": 150, "endFrame": 209}])
            v = [i for i in items if i.GetType() == "video"][0]
            F["append_without_record_goes_to_end"] = v.GetStart() == before_end
            return {"ok": True, "start": v.GetStart(), "before_end": before_end}
        test("append_sequential", append_seq)

        v1 = tl.GetItemListInTrack("video", 1)

        def speed():
            it = v1[1]
            d0, s0, e0 = it.GetDuration(), it.GetSourceStartFrame(), it.GetSourceEndFrame()
            ok = it.SetSpeed({"Percentage": 200.0, "PitchCorrection": True, "RippleTimeline": True})
            d1, s1, e1 = it.GetDuration(), it.GetSourceStartFrame(), it.GetSourceEndFrame()
            F["speed_mode"] = "keeps_source" if d1 < d0 else "keeps_duration"
            return {"ok": bool(ok), "before": [d0, s0, e0], "after": [d1, s1, e1], "get": it.GetSpeed()}
        test("set_speed", speed)

        def zoom():
            it = v1[0]
            ok = all([it.SetProperty("ZoomX", 1.2), it.SetProperty("ZoomY", 1.2),
                      it.SetProperty("Pan", 50.0), it.SetProperty("Tilt", -30.0)])
            return {"ok": ok, "zoom": it.GetProperty("ZoomX"), "pan": it.GetProperty("Pan"),
                    "tilt": it.GetProperty("Tilt")}
        test("zoom_pan", zoom)

        def retime():
            c = rb.const(resolve, "RETIME_OPTICAL_FLOW")
            return {"ok": bool(v1[1].SetProperty("RetimeProcess", c)), "const": c}
        test("retime_optical_flow", retime)

        def lut():
            from flow import luts
            p = paths.FLOW_LUT_DIR / "Probe" / "probe.cube"
            luts.write_cube(p, luts.build_transform("film", 0.8), "Flow probe")
            project.RefreshLUTList()
            g = v1[0].GetNodeGraph()
            n = g.GetNumNodes()
            ok_abs = g.SetLUT(1, str(p))
            got = g.GetLUT(1)
            ok_rel = None
            if not ok_abs:
                ok_rel = g.SetLUT(1, "Flow/Probe/probe.cube")
                got = g.GetLUT(1)
            F["lut_path_mode"] = "absolute" if ok_abs else ("relative" if ok_rel else "none")
            return {"ok": bool(ok_abs or ok_rel), "nodes": n, "abs": ok_abs, "rel": ok_rel, "got": got}
        test("set_lut", lut)

        def cdl():
            return bool(v1[0].SetCDL({"NodeIndex": 1, "Slope": "1.05 1.0 0.95", "Offset": "0 0 0",
                                      "Power": "1 1 1", "Saturation": 1.1}))
        test("set_cdl", cdl)

        def tl_graph():
            g = tl.GetNodeGraph()
            if g is None:
                return {"ok": False, "why": "no timeline graph"}
            n = g.GetNumNodes()
            ok = g.SetLUT(1, str(paths.FLOW_LUT_DIR / "Probe" / "probe.cube")) if n else False
            F["timeline_graph_nodes"] = n
            return {"ok": bool(ok), "nodes": n}
        test("timeline_graph_lut", tl_graph)

        def fusion_item():
            it = v1[0]
            comp = it.AddFusionComp()
            attrs = comp.GetAttrs()
            F["item_comp_range"] = [attrs.get("COMPN_RenderStart"), attrs.get("COMPN_RenderEnd"),
                                    attrs.get("COMPN_GlobalStart"), attrs.get("COMPN_GlobalEnd")]
            tools = comp.GetToolList(False)
            names = sorted(t.GetAttrs().get("TOOLS_Name") for t in tools.values())
            comp.Lock()
            mi, mo = comp.FindTool("MediaIn1"), comp.FindTool("MediaOut1")
            tr = comp.AddTool("Transform", -32768, -32768)
            tr.ConnectInput("Input", mi)
            mo.ConnectInput("Input", tr)
            s = attrs.get("COMPN_RenderStart") or 0
            tr.AddModifier("Size", "BezierSpline")
            tr.SetInput("Size", 1.0, s)
            tr.SetInput("Size", 1.3, s + 10)
            mid = tr.GetInput("Size", s + 5)
            comp.Unlock()
            F["fusion_keyframes"] = mid is not None and 1.0 < float(mid) < 1.3
            return {"ok": True, "tools": names, "mid_value": mid, "range": F["item_comp_range"]}
        test("fusion_item_comp", fusion_item)

        def tracks():
            ok = tl.AddTrack("video") and tl.AddTrack("video")
            return {"ok": bool(ok), "video_tracks": tl.GetTrackCount("video")}
        test("add_tracks", tracks)

        def title():
            before = [(i.GetStart(), i.GetDuration()) for i in tl.GetItemListInTrack("video", 1)]
            tl.SetTrackLock("video", 1, True)
            tl.SetTrackLock("audio", 1, True)
            target = start + 20
            tl.SetCurrentTimecode(frames_to_tc(target, fps))
            t = tl.InsertFusionTitleIntoTimeline("Text+")
            after = [(i.GetStart(), i.GetDuration()) for i in tl.GetItemListInTrack("video", 1)]
            F["title_insert_disturbs_v1"] = before != after
            if t is None:
                return {"ok": False, "why": "InsertFusionTitleIntoTimeline returned None"}
            pos = t.GetPosition()
            info = {"ok": True, "pos": pos, "start": t.GetStart(), "duration": t.GetDuration(),
                    "left_off": t.GetLeftOffset(), "right_off": t.GetRightOffset(), "v1_changed": before != after}
            F["title_lands_on_track"] = pos.get("trackIndex")
            F["title_lands_at_playhead"] = t.GetStart() == target
            ok_pos = t.SetPosition({"trackIndex": 3, "startFrame": start + 40})
            info["set_position"] = {"ok": bool(ok_pos), "pos": t.GetPosition(), "start": t.GetStart()}
            want = 15
            d = t.GetDuration()
            ok_trim = t.SetRightOffset(t.GetRightOffset() + (d - want))
            info["trim"] = {"ok": bool(ok_trim), "duration": t.GetDuration(), "right_off": t.GetRightOffset()}
            F["title_trim_works"] = t.GetDuration() == want
            ok_grow = t.SetRightOffset(t.GetRightOffset() - 10)
            info["grow"] = {"ok": bool(ok_grow), "duration": t.GetDuration()}
            comp = t.GetFusionCompByIndex(1)
            if comp:
                tp = comp.GetToolList(False, "TextPlus")
                tool = list(tp.values())[0] if tp else None
                if tool:
                    inputs = _tool_inputs(tool)
                    write_json(PROBE_DIR / "textplus_inputs.json", inputs)
                    tool.SetInput("StyledText", "FLOW")
                    tool.SetInput("Font", "Inter")
                    tool.SetInput("Style", "Black")
                    info["text"] = tool.GetInput("StyledText")
                    info["font"] = [tool.GetInput("Font"), tool.GetInput("Style")]
                    attrs = comp.GetAttrs()
                    F["title_comp_range"] = [attrs.get("COMPN_RenderStart"), attrs.get("COMPN_RenderEnd")]
                    F["textplus_input_count"] = len(inputs)
            tl.SetTrackLock("video", 1, False)
            tl.SetTrackLock("audio", 1, False)
            return info
        test("title_insert", title)

        def comp_clip():
            tl.SetTrackLock("video", 1, True)
            tl.SetTrackLock("audio", 1, True)
            tl.SetCurrentTimecode(frames_to_tc(start + 5, fps))
            it = tl.InsertFusionCompositionIntoTimeline()
            tl.SetTrackLock("video", 1, False)
            tl.SetTrackLock("audio", 1, False)
            if it is None:
                return {"ok": False}
            comp = it.GetFusionCompByIndex(1)
            names = sorted(t.GetAttrs().get("TOOLS_Name") for t in comp.GetToolList(False).values()) if comp else []
            ok_mode = it.SetProperty("CompositeMode", rb.const(resolve, "COMPOSITE_OVERLAY"))
            F["fusion_composition_insert"] = True
            return {"ok": True, "pos": it.GetPosition(), "duration": it.GetDuration(), "tools": names,
                    "composite_mode": bool(ok_mode)}
        test("fusion_composition_insert", comp_clip)

        def generator():
            out = {}
            tl.SetTrackLock("video", 1, True)
            tl.SetTrackLock("audio", 1, True)
            for fn, nm in (("InsertGeneratorIntoTimeline", "Adjustment Clip"),
                           ("InsertFusionGeneratorIntoTimeline", "Adjustment Clip"),
                           ("InsertGeneratorIntoTimeline", "Solid Color")):
                tl.SetCurrentTimecode(frames_to_tc(start + 70, fps))
                try:
                    it = getattr(tl, fn)(nm)
                    out[f"{fn}:{nm}"] = None if it is None else {"pos": it.GetPosition(), "type": it.GetType(),
                                                                  "name": it.GetName()}
                    if it is not None and nm == "Adjustment Clip":
                        F["adjustment_clip_insert"] = fn
                        c = it.AddFusionComp()
                        F["adjustment_clip_comp"] = c is not None
                except Exception as exc:
                    out[f"{fn}:{nm}"] = f"error {exc}"
            tl.SetTrackLock("video", 1, False)
            tl.SetTrackLock("audio", 1, False)
            return {"ok": any(isinstance(v, dict) for v in out.values()), **out}
        test("generator_insert", generator)

        def transition():
            it = tl.GetItemListInTrack("video", 1)[1]
            out = {}
            for cat in ("simple", "ofx", "fusion", None):
                opts = {"type": "Smooth Cut", "position": "start", "alignment": "center", "duration": 4}
                if cat:
                    opts["category"] = cat
                t = it.AddTransition(opts)
                out[str(cat)] = t is not None
                if t is not None:
                    F["smooth_cut_category"] = cat
                    break
            d = it.AddTransition({"type": "Cross Dissolve", "category": "simple", "position": "end",
                                  "alignment": "center", "duration": 6})
            out["cross_dissolve"] = d is not None
            return {"ok": any(out.values()), **out}
        test("transitions", transition)

        def effects():
            lst = resolve.GetEffectsList("video") or []
            write_json(PROBE_DIR / "effects_list.json", lst)
            return {"ok": bool(lst), "count": len(lst),
                    "sample": [e.get("name") for e in lst[:12]]}
        test("effects_list", effects)

        def markers():
            ok = tl.AddMarker(10, "Red", "Flow probe", "note", 1, "flow:probe")
            return {"ok": bool(ok), "markers": tl.GetMarkers()}
        test("markers", markers)

        def export_frame():
            p = PROBE_DIR / "probe_frame.jpg"
            r = tl.ExportFrame(20, {"outputType": "file", "filePath": str(p), "maxDimension": 960})
            return {"ok": bool(r and r.get("success")) and p.exists(), "result": r, "path": str(p)}
        test("export_frame", export_frame)

        def audio_modes():
            return {"ok": True, "modes": tl.GetNormalizeAudioModes()}
        test("normalize_modes", audio_modes)

        def fonts():
            fu = resolve.Fusion()
            fl = fu.FontManager.GetFontList() if fu else None
            n = len(fl or {})
            F["fusion_font_count"] = n
            return {"ok": n > 0, "count": n, "has_inter": bool(fl and "Inter" in fl)}
        test("font_list", fonts)

        def clip_enable():
            it = tl.GetItemListInTrack("video", 1)[0]
            ok = it.SetClipEnabled(False)
            st = it.GetClipEnabled()
            it.SetClipEnabled(True)
            return {"ok": bool(ok), "state_after_disable": st}
        test("clip_enable", clip_enable)

        def duplicate():
            d = tl.DuplicateTimeline(name + " dup")
            ok = d is not None
            if ok:
                project.SetCurrentTimeline(tl)
            return {"ok": ok}
        test("duplicate_timeline", duplicate)

        res["timeline"] = name
    finally:
        if orig and return_to_original:
            res["returned_to"] = orig if return_to(resolve, orig) else None
    res["ok"] = all(v.get("ok") for k, v in T.items() if k in (
        "append_range", "set_speed", "zoom_pan", "set_lut", "title_insert", "fusion_item_comp"))
    write_json(paths.CAPABILITIES_FILE, res)
    return res


def load_capabilities() -> dict:
    try:
        return read_json(paths.CAPABILITIES_FILE)
    except Exception:
        return {"facts": {}, "tests": {}}


def format_result(res: dict) -> str:
    lines = [f"Resolve {res.get('product')} {res.get('version')} · probe {'OK' if res.get('ok') else 'had failures'}"]
    for k, v in res.get("tests", {}).items():
        mark = "✓" if v.get("ok") else "✗"
        extra = v.get("error") or ""
        lines.append(f"  {mark} {k} {extra}")
    lines.append("facts: " + ", ".join(f"{k}={v}" for k, v in res.get("facts", {}).items()))
    if res.get("returned_to"):
        lines.append(f"switched back to '{res['returned_to']}'")
    return "\n".join(lines)
