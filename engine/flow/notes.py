"""Production notes: everything an editor left for Flow to read and complete.

Sources
  * markers (name + note + color) on the source clip, the timeline, and timeline items
  * placeholders: Text+ titles with placeholder text ("TITLE", "[name here]", "TBD",
    "TODO …", "XXX"), and any clip/generator named "Placeholder…", "PH: …", "TODO: …", "Flow: …"
  * clip metadata: Comments, Description, Keywords
  * a notes file: --file, or a sidecar next to the media (<clip>.notes.md, notes.md, …)
    Lines may start with a time or range: "0:12 title: Day one" / "0:12-0:18 slow-mo"

Each note gets an id (N1…), an anchor (source seconds for reel jobs, timeline
frames for Finish mode), and a keyword guess. Claude interprets the text, acts
on it, and records the outcome per note in the plan's "notes" list.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Optional

from flow.util import FlowError, fmt_time, read_json, write_json

PLACEHOLDER_TEXT = re.compile(
    r"^\s*(\[.*\]|<.*>|\{.*\}|tbd|todo\b.*|xxx+.*|placeholder.*|title( here)?|your title.*|"
    r"name here|lorem.*|ph[:\s].*|flow[:\s].*|\?+|insert .*|b-?roll.*|text here|headline)\s*$",
    re.IGNORECASE)
PLACEHOLDER_NAME = re.compile(r"^\s*(placeholder|ph:|todo:|flow:|tbd|b-?roll:)", re.IGNORECASE)
TIME_PREFIX = re.compile(
    r"^\s*(?:[-*•]\s*)?(?P<a>\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?)(?:\s*[-–—]\s*(?P<b>\d{1,2}(?::\d{2}){1,2}(?:\.\d+)?))?"
    r"\s*[:|\-–—]?\s*(?P<text>.*)$")
SIDECARS = ("{stem}.notes.md", "{stem}.notes.txt", "{stem}.md", "{stem}.txt", "notes.md", "notes.txt",
            "production notes.md", "production_notes.md", "production-notes.md", "shotlist.md")

KEYWORDS = [  # (kind, regex) — first match wins
    ("lower_third", r"lower[\s-]?third|\blt:|\bname\s*:|\bsuper\b|name tag"),
    ("end_card", r"end[\s-]?card|outro|credits|call to action|\bcta\b|follow for"),
    ("chapter", r"chapter|section|part \d|\bact\b"),
    ("location", r"location|venue|place:|city:|\bdate:"),
    ("title", r"\btitle|\btitel|headline|opening text|intro text|\bsupertitle"),
    ("hook", r"\bhook\b|cold open|open with|start with"),
    ("cut", r"\bcut\b|remove|delete|drop this|skip this|lose this|bad take|don'?t use"),
    ("keep", r"\bkeep\b|must stay|don'?t cut|leave this"),
    ("slowmo", r"slow[\s-]?mo|slow motion|slowmotion|\bslow\b|\b50%|half speed"),
    ("ramp", r"ramp|speed[\s-]?up|speed ramp|fast forward|timelapse|time[\s-]?lapse"),
    ("zoom", r"zoom|punch[\s-]?in|push[\s-]?in|crop in"),
    ("shake", r"shake|impact|hit\b|boom"),
    ("flash", r"flash|white out|strobe"),
    ("glitch", r"glitch|rgb|chromatic"),
    ("look", r"\blook\b|grade|grading|\blut\b|color|colour|teal|orange|film look|b&w|black and white|moody"),
    ("cinematic", r"cinematic|letterbox|bars|anamorphic|grain|halation|vignette|light leak"),
    ("mask", r"mask|spotlight|blur (the )?background|focus on|highlight box|circle"),
    ("broll", r"b-?roll|insert shot|cutaway|placeholder|need footage|drone|wide shot"),
    ("music", r"music|song|\bbeat\b|\bdrop\b|sync to"),
    ("caption", r"caption|subtitle|font|text style"),
]
COLOR_HINTS = {"Red": "cut", "Green": "keep", "Blue": "title", "Purple": "cinematic", "Yellow": "hook",
               "Cyan": "zoom", "Pink": "look", "Fuchsia": "mask", "Lavender": "broll"}


def classify(text: str, color: Optional[str] = None) -> str:
    t = (text or "").lower()
    for kind, rx in KEYWORDS:
        if re.search(rx, t):
            return kind
    if color and color in COLOR_HINTS:
        return COLOR_HINTS[color]
    return "note"


def parse_time(s: str) -> float:
    parts = [float(p) for p in s.split(":")]
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def _marker_text(info: dict) -> str:
    name = (info.get("name") or "").strip()
    note = (info.get("note") or "").strip()
    if re.match(r"^marker \d+$", name, re.IGNORECASE):
        name = ""
    return f"{name}: {note}" if name and note else (name or note)


def _add(notes: List[dict], **kw) -> None:
    text = (kw.get("text") or "").strip()
    if not text:
        return
    kw["text"] = text
    kw["kind"] = kw.get("kind") or classify(text, kw.get("color"))
    kw["id"] = f"N{len(notes) + 1}"
    kw.setdefault("status", "open")
    notes.append(kw)


def read_notes_file(path: Path) -> List[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue  # blank lines and markdown headings
        m = TIME_PREFIX.match(line)
        if m and m.group("text"):
            out.append({"t": parse_time(m.group("a")), "t_end": parse_time(m.group("b")) if m.group("b") else None,
                        "text": m.group("text").strip()})
        else:
            out.append({"t": None, "t_end": None, "text": re.sub(r"^\s*[-*•]\s*", "", line).lstrip("# ").strip()})
    return [o for o in out if o["text"]]


def find_sidecar(media: Path) -> Optional[Path]:
    for pat in SIDECARS:
        p = media.parent / pat.format(stem=media.stem)
        if p.exists() and p.is_file() and p.stat().st_size < 512_000:
            return p
    return None


def text_of_item(item: Any) -> Optional[str]:
    """Visible text of a Text+ title (None for anything else)."""
    try:
        if item.GetFusionCompCount() < 1:
            return None
        comp = item.GetFusionCompByIndex(1)
        tools = comp.GetToolList(False, "TextPlus") or {}
        for tool in tools.values():
            txt = tool.GetInput("StyledText")
            if txt:
                return str(txt)
    except Exception:
        return None
    return None


def placeholder_reason(item: Any) -> Optional[str]:
    name = ""
    try:
        name = item.GetName() or ""
    except Exception:
        pass
    if PLACEHOLDER_NAME.match(name):
        return name
    txt = text_of_item(item)
    if txt and (PLACEHOLDER_TEXT.match(txt) or "todo" in txt.lower() or txt.lower().startswith("flow:")):
        return txt
    return None


# ---------------------------------------------------------------- reel jobs (one clip)
def collect_for_job(job: Path, notes_file: Optional[str] = None, connect: bool = True) -> dict:
    clip = read_json(job / "clip.json")
    media = Path(clip["path"])
    fps = float(clip.get("fps") or 30.0)
    rinfo = clip.get("resolve") or {}
    notes: List[dict] = []
    warnings: List[str] = []
    rng = clip.get("range")

    if connect:
        try:
            from flow import resolve_bridge as rb
            from flow.find_clip import find_media_pool_item
            resolve = rb.connect()
            project, mp, _ = rb.current(resolve)
            mpi = find_media_pool_item(mp, str(media), rinfo.get("media_pool_id"))
            if mpi is not None:
                for frame, info in sorted((mpi.GetMarkers() or {}).items()):
                    t = float(frame) / fps
                    if rng and not (rng[0] - 0.01 <= t <= rng[1] + 0.01):
                        continue
                    _add(notes, source="clip marker", color=info.get("color"), text=_marker_text(info),
                         src_t=round(t, 3), src_t_end=round(t + max(0, int(info.get("duration", 1)) - 1) / fps, 3))
                meta = {}
                try:
                    meta = mpi.GetMetadata() or {}
                except Exception:
                    meta = {}
                for key in ("Description", "Comments", "Keywords", "Shot", "Scene"):
                    val = (meta.get(key) or "").strip() if isinstance(meta, dict) else ""
                    if val:
                        _add(notes, source=f"clip metadata · {key}", text=val, src_t=None)
            tl_name = rinfo.get("timeline")
            if tl_name:
                tl = _timeline_by_name(project, tl_name)
                if tl is not None:
                    _collect_timeline_notes_for_clip(tl, mpi, notes, fps)
        except FlowError as exc:
            warnings.append(f"Resolve not reachable, read only files: {exc}")
        except Exception as exc:
            warnings.append(f"Couldn't read markers from Resolve: {exc}")

    nf = Path(notes_file).expanduser() if notes_file else find_sidecar(media)
    if nf and nf.exists():
        for line in read_notes_file(nf):
            _add(notes, source=f"notes file · {nf.name}", text=line["text"], src_t=line["t"], src_t_end=line["t_end"])

    data = {"scope": "clip", "clip": clip["path"], "notes": notes, "warnings": warnings,
            "notes_file": str(nf) if nf else None}
    write_json(job / "notes.json", data)
    _append_review(job, data)
    return data


def _timeline_by_name(project: Any, name: str) -> Any:
    for i in range(1, (project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl and tl.GetName() == name:
            return tl
    return None


def _collect_timeline_notes_for_clip(tl: Any, mpi: Any, notes: List[dict], fps: float) -> None:
    """Timeline markers over the clip, item markers, and placeholders stacked above it."""
    start = tl.GetStartFrame()
    target_id = mpi.GetUniqueId() if mpi else None
    spans = []
    for it in tl.GetItemListInTrack("video", 1) or []:
        m = it.GetMediaPoolItem()
        if m is not None and target_id and m.GetUniqueId() == target_id:
            spans.append(it)
    if not spans:
        return

    def rec_to_src(frame_abs: int) -> Optional[float]:
        for it in spans:
            if it.GetStart() <= frame_abs < it.GetEnd():
                return round((it.GetSourceStartFrame() + (frame_abs - it.GetStart())) / fps, 3)
        return None

    for frame, info in sorted((tl.GetMarkers() or {}).items()):
        t = rec_to_src(start + int(frame))
        if t is not None:
            _add(notes, source="timeline marker", color=info.get("color"), text=_marker_text(info), src_t=t)
    for it in spans:
        for frame, info in sorted((it.GetMarkers() or {}).items()):
            t = round((it.GetSourceStartFrame() + int(frame)) / fps, 3)
            _add(notes, source="clip marker (timeline)", color=info.get("color"), text=_marker_text(info), src_t=t)
    for k in range(2, (tl.GetTrackCount("video") or 1) + 1):
        for it in tl.GetItemListInTrack("video", k) or []:
            why = placeholder_reason(it)
            if not why:
                continue
            t = rec_to_src(it.GetStart())
            _add(notes, source=f"placeholder V{k}", text=why, src_t=t,
                 src_t_end=rec_to_src(it.GetEnd() - 1), placeholder=True,
                 duration=round(it.GetDuration() / fps, 3))


# ---------------------------------------------------------------- whole timelines (Finish mode)
def collect_timeline(notes_file: Optional[str] = None, timeline: Any = None) -> dict:
    from flow import resolve_bridge as rb
    resolve = rb.connect()
    project, mp, cur = rb.current(resolve)
    tl = timeline or cur
    if tl is None:
        raise FlowError("No timeline is open in Resolve.")
    fps = float(tl.GetSetting("timelineFrameRate") or 30)
    start = tl.GetStartFrame()
    notes: List[dict] = []
    for frame, info in sorted((tl.GetMarkers() or {}).items()):
        _add(notes, source="timeline marker", color=info.get("color"), text=_marker_text(info),
             tl_frame=int(frame), tl_t=round(int(frame) / fps, 3),
             duration=round(max(1, int(info.get("duration", 1))) / fps, 3))
    for kind in ("video",):
        for k in range(1, (tl.GetTrackCount(kind) or 0) + 1):
            from flow.finish import clips_on_track
            for idx, it in enumerate(clips_on_track(tl, k)):
                ref = f"V{k}#{idx + 1}"
                for frame, info in sorted((it.GetMarkers() or {}).items()):
                    rel = it.GetStart() - start + int(frame)
                    _add(notes, source=f"clip marker {ref}", color=info.get("color"), text=_marker_text(info),
                         tl_frame=rel, tl_t=round(rel / fps, 3), item=ref)
                why = placeholder_reason(it)
                if why:
                    rel = it.GetStart() - start
                    _add(notes, source=f"placeholder {ref}", text=why, tl_frame=rel, tl_t=round(rel / fps, 3),
                         duration=round(it.GetDuration() / fps, 3), item=ref, placeholder=True)
    nf = Path(notes_file).expanduser() if notes_file else None
    if nf and nf.exists():
        for line in read_notes_file(nf):
            fr = int(round(line["t"] * fps)) if line["t"] is not None else None
            _add(notes, source=f"notes file · {nf.name}", text=line["text"], tl_frame=fr, tl_t=line["t"],
                 duration=(line["t_end"] - line["t"]) if line["t_end"] is not None and line["t"] is not None else None)
    return {"scope": "timeline", "timeline": tl.GetName(), "fps": fps, "notes": notes,
            "notes_file": str(nf) if nf else None, "warnings": []}


# ---------------------------------------------------------------- output
def format_notes(data: dict) -> str:
    notes = data.get("notes", [])
    if not notes:
        out = "No production notes found (markers, placeholders, metadata or notes file)."
    else:
        lines = []
        for n in notes:
            if n.get("src_t") is not None:
                where = f"src {fmt_time(n['src_t'])}"
            elif n.get("tl_t") is not None:
                where = f"at {fmt_time(n['tl_t'])}"
            else:
                where = "global"
            col = f" [{n['color']}]" if n.get("color") else ""
            lines.append(f"{n['id']:<4} {where:<12} {n['kind']:<11} {n['source']}{col} · {n['text']}")
        out = "\n".join(lines)
    for w in data.get("warnings", []):
        out += f"\nwarning: {w}"
    return out


def _append_review(job: Path, data: dict) -> None:
    review = job / "review.txt"
    if not review.exists():
        return
    text = review.read_text(encoding="utf-8")
    marker = "\nPRODUCTION NOTES"
    if marker in text:
        text = text[: text.index(marker)]
    section = [
        "",
        "PRODUCTION NOTES — handle each one; record it in the plan's \"notes\" list as done / skipped / needs_user",
        format_notes(data),
    ]
    review.write_text(text.rstrip("\n") + "\n" + "\n".join(section) + "\n", encoding="utf-8")
