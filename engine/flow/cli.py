"""`flow` command line. Every command prints a short human summary; --json prints data."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from flow import __version__, paths
from flow.util import FlowError, read_json


def _out(args, data, text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(text.rstrip())


# Which command produces each job file, for friendly "run X first" errors.
MISSING_STEP = {
    "clip.json": "find --path <clip>", "words.json": "transcribe", "analysis.json": "analyze",
    "edit_plan.draft.json": "analyze", "build.json": "compile", "broll.json": "broll",
}


# ---------------------------------------------------------------- commands
def cmd_doctor(args):
    from flow import doctor
    report = doctor.run_checks(connect_resolve=not args.offline)
    _out(args, report, doctor.format_report(report))
    return 0 if report["ok"] else 1


def cmd_probe(args):
    from flow import probe
    result = probe.run(switch=args.switch, return_to_original=not args.stay)
    _out(args, result, probe.format_result(result))
    return 0 if result.get("ok") else 1


def cmd_find(args):
    from flow.find_clip import from_resolve, open_job
    if args.path:
        job = open_job(Path(args.path))
    else:
        from flow import resolve_bridge
        resolve = resolve_bridge.connect()
        media, info, rng = from_resolve(resolve)
        job = open_job(Path(media), info, rng)
    clip = read_json(job / "clip.json")
    how = (clip.get("resolve") or {}).get("how", "path")
    text = (f"job {job.name}\nclip {clip['path']}\n"
            f"{clip['duration']:.1f}s · {clip['fps']:.3f} fps · {clip['width']}x{clip['height']} · found via {how}")
    if clip.get("range"):
        text += f"\nusing only the trimmed range {clip['range'][0]:.2f}–{clip['range'][1]:.2f}s"
    _out(args, {"job": str(job), "clip": clip}, text)
    return 0


def cmd_transcribe(args):
    from flow.transcribe import transcribe
    job = paths.resolve_job(args.job)
    clip = read_json(job / "clip.json")
    t0 = time.time()
    res = transcribe(Path(clip["path"]), job, clip["duration"], language=args.language,
                     model=Path(args.model) if args.model else None)
    _out(args, {k: v for k, v in res.items() if k != "words"} | {"word_count": len(res["words"])},
         f"{len(res['words'])} words · language {res['language']} · "
         f"{'cached' if res.get('cached') else f'{time.time() - t0:.1f}s'}")
    return 0


def cmd_analyze(args):
    from flow.analyze import analyze
    job = paths.resolve_job(args.job)
    a = analyze(job)
    s = a["stats"]
    text = (f"{len(a['words'])} words · {len(a['sentences'])} sentences · {len(a['silences'])} silences"
            f" · {len(a['fillers'])} fillers · {len(a['candidates'])} retake candidates"
            f" · {len(a['emphasis'])} emphasis peaks\n"
            f"speech {s['speech_time']:.1f}s, est. after auto-cut {s['est_after_cut']:.1f}s\n"
            f"review: {job / 'review.txt'}\nframes: {(a['frames'] or ['none'])[0]}\n"
            f"draft plan: {job / 'edit_plan.draft.json'}")
    _out(args, {"job": str(job), "stats": s, "frames": a["frames"], "review": str(job / "review.txt")}, text)
    return 0


def cmd_prepare(args):
    """find (optional) + transcribe + analyze + notes in one go."""
    if args.path or not args.job:
        rc = cmd_find(argparse.Namespace(path=args.path, json=False))
        if rc:
            return rc
    args.job = args.job or "latest"
    cmd_transcribe(args)
    cmd_analyze(args)
    try:
        from flow import notes
        job = paths.resolve_job(args.job)
        n = notes.collect_for_job(job, notes_file=args.notes_file, connect=not args.offline)
        print(f"{len(n['notes'])} production notes")
    except FlowError as exc:
        print(f"notes: {exc}")
    return 0


def cmd_notes(args):
    from flow import notes
    if args.timeline:
        data = notes.collect_timeline(notes_file=args.file)
    else:
        job = paths.resolve_job(args.job)
        data = notes.collect_for_job(job, notes_file=args.file, connect=not args.offline)
    _out(args, data, notes.format_notes(data))
    return 0


def cmd_styles(args):
    from flow.styles import get_style, list_styles
    if args.show:
        st = get_style(args.show)
        print(json.dumps({k: v for k, v in st.items() if not k.startswith("_")}, indent=2))
        return 0
    rows = list_styles()
    text = "\n".join(f"{r['id']:<20} {r['name']:<16} {r['description']}"
                     + ("  (custom)" if r["source"] == "custom" else "") for r in rows)
    _out(args, rows, text)
    return 0


def cmd_style_save(args):
    from flow.styles import save_user_style
    data = json.loads(Path(args.file).read_text()) if args.file else json.loads(sys.stdin.read())
    p = save_user_style(data, base=args.base)
    print(f"saved {p}")
    return 0


def cmd_fonts(args):
    from flow import fonts
    data = fonts.list_fonts(connect=not args.offline)
    _out(args, data, fonts.format_fonts(data, args.all))
    return 0


def cmd_luts(args):
    from flow import luts
    if args.install:
        written = luts.install_base_looks(paths.FLOW_LUT_DIR)
        print(f"installed {len(written)} looks into {paths.FLOW_LUT_DIR}")
    looks = {k: v[0] for k, v in luts.LOOKS.items()}
    user = luts.list_user_luts(paths.RESOLVE_LUT_DIR) if args.user else []
    _out(args, {"looks": looks, "user_luts": user},
         "looks: " + ", ".join(f"{k} ({v})" for k, v in looks.items())
         + (f"\n{len(user)} LUTs in Resolve's LUT folder" if args.user else ""))
    return 0


def cmd_compile(args):
    from flow.compile import compile_job, summary_text
    job = paths.resolve_job(args.job)
    b = compile_job(job)
    _out(args, {"build": str(job / "build.json"), "stats": b["stats"], "warnings": b["warnings"]},
         summary_text(b))
    return 0


def cmd_build(args):
    from flow import build
    from flow import live
    job = paths.resolve_job(args.job)
    report = build.build_job(job, live=live.from_args(args, job))
    _out(args, report, build.format_report(report))
    return 0 if report.get("ok") else 1


def cmd_frames(args):
    from flow import build
    out = build.export_timeline_frames(args.timeline, count=args.count, job=args.job)
    _out(args, out, "\n".join(out.get("frames", [])) or "no frames")
    return 0


def cmd_learn(args):
    from flow import learn_style
    style = learn_style.learn_from_timeline(args.name, base=args.base, save=not args.dry_run)
    _out(args, style, learn_style.format_style(style))
    return 0


def cmd_finish_scan(args):
    from flow import finish
    data = finish.scan(notes_file=args.file)
    _out(args, data, finish.format_scan(data))
    return 0


def cmd_finish(args):
    from flow import finish
    from flow import live
    plan = Path(args.plan) if args.plan else None
    report = finish.apply(plan, live=live.from_args(args, plan.parent if plan else paths.FLOW_HOME))
    _out(args, report, finish.format_report(report))
    return 0 if report.get("ok") else 1


def cmd_broll(args):
    from flow import broll
    job = paths.resolve_job(args.job)
    bins = None if args.bin is None else [b for b in args.bin if b]
    if args.no_bins:
        bins = []
    data = broll.scan(job, folders=args.folder or [], bins=bins, files=args.file or [],
                      use_resolve=not args.offline)
    text = broll.format_library(data)
    _out(args, {"job": str(job), "clips": len(data["clips"]), "sheets": [s["file"] for s in data["sheets"]],
                "review": str(job / "broll_review.txt")}, text + f"\nreview: {job / 'broll_review.txt'}")
    return 0


def cmd_broll_describe(args):
    from flow import broll
    job = paths.resolve_job(args.job)
    if args.file:
        items = read_json(Path(args.file))
    elif args.clip and args.text:
        items = {args.clip: " ".join(args.text)}
    else:
        raise FlowError("Give a clip id and text, or --file descriptions.json")
    res = broll.describe(job, items)
    _out(args, res, f"described {', '.join(res['described'])}")
    return 0


def cmd_aishots(args):
    from flow import aishots
    job = paths.resolve_job(args.job)
    if args.status:
        print(aishots.status(job))
        return 0
    data = aishots.prepare(job)
    _out(args, data, aishots.format_requests(data))
    return 0


def cmd_aishots_put(args):
    from flow import aishots
    code = aishots.put(args.url, Path(args.file), args.mime)
    print(f"uploaded {Path(args.file).name}: HTTP {code}")
    return 0 if 200 <= code < 300 else 1


def cmd_aishots_import(args):
    from flow import aishots
    job = paths.resolve_job(args.job)
    res = aishots.import_result(job, args.id, url=args.url, file=args.file, creation=args.creation)
    _out(args, res, f"{res['id']}: {res['file']} · {res['duration']:.2f}s · {res['size']}"
         + "".join(f"\n  note: {n}" for n in res["notes"]))
    return 0


def cmd_magnific(args):
    from flow import magnific as mg
    a, v = args.action, args.value
    if a == "watch":
        return mg.watch()
    if a == "selftest":
        from flow import magnific_live
        res = magnific_live.run_live(switch=args.switch, url=args.url, creation=args.creation, name=args.name,
                                     prompt=args.prompt, model=args.model)
        _out(args, res, magnific_live.format_result(res))
        return 0 if res.get("ok") else 1
    if a == "slots":
        from flow import magnific_slots as ms
        res = mg.slots(mark=not args.no_mark)
        _out(args, res, ms.format_slots(res))
        return 0
    if a == "status":
        st = mg.status(resolve=not args.offline)
        _out(args, st, mg.format_status(st))
        return 0
    if a == "sync":
        res = mg.sync(all_=args.all, where=args.place)
        _out(args, res, mg.format_sync(res))
        return 0
    if a == "folder":
        res = mg.set_folder(v, all_=args.all) if v else mg.status()
        _out(args, res, res["say"] if v else mg.format_status(res))
        return 0
    if a == "pull":
        res = mg.pull(url=args.url, file=args.file, name=args.name, creation=args.creation, prompt=args.prompt,
                      model=args.model, kind=args.kind, ext=args.ext, force=args.force, slot=args.slot,
                      version_of=args.version_of)
    elif a == "put":
        if not (v or args.file) or not args.slot:
            raise FlowError("Say which creation and which slot: `flow magnific put FILE --slot S3`.")
        res = mg.put(v or args.file, args.slot, src_in=args.src_in)
    elif a == "start":
        res = mg.start(where=args.place or v, interval=args.interval, still_seconds=args.still)
    elif a == "stop":
        res = mg.stop()
    elif a == "place":
        if not v:
            raise FlowError("Say where new creations go: `flow magnific place bin`, `… place slots` or "
                            "`… place playhead`.")
        res = mg.set_place(v, still_seconds=args.still)
    elif a == "undo":
        res = mg.undo()
    else:
        raise FlowError(f"unknown magnific action {a!r}")
    _out(args, res, res.get("say", "done"))
    return 0


def cmd_edit(args):
    from flow import liveedit
    ses = liveedit.Session(show=not args.quiet)
    target = int(args.clip) if args.clip else ("all" if args.all else (args.at or "here"))
    a = args.action
    if a == "where":
        w = ses.where()
        _out(args, w, liveedit.format_where(w))
        return 0
    if a == "log":
        rows = ses.entries
        _out(args, rows, "\n".join(f"{i + 1}. {e['op']}{' (undone)' if e.get('undone') else ''}: {e.get('say', '')}"
                                    for i, e in enumerate(rows)) or "no live edits on this timeline")
        return 0
    if a == "zoom":
        res = ses.zoom(target, float(args.zoom or 1.15))
    elif a == "captions":
        ch = {"color": args.color, "accent": args.accent, "size": args.size, "font": args.font,
              "font_style": args.font_style, "animation": args.animation, "case": args.case}
        if args.y is not None:
            ch["position"] = [float(args.x if args.x is not None else 0.5), float(args.y)]
        res = ses.captions(ch, args.from_, args.to)
    elif a == "caption-text":
        res = ses.caption_text(args.at or "here", args.text or "")
    elif a == "grade":
        ch = {"exposure": args.exposure, "contrast": args.contrast, "saturation": args.saturation,
              "warmth": args.warmth, "tint": args.tint, "vignette": args.vignette}
        res = ses.grade(target, ch, look=args.look,
                        intensity=float(args.intensity) if args.intensity is not None else None)
    elif a == "fx":
        res = ses.fx(args.at or "here", args.type or "shake", float(args.duration or 0.4), float(args.intensity or 1.0))
    elif a == "broll":
        res = ses.broll(args.at or "here", args.broll_clip, float(args.duration or 2.0), args.mode or "full")
    elif a == "undo":
        res = ses.undo()
    else:
        raise FlowError(f"unknown edit action {a!r}")
    _out(args, res, res.get("say", "done"))
    return 0


def cmd_status(args):
    from flow import status
    job = paths.resolve_job(args.job)
    st = status.job_status(job)
    _out(args, st, status.format_status(st))
    return 0


def cmd_jobs(args):
    from flow import status
    rows = status.list_jobs(limit=args.limit)
    _out(args, rows, status.format_jobs(rows))
    return 0


def cmd_render(args):
    from flow import render
    job = None
    if args.job or (paths.LATEST_LINK.exists() and not args.timeline):
        job = paths.resolve_job(args.job)
    res = render.render(job, timeline=args.timeline, out_dir=Path(args.out) if args.out else None,
                        codec=args.codec, wait=not args.no_wait, srt=not args.no_srt)
    _out(args, res, render.format_render(res))
    return 0


def cmd_srt(args):
    from flow import render
    job = paths.resolve_job(args.job)
    res = render.write_srt(job, Path(args.out).expanduser() if args.out else None)
    _out(args, res, f"{res['cues']} captions → {res['file']}")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flow", description="Flow: AI reel editor for DaVinci Resolve")
    p.add_argument("--version", action="version", version=f"flow {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("doctor", cmd_doctor, "check setup (Resolve, whisper, model, LUTs)")
    sp.add_argument("--offline", action="store_true", help="don't connect to Resolve")
    sp = add("probe", cmd_probe, "test Resolve API behaviors in a sandbox project")
    sp.add_argument("--switch", action="store_true", help="save & switch to the 'Flow Sandbox' project")
    sp.add_argument("--stay", action="store_true", help="stay in the sandbox afterwards")
    sp = add("find", cmd_find, "find the clip in Resolve (or --path) and open a job")
    sp.add_argument("--path")
    for name, fn, h in (("transcribe", cmd_transcribe, "speech to text with word timings"),
                        ("prepare", cmd_prepare, "find + transcribe + analyze + notes")):
        sp = add(name, fn, h)
        sp.add_argument("--job")
        sp.add_argument("--language", default="auto")
        sp.add_argument("--model")
        if name == "prepare":
            sp.add_argument("--path")
            sp.add_argument("--notes-file")
            sp.add_argument("--offline", action="store_true")
    sp = add("analyze", cmd_analyze, "silences, fillers, retakes, emphasis, color, frames")
    sp.add_argument("--job")
    sp = add("notes", cmd_notes, "read production notes: markers, placeholders, metadata")
    sp.add_argument("--job")
    sp.add_argument("--file", help="extra notes file (markdown/text)")
    sp.add_argument("--timeline", action="store_true", help="read the current timeline instead of a job's clip")
    sp.add_argument("--offline", action="store_true")
    sp = add("styles", cmd_styles, "list styles (or --show ID)")
    sp.add_argument("--show")
    sp = add("style-save", cmd_style_save, "save a custom style from JSON (file or stdin)")
    sp.add_argument("--file")
    sp.add_argument("--base", help="built-in style to extend")
    sp = add("fonts", cmd_fonts, "fonts Resolve can use for captions/titles")
    sp.add_argument("--all", action="store_true")
    sp.add_argument("--offline", action="store_true")
    sp = add("luts", cmd_luts, "list looks (--install writes them to Resolve)")
    sp.add_argument("--install", action="store_true")
    sp.add_argument("--user", action="store_true", help="also list LUTs already in Resolve")
    sp = add("broll", cmd_broll, "index B-roll (folders, media pool bins, files) for the current job")
    sp.add_argument("--job")
    sp.add_argument("--folder", action="append", help="folder of B-roll (repeatable)")
    sp.add_argument("--bin", action="append", help="media pool bin name (repeatable; default: B-Roll-like bins)")
    sp.add_argument("--file", action="append", help="single clip (repeatable)")
    sp.add_argument("--no-bins", action="store_true", help="don't read the media pool")
    sp.add_argument("--offline", action="store_true")
    sp = add("broll-describe", cmd_broll_describe, "remember what B-roll clips show")
    sp.add_argument("clip", nargs="?")
    sp.add_argument("text", nargs="*")
    sp.add_argument("--file", help='JSON {"B1": "description", ...}')
    sp.add_argument("--job")
    sp = add("aishots", cmd_aishots, "prepare AI shot generations from aishots_plan.json")
    sp.add_argument("--job")
    sp.add_argument("--status", action="store_true", help="show which shots are imported")
    sp = add("aishots-put", cmd_aishots_put, "upload a prepared file to a presigned URL")
    sp.add_argument("--url", required=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--mime", required=True)
    sp = add("aishots-import", cmd_aishots_import, "download a finished AI shot into the B-roll library")
    sp.add_argument("--job")
    sp.add_argument("--id", required=True)
    sp.add_argument("--url")
    sp.add_argument("--file")
    sp.add_argument("--creation", help="the service's creation id, kept for reference")
    sp = add("magnific", cmd_magnific, "Magnific creations into Resolve through a watched folder (see flow/magnific.py)")
    sp.add_argument("action", choices=["status", "folder", "pull", "sync", "start", "stop", "watch", "place", "undo",
                                       "slots", "put", "selftest"])
    sp.add_argument("value", nargs="?", help="folder: the folder · place/start: bin, slots or playhead · "
                                             "put: the creation's file")
    sp.add_argument("--all", action="store_true", help="folder/sync: also import files that were there before")
    sp.add_argument("--url", help="pull: the finished creation's https link")
    sp.add_argument("--file", help="pull: a local file instead of a link")
    sp.add_argument("--name", help="pull: a short title (file name, media pool Description)")
    sp.add_argument("--creation", help="pull: the Magnific creation id (kept in the clip metadata)")
    sp.add_argument("--prompt", help="pull: the prompt (media pool Comments)")
    sp.add_argument("--model", help="pull: the model slug (media pool Keywords)")
    sp.add_argument("--kind", choices=["video", "image", "audio"], help="pull: what the file must be")
    sp.add_argument("--ext", help="pull: file extension when the link hides it (.mov, .png, .wav)")
    sp.add_argument("--force", action="store_true", help="pull: save again even if the creation is in the folder")
    sp.add_argument("--slot", help="pull: the slot it was made for (S3) · put: the slot to build it into")
    sp.add_argument("--version-of", dest="version_of", help="pull: the creation it is a new version of")
    sp.add_argument("--in", dest="src_in", type=float,
                    help="put: where in the creation the used part starts, in seconds (default: the middle)")
    sp.add_argument("--no-mark", dest="no_mark", action="store_true",
                    help="slots: only read; don't write slot ids into Resolve markers")
    sp.add_argument("--place", choices=["bin", "slots", "playhead"], help="sync/start: where new creations go")
    sp.add_argument("--still", type=float, help="seconds a still image lasts on the timeline (default 5)")
    sp.add_argument("--interval", type=float, help="start: seconds between the watcher's looks (default 3)")
    sp.add_argument("--offline", action="store_true", help="status: don't ask Resolve which timeline is open")
    sp.add_argument("--switch", action="store_true",
                    help="selftest: save the open project, run in the Flow Sandbox project, switch back after")
    sp = add("edit", cmd_edit, "live edit the open Flow timeline in place (see flow/liveedit.py)")
    sp.add_argument("action", choices=["where", "zoom", "captions", "caption-text", "grade", "fx", "broll",
                                       "undo", "log"])
    sp.add_argument("--at", help="'here' (playhead, default) or output seconds")
    sp.add_argument("--clip", help="clip number on the video track (1 = first)")
    sp.add_argument("--all", action="store_true", help="every clip (grade)")
    sp.add_argument("--zoom", type=float)
    sp.add_argument("--color"); sp.add_argument("--accent"); sp.add_argument("--size", type=float)
    sp.add_argument("--font"); sp.add_argument("--font-style", dest="font_style")
    sp.add_argument("--animation"); sp.add_argument("--case")
    sp.add_argument("--x", type=float); sp.add_argument("--y", type=float)
    sp.add_argument("--from", dest="from_", type=float); sp.add_argument("--to", type=float)
    sp.add_argument("--text")
    for k in ("exposure", "contrast", "saturation", "warmth", "tint", "vignette", "intensity", "duration"):
        sp.add_argument(f"--{k}")
    sp.add_argument("--look"); sp.add_argument("--type"); sp.add_argument("--mode")
    sp.add_argument("--broll-clip", dest="broll_clip", help="B-roll/AI shot id (B3, G1)")
    sp.add_argument("--quiet", action="store_true", help="don't move the playhead or switch pages")
    sp = add("compile", cmd_compile, "edit_plan.json -> build.json")
    sp.add_argument("--job")
    sp = add("build", cmd_build, "build the timeline in Resolve from build.json")
    sp.add_argument("--job")
    sp.add_argument("--live", action="store_true", help="build in front of the user: pages, playhead, playback")
    sp.add_argument("--pace", type=float, default=1.0, help="live mode speed: 0.5 = twice as fast, 2 = slower")
    sp.add_argument("--play", choices=["scrub", "keys", "none"], default="scrub",
                    help="live playback at the end (keys = real playback with sound, needs Accessibility)")
    sp = add("frames", cmd_frames, "export stills from a timeline to check the result")
    sp.add_argument("--timeline", help="timeline name (default: current)")
    sp.add_argument("--count", type=int, default=4)
    sp.add_argument("--job")
    sp = add("learn-style", cmd_learn, "learn a custom style from the current Resolve timeline")
    sp.add_argument("name")
    sp.add_argument("--base", default="talking-head")
    sp.add_argument("--dry-run", action="store_true")
    sp = add("finish-scan", cmd_finish_scan, "read the current timeline for Finish mode")
    sp.add_argument("--file", help="extra notes file")
    sp = add("finish", cmd_finish, "apply finish_plan.json to a duplicate of the timeline")
    sp.add_argument("--plan")
    sp.add_argument("--live", action="store_true", help="work in front of the user: pages, playhead, playback")
    sp.add_argument("--pace", type=float, default=1.0)
    sp.add_argument("--play", choices=["scrub", "keys", "none"], default="scrub")
    sp = add("status", cmd_status, "where the job stands and what to run next (offline)")
    sp.add_argument("--job")
    sp = add("jobs", cmd_jobs, "list recent jobs (* = latest)")
    sp.add_argument("--limit", type=int, default=10)
    sp = add("render", cmd_render, "render the job's timeline (or --timeline) to a file via the Deliver page")
    sp.add_argument("--job")
    sp.add_argument("--timeline", help="timeline name (default: the job's last build, then the current one)")
    sp.add_argument("--out", help="output folder (default ~/Movies/Flow)")
    sp.add_argument("--codec", choices=["h264", "h265", "prores"], default="h264")
    sp.add_argument("--no-wait", action="store_true", help="queue and start the render, don't wait")
    sp.add_argument("--no-srt", action="store_true", help="don't write the .srt captions next to it")
    sp = add("srt", cmd_srt, "write the job's captions as an .srt file (offline)")
    sp.add_argument("--job")
    sp.add_argument("--out", help="output .srt path (default: in the job folder)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    paths.ensure_dirs()
    try:
        return int(args.fn(args) or 0)
    except FlowError as exc:
        print(f"flow: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        name = Path(exc.filename or "").name
        step = MISSING_STEP.get(name)
        hint = f" Run `flow {step}` first." if step else ""
        print(f"flow: missing {exc.filename}.{hint}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"flow: a JSON file is malformed (line {exc.lineno}, column {exc.colno}): {exc.msg}. "
              "Fix it and run the command again.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
