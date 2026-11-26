"""Live check of `flow magnific` in a running Resolve, inside the "Flow Sandbox" project.

    flow magnific selftest --switch [--url <a delivered Magnific link> --creation ID --name TITLE]

Like `flow probe --switch`: saves the project that is open, opens the sandbox, and switches back at
the end. It runs on its own Flow home and folder (your Magnific folder, settings and watcher are not
touched) with clips it makes with ffmpeg, on a new timeline "Flow Magnific Live <time>" that stays in
the sandbox to look at. Every step is written to ~/.flow/probe/magnific_live.json as soon as it ends,
so if Resolve quits, that file shows where.

It checks the watch folder (import into Flow › Magnific with metadata, the bin view put back, WebP,
playhead placement of a video, a still, a WAV and a video with sound, undo, the background watcher)
and probes what the next Magnific steps need: a new version as a take on a clip, a clip dropped into
a gap between two clips (exactly, and too long), ReplaceClip, markers that carry a slot id, and the
provenance tags in a file Magnific delivered.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from flow import paths
from flow.util import FlowError, frames_to_tc, run, which, write_json

FPS = 30
W, H = 1080, 1920
PROMPT = "Drone shot over a small harbour at dusk, slow push in, warm lights on the water"
CORE = ("bin_import", "playhead_video", "playhead_still", "playhead_audio", "playhead_sound", "undo", "watcher")


# ---------------------------------------------------------------- test media (ffmpeg)
def _ff(args: List[str]) -> None:
    exe = which("ffmpeg")
    if not exe:
        raise FlowError("ffmpeg not found (brew install ffmpeg)")
    run([exe, "-nostdin", "-y", "-v", "error", *args])


def _pic(pattern: str, seconds: float, size: str = f"{W}x{H}") -> List[str]:
    join = ":" if "=" in pattern else "="          # color=c=…:size=… vs testsrc2=size=…
    return ["-f", "lavfi", "-i", f"{pattern}{join}size={size}:rate={FPS}:duration={seconds}"]


def _tone(freq: int, seconds: float) -> List[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}"]


PRORES = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]


def _h264(inputs: List[str], out: Path) -> None:
    last = None
    for enc in (["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"],
                ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p"]):
        try:
            _ff(inputs + enc + ["-c:a", "aac", "-shortest", str(out)])
            return
        except FlowError as exc:
            last = exc
    raise last  # type: ignore[misc]


def make_media(d: Path) -> dict:
    """The clips the test drops into its folder. Kept between runs (made once)."""
    d.mkdir(parents=True, exist_ok=True)
    jobs: Dict[str, Callable[[Path], None]] = {
        "aroll.mp4": lambda p: _h264(_pic("testsrc2", 6) + _tone(220, 6), p),
        "drone_v1.mov": lambda p: _ff(_pic("smptehdbars", 3) + PRORES + [str(p)]),
        "still16.png": lambda p: _ff(_pic("testsrc2", 1) + ["-frames:v", "1", "-pix_fmt", "rgb48be", str(p)]),
        "tone.wav": lambda p: _ff(_tone(440, 2) + ["-c:a", "pcm_s24le", str(p)]),
        "web.webp": lambda p: _ff(_pic("testsrc", 1, "540x960") + ["-frames:v", "1", "-c:v", "libwebp", str(p)]),
        "ph_video.mov": lambda p: _ff(_pic("rgbtestsrc", 3) + PRORES + [str(p)]),
        "ph_still.png": lambda p: _ff(_pic("smptehdbars", 1) + ["-frames:v", "1", "-pix_fmt", "rgb48be", str(p)]),
        "ph_tone.wav": lambda p: _ff(_tone(660, 2) + ["-c:a", "pcm_s24le", str(p)]),
        "ph_sound.mp4": lambda p: _h264(_pic("testsrc", 2) + _tone(330, 2), p),
        "watch_me.mov": lambda p: _ff(_pic("yuvtestsrc", 2) + PRORES + [str(p)]),
        "gapfill.mov": lambda p: _ff(_pic("color=c=0x6a0dad", 4) + PRORES + [str(p)]),
        "replace_a.mov": lambda p: _ff(_pic("color=c=0xc0392b", 2) + PRORES + [str(p)]),
        "replace_b.mov": lambda p: _ff(_pic("color=c=0x27ae60", 2) + PRORES + [str(p)]),
    }
    files, skipped = {}, {}
    for name, make in jobs.items():
        p = d / name
        try:
            if not p.exists():
                tmp = p.with_name(".tmp-" + name)
                make(tmp)
                os.replace(tmp, p)
            files[name] = str(p)
        except FlowError as exc:
            skipped[name] = str(exc).splitlines()[0]
    need = set(jobs) - {"web.webp"}
    return {"ok": need <= set(files), "files": files, "skipped": skipped}


# ---------------------------------------------------------------- Resolve helpers
def _bin(mp: Any, names=("Flow", "Magnific")) -> Any:
    f = mp.GetRootFolder()
    for n in names:
        f = next((s for s in (f.GetSubFolderList() or []) if s.GetName() == n), None)
        if f is None:
            return None
    return f


def _uids(folder: Any) -> set:
    return {c.GetUniqueId() for c in (folder.GetClipList() or [])} if folder is not None else set()


def _call(obj: Any, fn: str, *a: Any) -> Any:
    try:
        return getattr(obj, fn)(*a)
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"


def _meta(m: Any) -> dict:
    return {"name": _call(m, "GetName"), "color": _call(m, "GetClipColor"),
            "keywords": _call(m, "GetMetadata", "Keywords"), "comments": _call(m, "GetMetadata", "Comments"),
            "description": _call(m, "GetMetadata", "Description"),
            "creation": _call(m, "GetThirdPartyMetadata", "Magnific Creation"),
            "frames": _call(m, "GetClipProperty", "Frames"), "fps": _call(m, "GetClipProperty", "FPS"),
            "resolution": _call(m, "GetClipProperty", "Resolution"),
            "codec": _call(m, "GetClipProperty", "Video Codec"), "duration": _call(m, "GetClipProperty", "Duration")}


def _snap(tl: Any, skip=()) -> dict:
    """Every clip on every track (start, length, name), but the tracks named in skip."""
    out = {}
    for lane in ("video", "audio"):
        for k in range(1, int(tl.GetTrackCount(lane) or 0) + 1):
            name = tl.GetTrackName(lane, k) or f"{lane} {k}"
            if name in skip:
                continue
            out[f"{lane}{k} {name}"] = [[it.GetStart(), it.GetDuration(), it.GetName()]
                                        for it in tl.GetItemListInTrack(lane, k) or []]
    return out


def _track(tl: Any, lane: str, name: str):
    for k in range(1, int(tl.GetTrackCount(lane) or 0) + 1):
        if (tl.GetTrackName(lane, k) or "") == name:
            return k, list(tl.GetItemListInTrack(lane, k) or [])
    return None, []


def _at(tl: Any, lane: str, k: int, frame: int) -> Any:
    return next((it for it in tl.GetItemListInTrack(lane, k) or [] if it.GetStart() == frame), None)


def _clip_name(it: Any) -> Any:
    try:
        return it.GetMediaPoolItem().GetName()
    except Exception as exc:
        return f"error: {exc}"


def _take(info: Any) -> Any:
    if not isinstance(info, dict):
        return info
    m = info.get("mediaPoolItem")
    return {"startFrame": info.get("startFrame"), "endFrame": info.get("endFrame"),
            "clip": _call(m, "GetName") if m is not None else None}


def _brief(r: dict) -> dict:
    return {k: r.get(k) for k in ("imported", "placed", "waiting", "failed", "notes", "blocked") if r.get(k)}


def _tags(p: str) -> dict:
    exe = which("ffprobe")
    if not exe:
        return {}
    proc = run([exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", p], check=False)
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return {}
    cut = lambda d: {k: (str(v)[:160]) for k, v in (d or {}).items()}  # noqa: E731
    fmt = data.get("format", {})
    return {"format": fmt.get("format_name"), "format_tags": cut(fmt.get("tags")),
            "stream_tags": [cut(s.get("tags")) for s in data.get("streams", []) if s.get("tags")]}


# ---------------------------------------------------------------- the run
def run_live(switch: bool = False, url: Optional[str] = None, creation: Optional[str] = None,
             name: Optional[str] = None, prompt: Optional[str] = None, model: Optional[str] = None,
             out: Callable[[str], None] = print) -> dict:
    from flow import magnific as mg, probe, resolve_bridge as rb
    from flow.build import _caps, _import_into
    from flow.find_clip import _file_path, find_media_pool_item

    real_home = paths.FLOW_HOME
    report = real_home / "probe" / "magnific_live.json"
    base = real_home / "probe" / "magnific_live"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    home, root = base / f"home-{stamp}", base / f"folder-{stamp}"
    res: Dict[str, Any] = {"ok": False, "date": _dt.datetime.now().isoformat(timespec="seconds"),
                           "tests": {}, "facts": {}, "home": str(home), "folder": str(root), "report": str(report)}
    T, F = res["tests"], res["facts"]
    keep: Dict[str, Any] = {}

    def save() -> None:
        try:
            write_json(report, res)
        except Exception:
            pass

    def test(label: str, fn: Callable[[], Any], need: Optional[List[str]] = None) -> Any:
        missing = [n for n in (need or []) if n not in keep]
        if missing:
            T[label] = {"ok": False, "skipped": f"needs {', '.join(missing)} from an earlier step"}
            out(f"- {label}: skipped ({T[label]['skipped']})")
            save()
            return None
        out(f"… {label}")
        t0 = time.time()
        val = None
        try:
            val = fn()
            ok = bool(val.get("ok", True)) if isinstance(val, dict) else bool(val)
            T[label] = {"ok": ok, "detail": val}
        except Exception as exc:
            T[label] = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "trace": traceback.format_exc().splitlines()[-4:]}
        T[label]["seconds"] = round(time.time() - t0, 1)
        out(f"{'✓' if T[label]['ok'] else '✗'} {label}" + (f": {T[label].get('error')}" if T[label].get("error") else ""))
        save()
        return val

    made = test("make_media", lambda: make_media(base / "media"))
    if not made or not made.get("ok"):
        return res
    m = made["files"]

    old_env = os.environ.get("FLOW_HOME")
    os.environ["FLOW_HOME"] = str(home)       # the background watcher this test starts uses it too
    paths.FLOW_HOME = home
    home.mkdir(parents=True, exist_ok=True)
    if paths.CAPABILITIES_FILE.exists():      # the real probe results (the constant still points there)
        shutil.copy2(paths.CAPABILITIES_FILE, home / "capabilities.json")
    end_incl = bool(_caps().get("end_frame_inclusive", False))
    F["caps_end_frame_inclusive"] = end_incl
    e = lambda n: n - (1 if end_incl else 0)  # noqa: E731  (AppendToTimeline endFrame for n frames)

    resolve, orig = None, None
    try:
        resolve = rb.connect()
        res["product"], res["version"] = resolve.GetProductName(), rb.version_string(resolve)
        project, orig = probe.open_sandbox(resolve, switch)
        res["original_project"] = orig
        save()
        mp = project.GetMediaPool()

        def setup():
            a = find_media_pool_item(mp, m["aroll.mp4"]) or (mp.ImportMedia([m["aroll.mp4"]]) or [None])[0]
            if a is None:
                raise FlowError("Resolve didn't import the test A-roll")
            nm = "Flow Magnific Live " + time.strftime("%H%M%S")
            tl = mp.CreateEmptyTimeline(nm)
            project.SetCurrentTimeline(tl)
            tl.SetSettings({"useCustomSettings": "1", "timelineResolutionWidth": str(W),
                            "timelineResolutionHeight": str(H), "timelineFrameRate": str(FPS),
                            "timelineInputResMismatchBehavior": "scaleToFit",
                            "timelineOutputResMatchTimelineRes": "1"})
            start = int(tl.GetStartFrame())
            fps = float(tl.GetSetting("timelineFrameRate") or FPS)
            got = mp.AppendToTimeline([{"mediaPoolItem": a, "startFrame": 0, "endFrame": e(int(6 * fps)),
                                        "trackIndex": 1, "recordFrame": start}]) or []
            keep.update(tl=tl, start=start, fps=fps, aroll=a)
            return {"ok": bool(got), "timeline": nm, "start": start, "fps": fps,
                    "size": [tl.GetSetting("timelineResolutionWidth"), tl.GetSetting("timelineResolutionHeight")],
                    "edit": _snap(tl)}
        test("setup_timeline", setup)

        def tl_():
            return keep["tl"]

        def bin_import():
            tl = tl_()
            mg.set_folder(str(root))
            mg.set_place("bin")
            pulled = mg.pull(file=m["drone_v1.mov"], name="Harbour dusk drone", creation="flowtest-1",
                             prompt=PROMPT, model="flow-test")
            keep["drone"] = pulled["file"]
            shutil.copy2(m["still16.png"], root / "still16.png")
            shutil.copy2(m["tone.wav"], root / "tone.wav")
            prev = mp.GetCurrentFolder()
            prev_id = prev.GetUniqueId() if prev else None
            before, counts = _snap(tl), (tl.GetTrackCount("video"), tl.GetTrackCount("audio"))
            r = mg.sync(wait=True)
            in_bin = _uids(_bin(mp))
            got = {}
            for k, p in (("drone", pulled["file"]), ("still", str(root / "still16.png")), ("tone", str(root / "tone.wav"))):
                it = find_media_pool_item(mp, p)
                got[k] = None if it is None else {**_meta(it), "in_bin": it.GetUniqueId() in in_bin}
            cur = mp.GetCurrentFolder()
            d = got.get("drone") or {}
            F["metadata_comments"] = d.get("comments") == PROMPT
            F["metadata_description"] = d.get("description") == "Harbour dusk drone"
            F["metadata_keywords"] = "Magnific" in str(d.get("keywords"))
            F["third_party_creation"] = d.get("creation") == "flowtest-1"
            F["prores_frames_fps"] = [d.get("frames"), d.get("fps")]
            F["bin_view_restored"] = (cur.GetUniqueId() if cur else None) == prev_id
            same = _snap(tl) == before and (tl.GetTrackCount("video"), tl.GetTrackCount("audio")) == counts
            ok = (len(r["imported"]) == 3 and all(v and v["in_bin"] and v["color"] == "Purple" for v in got.values())
                  and F["metadata_comments"] and F["metadata_description"] and F["metadata_keywords"]
                  and F["third_party_creation"] and F["bin_view_restored"] and same)
            return {"ok": ok, "sync": _brief(r), "clips": got, "bin_before": prev.GetName() if prev else None,
                    "bin_after": cur.GetName() if cur else None, "timeline_untouched": same}
        test("bin_import", bin_import, need=["tl"])

        def webp():
            if "web.webp" not in m:
                return {"ok": True, "skipped": made["skipped"].get("web.webp")}
            shutil.copy2(m["web.webp"], root / "web.webp")
            r = mg.sync(wait=True)
            png = root / "web.png"
            it = find_media_pool_item(mp, str(png)) if png.exists() else None
            return {"ok": png.exists() and it is not None, "sync": _brief(r)}
        test("webp_to_png", webp, need=["tl"])

        def ph(file: str, lane: str, expect_len: int, still: float = 2.0):
            """Drop one file in playhead mode with the playhead 1 s in; check where it landed."""
            tl, start, fps = tl_(), keep["start"], keep["fps"]
            mg.set_place("playhead", still_seconds=still)
            at = int(round(fps))
            tl.SetCurrentTimecode(frames_to_tc(start + at, fps))
            tc0 = tl.GetCurrentTimecode()
            skip = set(mg.TRACK.values())
            before = _snap(tl, skip)
            k0, items0 = _track(tl, lane, mg.TRACK[lane])
            spans = [(it.GetStart() - start, it.GetEnd() - start) for it in items0]
            want = mg.free_spot(spans, at, expect_len)
            shutil.copy2(m[file], root / file)
            r = mg.sync(wait=True)
            k, items = _track(tl, lane, mg.TRACK[lane])
            new = [it for it in items if it.GetUniqueId() not in {x.GetUniqueId() for x in items0}]
            it = new[0] if new else None
            got = None if it is None else {"track": k, "track_name": tl.GetTrackName(lane, k),
                                           "start": it.GetStart() - start, "length": it.GetDuration(),
                                           "clip": _clip_name(it), "uid": it.GetUniqueId()}
            F.setdefault("playhead_after_place", []).append([tc0, tl.GetCurrentTimecode()])
            cur = project.GetCurrentTimeline()
            same = _snap(tl, skip) == before
            ok = (it is not None and got["start"] == want and got["length"] == expect_len and same
                  and cur is not None and cur.GetName() == tl.GetName())
            return {"ok": ok, "expected": {"start": want, "length": expect_len}, "got": got,
                    "rest_of_timeline_untouched": same, "sync": _brief(r), "uid": got and got["uid"]}

        def ph_video():
            v = ph("ph_video.mov", "video", int(3 * keep["fps"]))
            if v and v.get("uid"):
                keep["ph_item"] = v["uid"]
            return v
        test("playhead_video", ph_video, need=["tl"])
        test("playhead_still", lambda: ph("ph_still.png", "video", int(2 * keep["fps"])), need=["tl"])
        test("playhead_audio", lambda: ph("ph_tone.wav", "audio", int(2 * keep["fps"])), need=["tl"])

        def ph_sound():
            tl = tl_()
            audio_before = _snap(tl).copy()
            audio_before = {k: v for k, v in audio_before.items() if k.startswith("audio")}
            v = ph("ph_sound.mp4", "video", int(2 * keep["fps"]))
            audio_after = {k: v2 for k, v2 in _snap(tl).items() if k.startswith("audio")}
            v["audio_tracks_untouched"] = audio_before == audio_after
            v["ok"] = bool(v["ok"] and v["audio_tracks_untouched"])
            if v.get("uid"):
                keep["sound_item"] = v["uid"]
            return v
        test("playhead_sound", ph_sound, need=["tl"])

        def undo():
            tl = tl_()
            r = mg.undo()
            k, items = _track(tl, "video", mg.TRACK["video"])
            gone = keep["sound_item"] not in {it.GetUniqueId() for it in items}
            still_in_pool = find_media_pool_item(mp, str(root / "ph_sound.mp4")) is not None
            return {"ok": gone and still_in_pool, "say": r.get("say"), "gone": gone, "still_in_pool": still_in_pool}
        test("undo", undo, need=["tl", "sound_item"])

        def watcher():
            mg.set_place("bin")
            w = mg.start()
            t0 = time.time()
            shutil.copy2(m["watch_me.mov"], root / "watch_me.mov")
            hit = None
            while time.time() - t0 < 45:
                h = (mg._load("state.json", {}).get("files", {}) or {}).get("watch_me.mov") or {}
                if h.get("status") in ("imported", "failed"):
                    hit = h
                    break
                time.sleep(1.0)
            took = round(time.time() - t0, 1)
            s = mg.stop()
            try:
                log = mg.log_file().read_text(encoding="utf-8").splitlines()[-8:]
            except OSError:
                log = []
            it = find_media_pool_item(mp, str(root / "watch_me.mov"))
            return {"ok": bool(hit and hit.get("status") == "imported" and it is not None and s.get("stopped")),
                    "started": w.get("started"), "seconds_to_import": took, "state": hit, "stopped": s.get("stopped"),
                    "log": log}
        test("watcher", watcher, need=["tl"])

        # ------------------------------------------------ probes for the next steps (facts only)
        def takes():
            tl, start = tl_(), keep["start"]
            k, items = _track(tl, "video", mg.TRACK["video"])
            it = next((x for x in items if x.GetUniqueId() == keep["ph_item"]), None)
            v2 = find_media_pool_item(mp, keep["drone"])
            short = find_media_pool_item(mp, str(root / "ph_sound.mp4"))
            if it is None or v2 is None:
                return {"ok": False, "why": "no placed clip or no second version to try"}
            s0, d0 = it.GetStart(), it.GetDuration()
            o: Dict[str, Any] = {"before": [s0 - start, d0, _clip_name(it)]}
            o["add"] = _call(it, "AddTake", v2)
            o["count"] = _call(it, "GetTakesCount")
            o["selected"] = _call(it, "GetSelectedTakeIndex")
            n = o["count"] if isinstance(o["count"], int) else 0
            o["select_new"] = _call(it, "SelectTakeByIndex", n) if n else None
            it2 = _at(tl, "video", k, s0) or it
            o["after_select"] = [it2.GetStart() - start, it2.GetDuration(), _clip_name(it2)]
            o["takes"] = [_take(_call(it2, "GetTakeByIndex", i)) for i in range(1, n + 1)]
            if short is not None and n:
                o["add_short"] = _call(it2, "AddTake", short)
                n2 = _call(it2, "GetTakesCount")
                o["select_short"] = _call(it2, "SelectTakeByIndex", n2) if isinstance(n2, int) else None
                it3 = _at(tl, "video", k, s0) or it2
                o["after_short"] = [it3.GetStart() - start, it3.GetDuration(), _clip_name(it3)]
                _call(it3, "SelectTakeByIndex", 2)
            it4 = _at(tl, "video", k, s0) or it2
            o["finalize"] = _call(it4, "FinalizeTake")
            it5 = _at(tl, "video", k, s0) or it4
            o["after_finalize"] = [it5.GetStart() - start, it5.GetDuration(), _clip_name(it5),
                                   _call(it5, "GetTakesCount")]
            F["take_add_works"] = o["add"] is True and n >= 2
            F["take_keeps_slot"] = o["after_select"][:2] == [s0 - start, d0]
            F["take_shorter_version"] = o.get("after_short")
            return {"ok": bool(F["take_add_works"] and F["take_keeps_slot"]), **o}
        test("probe_takes_for_versions", takes, need=["tl", "ph_item", "drone"])

        def gap():
            tl, start, a = tl_(), keep["start"], keep["aroll"]
            tl.AddTrack("video")
            k = int(tl.GetTrackCount("video"))
            tl.SetTrackName("video", k, "Gap Test")
            keep["gap_track"] = k
            mp.AppendToTimeline([{"mediaPoolItem": a, "startFrame": 0, "endFrame": e(30), "trackIndex": k,
                                  "recordFrame": start, "mediaType": 1},
                                 {"mediaPoolItem": a, "startFrame": 60, "endFrame": e(120), "trackIndex": k,
                                  "recordFrame": start + 90, "mediaType": 1}])

            def spans():
                return sorted([it.GetStart() - start, it.GetDuration(), it.GetName()]
                              for it in tl.GetItemListInTrack("video", k) or [])
            s0 = spans()
            fill = find_media_pool_item(mp, m["gapfill.mov"]) or (_import_into(mp, _bin(mp), [m["gapfill.mov"]]) or [None])[0]
            if fill is None:
                return {"ok": False, "why": "gap clip not imported", "before": s0}
            r1 = mp.AppendToTimeline([{"mediaPoolItem": fill, "startFrame": 0, "endFrame": e(60), "trackIndex": k,
                                       "recordFrame": start + 30, "mediaType": 1}]) or []
            s1 = spans()
            if r1:
                tl.DeleteClips([x for x in r1 if x], False)
            r2 = mp.AppendToTimeline([{"mediaPoolItem": fill, "startFrame": 0, "endFrame": e(90), "trackIndex": k,
                                       "recordFrame": start + 30, "mediaType": 1}]) or []
            s2 = spans()
            F["append_exact_gap"] = [x[:2] for x in s1] == [[0, 30], [30, 60], [90, 60]]
            F["append_too_long"] = {"returned": len(r2), "track_after": [x[:2] for x in s2]}
            return {"ok": bool(F["append_exact_gap"]), "before": s0, "exact": s1, "too_long": s2}
        test("probe_fill_gap", gap, need=["tl", "aroll"])

        def replace():
            tl, start = tl_(), keep["start"]
            k = keep.get("gap_track") or 1
            ra = find_media_pool_item(mp, m["replace_a.mov"]) or (_import_into(mp, _bin(mp), [m["replace_a.mov"]]) or [None])[0]
            if ra is None:
                return {"ok": False, "why": "not imported"}
            got = mp.AppendToTimeline([{"mediaPoolItem": ra, "startFrame": 0, "endFrame": e(60), "trackIndex": k,
                                        "recordFrame": start + 240, "mediaType": 1}]) or []
            d0 = got[0].GetDuration() if got else None
            ok = _call(ra, "ReplaceClip", m["replace_b.mov"])
            fp = _file_path(ra)
            it = _at(tl, "video", k, start + 240)
            F["replace_clip"] = ok is True and fp is not None and Path(fp).name == "replace_b.mov"
            return {"ok": bool(F["replace_clip"]), "replace": ok, "file_after": fp,
                    "item": [d0, it.GetDuration() if it else None, _clip_name(it) if it else None]}
        test("probe_replace_clip", replace, need=["tl"])

        def markers():
            tl = tl_()
            k, items = _track(tl, "video", mg.TRACK["video"])
            it = items[0] if items else None
            o: Dict[str, Any] = {}
            if it is not None:
                o["item_add"] = _call(it, "AddMarker", 0, "Purple", "Flow slot S1", "a Magnific slot", 1, "flow:slot:S1")
                o["item_markers"] = _call(it, "GetMarkers")
                o["item_by_custom"] = _call(it, "GetMarkerByCustomData", "flow:slot:S1")
                o["creation_via_item"] = _call(it.GetMediaPoolItem(), "GetThirdPartyMetadata", "Magnific Creation")
            o["tl_add"] = _call(tl, "AddMarker", 150, "Blue", "Slot S2", "needs: drone shot of the harbour", 30, "flow:slot:S2")
            o["tl_by_custom"] = _call(tl, "GetMarkerByCustomData", "flow:slot:S2")
            F["marker_custom_data"] = bool(o.get("tl_by_custom")) and not str(o.get("tl_by_custom")).startswith("error")
            return {"ok": o.get("tl_add") is True, **o}
        test("probe_markers", markers, need=["tl"])

        if url:
            def delivered():
                mg.set_place("bin")
                p = mg.pull(url=url, name=name or "Magnific delivered", creation=creation, prompt=prompt, model=model)
                r = mg.sync(wait=True)
                it = find_media_pool_item(mp, p["file"])
                F["delivered_tags"] = _tags(p["file"])
                return {"ok": it is not None, "pull": {k: p.get(k) for k in ("name", "kind", "size", "duration")},
                        "clip": _meta(it) if it is not None else None, "sync": _brief(r), "tags": F["delivered_tags"]}
            test("delivered_magnific_file", delivered, need=["tl"])
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"
        res["trace"] = traceback.format_exc().splitlines()[-4:]
    finally:
        try:
            if mg.running():
                mg.stop()
        except Exception:
            pass
        if resolve is not None and orig:
            try:
                res["returned_to"] = orig if probe.return_to(resolve, orig) else None
            except Exception as exc:
                res["returned_to"] = None
                res["return_error"] = f"{type(exc).__name__}: {exc}"
        paths.FLOW_HOME = real_home
        if old_env is None:
            os.environ.pop("FLOW_HOME", None)
        else:
            os.environ["FLOW_HOME"] = old_env
        res["ok"] = all((T.get(k) or {}).get("ok") for k in CORE) and "error" not in res
        save()
    return res


def format_result(res: dict) -> str:
    lines = [f"Resolve {res.get('product', '?')} {res.get('version', '')} · Magnific live test "
             + ("OK" if res.get("ok") else "had failures")]
    for k, v in res.get("tests", {}).items():
        mark = "✓" if v.get("ok") else ("-" if v.get("skipped") else "✗")
        lines.append(f"  {mark} {k} {v.get('error') or v.get('skipped') or ''}".rstrip())
    if res.get("error"):
        lines.append(f"stopped: {res['error']}")
    lines.append("facts: " + json.dumps(res.get("facts", {}), ensure_ascii=False))
    if res.get("original_project"):
        lines.append(f"switched back to '{res['original_project']}'" if res.get("returned_to")
                     else f"could NOT switch back to '{res['original_project']}': {res.get('return_error', '')}")
    lines.append(f"full report: {res.get('report')}")
    return "\n".join(lines)
