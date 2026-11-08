"""AI shots: generated or restyled footage on the reel, made with a video-generation service.

    flow aishots --job J               prepare aishots_plan.json: cut source windows, grab reference
                                       frames, write the exact generation requests (aishots/requests.json)
    flow aishots-put --url U --file F  upload a prepared file to a presigned URL from the service
    flow aishots-import --job J --id R1 --url U   download a finished shot into the B-roll library

Two kinds of shot (ids: R1, R2… for restyles, G1, G2… for generated):
  restyle   a window of the speaker's own clip (3–30 s), re-rendered in a new look with the same
            motion. It goes on the B-roll lane cut exactly like the edit underneath (same cuts,
            speed ramps, punch-ins), so the reel can switch into the new look and back.
  generate  a new shot from a prompt (optionally guided by a still of the source clip), used like
            any other B-roll clip.

The generation itself runs in the Claude session through the user's connected generation tools
(Flow has no API key). Flow only prepares the inputs and places the results. Generations cost
credits: the skill always checks the price and asks before starting one.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow import broll
from flow.util import FlowError, ffprobe, read_json, run, which, write_json

PLAN = "aishots_plan.json"

# shortest/longest source a model accepts (seconds); unknown slugs fall back to "auto"
MODIFY_LIMITS = {
    "video-modify-kling4": (3, 30), "video-modify-kling4-flash": (3, 20), "video-modify-seedance2.5": (4, 30),
    "video-modify-seedance2": (4, 15), "video-modify-aleph2": (2, 30), "video-modify-minimax-h3": (2, 15),
    "video-modify-grok": (1, 8.6), "video-modify-gemini-omni-1_1": (3, 10),
    "video-modify-gemini-omni-1_2-flash": (3, 10), "auto": (1, 30),
}
DEFAULT_MODIFY = "video-modify-kling4"


def _dir(job: Path) -> Path:
    d = job / "aishots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_plan(job: Path) -> dict:
    p = job / PLAN
    if not p.exists():
        raise FlowError(f"No {PLAN} in {job}. Write the shots you want first (see the Flow skill, AI shots).")
    data = read_json(p)
    shots = data.get("shots") if isinstance(data, dict) else data
    if not isinstance(shots, list) or not shots:
        raise FlowError(f"{PLAN} has no shots")
    return {"shots": shots, **({k: v for k, v in data.items() if k != "shots"} if isinstance(data, dict) else {})}


def validate(shot: dict, clip: dict) -> List[str]:
    """Problems that would waste a paid generation."""
    errs = []
    sid, kind = str(shot.get("id", "")), shot.get("kind")
    if kind not in ("restyle", "generate"):
        errs.append(f"{sid}: kind must be restyle or generate")
        return errs
    want = "R" if kind == "restyle" else "G"
    if not (sid[:1] == want and sid[1:].isdigit()):
        errs.append(f"{sid}: {kind} ids are {want}1, {want}2, …")
    if not (shot.get("prompt") or "").strip():
        errs.append(f"{sid}: needs a prompt")
    if kind == "restyle":
        w = shot.get("window")
        if not (isinstance(w, list) and len(w) == 2 and 0 <= float(w[0]) < float(w[1])):
            errs.append(f"{sid}: window must be [start, end] in source seconds")
            return errs
        if float(w[1]) > float(clip.get("duration", 1e9)) + 0.01:
            errs.append(f"{sid}: window ends after the clip ({clip.get('duration')}s)")
        lo, hi = MODIFY_LIMITS.get(shot.get("model") or DEFAULT_MODIFY, MODIFY_LIMITS["auto"])
        n = float(w[1]) - float(w[0])
        if not lo <= n <= hi:
            errs.append(f"{sid}: {n:.1f}s window, but {shot.get('model') or DEFAULT_MODIFY} takes {lo}–{hi}s")
        if min(int(clip.get("width") or 0), int(clip.get("height") or 0)) < 700:
            errs.append(f"{sid}: source is under 700 px on its short side; most restyle models refuse that")
    else:
        d = float(shot.get("duration", 0) or 0)
        if not 1 <= d <= 30:
            errs.append(f"{sid}: duration must be 1–30 s")
        rf = shot.get("ref_frame")
        if rf is not None and not 0 <= float(rf) <= float(clip.get("duration", 1e9)):
            errs.append(f"{sid}: ref_frame is outside the clip")
    return errs


def _cut(src: Path, w0: float, w1: float, out: Path) -> None:
    run([which("ffmpeg"), "-v", "error", "-y", "-ss", f"{w0:.3f}", "-i", str(src), "-t", f"{w1 - w0:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])


def _still(src: Path, t: float, out: Path) -> None:
    run([which("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-q:v", "2", str(out)])


def prepare(job: Path) -> dict:
    """Cut the inputs and write one generation request per shot (aishots/requests.json)."""
    if not which("ffmpeg"):
        raise FlowError("ffmpeg not found. Install it: brew install ffmpeg")
    clip = read_json(job / "clip.json")
    src = Path(clip["path"])
    plan = load_plan(job)
    errs = [e for s in plan["shots"] for e in validate(s, clip)]
    ids = [s.get("id") for s in plan["shots"]]
    errs += [f"{i}: id used twice" for i in set(ids) if ids.count(i) > 1]
    if errs:
        raise FlowError("aishots plan problems:\n  " + "\n  ".join(errs))
    d = _dir(job)
    reqs = []
    for s in plan["shots"]:
        sid = s["id"]
        uploads: List[dict] = []
        if s["kind"] == "restyle":
            w0, w1 = float(s["window"][0]), float(s["window"][1])
            f = d / f"{sid}_source.mp4"
            _cut(src, w0, w1, f)
            uploads.append({"name": f.name, "path": str(f), "mime": "video/mp4", "size": f.stat().st_size,
                            "role": "source video"})
            args = {"prompt": s["prompt"], "slug": s.get("model") or DEFAULT_MODIFY,
                    "resolution": s.get("resolution", "1080p"), "videoUrl": f"<uploaded {f.name}>"}
            tool = "video_modify"
        else:
            args = {"prompt": s["prompt"], "duration": float(s["duration"]),
                    "aspectRatio": s.get("aspect", "9:16")}
            if s.get("model"):
                args["slug"] = s["model"]
            if s.get("resolution"):
                args["resolution"] = s["resolution"]
            if s.get("ref_frame") is not None:
                f = d / f"{sid}_ref.jpg"
                _still(src, float(s["ref_frame"]), f)
                uploads.append({"name": f.name, "path": str(f), "mime": "image/jpeg", "size": f.stat().st_size,
                                "role": "start frame" if s.get("ref_as") == "start" else "style reference"})
                if s.get("ref_as") == "start":
                    args["keyframes"] = {"start": {"type": "image", "url": f"<uploaded {f.name}>"}}
                else:
                    args["references"] = [{"type": "image", "url": f"<uploaded {f.name}>"}]
            tool = "video_generate"
        for u in uploads:  # size limits of the upload endpoint
            lim = 200e6 if u["mime"].startswith("video") else 25e6
            if u["size"] > lim:
                raise FlowError(f"{sid}: {u['name']} is {u['size'] / 1e6:.0f} MB, over the {lim / 1e6:.0f} MB upload limit")
        reqs.append({"id": sid, "kind": s["kind"], "tool": tool, "args": args, "uploads": uploads,
                     "window": s.get("window"), "prompt": s["prompt"], "model": args.get("slug")})
    out = {"job": str(job), "prepared": time.strftime("%Y-%m-%dT%H:%M:%S"), "requests": reqs}
    write_json(d / "requests.json", out)
    return out


def format_requests(data: dict) -> str:
    lines = [f"AI SHOTS · {len(data['requests'])} generation(s) prepared in {Path(data['job']) / 'aishots'}"]
    for r in data["requests"]:
        what = (f"restyle source {r['window'][0]:.2f}–{r['window'][1]:.2f}s"
                if r["kind"] == "restyle" else f"generate {r['args'].get('duration')}s {r['args'].get('aspectRatio')}")
        lines.append(f"  {r['id']}: {what} · {r['tool']} · {r.get('model') or 'auto model'}")
        lines.append(f"      prompt: {r['prompt'][:110]}")
        for u in r["uploads"]:
            lines.append(f"      upload: {u['path']} ({u['mime']}, {u['size'] / 1e6:.1f} MB, {u['role']})")
    lines += ["", "Next (in the Claude session): price each request (simulate_cost) and ask the user;",
              "upload each file (request_upload -> flow aishots-put -> finalize_upload), run the tool with",
              "the uploaded identifiers/URLs, wait, then: flow aishots-import --id <ID> --url <result url>."]
    return "\n".join(lines)


def put(url: str, file: Path, mime: str) -> int:
    if not url.startswith("https://"):
        raise FlowError("Upload URL must be https")
    data = Path(file).read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT", headers={"Content-Type": mime})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.status


def _download(url: str, out: Path) -> None:
    if not url.startswith("https://"):
        raise FlowError("Result URL must be https")
    tmp = out.with_suffix(".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "flow"}), timeout=600) as r, \
            open(tmp, "wb") as fh:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            fh.write(b)
    tmp.rename(out)


def import_result(job: Path, sid: str, url: Optional[str] = None, file: Optional[str] = None,
                  creation: Optional[str] = None) -> dict:
    """Bring a finished shot into the job's B-roll library under its shot id."""
    plan = load_plan(job)
    shot = next((s for s in plan["shots"] if s.get("id") == sid), None)
    if shot is None:
        raise FlowError(f"{sid} is not in {PLAN}")
    d = _dir(job)
    out = d / f"{sid}.mp4"
    if file:
        src = Path(file).expanduser()
        if not src.exists():
            raise FlowError(f"No such file: {src}")
        out.write_bytes(src.read_bytes())
    elif url:
        _download(url, out)
    else:
        raise FlowError("Give --url or --file")
    facts = ffprobe(out)
    if not facts["has_video"]:
        raise FlowError(f"{out.name} has no video stream")
    ai: Dict[str, Any] = {"kind": shot["kind"], "prompt": shot["prompt"], "model": shot.get("model"),
                          "creation": creation, "imported": time.strftime("%Y-%m-%dT%H:%M:%S")}
    notes = []
    if shot["kind"] == "restyle":
        w0, w1 = float(shot["window"][0]), float(shot["window"][1])
        ai["window"] = [w0, w1]
        ratio = facts["duration"] / (w1 - w0)
        ai["time_scale"] = 1.0 if abs(ratio - 1.0) < 0.02 else round(ratio, 5)
        if ai["time_scale"] != 1.0:
            notes.append(f"result is {facts['duration']:.2f}s for a {w1 - w0:.2f}s window; timing is stretched to match")
        clip = read_json(job / "clip.json")
        ca, ra = (clip.get("width") or 1) / (clip.get("height") or 1), (facts["width"] or 1) / (facts["height"] or 1)
        if abs(ca - ra) > 0.02:
            notes.append("result has a different aspect ratio than the source; the framing will not line up")
    data = broll.load_index(job)
    clips = [c for c in data.get("clips", []) if c["id"] != sid]
    desc = shot.get("description") or f"AI {shot['kind']}: {shot['prompt']}"
    c = broll.clip_entry(out, sid, f"ai:{shot['kind']}", None, {}, broll._library(), ai=ai, description=desc)
    if c is None:
        raise FlowError(f"{out.name} could not be read as video")
    clips.append(c)
    broll.write_index(job, clips, data.get("note"))
    return {"id": sid, "file": str(out), "duration": c["duration"], "size": f"{c['width']}x{c['height']}",
            "notes": notes}


def status(job: Path) -> str:
    plan = load_plan(job)
    have = {c["id"] for c in broll.load_index(job).get("clips", []) if c.get("ai")}
    lines = []
    for s in plan["shots"]:
        lines.append(f"{s.get('id')}: {s.get('kind')} · {'imported' if s.get('id') in have else 'not imported'}"
                     f" · {s.get('prompt', '')[:80]}")
    return "\n".join(lines)
