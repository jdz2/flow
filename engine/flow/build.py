"""Build a Flow timeline inside DaVinci Resolve from build.json.

Order matters (learned from probing Resolve 21):
  1. new timeline (custom 9:16 settings) in a "Flow" bin
  2. text + overlay clips are inserted while V1 is still empty (Resolve inserts at the
     playhead on V1 as an insert edit), each moved to its own track and trimmed,
     with every other track locked so nothing ripples
  3. cut video laid on V1/A1 with explicit record frames (no ripple), then per clip:
     speed, zoom/pan, retime, grade (LUT + CDL), Fusion effects, transitions
  4. audio normalize, markers, preview stills
Nothing outside the new timeline is modified.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from flow import live as live_mod, paths, resolve_bridge as rb
from flow.captions import style_caption, style_title, title_config
from flow.find_clip import find_media_pool_item
from flow.fusion_fx import OVERLAY_COMPOSITE, apply_image_fx, build_overlay
from flow.util import FlowError, frames_to_tc, read_json, write_json


def _caps() -> dict:
    try:
        return read_json(paths.CAPABILITIES_FILE).get("facts", {})
    except Exception:
        return {}


class Report:
    def __init__(self):
        self.counts: Dict[str, int] = {}
        self.failed: Dict[str, int] = {}
        self.notes: List[str] = []

    def ok(self, key: str, n: int = 1):
        self.counts[key] = self.counts.get(key, 0) + n

    def fail(self, key: str, why: str = ""):
        self.failed[key] = self.failed.get(key, 0) + 1
        if why and len(self.notes) < 40:
            msg = f"{key}: {why}"
            if msg not in self.notes:
                self.notes.append(msg)


# ---------------------------------------------------------------- inserting text/overlay clips
class Inserter:
    """Insert generator-type clips at exact frames on chosen tracks without disturbing anything.

    Resolve inserts at the playhead on V1 (an insert edit). So V1 must be empty and every other
    track locked; each new clip is then moved to its destination track and trimmed.
    """

    def __init__(self, tl: Any, fps: float, report: Report):
        self.tl, self.fps, self.r = tl, fps, report
        self.start = tl.GetStartFrame()
        self.vacated: Optional[int] = None
        self.lock_state: Dict[tuple, bool] = {}

    def __enter__(self):
        tl = self.tl
        # insert edits ripple timeline markers; remember them to put them back exactly
        try:
            self.markers = dict(tl.GetMarkers() or {})
        except Exception:
            self.markers = {}
        v1 = tl.GetItemListInTrack("video", 1) or []
        if v1:  # Finish mode: park V1's clips on a temporary track
            tl.AddTrack("video")
            self.vacated = tl.GetTrackCount("video")
            if not tl.MoveItems(v1, {"trackIndex": self.vacated}, False):
                raise FlowError("Couldn't make room on V1 to place text and overlays.")
        for kind in ("video", "audio"):
            for k in range(1, (tl.GetTrackCount(kind) or 0) + 1):
                if kind == "video" and k == 1:
                    continue
                self.lock_state[(kind, k)] = bool(tl.GetIsTrackLocked(kind, k))
                tl.SetTrackLock(kind, k, True)
        tl.SetTrackLock("video", 1, False)
        return self

    def __exit__(self, *exc):
        tl = self.tl
        for (kind, k), was in self.lock_state.items():
            tl.SetTrackLock(kind, k, False)
        if self.vacated:
            parked = tl.GetItemListInTrack("video", self.vacated) or []
            if parked:
                tl.MoveItems(parked, {"trackIndex": 1}, False)
            try:
                tl.DeleteTrack("video", self.vacated)
            except Exception:
                pass
        for (kind, k), was in self.lock_state.items():
            if was:
                tl.SetTrackLock(kind, k, True)
        if self.markers is not None:
            now = tl.GetMarkers() or {}
            if set(now) != set(self.markers):
                for f in list(now):
                    tl.DeleteMarkerAtFrame(int(f))
                for f, m in sorted(self.markers.items()):
                    tl.AddMarker(int(f), m.get("color", "Blue"), m.get("name", ""), m.get("note", ""),
                                 int(m.get("duration", 1) or 1), m.get("customData", "") or "")
        return False

    def place(self, maker: Callable[[], Any], f0: int, f1: int, track: int, what: str) -> List[Any]:
        """Create clip(s) covering [f0, f1) on `track`. Long spans are tiled if needed."""
        made = []
        cur = f0
        guard = 0
        while cur < f1 and guard < 200:
            guard += 1
            self.tl.SetCurrentTimecode(frames_to_tc(self.start + cur, self.fps))
            # In/Out marks make Resolve insert the exact length (Out is inclusive)
            marked = bool(self.tl.SetMarkInOut(int(cur), int(f1 - 1)))
            try:
                it = maker()
            finally:
                if marked:
                    self.tl.ClearMarkInOut()
            if it is None:
                self.r.fail(what, "Resolve refused to insert the clip")
                break
            self.tl.SetTrackLock("video", track, False)
            moved = it.SetPosition({"trackIndex": track, "startFrame": self.start + cur})
            if not moved:
                self.r.fail(what, "couldn't move the clip to its track")
            want = f1 - cur
            d = it.GetDuration()
            if d != want:
                ro = it.GetRightOffset()
                it.SetRightOffset((ro or 0) + (d - want))
            got = it.GetDuration()
            if got > want:
                self.r.fail(what, f"couldn't trim to {want} frames (got {got})")
            self.tl.SetTrackLock("video", track, True)
            made.append(it)
            if got <= 0:
                break
            cur += got
        return made


# ---------------------------------------------------------------- helpers
def _flow_bin(mp: Any) -> Any:
    root = mp.GetRootFolder()
    for f in root.GetSubFolderList() or []:
        if f.GetName() == "Flow":
            return f
    return mp.AddSubFolder(root, "Flow")


def _next_version_name(project: Any, base: str) -> str:
    names = set()
    for i in range(1, (project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl:
            names.add(tl.GetName())
    v = 1
    while f"{base} · v{v}" in names:
        v += 1
    return f"{base} · v{v}"


def _ensure_tracks(tl: Any, n_video: int) -> None:
    while (tl.GetTrackCount("video") or 0) < n_video:
        if not tl.AddTrack("video"):
            break


def install_lut(src: str, job_name: str) -> Path:
    dest = paths.FLOW_LUT_DIR / "Jobs" / f"{job_name}.cube"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def _apply_grade(resolve: Any, project: Any, items: List[Any], grade: dict, job_name: str, r: Report,
                 on_item: Any = None, after_item: Any = None) -> None:
    lut_src = grade.get("user_lut") or grade.get("lut_file")
    lut_path = None
    if lut_src:
        try:
            lut_path = install_lut(lut_src, job_name) if not grade.get("user_lut") else Path(lut_src)
            project.RefreshLUTList()
        except Exception as exc:
            r.fail("grade", f"couldn't install LUT: {exc}")
            lut_path = None
    cdl = grade.get("cdl")
    from flow.luts import cdl_strings
    from flow import nodetree
    use_tree = bool(grade.get("nodes", True)) and nodetree.available()
    bal, exp = nodetree.split_cdl(cdl)
    if use_tree:
        nodetree.offer_powergrade(project)
    for k, it in enumerate(items):
        if on_item:
            on_item(k)
        if use_tree:
            ok_, why = nodetree.can_apply(it, replace=bool(grade.get("replace_existing")))
            if ok_:
                res = nodetree.apply_tree(it, balance=bal, exposure=exp,
                                          look_lut=str(lut_path) if lut_path else None,
                                          vignette=float(grade.get("vignette", 0.0) or 0.0))
                if res["ok"]:
                    r.ok("node_tree")
                    if lut_path:
                        r.ok("lut")
                    if after_item:
                        after_item(k)
                    continue
                r.fail("node_tree", ", ".join(res["failed"]))
            else:
                r.notes.append(f"{it.GetName()}: {why}; single-node grade instead")
        if lut_path:
            try:
                g = it.GetNodeGraph()
                if g is not None and g.SetLUT(1, str(lut_path)):
                    r.ok("lut")
                else:
                    r.fail("lut", "SetLUT returned False")
            except Exception as exc:
                r.fail("lut", str(exc))
        if cdl:
            try:
                if it.SetCDL({"NodeIndex": 1, **cdl_strings(cdl)}):
                    r.ok("cdl")
                else:
                    r.fail("cdl", "SetCDL returned False")
            except Exception as exc:
                r.fail("cdl", str(exc))


def _normalize_audio(tl: Any, target: float, r: Report) -> None:
    items = list(tl.GetItemListInTrack("audio", 1) or [])  # the voice only; B-roll sound stays low
    if not items:
        return
    try:
        modes = tl.GetNormalizeAudioModes() or []
    except Exception:
        modes = []
    mode = next((m for m in modes if "1770" in m or "EBU" in m.upper() or "LUFS" in m.upper()), None)
    opts = {"targetLoudness": float(target)} if mode else {"targetLevel": -1.0}
    if mode:
        opts["normalizationMode"] = mode
    try:
        if tl.NormalizeAudioLevel(items, opts):
            r.ok("audio_normalize")
        else:
            r.fail("audio_normalize", f"mode {mode}")
    except Exception as exc:
        r.fail("audio_normalize", str(exc))


def refresh_view(resolve: Any, project: Any, tl: Any) -> None:
    """Make Resolve evaluate freshly built Fusion clips before we export stills."""
    try:
        resolve.OpenPage("color")
        project.SetCurrentTimeline(tl)
        resolve.OpenPage("edit")
        time.sleep(1.0)
    except Exception:
        pass


def export_frames(tl: Any, frames: List[int], out_dir: Path, prefix: str = "frame") -> List[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for k, f in enumerate(frames):
        p = out_dir / f"{prefix}_{k + 1}.jpg"
        try:
            res = tl.ExportFrame(int(f), {"outputType": "file", "filePath": str(p), "maxDimension": 960})
            if res and res.get("success") and p.exists():
                got.append(str(p))
        except Exception:
            continue
    return got


def _import_into(mp: Any, folder: Any, files: List[str]) -> List[Any]:
    """Import into a bin. Resolve 21.1.2 can crash (media pool UI race) when a script switches
    the current bin and imports right away, so give the UI time to settle on both sides."""
    cur = mp.GetCurrentFolder()
    if folder is not None and (cur is None or cur.GetUniqueId() != folder.GetUniqueId()):
        mp.SetCurrentFolder(folder)
        time.sleep(1.5)
    got = mp.ImportMedia(list(files)) or []
    time.sleep(1.5)
    return got


def _broll_media(mp: Any, flow_bin: Any, entries: List[dict]) -> Dict[str, Any]:
    """Find or import every B-roll clip BEFORE the timeline is touched.
    (Importing after timeline edits crashed Resolve 21.1.2 in testing.)"""
    found: Dict[str, Any] = {}
    missing = []
    for e in entries:
        if e["path"] in found:
            continue
        mpi = find_media_pool_item(mp, e["path"], e.get("media_pool_id"))
        if mpi is None:
            missing.append(e["path"])
        found[e["path"]] = mpi
    if missing:
        sub = next((f for f in (flow_bin.GetSubFolderList() or []) if f.GetName() == "B-Roll"), None)
        if sub is None:
            sub = mp.AddSubFolder(flow_bin, "B-Roll")
            time.sleep(1.0)
        got = _import_into(mp, sub or flow_bin, sorted(set(missing)))
        from flow.find_clip import _file_path
        for it in got:
            fp = _file_path(it)
            for path in missing:
                if fp and Path(fp).resolve() == Path(path).resolve():
                    found[path] = it
    return found


def _place_broll(resolve: Any, project: Any, mp: Any, tl: Any, b: dict, start: int, track: int,
                 media: Dict[str, Any], end_inclusive: bool, r: Report, live: Any = None) -> None:
    live = live or live_mod.NULL
    retime_of = rb.const(resolve, "RETIME_OPTICAL_FLOW")
    fit = rb.const(resolve, "SCALE_FIT")
    placed = []
    nat_track = None
    for e in b["broll"]:
        mpi = media.get(e["path"])
        if mpi is None:
            r.fail("broll", f"couldn't import {e['name']}")
            continue
        info = {"mediaPoolItem": mpi, "startFrame": e["src_f0"],
                "endFrame": e["src_f1"] - (1 if end_inclusive else 0), "mediaType": 1,
                "trackIndex": track, "recordFrame": start + e["rec_f0"]}
        live.show(e["rec_f0"], 0.1, (f"AI SHOT · {e['id']} ({e['ai']})" if e.get("ai") else f"B-ROLL · {e['id']} {e['name']}")
                  + f" at {e['rec_f0'] / max(1.0, float(b['fps'])):.1f}s")
        it = (mp.AppendToTimeline([info]) or [None])[0]
        if it is None:
            r.fail("broll", f"append failed for {e['id']}")
            continue
        if abs(e["speed_pct"] - 100.0) > 0.01:
            if not it.SetSpeed({"Percentage": e["speed_pct"], "PitchCorrection": False, "RippleTimeline": False}):
                r.fail("broll_speed", e["id"])
            it.SetProperty("RetimeProcess", retime_of)
        d = it.GetDuration()
        if d != e["rec_len"]:
            it.SetRightOffset((it.GetRightOffset() or 0) + (d - e["rec_len"]))
        if it.GetDuration() > e["rec_len"]:
            r.fail("broll", f"{e['id']} couldn't be trimmed to {e['rec_len']} frames")
        if e["mode"] == "pip" and fit is not None:
            it.SetProperty("Scaling", fit)
        props = [it.SetProperty("ZoomX", float(e.get("zoom", 1.0))), it.SetProperty("ZoomY", float(e.get("zoom", 1.0))),
                 it.SetProperty("Pan", float(e.get("pan", 0.0))), it.SetProperty("Tilt", float(e.get("tilt", 0.0)))]
        if not all(props):
            r.fail("broll_framing", e["id"])
        it.SetClipColor("Purple" if e.get("ai") else "Orange")
        try:
            it.SetName((f"AI {e['id']} · {e['name']}" if e.get("ai") else f"B-roll {e['id']} · {e['name']}")[:60])
        except Exception:
            pass
        if e.get("nat"):
            if nat_track is None:
                tl.AddTrack("audio", "stereo")
                nat_track = tl.GetTrackCount("audio")
                try:
                    tl.SetTrackName("audio", nat_track, "Flow B-Roll Sound")
                except Exception:
                    pass
            n = e["nat"]
            a = (mp.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": n["src_f0"],
                                       "endFrame": n["src_f1"] - (1 if end_inclusive else 0), "mediaType": 2,
                                       "trackIndex": nat_track, "recordFrame": start + n["rec_f0"]}]) or [None])[0]
            if a is None:
                r.fail("broll_sound", e["id"])
            else:
                d = a.GetDuration()
                if d != n["rec_len"]:
                    a.SetRightOffset((a.GetRightOffset() or 0) + (d - n["rec_len"]))
                if a.SetProperty("AudioVolume", float(e["nat_db"])):
                    r.ok("broll_sound")
                else:
                    r.fail("broll_sound", "volume not set")
        placed.append((e, it))
        r.ok("broll")
        live.show(e["rec_f0"] + min(6, e["rec_len"] // 2), 0.35)
    g = b.get("grade") or {}
    if g.get("look") or g.get("user_lut"):  # same look as the speaker; the speaker's CDL balance is not for B-roll
        # AI shots carry their own look; the reel's LUT would double it
        _apply_grade(resolve, project, [it for e, it in placed if not e.get("ai")], {**g, "cdl": None},
                     Path(b["job"]).name, r)
    for e, it in placed:
        if e.get("fx"):
            res = apply_image_fx(it, e["fx"])
            for t in res["done"]:
                r.ok(f"fx:broll_{t}")
            for t in res["failed"]:
                r.fail("fx", f"B-roll {t}")
        if e.get("fade"):
            for pos in ("start", "end"):
                t = it.AddTransition({"type": "Cross Dissolve", "category": "simple", "position": pos,
                                      # inside the clip, so it fades from/to the speaker underneath
                                      "alignment": "right" if pos == "start" else "left",
                                      "duration": int(e["fade"])})
                r.ok("broll_fade") if t is not None else r.fail("broll_fade", e["id"])


# ---------------------------------------------------------------- main build
def build_job(job: Path, live: Any = None) -> dict:
    live = live or live_mod.NULL
    t0 = time.time()
    b = read_json(job / "build.json")
    caps = _caps()
    end_inclusive = bool(caps.get("end_frame_inclusive", False))
    r = Report()
    resolve = rb.connect()
    studio = rb.is_studio(resolve)
    project, mp, _ = rb.current(resolve)
    try:
        resolve.OpenPage("edit")
    except Exception:
        pass

    media = b["media"]
    mpi = find_media_pool_item(mp, media, (b.get("resolve_item") or {}).get("media_pool_id"))
    flow_bin = _flow_bin(mp)
    prev_folder = mp.GetCurrentFolder()
    if mpi is None:
        imported = _import_into(mp, flow_bin, [media])
        mpi = imported[0] if imported else None
    if mpi is None:
        raise FlowError(f"Couldn't import {media} into Resolve.")

    broll_media = _broll_media(mp, flow_bin, b.get("broll") or []) if b.get("broll") else {}

    tlcfg = b["timeline"]
    fps = float(b["fps"])
    name = _next_version_name(project, tlcfg["name"])
    mp.SetCurrentFolder(flow_bin)
    tl = mp.CreateEmptyTimeline(name)
    if prev_folder is not None:
        mp.SetCurrentFolder(prev_folder)
    if tl is None:
        raise FlowError("Resolve couldn't create the timeline.")
    project.SetCurrentTimeline(tl)
    ok = tl.SetSettings({
        "useCustomSettings": "1",
        "timelineResolutionWidth": str(tlcfg["width"]),
        "timelineResolutionHeight": str(tlcfg["height"]),
        "timelineFrameRate": tlcfg["fps_str"],
        "timelineInputResMismatchBehavior": "scaleToCrop",
        "timelineOutputResMatchTimelineRes": "1",
    })
    if not ok:
        r.fail("timeline_settings", "some settings were not accepted")
    start = tl.GetStartFrame()
    live.attach(resolve, project, tl, fps, start)
    live.front()
    live.page("edit", 0.8)
    live.say(f"EDIT · new timeline “{name}” · {len(b['segments'])} clips, {len(b.get('captions', []))} captions"
             + (f", {len(b['broll'])} B-roll" if b.get("broll") else ""))

    # track layout: V1 video | B-roll | FX lanes | title lanes | captions | hook
    fx_lanes = int(tlcfg.get("fx_lanes", 0))
    title_lanes = int(tlcfg.get("title_lanes", 0))
    broll_lanes = int(tlcfg.get("broll_lanes", 0))
    t_broll = 2
    t_fx = 2 + broll_lanes
    t_titles = t_fx + fx_lanes
    t_caps = t_titles + title_lanes
    t_hook = t_caps + 1
    _ensure_tracks(tl, t_hook)
    names = {1: "Flow Video", t_caps: "Flow Captions", t_hook: "Flow Hook"}
    if broll_lanes:
        names[t_broll] = "Flow B-Roll"
    for k in range(fx_lanes):
        names[t_fx + k] = f"Flow FX {k + 1}"
    for k in range(title_lanes):
        names[t_titles + k] = f"Flow Titles {k + 1}" if title_lanes > 1 else "Flow Titles"
    for k, nm in names.items():
        try:
            tl.SetTrackName("video", k, nm)
        except Exception:
            pass

    style = b["style"]
    total = int(tlcfg["frames"])

    # ---- 2. text + overlays while V1 is empty
    jobs = []
    for c in b.get("captions", []):
        jobs.append(("caption", c["f0"], c["f1"], t_caps, c))
    if b.get("hook"):
        jobs.append(("hook", b["hook"]["f0"], b["hook"]["f1"], t_hook, b["hook"]))
    for t in b.get("titles", []):
        jobs.append(("title", t["f0"], t["f1"], t_titles + int(t.get("lane", 0)), t))
    for o in b.get("overlays", []):
        jobs.append(("overlay", o["f0"], o["f1"], t_fx + int(o.get("lane", 0)), o))
    jobs.sort(key=lambda j: j[1])

    def title_maker():
        return tl.InsertFusionTitleIntoTimeline("Text+")

    def comp_maker():
        return tl.InsertFusionCompositionIntoTimeline()

    if jobs:
        live.say(f"TEXT · {sum(1 for j in jobs if j[0] == 'caption')} captions"
                 + (" + hook title" if b.get("hook") else "") + " (word-timed)")
    with Inserter(tl, fps, r) as ins:
        for kind, f0, f1, track, data in jobs:
            if f1 <= f0:
                continue
            live.beat(0.05)
            if kind == "overlay":
                made = ins.place(comp_maker, f0, f1, track, "overlay")
                for it in made:
                    try:
                        d = it.GetDuration()
                        fx = dict(data)
                        fx["params"] = dict(data.get("params", {}))
                        if build_overlay(it, fx, d):
                            r.ok(f"overlay:{data['type']}")
                            if fx.get("_range", {}).get("error"):
                                r.fail("comp_range", fx["_range"]["error"])
                        else:
                            r.fail(f"overlay:{data['type']}", "builder failed")
                        mode = OVERLAY_COMPOSITE.get(data["type"])
                        if mode:
                            it.SetProperty("CompositeMode", rb.const(resolve, mode[0]))
                            it.SetProperty("Opacity", mode[1])
                        it.SetClipColor("Purple")
                        try:
                            it.SetName(f"Flow {data['type']}")
                        except Exception:
                            pass
                    except Exception as exc:
                        r.fail(f"overlay:{data['type']}", str(exc))
                continue
            made = ins.place(title_maker, f0, f1, track, kind)
            for it in made:
                d = it.GetDuration()
                try:
                    if kind == "caption":
                        ok = style_caption(it, data["text"], style["captions"], bool(data.get("accent")), d,
                                           fade_out=not data.get("handoff"), fps=fps)
                        label = data["text"]
                    elif kind == "hook":
                        ok = style_caption(it, data["text"], style["hook"], False, d, fps=fps)
                        label = "Hook: " + data["text"].replace("\n", " ")
                    else:
                        cfg = title_config(data["kind"], {"captions": style["captions"],
                                                          "titles": style.get("titles", {})}, data.get("style"))
                        ok = style_title(it, data["text"], cfg, d, data.get("subtext"))
                        label = f"{data['kind']}: {data['text']}"
                    r.ok(kind) if ok else r.fail(kind, "Text+ tool not found")
                    try:
                        it.SetName(label[:60])
                    except Exception:
                        pass
                    it.SetClipColor({"caption": "Teal", "hook": "Yellow"}.get(kind, "Blue"))
                except Exception as exc:
                    r.fail(kind, str(exc))

    # ---- 3. video on V1
    v_items: List[Any] = []
    retime_of = rb.const(resolve, "RETIME_OPTICAL_FLOW")
    warp = rb.const(resolve, "MOTION_EST_SPEED_WARP_FASTER") if studio else None
    def _retime(item, s, pitch):
        if abs(float(s.get("speed_pct", 100.0)) - 100.0) <= 0.01:
            return True
        ok = item.SetSpeed({"Percentage": float(s["speed_pct"]), "PitchCorrection": pitch,
                            "RippleTimeline": False})
        return ok

    def _fit(item, want):
        d = item.GetDuration()
        if d != want:
            item.SetRightOffset((item.GetRightOffset() or 0) + (d - want))

    live.say(f"CUT · assembling {len(b['segments'])} clips: silences, fillers and retakes out")
    for s in b["segments"]:
        split = s.get("video")
        if split:  # J/L cut: sound and picture are appended separately, then linked
            v = split
            a_info = {"mediaPoolItem": mpi, "startFrame": s["src_f0"],
                      "endFrame": s["src_f1"] - (1 if end_inclusive else 0), "mediaType": 2,
                      "trackIndex": 1, "recordFrame": start + s["rec_f0"]}
            v_info = {"mediaPoolItem": mpi, "startFrame": v["src_f0"],
                      "endFrame": v["src_f1"] - (1 if end_inclusive else 0), "mediaType": 1,
                      "trackIndex": 1, "recordFrame": start + v["rec_f0"]}
            aud = (mp.AppendToTimeline([a_info]) or [None])[0]
            vid = (mp.AppendToTimeline([v_info]) or [None])[0]
            if vid is None or aud is None:
                r.fail("segment", f"split append failed at {s['rec_f0']}")
                if vid is None:
                    continue
            r.ok("segment")
            if aud is not None:
                if not _retime(aud, s, True):
                    r.fail("speed", f"SetSpeed {s['speed_pct']} (sound)")
                _fit(aud, s["rec_len"])
                try:
                    tl.SetClipsLinked([vid, aud], True)
                except Exception:
                    pass
            if not _retime(vid, s, False):
                r.fail("speed", f"SetSpeed {s['speed_pct']}")
            _fit(vid, v["rec_len"])
            if vid.GetDuration() != v["rec_len"] or (aud is not None and aud.GetDuration() != s["rec_len"]):
                r.fail("split_edit", f"lengths off at {s['rec_f0']}")
            if s.get("split_in"):
                r.ok(f"{s['split_in']['type']}_cut")
        else:
            info = {"mediaPoolItem": mpi, "startFrame": s["src_f0"],
                    "endFrame": s["src_f1"] - (1 if end_inclusive else 0),
                    "trackIndex": 1, "recordFrame": start + s["rec_f0"]}
            if not s.get("audio", True):
                info["mediaType"] = 1
            appended = mp.AppendToTimeline([info]) or []
            vid = next((i for i in appended if i.GetType() == "video"), appended[0] if appended else None)
            if vid is None:
                r.fail("segment", f"append failed at {s['rec_f0']}")
                continue
            r.ok("segment")
            if abs(float(s.get("speed_pct", 100.0)) - 100.0) > 0.01:
                ok = _retime(vid, s, bool(s.get("audio")))
                if not ok:
                    r.fail("speed", f"SetSpeed {s['speed_pct']}")
                _fit(vid, s["rec_len"])
                r.ok("speed") if ok else None
        if s["kind"] in ("ramp", "slowmo"):
            vid.SetProperty("RetimeProcess", retime_of)
            if warp is not None:
                vid.SetProperty("MotionEstimation", warp)
        z = float(s.get("zoom", 1.0))
        if abs(z - 1.0) > 1e-4 or s.get("pan") or s.get("tilt"):
            ok = all([vid.SetProperty("ZoomX", z), vid.SetProperty("ZoomY", z),
                      vid.SetProperty("Pan", float(s.get("pan", 0.0))),
                      vid.SetProperty("Tilt", float(s.get("tilt", 0.0)))])
            r.ok("zoom") if ok else r.fail("zoom", "SetProperty failed")
        color = {"cold_open": "Yellow", "ramp": "Purple", "slowmo": "Purple"}.get(s["kind"])
        if color:
            vid.SetClipColor(color)
        v_items.append((s, vid))
        note = {"ramp": "speed ramp", "slowmo": "slow motion", "cold_open": "cold open"}.get(s["kind"])
        if s.get("split_in"):
            note = f"{s['split_in']['type']}-cut"
        if abs(float(s.get("zoom", 1.0)) - 1.0) > 1e-4 and not note:
            note = f"punch-in {float(s['zoom']):.2f}x"
        live.show(s["rec_f0"] + 1, 0.12, f"CUT · {note} at {s['rec_f0'] / fps:.1f}s" if note else None)

    # grade
    g = b.get("grade") or {}
    if g.get("look") or g.get("user_lut") or g.get("cdl"):
        from flow import nodetree
        solo = nodetree.Solo(tl)
        if live.enabled:
            live.say(f"COLOR · {g.get('look') or 'LUT'} on {len(v_items)} clips"
                     + (" · node tree Input › Balance › Exposure › Look › Skin › Vignette"
                        if g.get("nodes", True) and nodetree.available() else ""))
            solo.upto(1)   # the Color page shows the speaker clip, not the captions above it
            live.show(0, 0.1)
            live.page("color", 1.0)

        def _grade_step(k):   # playhead onto the clip first, so the Color page holds it while it is graded
            vr = v_items[k][0].get("video") or v_items[k][0]
            live.show(vr["rec_f0"] + 2, 0.05, f"COLOR · clip {k + 1}/{len(v_items)}" if k % 4 == 0 else None)

        def _graded(k):       # then hold on the finished tree
            live.beat(0.45)
        _apply_grade(resolve, project, [v for _, v in v_items], g, Path(b["job"]).name, r,
                     on_item=_grade_step if live.enabled else None, after_item=_graded if live.enabled else None)
        if g.get("stills", True):
            shots = []
            for k, (s_, _) in enumerate(v_items):
                vr = s_.get("video") or s_   # picture range (J/L cuts move it)
                shots.append((vr["rec_f0"] + min(6, max(0, vr["rec_len"] // 2)),
                              f"{k + 1:02d} · {s_['kind']} @ {vr['rec_f0'] / fps:.1f}s"))
            from flow import nodetree
            st = nodetree.grab_stills(resolve, project, tl, shots, fps,
                                     name if name.startswith("Flow") else f"Flow · {name}", job / "stills")
            if st.get("stills"):
                r.ok("gallery_still", st["stills"])
                live.say(f"GALLERY · {st['stills']} reference stills in album “{st['album']}”")
            elif st.get("error"):
                r.fail("gallery", st["error"])
        live.beat(0.6)
        solo.restore()
        live.page("edit", 0.5)

    # B-roll on its own track above the speaker
    if b.get("broll"):
        _place_broll(resolve, project, mp, tl, b, start, t_broll, broll_media, end_inclusive, r, live)

    # Fusion image effects + transitions + reframe
    shown_fusion = False
    for s, vid in v_items:
        if s.get("fx"):
            live.show(s["rec_f0"] + 1, 0.1, "FX · " + ", ".join(f["type"] for f in s["fx"]) + f" at {s['rec_f0'] / fps:.1f}s")
            res = apply_image_fx(vid, s["fx"])
            if live.enabled and not shown_fusion and res["done"]:
                live.page("fusion", 1.5)   # one look at the node tree Flow just built
                live.page("edit", 0.3)
                shown_fusion = True
            for t in res["done"]:
                r.ok(f"fx:{t}")
            for t in res["failed"]:
                r.fail("fx", t)
    for s, vid in v_items:
        tr = s.get("transition_in")
        if tr:
            t = vid.AddTransition({"type": tr["type"], "category": "simple", "position": "start",
                                   "alignment": "center", "duration": int(tr["frames"])})
            r.ok("transition") if t is not None else r.fail("transition", tr["type"])
    if tlcfg.get("reframe") == "smart":
        if studio:
            for _, vid in v_items:
                try:
                    vid.SmartReframe()
                    r.ok("smart_reframe")
                except Exception as exc:
                    r.fail("smart_reframe", str(exc))
        else:
            r.fail("smart_reframe", "needs DaVinci Resolve Studio")

    # audio
    acfg = style.get("audio") or {}
    if acfg.get("normalize"):
        _normalize_audio(tl, float(acfg.get("target_lufs", -14.0)), r)
    if acfg.get("voice_isolation") and studio:
        try:
            tl.SetVoiceIsolationState(1, {"isEnabled": True, "amount": 60})
            r.ok("voice_isolation")
        except Exception as exc:
            r.fail("voice_isolation", str(exc))

    # markers
    for m in b.get("markers", []):
        try:
            if tl.AddMarker(int(m["frame"]), m.get("color", "Blue"), m.get("name", "Flow")[:60],
                            m.get("note", "")[:500], 1, "flow"):
                r.ok("marker")
        except Exception as exc:
            r.fail("marker", str(exc))

    if live.enabled:
        live.say(f"REVIEW · playing the reel ({total / fps:.1f}s)")
        mode = live.play(0, total)
        live.say("REVIEW · done" + (" (picture only; run with --play keys for sound)" if mode == "scrub" else ""))
    try:
        tl.SetCurrentTimecode(frames_to_tc(start, fps))
    except Exception:
        pass

    # preview stills for checking the result
    picks = []
    if b.get("hook"):
        picks.append(min(total - 1, max(0, b["hook"]["f1"] // 2)))
    accent = next((c for c in b.get("captions", []) if c.get("accent")), None)
    if accent:
        picks.append(accent["f0"] + 3)
    picks += [int(total * 0.45), int(total * 0.8)]
    refresh_view(resolve, project, tl)
    preview = export_frames(tl, sorted(set(p for p in picks if 0 <= p < total))[:4], job / "preview")

    v_count = len(tl.GetItemListInTrack("video", 1) or [])
    report = {
        "ok": bool(v_items) and not r.failed.get("segment"),
        "timeline": name, "project": project.GetName(), "studio": studio,
        "counts": r.counts, "failed": r.failed, "problems": r.notes,
        "v1_clips": v_count, "duration_s": round(total / fps, 2),
        "preview": preview, "seconds": round(time.time() - t0, 1),
        "warnings": b.get("warnings", []),
    }
    write_json(job / "report.json", report)
    return report


def format_report(rep: dict) -> str:
    c = rep.get("counts", {})
    lines = [
        f"{'Done' if rep.get('ok') else 'Finished with problems'} in {rep.get('seconds')}s → timeline “{rep.get('timeline')}”"
        f" ({rep.get('duration_s')}s)",
        f"clips {c.get('segment', 0)} · B-roll {c.get('broll', 0)} · J/L cuts {c.get('J_cut', 0)}/{c.get('L_cut', 0)}"
        f" · captions {c.get('caption', 0)} · hook {c.get('hook', 0)}"
        f" · titles {c.get('title', 0)} · zooms {c.get('zoom', 0)} · speed {c.get('speed', 0)}"
        f" · LUT {c.get('lut', 0)} · transitions {c.get('transition', 0)} · markers {c.get('marker', 0)}",
    ]
    if c.get("node_tree") or c.get("gallery_still"):
        lines.append(f"node trees {c.get('node_tree', 0)} · gallery stills {c.get('gallery_still', 0)}")
    fx = {k: v for k, v in c.items() if k.startswith(("fx:", "overlay:"))}
    if fx:
        lines.append("effects " + ", ".join(f"{k.split(':')[1]}×{v}" for k, v in fx.items()))
    if rep.get("failed"):
        lines.append("failed: " + ", ".join(f"{k}×{v}" for k, v in rep["failed"].items()))
    for p in rep.get("problems", [])[:10]:
        lines.append(f"  · {p}")
    for w in rep.get("warnings", []):
        lines.append(f"warning: {w}")
    if rep.get("preview"):
        lines.append("preview: " + ", ".join(rep["preview"]))
    return "\n".join(lines)


def export_timeline_frames(timeline_name: Optional[str], count: int = 4, job: Optional[str] = None) -> dict:
    resolve = rb.connect()
    project, _, tl = rb.current(resolve)
    if timeline_name:
        from flow.notes import _timeline_by_name
        tl = _timeline_by_name(project, timeline_name)
    if tl is None:
        raise FlowError("Timeline not found.")
    n = tl.GetEndFrame() - tl.GetStartFrame()
    frames = [int(n * (k + 0.5) / count) for k in range(count)]
    out = (paths.resolve_job(job) if job else paths.FLOW_HOME) / "preview"
    return {"timeline": tl.GetName(), "frames": export_frames(tl, frames, out, "check")}
