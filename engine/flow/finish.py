"""Finish mode: complete an existing (multi-clip) timeline from its production notes.

    flow finish-scan            read the current timeline: clips, markers, placeholders, media pool
    (Claude writes finish_plan.json)
    flow finish                 apply it to a DUPLICATE of the timeline (the original is never touched)

What it can do on the duplicate:
  * titles / lower thirds / chapters / location / end card / quote (Text+, own tracks)
  * overlays: letterbox, grain, vignette, light leak, flash, spotlight, …  (Fusion clips, own tracks)
  * a global look (LUT) on adjustment clips above everything — clip grades stay untouched
  * per clip: slow-mo / speed-up that keeps the clip's slot (music sync holds), zoom/punch-in,
    push-in and other Fusion image effects
  * placeholders: fill placeholder text, turn a placeholder into a title, or replace it with
    a media-pool clip (B-roll) on a track above, then disable the placeholder
  * markers: every handled note is re-marked ✓ green, open ones red "Flow needs you"
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow import nodetree, paths, resolve_bridge as rb
from flow.build import Inserter, Report, _ensure_tracks, export_frames, install_lut, refresh_view
from flow.captions import style_title, title_config, text_tool
from flow.compile import assign_lanes
from flow.fusion_fx import IMAGE_FX, OVERLAY_COMPOSITE, apply_image_fx, build_overlay
from flow.notes import _timeline_by_name, collect_timeline, placeholder_reason, text_of_item
from flow.util import FlowError, fmt_time, read_json, write_json

FINISH_DIR = paths.FLOW_HOME / "finish"
NOTE_COLORS = {"done": "Green", "needs_user": "Red", "skipped": "Cream"}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:60] or "timeline"


def _file_path(mpi: Any) -> str:
    try:
        return mpi.GetClipProperty("File Path") or ""
    except Exception:
        return ""


def clips_on_track(tl: Any, k: int) -> List[Any]:
    """Timeline clips on a video track, without transitions (Resolve lists those as items too)."""
    out = []
    for it in tl.GetItemListInTrack("video", k) or []:
        try:
            if it.GetType() == "transition":
                continue
        except Exception:
            pass
        out.append(it)
    return out


def _items_by_ref(tl: Any) -> Dict[str, Any]:
    refs = {}
    for k in range(1, (tl.GetTrackCount("video") or 0) + 1):
        for idx, it in enumerate(clips_on_track(tl, k)):
            refs[f"V{k}#{idx + 1}"] = it
    return refs


def _pool_inventory(mp: Any, limit: int = 400) -> List[dict]:
    out = []
    stack = [(mp.GetRootFolder(), "")]
    while stack and len(out) < limit:
        folder, path = stack.pop()
        for c in folder.GetClipList() or []:
            try:
                typ = c.GetClipProperty("Type") or ""
                if "Video" not in typ and "Still" not in typ:
                    continue
                meta = c.GetMetadata() or {}
                out.append({
                    "name": c.GetName(), "bin": path or "/", "path": _file_path(c), "type": typ,
                    "duration": c.GetClipProperty("Duration"), "fps": c.GetClipProperty("FPS"),
                    "keywords": meta.get("Keywords", "") if isinstance(meta, dict) else "",
                    "description": meta.get("Description", "") if isinstance(meta, dict) else "",
                    "comments": meta.get("Comments", "") if isinstance(meta, dict) else "",
                    "id": c.GetUniqueId(),
                })
            except Exception:
                continue
        for sub in folder.GetSubFolderList() or []:
            stack.append((sub, f"{path}/{sub.GetName()}"))
    return out


def scan(notes_file: Optional[str] = None) -> dict:
    resolve = rb.connect()
    project, mp, tl = rb.current(resolve)
    if tl is None:
        raise FlowError("Open the timeline you want Flow to finish.")
    fps = float(tl.GetSetting("timelineFrameRate") or 30)
    start = tl.GetStartFrame()
    tracks = []
    used = set()
    for k in range(1, (tl.GetTrackCount("video") or 0) + 1):
        items = []
        for idx, it in enumerate(clips_on_track(tl, k)):
            mpi = it.GetMediaPoolItem()
            if mpi is not None:
                used.add(mpi.GetUniqueId())
            entry = {
                "ref": f"V{k}#{idx + 1}", "name": it.GetName(), "type": it.GetType(),
                "start": round((it.GetStart() - start) / fps, 3), "end": round((it.GetEnd() - start) / fps, 3),
                "duration": round(it.GetDuration() / fps, 3), "media": _file_path(mpi) if mpi else None,
                "enabled": bool(it.GetClipEnabled()), "color": it.GetClipColor(),
                "fusion": it.GetFusionCompCount(),
            }
            try:
                entry["speed"] = (it.GetSpeed() or {}).get("Percentage")
            except Exception:
                pass
            txt = text_of_item(it)
            if txt:
                entry["text"] = txt[:120]
            if placeholder_reason(it):
                entry["placeholder"] = True
            items.append(entry)
        tracks.append({"track": k, "name": tl.GetTrackName("video", k), "items": items})
    notes = collect_timeline(notes_file, tl)
    pool = _pool_inventory(mp)
    for p in pool:
        p["used"] = p["id"] in used
    data = {
        "project": project.GetName(), "timeline": tl.GetName(), "fps": fps,
        "width": tl.GetSetting("timelineResolutionWidth"), "height": tl.GetSetting("timelineResolutionHeight"),
        "duration": round((tl.GetEndFrame() - start) / fps, 3), "tracks": tracks,
        "audio_tracks": tl.GetTrackCount("audio"), "notes": notes["notes"], "media_pool": pool,
        "studio": rb.is_studio(resolve),
    }
    out = FINISH_DIR / _slug(tl.GetName())
    write_json(out / "finish_scan.json", data)
    (out / "finish_review.txt").write_text(format_scan(data, full=True), encoding="utf-8")
    data["dir"] = str(out)
    return data


def format_scan(d: dict, full: bool = False) -> str:
    lines = [f"FINISH SCAN · {d['timeline']} ({d['project']})",
             f"{fmt_time(d['duration'])} · {d['fps']} fps · {d['width']}x{d['height']} · "
             f"{len(d['tracks'])} video / {d['audio_tracks']} audio tracks · Studio: {d['studio']}"]
    if d.get("dir"):
        lines.append(f"files: {d['dir']}/finish_scan.json, finish_review.txt → write finish_plan.json there")
    lines.append("")
    lines.append("PRODUCTION NOTES")
    if not d["notes"]:
        lines.append("  (none: no markers, placeholders or notes file)")
    for n in d["notes"]:
        where = f"at {fmt_time(n['tl_t'])}" if n.get("tl_t") is not None else "global"
        col = f" [{n['color']}]" if n.get("color") else ""
        item = f" on {n['item']}" if n.get("item") else ""
        lines.append(f"  {n['id']:<4} {where:<10} {n['kind']:<11} {n['source']}{col}{item} · {n['text']}")
    lines.append("")
    lines.append("CLIPS (ref · start–end · name · media)")
    for t in d["tracks"]:
        lines.append(f"  V{t['track']} {t.get('name') or ''} — {len(t['items'])} clips")
        if not full and len(t["items"]) > 12:
            continue
        for it in t["items"]:
            flag = " PLACEHOLDER" if it.get("placeholder") else ""
            spd = f" {it['speed']:.0f}%" if it.get("speed") and abs(it["speed"] - 100) > 0.5 else ""
            txt = f" “{it['text']}”" if it.get("text") else ""
            lines.append(f"    {it['ref']:<7} {fmt_time(it['start'])}–{fmt_time(it['end'])} {it['type']:<9}"
                         f" {it['name']}{txt}{spd}{flag}")
    if full:
        unused = [p for p in d["media_pool"] if not p.get("used")]
        lines.append("")
        lines.append(f"UNUSED MEDIA POOL CLIPS (for B-roll placeholders) · {len(unused)}")
        for p in unused[:150]:
            meta = " · ".join(x for x in (p.get("keywords"), p.get("description"), p.get("comments")) if x)
            lines.append(f"    {p['name']} [{p['bin']}] {p.get('duration') or ''}" + (f" — {meta}" if meta else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- apply
def _find_pool_clip(mp: Any, key: str) -> Any:
    key_l = key.lower()
    stack = [mp.GetRootFolder()]
    fuzzy = None
    while stack:
        folder = stack.pop()
        for c in folder.GetClipList() or []:
            try:
                if c.GetUniqueId() == key or _file_path(c) == key or c.GetName() == key:
                    return c
                if fuzzy is None and key_l in (c.GetName() or "").lower():
                    fuzzy = c
            except Exception:
                continue
        stack.extend(folder.GetSubFolderList() or [])
    return fuzzy


def _keep_slot_speed(it: Any, pct: float, r: Report, ref: str) -> None:
    """Change clip speed but keep its slot length (so music sync holds)."""
    d = it.GetDuration()
    if pct < 100:
        need = max(1, int(round(d * pct / 100.0)))
        it.SetRightOffset((it.GetRightOffset() or 0) + (d - need))
    ok = it.SetSpeed({"Percentage": float(pct), "PitchCorrection": True, "RippleTimeline": False})
    nd = it.GetDuration()
    if nd != d:
        it.SetRightOffset((it.GetRightOffset() or 0) + (nd - d))
    if it.GetDuration() != d:
        r.fail("speed", f"{ref}: not enough media to keep the slot at {pct:.0f}% ({it.GetDuration()} of {d} frames)")
    elif ok:
        r.ok("speed")


def apply(plan_path: Optional[Path] = None, live: Any = None) -> dict:
    from flow import live as live_mod
    live = live or live_mod.NULL
    t0 = time.time()
    resolve = rb.connect()
    project, mp, cur = rb.current(resolve)
    if plan_path is None:
        if cur is None:
            raise FlowError("No timeline open and no --plan given.")
        plan_path = FINISH_DIR / _slug(cur.GetName()) / "finish_plan.json"
    if not plan_path.exists():
        raise FlowError(f"No finish plan at {plan_path}")
    plan = read_json(plan_path)
    src = _timeline_by_name(project, plan.get("timeline")) if plan.get("timeline") else cur
    if src is None:
        raise FlowError(f"Timeline '{plan.get('timeline')}' not found in this project.")
    try:
        resolve.OpenPage("edit")
    except Exception:
        pass
    fps = float(src.GetSetting("timelineFrameRate") or 30)
    base = f"{plan.get('name') or src.GetName()} · Flow"
    names = {project.GetTimelineByIndex(i).GetName() for i in range(1, (project.GetTimelineCount() or 0) + 1)}
    v = 1
    while f"{base} v{v}" in names:
        v += 1
    new_name = f"{base} v{v}"
    tl = src.DuplicateTimeline(new_name)
    if tl is None:
        raise FlowError("Resolve couldn't duplicate the timeline.")
    project.SetCurrentTimeline(tl)
    start = tl.GetStartFrame()
    total = tl.GetEndFrame() - start
    r = Report()
    refs = _items_by_ref(tl)
    base_tracks = tl.GetTrackCount("video")
    w = int(tl.GetSetting("timelineResolutionWidth") or 1920)
    h = int(tl.GetSetting("timelineResolutionHeight") or 1080)

    def F(sec: float) -> int:
        return int(round(float(sec) * fps))

    live.attach(resolve, project, tl, fps, start)
    live.front()
    live.page("edit", 0.8)
    live.say(f"FINISH · duplicated “{src.GetName()}” → “{new_name}” · {len(plan.get('notes') or [])} notes to complete")

    # ---- grade every clip (measured first, before anything else changes)
    gc = plan.get("grade_clips")
    grade_rows: List[dict] = []
    if gc:
        from flow import grade
        tracks = set(gc.get("tracks") or [1])
        cand = []
        for ref, it in refs.items():
            k = int(ref[1:ref.index("#")])
            if k in tracks and it.GetType() == "video" and it.GetMediaPoolItem() is not None and not text_of_item(it):
                cand.append((ref, it))
        match = {a: b for a, b in (gc.get("match") or [])}
        if live.enabled:
            live.say(f"COLOR · measuring and grading {len(cand)} clips one by one ({gc.get('look', 'nightlife')})")
            live.page("color", 1.0)
        n_cand = len(cand)
        seen = [0]

        solo = nodetree.Solo(tl)

        def _on_clip(label, it):
            seen[0] += 1
            solo.upto(nodetree.track_of(it))
            live.show(it.GetStart() - start + 1, 0.25, f"COLOR · {label} ({seen[0]}/{n_cand})" if seen[0] % 5 == 1 else None)
        if gc.get("masks") or gc.get("reference"):
            # mask tree: reference-matched (or named) look on 04 Look + rule-based qualifiers and windows
            ref = gc.get("reference")
            if ref and not Path(str(ref)).expanduser().exists():
                r.fail("grade", f"reference image not found: {ref}")
                ref = None
            out = grade.grade_clips_masked(project, cand, plan_path.parent / "grade",
                                           look=gc.get("look") if not ref else "reference",
                                           reference=str(Path(str(ref)).expanduser()) if ref else None,
                                           masks_cfg=gc.get("masks", True), subjects=gc.get("subjects"),
                                           vignette=gc.get("vignette"), ref_strength=gc.get("reference_strength"),
                                           replace=bool(gc.get("replace_existing", False)),
                                           on_clip=_on_clip if live.enabled else None,
                                           lo=float(gc.get("min", 0.6)), hi=float(gc.get("max", 1.0)),
                                           soft=float(gc.get("soft", 0.0)))
            grade_rows = out["rows"]
            if out.get("reference"):
                write_json(plan_path.parent / "reference_match.json", out["reference"])
                r.ok("reference_match")
            for row in grade_rows:
                for lab in row.get("mask_nodes_on") or []:
                    r.ok(f"mask:{lab.lower()}")
        else:
            grade_rows = grade.grade_clips(project, cand, gc.get("look", "nightlife"), plan_path.parent / "grade",
                                           float(gc.get("min", 0.6)), float(gc.get("max", 1.0)), match=match,
                                           keep_green=set(gc.get("keep_green") or []),
                                           on_clip=_on_clip if live.enabled else None,
                                           nodes=bool(gc.get("nodes", True)), vignette=float(gc.get("vignette", 0.0)),
                                           soft=float(gc.get("soft", 0.0)))
        solo.restore()
        live.page("edit", 0.5)
        write_json(plan_path.parent / "grade_report.json", grade_rows)
        for row in grade_rows:
            if row.get("status") == "graded":
                r.ok("grade")
            else:
                r.fail("grade", f"{row['clip']}: {row.get('status')}")

    # every image effect for a clip goes into ONE Fusion comp (a second AddFusionComp would replace the first)
    pending: Dict[str, tuple] = {}

    def queue_fx(item: Any, label: str, fx: dict) -> None:
        key = item.GetUniqueId()
        pending.setdefault(key, (item, label, []))[2].append(fx)

    # ---- per-clip operations (before anything moves)
    for op in plan.get("clips") or []:
        it = refs.get(op.get("item", ""))
        if it is None:
            r.fail("clip", f"unknown clip ref {op.get('item')}")
            continue
        ref = op["item"]
        live.show(it.GetStart() - start + 1, 0.25, f"CLIP · {ref} " + ", ".join(k for k in op if k != "item"))
        if op.get("speed"):
            _keep_slot_speed(it, float(op["speed"]), r, ref)
            if float(op["speed"]) < 100:
                it.SetProperty("RetimeProcess", rb.const(resolve, "RETIME_OPTICAL_FLOW"))
                warp = rb.const(resolve, "MOTION_EST_SPEED_WARP_FASTER")
                if warp is not None:
                    it.SetProperty("MotionEstimation", warp)
        if op.get("stabilize"):  # Resolve's stabilizer (Edit/Color inspector settings), analysed now
            try:
                ok = bool(it.Stabilize())
            except Exception:
                ok = False
            r.ok("stabilize") if ok else r.fail("stabilize", ref)
        if op.get("retime"):  # optical flow (+ Speed Warp) on an existing speed change
            ok = it.SetProperty("RetimeProcess", rb.const(resolve, "RETIME_OPTICAL_FLOW"))
            warp = rb.const(resolve, "MOTION_EST_SPEED_WARP_FASTER")
            if warp is not None:
                it.SetProperty("MotionEstimation", warp)
            r.ok("retime") if ok else r.fail("retime", ref)
        if op.get("look"):  # a look on this clip only; clips with a LUT on node 1 stay as graded
            from flow import luts
            key = luts.normalize_look(op["look"])
            inten = float(op.get("intensity", 0.7))
            cube = FINISH_DIR / _slug(src.GetName()) / f"look_{key}_{int(inten * 100)}.cube"
            if not cube.exists():
                luts.write_cube(cube, luts.build_transform(key, inten), f"Flow {key} {int(inten * 100)}")
            installed = install_lut(str(cube), f"{_slug(new_name)}_{key}_{int(inten * 100)}")
            project.RefreshLUTList()
            g = it.GetNodeGraph()
            if g is None:
                r.fail("clip_look", ref)
            elif (g.GetLUT(1) or "") and not op.get("replace_existing"):
                r.notes.append(f"clip_look: {ref} already has a LUT on node 1; left as graded")
            else:
                r.ok("clip_look") if g.SetLUT(1, str(installed)) else r.fail("clip_look", ref)
        if op.get("zoom"):
            z = float(op["zoom"])
            ok = it.SetProperty("ZoomX", z) and it.SetProperty("ZoomY", z)
            r.ok("zoom") if ok else r.fail("zoom", ref)
        fxs = []
        d = it.GetDuration()
        if op.get("push_in"):
            fxs.append({"type": "push_in", "lf0": 0, "lf1": d, "uf0": 0, "uf1": d,
                        "params": {"zoom_from": 1.0, "zoom_to": float(op["push_in"]), "center": op.get("center", [0.5, 0.5])}})
        for e in op.get("effects") or []:
            e = {"type": e} if isinstance(e, str) else e
            a = F(e["at"]) if "at" in e else 0
            b = a + F(e.get("duration", 0.4)) if "at" in e else d
            fxs.append({"type": e["type"], "lf0": a, "lf1": min(d, b), "uf0": a, "uf1": b,
                        "params": {"intensity": e.get("intensity", 1.0), "center": e.get("center", [0.5, 0.5]),
                                   **{k: v2 for k, v2 in e.items() if k not in ("type", "at", "duration")}}})
        for fx in fxs:
            queue_fx(it, ref, fx)

    footage = []
    for k in range(1, base_tracks + 1):
        footage += [it for it in clips_on_track(tl, k) if it.GetType() == "video" and it.GetMediaPoolItem() is not None
                    and not text_of_item(it)]

    # global look: a LUT on node 1 of every footage clip that has no LUT yet (graded clips stay as they are)
    look = plan.get("look") or None
    if look:
        lut = look.get("lut")
        if not lut and look.get("look"):
            from flow import luts
            key = luts.normalize_look(look["look"])
            tmp = FINISH_DIR / _slug(src.GetName()) / "look.cube"
            luts.write_cube(tmp, luts.build_transform(key, float(look.get("intensity", 0.8))), f"Flow {key}")
            lut = str(install_lut(str(tmp), _slug(new_name)))
            project.RefreshLUTList()
        kept = 0
        if live.enabled:
            live.say(f"COLOR · {look.get('look') or Path(str(lut)).stem} on {len(footage)} clips")
            live.page("color", 1.0)
        look_solo = nodetree.Solo(tl)
        for k, it in enumerate(footage):
            if live.enabled:
                look_solo.upto(nodetree.track_of(it))
            live.show(it.GetStart() - start + 1, 0.3, f"COLOR · clip {k + 1}/{len(footage)}" if k % 4 == 0 else None)
            g = it.GetNodeGraph()
            if g is None:
                continue
            if (g.GetLUT(1) or "") and not look.get("replace_existing"):
                kept += 1
                continue
            if look.get("nodes", True) and nodetree.available() and nodetree.can_apply(it, bool(look.get("replace_existing")))[0]:
                res = nodetree.apply_tree(it, look_lut=str(lut), keep_input=not look.get("replace_existing"),
                                          vignette=float(look.get("vignette", 0.0)))
                if res["ok"]:
                    r.ok("look")
                    r.ok("node_tree")
                    continue
            r.ok("look") if g.SetLUT(1, str(lut)) else r.fail("look", it.GetName())
        look_solo.restore()
        live.page("edit", 0.4)
        if kept:
            r.notes.append(f"look: {kept} clip(s) already had a LUT on node 1 and were left as graded "
                           f"(set \"replace_existing\": true to override)")

    # reference stills of the graded clips into a Gallery album (and jpgs next to the plan)
    if (gc or look) and plan.get("stills", True):
        shots = [(it.GetStart() - start + max(0, min(8, it.GetDuration() // 2)), f"{k + 1:02d} · {it.GetName()}",
                  nodetree.track_of(it)) for k, it in enumerate(sorted(footage, key=lambda x: x.GetStart()))]
        stl = nodetree.grab_stills(resolve, project, tl, shots, fps, f"Flow · {new_name}",
                                   plan_path.parent / "stills" / _slug(new_name), limit=int(plan.get("stills_limit", 24)))
        if stl.get("stills"):
            r.ok("gallery_still", stl["stills"])
            live.say(f"GALLERY · {stl['stills']} reference stills in album “{stl['album']}”")
        live.page("edit", 0.3)

    # picture effects over a time range: Fusion comps on the footage clips under that range
    for e in plan.get("effects") or []:
        if e.get("type") not in IMAGE_FX:
            continue
        a = F(e.get("from", e.get("at", 0)))
        b = F(e["to"]) if "to" in e else (a + F(e.get("duration", 0.4)) if "at" in e else total)
        for it in footage:
            s0, s1 = it.GetStart() - start, it.GetEnd() - start
            lo, hi = max(a, s0), min(b, s1)
            if hi <= lo:
                continue
            fx = {"type": e["type"], "lf0": lo - s0, "lf1": hi - s0, "uf0": a - s0, "uf1": b - s0,
                  "params": {"center": [0.5, 0.5], "intensity": 1.0,
                             **{k: v2 for k, v2 in e.items() if k not in ("type", "from", "to", "at", "duration")}}}
            queue_fx(it, it.GetName(), fx)
    for item, label, fxs in pending.values():
        res = apply_image_fx(item, fxs)
        for t in res["done"]:
            r.ok(f"fx:{t}")
        for t in res["failed"]:
            r.fail("fx", f"{label} {t}")

    # placeholder text fills (no inserts needed)
    disable: List[Any] = []
    title_jobs: List[dict] = []
    replace_jobs: List[dict] = []
    for op in plan.get("placeholders") or []:
        it = refs.get(op.get("item", ""))
        if it is None:
            r.fail("placeholder", f"unknown ref {op.get('item')}")
            continue
        action = op.get("action", "text")
        if action == "text":
            comp, tool = text_tool(it)
            if tool is not None:
                tool.SetInput("StyledText", op.get("text", ""))
                if op.get("kind"):
                    cfg = title_config(op["kind"], {"captions": {"font": op.get("font", "Inter")}}, op.get("style"))
                    style_title(it, op.get("text", ""), cfg, it.GetDuration(), op.get("subtext"))
                r.ok("placeholder_text")
            else:
                title_jobs.append({"kind": op.get("kind", "title"), "text": op.get("text", ""),
                                   "subtext": op.get("subtext"), "f0": it.GetStart() - start,
                                   "f1": it.GetEnd() - start, "style": op.get("style")})
                disable.append(it)
        elif action == "title":
            title_jobs.append({"kind": op.get("kind", "title"), "text": op.get("text", ""),
                               "subtext": op.get("subtext"), "f0": it.GetStart() - start,
                               "f1": it.GetEnd() - start, "style": op.get("style")})
            disable.append(it)
        elif action == "replace":
            replace_jobs.append({"item": it, "op": op})

    live.say(f"TEXT + OVERLAYS · {len(plan.get('titles') or [])} titles")
    # ---- titles + overlays (new tracks on top)
    for t in plan.get("titles") or []:
        kind = t.get("kind", "title")
        from flow.captions import TITLE_DEFAULTS
        dur = float(t.get("duration") or TITLE_DEFAULTS.get(kind, {}).get("duration", 3.0))
        f0 = F(t["at"]) if "at" in t else (max(0, total - F(dur)) if kind == "end_card" else 0)
        from flow.compile import transform_text
        case = t.get("case") or TITLE_DEFAULTS.get(kind, {}).get("case", "none")
        title_jobs.append({"kind": kind, "text": transform_text(t.get("text", ""), case, False),
                           "subtext": t.get("subtext"), "f0": f0, "f1": min(total, f0 + F(dur)), "style": {k: t[k] for k in (
                               "font", "font_style", "size", "color", "position", "animation", "box", "align")
                               if k in t}})
    overlays = []
    for e in plan.get("effects") or []:
        f0 = F(e.get("from", e.get("at", 0)))
        f1 = F(e["to"]) if "to" in e else (f0 + F(e.get("duration", 0.3)) if "at" in e else total)
        overlays.append({"type": e["type"], "f0": max(0, f0), "f1": min(total, f1),
                         "params": {**{k: v2 for k, v2 in e.items() if k not in ("type", "from", "to", "at",
                                                                               "duration")},
                                    "_w": w, "_h": h, "_global": f0 <= 0 and f1 >= total}})
    title_jobs = [t for t in title_jobs if t["f1"] > t["f0"] and t.get("text")]
    image_ranges = [o for o in overlays if o["type"] in IMAGE_FX]
    overlays = [o for o in overlays if o["type"] not in IMAGE_FX]
    n_title = assign_lanes(title_jobs)
    n_fx = assign_lanes(overlays)
    look = plan.get("look") or None
    base_track = tl.GetTrackCount("video")
    t_broll = base_track + 1 if replace_jobs else None
    t_fx = (t_broll or base_track) + 1
    t_titles = t_fx + n_fx
    _ensure_tracks(tl, t_titles + n_title - 1)
    for k in range(n_fx):
        tl.SetTrackName("video", t_fx + k, f"Flow FX {k + 1}")
    for k in range(n_title):
        tl.SetTrackName("video", t_titles + k, f"Flow Titles {k + 1}")
    if t_broll:
        tl.SetTrackName("video", t_broll, "Flow B-roll")

    jobs = [("title", t["f0"], t["f1"], t_titles + t["lane"], t) for t in title_jobs]
    jobs += [("overlay", o["f0"], o["f1"], t_fx + o["lane"], o) for o in overlays]
    jobs.sort(key=lambda j: j[1])

    with Inserter(tl, fps, r) as ins:
        for kind, f0, f1, track, data in jobs:
            if kind == "title":
                for it in ins.place(lambda: tl.InsertFusionTitleIntoTimeline("Text+"), f0, f1, track, "title"):
                    cfg = title_config(data["kind"], {"captions": {"font": plan.get("font", "Inter")},
                                                      "titles": plan.get("title_styles", {})}, data.get("style"))
                    ok = style_title(it, data["text"], cfg, it.GetDuration(), data.get("subtext"))
                    r.ok("title") if ok else r.fail("title", data["text"])
                    try:
                        it.SetName(f"{data['kind']}: {data['text']}"[:60])
                    except Exception:
                        pass
                    it.SetClipColor("Blue")
            elif kind == "overlay":
                for it in ins.place(lambda: tl.InsertFusionCompositionIntoTimeline(), f0, f1, track, "overlay"):
                    ok = build_overlay(it, data, it.GetDuration())
                    r.ok(f"overlay:{data['type']}") if ok else r.fail("overlay", data["type"])
                    mode = OVERLAY_COMPOSITE.get(data["type"])
                    if mode:
                        it.SetProperty("CompositeMode", rb.const(resolve, mode[0]))
                        it.SetProperty("Opacity", mode[1])
                    it.SetClipColor("Purple")

    # B-roll replacements (AppendToTimeline with record frame on the B-roll track; no ripple)
    for jb in replace_jobs:
        it, op = jb["item"], jb["op"]
        clip = _find_pool_clip(mp, op.get("clip", ""))
        if clip is None:
            r.fail("replace", f"no media pool clip matches {op.get('clip')!r}")
            continue
        sf = F(op.get("src_in", 0.0))
        app = mp.AppendToTimeline([{"mediaPoolItem": clip, "startFrame": sf, "endFrame": sf + it.GetDuration(),
                                    "trackIndex": t_broll, "recordFrame": it.GetStart(), "mediaType": 1}])
        if app:
            r.ok("replace")
            disable.append(it)
        else:
            r.fail("replace", f"couldn't place {op.get('clip')}")
    for it in disable:
        try:
            it.SetClipEnabled(False)
        except Exception:
            pass

    if live.enabled:
        live.say(f"REVIEW · playing “{new_name}” ({total / fps:.1f}s)")
        live.play(0, total)
        live.show(0, 0)
    # ---- note markers: complete the user's markers on the duplicate
    scan_notes = {}
    try:
        scan_notes = {n["id"]: n for n in read_json(plan_path.parent / "finish_scan.json").get("notes", [])}
    except Exception:
        pass
    for rec in plan.get("notes") or []:
        n = scan_notes.get(rec.get("id"), {})
        status = rec.get("status", "done")
        frame = n.get("tl_frame")
        if frame is None and rec.get("at") is not None:
            frame = F(rec["at"])
        if frame is None:
            frame = 0
        frame = max(0, min(total - 1, int(frame)))
        if n.get("source") == "timeline marker":
            try:
                tl.DeleteMarkerAtFrame(frame)
            except Exception:
                pass
        label = {"done": "✓", "needs_user": "Flow needs you:", "skipped": "Skipped:"}.get(status, status)
        if tl.AddMarker(frame, NOTE_COLORS.get(status, "Sky"), f"{label} {n.get('text', rec.get('id', ''))}"[:60],
                        f"{rec.get('action') or ''} {rec.get('reason') or ''}".strip()[:500], 1, "flow"):
            r.ok(f"note_{status}")

    picks = sorted({t["f0"] + max(1, (t["f1"] - t["f0"]) // 2) for t in title_jobs} | {total // 2})[:4]
    refresh_view(resolve, project, tl)
    preview = export_frames(tl, picks, plan_path.parent / "preview", "finish")
    rep = {"ok": not r.failed.get("title") and not r.failed.get("clip"), "timeline": new_name,
           "source_timeline": src.GetName(), "counts": r.counts, "failed": r.failed, "problems": r.notes,
           "preview": preview, "seconds": round(time.time() - t0, 1)}
    write_json(plan_path.parent / "finish_report.json", rep)
    return rep


def format_report(rep: dict) -> str:
    lines = [f"{'Done' if rep['ok'] else 'Finished with problems'} in {rep['seconds']}s → “{rep['timeline']}”"
             f" (duplicate of “{rep['source_timeline']}”, original untouched)",
             "applied: " + (", ".join(f"{k}×{v}" for k, v in rep["counts"].items()) or "nothing")]
    if rep["failed"]:
        lines.append("failed: " + ", ".join(f"{k}×{v}" for k, v in rep["failed"].items()))
    for p in rep.get("problems", [])[:10]:
        lines.append(f"  · {p}")
    if rep.get("preview"):
        lines.append("preview: " + ", ".join(rep["preview"]))
    return "\n".join(lines)
