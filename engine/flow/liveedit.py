"""Live edits: change the open Flow timeline in place while the user watches, from plain requests.

    flow edit where                                  what is under the playhead (clip, caption, word, grade)
    flow edit zoom --at here --zoom 1.2              punch in on the clip under the playhead
    flow edit captions --color "#FFD400" --size 1.2  restyle captions (all, or --from/--to seconds)
    flow edit caption-text --at here --text "..."    change one caption's words
    flow edit grade --at here|all|3 --exposure 0.3 --warmth 0.2 --look film --vignette 0.2
    flow edit fx --at here --type shake --duration 0.4
    flow edit broll --at here --clip B3 --duration 2
    flow edit undo                                   take back the last live edit
    flow edit log                                    list live edits on this timeline

Claude turns "punch in here", "make the captions yellow", "warmer on clip 3", "shake on the drop"
into these calls. "here" is the playhead. Every change is shown in Resolve (playhead on it, Color page
for grades), logged with what it replaced (jobs/<job>/live_edits.json) so `undo` can restore it, and
folded into the job's plan where the plan has a place for it (caption style, look), so the next full
rebuild keeps it. Only timelines Flow built are edited; anything else is refused.

Limits of Resolve's API: no blade, so a zoom applies to the whole clip under the playhead; no keying,
so skin and window changes stay with the colorist.
"""
from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flow import paths, resolve_bridge as rb
from flow.util import FlowError, frames_to_tc, read_json, tc_to_frames, write_json

TRACK_NAMES = {"video": "Flow Video", "broll": "Flow B-Roll", "captions": "Flow Captions", "hook": "Flow Hook"}
GRADE_KEYS = ("exposure", "contrast", "saturation", "warmth", "tint", "vignette")
NEUTRAL = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "warmth": 0.0, "tint": 0.0, "vignette": 0.0}


def is_flow_timeline(name: str) -> bool:
    return name.startswith("Flow · ") or " · Flow v" in name


def _version_base(name: str) -> str:
    parts = name.rsplit(" · v", 1)
    return parts[0] if len(parts) == 2 and parts[1].isdigit() else name


def find_job(tl_name: str) -> Optional[Path]:
    """The reel job a Flow timeline was built from (newest build whose timeline name matches)."""
    base = _version_base(tl_name)
    best = None
    for b in sorted(paths.JOBS_DIR.glob("*/build.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            name = read_json(b).get("timeline", {}).get("name")
        except Exception:
            continue
        if name == base:
            rep = b.parent / "report.json"
            exact = rep.exists() and read_json(rep).get("timeline") == tl_name
            if exact:
                return b.parent
            best = best or b.parent
    return best


# ---------------------------------------------------------------- pure helpers (unit-tested)
def zoom_framing(fr: Optional[dict], z: float, seg: Optional[dict] = None) -> Tuple[float, float]:
    """Pan/tilt that keep the subject steady at zoom z (same math as compile)."""
    if not fr:
        if seg and seg.get("zoom"):
            k = z / float(seg["zoom"])
            return round(float(seg.get("pan", 0.0)) * k, 2), round(float(seg.get("tilt", 0.0)) * k, 2)
        return 0.0, 0.0
    pan = fr["base_pan"] - (z - 1.0) * fr["px"]
    tilt = fr["base_tilt"] + (z - 1.0) * -fr["py"]
    lim_x = max(0.0, (fr["img_w"] * z - fr["tw"]) / 2)
    lim_y = max(0.0, (fr["img_h"] * z - fr["th"]) / 2)
    return round(max(-lim_x, min(lim_x, pan)), 2), round(max(-lim_y, min(lim_y, tilt)), 2)


def grade_cdls(base: Optional[dict], st: dict) -> Dict[str, dict]:
    """Node CDLs for the Flow tree from the build's auto-balance plus the live adjustments.
    exposure in stops, contrast/saturation as factors, warmth/tint −1…1, vignette 0…0.8."""
    b = base or {"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "saturation": 1.0}
    w, t = float(st.get("warmth", 0.0)), float(st.get("tint", 0.0))
    off = [b["offset"][0] + 0.03 * w, b["offset"][1] - 0.02 * t, b["offset"][2] - 0.03 * w]
    gain = 2.0 ** float(st.get("exposure", 0.0))
    con = float(st.get("contrast", 1.0))
    f = lambda v: " ".join(f"{x:.4f}" for x in v)  # noqa: E731
    out = {"balance": {"Slope": "1 1 1", "Offset": f(off), "Power": "1 1 1", "Saturation": "1.0"},
           "exposure": {"Slope": f([x * gain for x in b["slope"]]), "Offset": "0 0 0",
                        "Power": f([x * con for x in b["power"]]),
                        "Saturation": f"{float(b.get('saturation', 1.0)) * float(st.get('saturation', 1.0)):.4f}"}}
    v = max(0.0, min(0.8, float(st.get("vignette", 0.0))))
    s = f"{1.0 - v:.4f}"
    out["vignette"] = {"Slope": f"{s} {s} {s}", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": "1.0"}
    return out


def apply_delta(st: dict, change: dict) -> dict:
    """Relative changes add up (exposure +0.3 twice = +0.6); factors multiply; absolute with '='."""
    new = dict(st)
    for k, v in change.items():
        if k not in GRADE_KEYS or v is None:
            continue
        if isinstance(v, str) and v.startswith("="):
            new[k] = float(v[1:])
        elif k in ("contrast", "saturation"):
            new[k] = round(float(new.get(k, 1.0)) * float(v), 4)
        else:
            new[k] = round(float(new.get(k, 0.0)) + float(v), 4)
    new["vignette"] = max(0.0, min(0.8, new.get("vignette", 0.0)))
    return new


# ---------------------------------------------------------------- session
class Session:
    def __init__(self, show: bool = True):
        self.resolve = rb.connect()
        self.project, self.mp, self.tl = rb.current(self.resolve)
        if self.tl is None:
            raise FlowError("No timeline is open in Resolve.")
        self.name = self.tl.GetName()
        if not is_flow_timeline(self.name):
            raise FlowError(f"“{self.name}” wasn't built by Flow. Live edits only change Flow timelines; "
                            "build one with /flow or duplicate this one first.")
        self.fps = float(self.tl.GetSetting("timelineFrameRate") or 30)
        self.start = self.tl.GetStartFrame()
        self.job = find_job(self.name)
        self.b = read_json(self.job / "build.json") if self.job else {}
        self.show_changes = show
        self.log_path = (self.job / "live_edits.json") if self.job else (paths.FLOW_HOME / "live_edits.json")
        try:
            self.log = read_json(self.log_path)
        except Exception:
            self.log = {}
        self.entries: List[dict] = self.log.setdefault(self.name, {"edits": [], "grade": {}, "fx": {}}).get("edits")
        self.state = self.log[self.name]

    # ---- timeline lookup
    def track(self, key: str) -> Optional[int]:
        want = TRACK_NAMES[key]
        for k in range(1, int(self.tl.GetTrackCount("video") or 0) + 1):
            if (self.tl.GetTrackName("video", k) or "") == want:
                return k
        return 1 if key == "video" else None

    def items(self, key: str) -> List[Any]:
        k = self.track(key)
        if not k:
            return []
        from flow.finish import clips_on_track  # transitions are listed as items too; skip them
        return clips_on_track(self.tl, k)

    def playhead(self) -> int:
        return tc_to_frames(self.tl.GetCurrentTimecode(), self.fps) - self.start

    def frame(self, at: Any) -> int:
        """'here' = playhead; a number = output seconds; 'clip 3' / 3 handled by clip()."""
        if at in (None, "here", "playhead"):
            return self.playhead()
        return int(round(float(str(at).rstrip("s")) * self.fps))

    def item_at(self, key: str, f: int) -> Optional[Any]:
        for it in self.items(key):
            if it.GetStart() - self.start <= f < it.GetEnd() - self.start:
                return it
        return None

    def clips(self, at: Any) -> List[Tuple[int, Any]]:
        """V1 clips as (1-based index, item): 'all', 'here', a time, or a clip number."""
        v = self.items("video")
        if at == "all":
            return list(enumerate(v, 1))
        if isinstance(at, int) or (isinstance(at, str) and at.lower().startswith("clip")):
            n = int(str(at).lower().replace("clip", "").strip())
            if not 1 <= n <= len(v):
                raise FlowError(f"There are {len(v)} clips; clip {n} doesn't exist.")
            return [(n, v[n - 1])]
        f = self.frame(at)
        for k, it in enumerate(v, 1):
            if it.GetStart() - self.start <= f < it.GetEnd() - self.start:
                return [(k, it)]
        raise FlowError(f"No clip on {TRACK_NAMES['video']} at {f / self.fps:.2f}s.")

    def segment(self, k: int) -> Optional[dict]:
        segs = self.b.get("segments") or []
        return segs[k - 1] if 0 < k <= len(segs) and len(segs) == len(self.items("video")) else None

    # ---- showing + logging
    def show(self, f: int, page: str = "edit", item: Any = None) -> None:
        """Bring the change into view. If the playhead is already on `item`, leave it where the user
        put it (the next "here" should still mean that spot)."""
        if not self.show_changes:
            return
        try:
            if self.resolve.GetCurrentPage() != page:
                self.resolve.OpenPage(page)
            if item is not None and item.GetStart() - self.start <= self.playhead() < item.GetEnd() - self.start:
                return
            self.tl.SetCurrentTimecode(frames_to_tc(self.start + max(0, int(f)), self.fps))
        except Exception:
            pass

    def record(self, entry: dict) -> dict:
        entry["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.entries.append(entry)
        self.save()
        return entry

    def save(self) -> None:
        write_json(self.log_path, self.log)

    # ---- where am I
    def where(self) -> dict:
        f = self.playhead()
        out: Dict[str, Any] = {"timeline": self.name, "job": str(self.job) if self.job else None,
                               "frame": f, "seconds": round(f / self.fps, 2), "tracks": {}}
        for k in range(1, int(self.tl.GetTrackCount("video") or 0) + 1):
            for it in self.tl.GetItemListInTrack("video", k) or []:
                if it.GetStart() - self.start <= f < it.GetEnd() - self.start:
                    out["tracks"][self.tl.GetTrackName("video", k) or f"V{k}"] = it.GetName()
        try:
            k, it = self.clips("here")[0]
            out["clip"] = {"index": k, "of": len(self.items("video")), "name": it.GetName(),
                           "from": round((it.GetStart() - self.start) / self.fps, 2),
                           "to": round((it.GetEnd() - self.start) / self.fps, 2),
                           "zoom": it.GetProperty("ZoomX")}
            g = it.GetNodeGraph()
            if g is not None:
                out["clip"]["nodes"] = [f"{i:02d} {g.GetNodeLabel(i) or ''}".strip()
                                        for i in range(1, int(g.GetNumNodes() or 0) + 1)]
            out["clip"]["grade_live"] = self.state["grade"].get(str(k))
        except FlowError:
            pass
        cap = next((c for c in self.b.get("captions", []) if c["f0"] <= f < c["f1"]), None)
        if cap:
            out["caption"] = cap["text"]
        return out

    # ---- zoom
    def zoom(self, at: Any, z: float) -> dict:
        (k, it), = self.clips(at)
        before = {p: it.GetProperty(p) for p in ("ZoomX", "ZoomY", "Pan", "Tilt")}
        pan, tilt = zoom_framing(self.b.get("framing"), float(z), self.segment(k))
        ok = all([it.SetProperty("ZoomX", float(z)), it.SetProperty("ZoomY", float(z)),
                  it.SetProperty("Pan", pan), it.SetProperty("Tilt", tilt)])
        self.show(it.GetStart() - self.start + 2, item=it)
        return self.record({"op": "zoom", "clip": k, "before": before,
                            "after": {"ZoomX": z, "ZoomY": z, "Pan": pan, "Tilt": tilt}, "ok": ok,
                            "say": f"clip {k} punched in to {z:.2f}x (whole clip: Resolve can't split at the playhead)"})

    # ---- captions
    def _caption_cfg(self) -> dict:
        cfg = copy.deepcopy((self.b.get("style") or {}).get("captions") or {})
        for e in self.entries:
            # only whole-timeline restyles define the base style; ranged ones stay in their range
            if e["op"] == "captions" and not e.get("undone") and not any(x is not None for x in e.get("range") or []):
                cfg.update(e["after"])
        return cfg

    def captions(self, changes: dict, t0: Optional[float] = None, t1: Optional[float] = None) -> dict:
        before_cfg = self._caption_cfg()
        cfg = dict(before_cfg)
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            raise FlowError("Say what to change: color, accent, size, font, font_style, position, animation…")
        if "size" in changes and isinstance(changes["size"], (int, float)) and changes["size"] < 4:
            changes["size"] = round(float(before_cfg.get("size", 0.065)) * float(changes["size"]), 4)
        cfg.update(changes)
        n = self._restyle(cfg, t0, t1)
        self.show(self.playhead())
        whole = t0 is None and t1 is None
        entry = self.record({"op": "captions", "before": {k: before_cfg.get(k) for k in changes},
                             "after": changes, "range": [t0, t1], "count": n,
                             "say": f"{n} captions restyled: " + ", ".join(f"{k} {v}" for k, v in changes.items())})
        if whole:
            entry["plan_before"] = self._fold({"overrides": {"captions": changes}})
            self.save()
        return entry

    def _restyle(self, cfg: dict, t0: Optional[float], t1: Optional[float]) -> int:
        from flow.captions import style_caption, text_tool
        n = 0
        caps = {c["f0"]: c for c in self.b.get("captions", [])}
        for it in self.items("captions"):
            f0 = it.GetStart() - self.start
            if t0 is not None and f0 < t0 * self.fps or t1 is not None and f0 >= t1 * self.fps:
                continue
            comp, tool = text_tool(it)
            if tool is None:
                continue
            text = tool.GetInput("StyledText") or caps.get(f0, {}).get("text", "")
            case = cfg.get("case")  # live --case: upper/lower apply now ("none" can't recover the original)
            if case == "upper":
                text = text.upper()
            elif case == "lower":
                text = text.lower()
            accent = bool(caps.get(f0, {}).get("accent"))
            if style_caption(it, text, cfg, accent, it.GetDuration()):
                n += 1
        return n

    def caption_text(self, at: Any, text: str) -> dict:
        from flow.captions import text_tool
        f = self.frame(at)
        it = self.item_at("captions", f)
        if it is None:
            raise FlowError(f"No caption at {f / self.fps:.2f}s.")
        comp, tool = text_tool(it)
        old = tool.GetInput("StyledText") if tool else None
        ok = bool(tool) and tool.SetInput("StyledText", text) is not False
        self.show(it.GetStart() - self.start + 2, item=it)
        return self.record({"op": "caption_text", "frame": it.GetStart() - self.start, "before": old,
                            "after": text, "ok": ok, "say": f"caption “{old}” → “{text}”"})

    # ---- grade
    def grade(self, at: Any, change: dict, look: Optional[str] = None, intensity: Optional[float] = None) -> dict:
        from flow import luts, nodetree
        from flow.build import install_lut
        targets = self.clips(at)
        base_cdl = (self.b.get("grade") or {}).get("cdl")
        lut_path = None
        if look:
            key = luts.normalize_look(look)
            inten = float(intensity if intensity is not None else (self.b.get("grade") or {}).get("intensity", 0.8))
            tmp = (self.job or paths.FLOW_HOME) / f"live_{key}_{int(inten * 100)}.cube"
            luts.write_cube(tmp, luts.build_transform(key, inten), f"Flow {key} {int(inten * 100)}")
            lut_path = install_lut(str(tmp), f"{Path(str(self.job or 'live')).name}_live_{key}_{int(inten * 100)}")
            self.project.RefreshLUTList()
        done, skipped = [], []
        page_shown = False
        home = self.playhead()          # come back here afterwards: the next "here" means where the user is
        solo = nodetree.Solo(self.tl)
        try:
            for k, it in targets:
                g = it.GetNodeGraph()
                st_old = dict(self.state["grade"].get(str(k)) or NEUTRAL)
                old_look = g.GetLUT(4) if g is not None and int(g.GetNumNodes() or 0) >= 4 else None
                labels = [g.GetNodeLabel(i) for i in range(1, int(g.GetNumNodes() or 0) + 1)] if g else []
                if labels[:len(nodetree.LABELS)] != nodetree.LABELS:
                    skipped.append(k)
                    continue
                if self.show_changes and not page_shown:
                    solo.upto(nodetree.track_of(it))
                    self.show(it.GetStart() - self.start + 2, "color", item=it)
                    page_shown = True
                st = apply_delta(st_old, change)
                cd = grade_cdls(base_cdl, st)
                for node, vals in cd.items():
                    it.SetCDL({"NodeIndex": str(nodetree.NODES[node]), **vals})
                if lut_path:
                    it.GetNodeGraph().SetLUT(4, str(lut_path))
                self.state["grade"][str(k)] = st
                done.append({"clip": k, "before": st_old, "after": st, "look_before": old_look,
                             "look_after": str(lut_path) if lut_path else None})
                if self.show_changes and len(targets) > 1:
                    self.show(it.GetStart() - self.start + 2, "color")
                    time.sleep(0.2)
        finally:
            time.sleep(0.6 if self.show_changes else 0)
            solo.restore()
            if len(targets) > 1:
                self.show(home, "color")
        what = ", ".join(f"{k} {v}" for k, v in change.items() if v is not None)
        if look:
            what = (what + ", " if what else "") + f"look {look}" + (f" {intensity}" if intensity is not None else "")
        entry = self.record({"op": "grade", "clips": done, "say": f"graded {len(done)} clip(s): {what}"
                             + (f" · clips {skipped} have no Flow node tree, left alone" if skipped else "")})
        if look and at == "all":
            entry["plan_before"] = self._fold({"grade": {"look": look, **({"intensity": intensity}
                                                                          if intensity is not None else {})}})
            self.save()
        return entry

    # ---- effects
    def fx(self, at: Any, kind: str, duration: float = 0.4, intensity: float = 1.0) -> dict:
        from flow.fusion_fx import IMAGE_BUILDERS, apply_image_fx
        if kind not in IMAGE_BUILDERS:
            raise FlowError(f"Unknown effect {kind!r}. Live effects: {', '.join(sorted(IMAGE_BUILDERS))}")
        f = self.frame(at)
        (k, it), = self.clips(at)
        lf0 = max(0, f - (it.GetStart() - self.start))
        lf1 = min(it.GetDuration(), lf0 + max(2, int(round(duration * self.fps))))
        seg = self.segment(k) or {}
        live_fx = self.state["fx"].setdefault(str(k), [])
        new = {"type": kind, "lf0": lf0, "lf1": lf1, "params": {"intensity": intensity}}
        before = list(live_fx)
        live_fx.append(new)
        ok = self._rebuild_fx(it, list(seg.get("fx") or []) + live_fx)
        self.show(f)
        return self.record({"op": "fx", "clip": k, "before": before, "after": list(live_fx), "ok": ok,
                            "say": f"{kind} on clip {k} at {f / self.fps:.2f}s for {duration:.2f}s"})

    def _rebuild_fx(self, it: Any, fxs: List[dict]) -> bool:
        from flow.fusion_fx import apply_image_fx
        res = apply_image_fx(it, fxs, reuse=True)   # one comp holds all effects of a clip
        return not res["failed"]

    # ---- B-roll
    def broll(self, at: Any, clip: str, duration: float = 2.0, mode: str = "full") -> dict:
        from flow import broll as broll_mod
        from flow.build import Report, _broll_media, _flow_bin, _place_broll
        if not self.job:
            raise FlowError("No Flow job found for this timeline, so its B-roll library is unknown.")
        f = self.frame(at)
        warn = set()
        b = self.b
        fr = b.get("framing") or {}
        ctx = {"fps": self.fps, "total": self.tl.GetEndFrame() - self.start, "hook_end": 0,
               "word_frame": lambda i, end: None, "src_to_rec": lambda t: None,
               "tw": fr.get("tw", 1080), "th": fr.get("th", 1920), "sentences": [], "words": [],
               "removed": set(), "cut_frames": [], "segments": b.get("segments"),
               "vrange": lambda s: (lambda v: (v["rec_f0"], v["rec_len"], v["src_f0"], v["src_f1"]))(s.get("video") or s)}
        ents = broll_mod.compile_broll([{"clip": clip, "at": f / self.fps, "duration": duration, "mode": mode,
                                         "over_hook": True}], (b.get("style") or {}).get("broll", {}),
                                       broll_mod.load_index(self.job), ctx, warn)
        if not ents:
            raise FlowError("; ".join(sorted(warn)) or f"B-roll {clip} could not be placed.")
        k = self.track("broll")
        if k is None:
            self.tl.AddTrack("video")
            k = int(self.tl.GetTrackCount("video"))
            self.tl.SetTrackName("video", k, TRACK_NAMES["broll"])
        for e in ents:   # free the lane where the new insert goes
            for old in self.items("broll"):
                s0, s1 = old.GetStart() - self.start, old.GetEnd() - self.start
                if s0 < e["rec_f0"] + e["rec_len"] and e["rec_f0"] < s1:
                    raise FlowError(f"B-roll {old.GetName()} already covers {s0 / self.fps:.1f}–{s1 / self.fps:.1f}s; "
                                    "undo it or pick another spot.")
        media = _broll_media(self.mp, _flow_bin(self.mp), ents)
        r = Report()
        before = {x.GetUniqueId() for x in self.items("broll")}
        _place_broll(self.resolve, self.project, self.mp, self.tl,
                     {**b, "broll": ents, "fps": self.fps}, self.start, k, media, False, r)
        placed = [x.GetUniqueId() for x in self.items("broll") if x.GetUniqueId() not in before]
        self.show(f + 3)
        return self.record({"op": "broll", "placed": placed, "clip": clip, "ok": bool(placed),
                            "say": f"B-roll {clip} at {f / self.fps:.2f}s for {duration:.1f}s"
                                   + (f" ({'; '.join(sorted(warn))})" if warn else "")})

    # ---- undo
    def undo(self) -> dict:
        live = [e for e in self.entries if not e.get("undone")]
        if not live:
            raise FlowError("Nothing to undo on this timeline.")
        e = live[-1]
        op = e["op"]
        if op == "zoom":
            (k, it), = self.clips(e["clip"])
            for p, v in e["before"].items():
                if v is not None:
                    it.SetProperty(p, v)
            self.show(it.GetStart() - self.start + 2)
        elif op == "captions":
            e["undone"] = True   # the style is recomputed without this edit
            self._restyle(self._caption_cfg(), *(e.get("range") or [None, None]))
            self._unfold(e.get("plan_before"))
            self.show(self.playhead())
        elif op == "caption_text":
            from flow.captions import text_tool
            it = self.item_at("captions", e["frame"])
            tool = text_tool(it)[1] if it is not None else None
            if tool is not None and e.get("before") is not None:
                tool.SetInput("StyledText", e["before"])
                self.show(e["frame"] + 2)
        elif op == "grade":
            base_cdl = (self.b.get("grade") or {}).get("cdl")
            for c in e["clips"]:
                (k, it), = self.clips(c["clip"])
                for node, vals in grade_cdls(base_cdl, c["before"]).items():
                    from flow import nodetree
                    it.SetCDL({"NodeIndex": str(nodetree.NODES[node]), **vals})
                if c.get("look_after") and c.get("look_before"):
                    it.GetNodeGraph().SetLUT(4, c["look_before"])
                self.state["grade"][str(k)] = c["before"]
            self._unfold(e.get("plan_before"))
        elif op == "fx":
            (k, it), = self.clips(e["clip"])
            self.state["fx"][str(k)] = list(e["before"])
            self._rebuild_fx(it, list((self.segment(k) or {}).get("fx") or []) + e["before"])
            self.show(it.GetStart() - self.start + 2)
        elif op == "broll":
            gone = [x for x in self.items("broll") if x.GetUniqueId() in set(e.get("placed") or [])]
            if gone:
                self.tl.DeleteClips(gone, False)
        e["undone"] = True
        self.save()
        return {"undid": op, "say": "undone: " + e.get("say", op)}

    # ---- plan folding (so a full rebuild keeps the change)
    def _fold(self, patch: dict) -> Optional[list]:
        """Write patch into the job's edit_plan.json; returns what it replaced, for undo."""
        if not self.job or not (self.job / "edit_plan.json").exists():
            return None
        plan = read_json(self.job / "edit_plan.json")
        before = fold_patch(plan, patch)
        write_json(self.job / "edit_plan.json", plan)
        return before

    def _unfold(self, before: Optional[list]) -> None:
        if before and self.job and (self.job / "edit_plan.json").exists():
            plan = read_json(self.job / "edit_plan.json")
            unfold_patch(plan, before)
            write_json(self.job / "edit_plan.json", plan)


MISSING = "__missing__"


def fold_patch(plan: dict, patch: dict, path: tuple = ()) -> list:
    """Merge patch into plan in place; return [(path, old value or MISSING)] for every leaf it set."""
    before = []
    for k, v in patch.items():
        if isinstance(v, dict):
            node = plan.get(k)
            if not isinstance(node, dict):
                before.append((list(path + (k,)), node if k in plan else MISSING))
                plan[k] = {}
                fold_patch(plan[k], v, path + (k,))
                continue
            before += fold_patch(node, v, path + (k,))
        else:
            before.append((list(path + (k,)), plan.get(k, MISSING) if k in plan else MISSING))
            plan[k] = v
    return before


def unfold_patch(plan: dict, before: list) -> None:
    """Undo fold_patch: put old values back, remove keys that did not exist (deepest first)."""
    for path, old in sorted(before, key=lambda x: -len(x[0])):
        node = plan
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict):
            continue
        if old == MISSING:
            node.pop(path[-1], None)
        else:
            node[path[-1]] = old


def format_where(w: dict) -> str:
    lines = [f"{w['timeline']} · playhead {w['seconds']:.2f}s (frame {w['frame']})"]
    c = w.get("clip")
    if c:
        lines.append(f"clip {c['index']}/{c['of']} · {c['name']} · {c['from']:.2f}–{c['to']:.2f}s · zoom {c['zoom']}")
        if c.get("nodes"):
            lines.append("nodes: " + " › ".join(c["nodes"]))
        if c.get("grade_live"):
            lines.append("live grade: " + ", ".join(f"{k} {v}" for k, v in c["grade_live"].items()))
    if w.get("caption"):
        lines.append(f"caption: “{w['caption']}”")
    for k, v in w.get("tracks", {}).items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)
