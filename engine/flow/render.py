"""`flow render`: export a Flow timeline to a finished file, and `flow srt`: caption sidecars.

    flow render [--job J] [--timeline NAME] [--out DIR] [--codec h264|h265|prores] [--no-wait]
    flow srt [--job J] [--out FILE]

render queues one job on Resolve's Deliver page (whole timeline, timeline resolution, audio on) and
waits for it. The timeline defaults to the job's last build (report.json), then the current one.
srt needs no Resolve: it writes the job's captions (build.json) as SubRip, timed to the output.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from flow import resolve_bridge as rb
from flow.util import FlowError, read_json, write_json

DEFAULT_OUT = Path.home() / "Movies" / "Flow"

# codec choice -> (container, codec names Resolve may use for it, in order of preference)
CODECS: Dict[str, tuple] = {
    "h264": ("mp4", ["H264", "H.264", "H264_AVC"]),
    "h265": ("mp4", ["H265", "H.265", "HEVC", "H265_HEVC"]),
    "prores": ("mov", ["ProRes422HQ", "Apple ProRes 422 HQ", "ProRes422", "Apple ProRes 422"]),
}


# ---------------------------------------------------------------- SRT (offline)
def srt_time(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def captions_srt(build: dict, words: Optional[list] = None) -> str:
    """SubRip text for a build.json's speech captions (the hook is an on-screen title, not speech)."""
    fps = float(build.get("fps") or 30.0)
    cues: List[tuple] = []
    for c in build.get("captions") or []:
        text = c.get("text", "")
        if words and c.get("words"):  # the spoken words, with their own case and punctuation
            try:
                text = " ".join(words[i]["text"] for i in c["words"])
            except (IndexError, KeyError, TypeError):
                pass
        cues.append((c["f0"], c["f1"], text))
    cues = [c for c in cues if c[1] > c[0] and str(c[2]).strip()]
    cues.sort(key=lambda c: c[0])
    out = []
    for n, (f0, f1, text) in enumerate(cues, 1):
        text = re.sub(r"[ \t]+", " ", str(text)).strip()
        out.append(f"{n}\n{srt_time(f0 / fps)} --> {srt_time(f1 / fps)}\n{text}\n")
    return "\n".join(out)


def write_srt(job: Path, out: Optional[Path] = None) -> dict:
    b = read_json(job / "build.json")
    words = None
    if (job / "analysis.json").exists():
        words = read_json(job / "analysis.json").get("words")
    text = captions_srt(b, words)
    out = out or job / f"{_safe(b.get('clip_name') or job.name)}.srt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"file": str(out), "cues": text.count(" --> ")}


# ---------------------------------------------------------------- render (Resolve)
def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "", name.replace("·", "-")).strip() or "Flow"


def _pick_codec(project: Any, choice: str) -> tuple:
    if choice not in CODECS:
        raise FlowError(f"Unknown codec {choice!r}. Use one of: {', '.join(CODECS)}.")
    fmt, wanted = CODECS[choice]
    try:
        available = project.GetRenderCodecs(fmt) or {}
    except Exception:
        available = {}
    # GetRenderCodecs returns {description: codec name}; match either side.
    names = {str(v).lower(): v for v in available.values()}
    names.update({str(k).lower(): v for k, v in available.items()})
    for w in wanted:
        if w.lower() in names:
            return fmt, names[w.lower()]
    if available:
        raise FlowError(f"Resolve can't render {choice} into .{fmt} here. Available: "
                        + ", ".join(sorted(map(str, available))))
    return fmt, wanted[0]


def _target_timeline(project: Any, job: Optional[Path], name: Optional[str]) -> Any:
    from flow.notes import _timeline_by_name
    if not name and job is not None and (job / "report.json").exists():
        name = read_json(job / "report.json").get("timeline")
    if name:
        tl = _timeline_by_name(project, name)
        if tl is None:
            raise FlowError(f"No timeline named “{name}” in this project.")
        return tl
    tl = project.GetCurrentTimeline()
    if tl is None:
        raise FlowError("No timeline is open in Resolve.")
    return tl


def render(job: Optional[Path] = None, timeline: Optional[str] = None, out_dir: Optional[Path] = None,
           codec: str = "h264", wait: bool = True, srt: bool = True, timeout: float = 3600,
           poll: float = 1.0, resolve: Any = None, log=print) -> dict:
    resolve = resolve or rb.connect()
    project, _, _ = rb.current(resolve)
    tl = _target_timeline(project, job, timeline)
    project.SetCurrentTimeline(tl)
    out_dir = Path(out_dir or DEFAULT_OUT).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _safe(tl.GetName())

    fmt, codec_name = _pick_codec(project, codec)
    try:
        resolve.OpenPage("deliver")
    except Exception:
        pass
    try:
        project.SetCurrentRenderMode(1)  # single clip
    except Exception:
        pass
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec_name):
        raise FlowError(f"Resolve refused the render format {fmt}/{codec_name}.")
    settings = {
        "SelectAllFrames": True, "TargetDir": str(out_dir), "CustomName": base,
        "ExportVideo": True, "ExportAudio": True,
    }
    try:
        w = int(tl.GetSetting("timelineResolutionWidth") or 0)
        h = int(tl.GetSetting("timelineResolutionHeight") or 0)
        if w and h:
            settings.update({"FormatWidth": w, "FormatHeight": h})
    except Exception:
        pass
    if not project.SetRenderSettings(settings):
        raise FlowError("Resolve refused the render settings.")
    job_id = project.AddRenderJob()
    if not job_id:
        raise FlowError("Resolve couldn't add the render job (is the Deliver page busy?).")
    if not project.StartRendering(job_id):
        raise FlowError("Resolve couldn't start rendering.")
    file = out_dir / f"{base}.{fmt}"
    result = {"timeline": tl.GetName(), "job_id": job_id, "file": str(file), "codec": codec_name,
              "status": "Rendering", "percent": 0}

    srt_file = None
    if srt and job is not None and (job / "build.json").exists():
        try:
            srt_file = write_srt(job, out_dir / f"{base}.srt")["file"]
        except FlowError:
            srt_file = None
    result["srt"] = srt_file

    if not wait:
        return result
    t0, last = time.time(), -10
    while True:
        st = project.GetRenderJobStatus(job_id) or {}
        status = str(st.get("JobStatus") or "")
        pct = int(st.get("CompletionPercentage") or 0)
        if pct >= last + 10:
            log(f"RENDER · {pct}%")
            last = pct
        if status in ("Complete", "Failed", "Cancelled") or (
                not status and not project.IsRenderingInProgress()):
            result.update(status=status or "Complete", percent=pct)
            if st.get("Error"):
                result["error"] = st["Error"]
            break
        if time.time() - t0 > timeout:
            result.update(status="Timeout", percent=pct)
            break
        time.sleep(poll)
    if job is not None:
        write_json(job / "render.json", result)
    if result["status"] != "Complete":
        raise FlowError(f"Render {result['status'].lower()}: {result.get('error') or 'see the Deliver page'}")
    return result


def format_render(res: dict) -> str:
    lines = [f"{'Rendered' if res.get('status') == 'Complete' else 'Render queued'} “{res['timeline']}”"
             f" → {res['file']} ({res['codec']})"]
    if res.get("srt"):
        lines.append(f"captions: {res['srt']}")
    return "\n".join(lines)
