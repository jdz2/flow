"""`flow status`: where a job stands, and `flow jobs`: the recent jobs. Offline, no Resolve.

Each job moves through find → transcribe → analyze → plan → compile → build (→ render). status
says which steps are done, what the last build produced, and the one command to run next.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional

from flow import paths
from flow.util import FlowError, fmt_time, read_json

STEPS = [  # (step, file it leaves behind, command that makes it)
    ("find", "clip.json", "flow find"),
    ("transcribe", "words.json", "flow transcribe"),
    ("analyze", "analysis.json", "flow analyze"),
    ("plan", "edit_plan.json", "write edit_plan.json (start from edit_plan.draft.json)"),
    ("compile", "build.json", "flow compile"),
    ("build", "report.json", "flow build"),
    ("render", "render.json", "flow render"),
]


def _load(p: Path) -> Optional[dict]:
    try:
        return read_json(p)
    except (OSError, FlowError):
        return None


def job_status(job: Path) -> dict:
    done, stale = [], []
    prev_mtime = 0.0
    for step, fname, _ in STEPS:
        f = job / fname
        if f.exists():
            done.append(step)
            m = f.stat().st_mtime
            if m + 1 < prev_mtime and step in ("compile", "build", "render"):
                stale.append(step)  # an earlier step changed after this one ran
            prev_mtime = max(prev_mtime, m)
    nxt = next(((s, cmd) for s, f, cmd in STEPS if not (job / f).exists()), None)
    if stale:
        s = stale[0]
        nxt = (s, dict((st, cmd) for st, _, cmd in STEPS)[s])
    clip = _load(job / "clip.json") or {}
    out: dict = {"job": job.name, "path": str(job), "clip": clip.get("path"),
                 "duration": clip.get("duration"), "done": done, "stale": stale,
                 "next": nxt[1] if nxt and nxt[0] != "render" else None,
                 "updated": max((f.stat().st_mtime for f in job.glob("*.json")), default=0)}
    b = _load(job / "build.json")
    if b:
        s = b.get("stats", {})
        out["edit"] = {"style": (b.get("style") or {}).get("name") if isinstance(b.get("style"), dict) else b.get("style"),
                       "before": s.get("source_duration"), "after": s.get("output_duration"),
                       "cuts": s.get("jump_cuts"), "captions": s.get("captions"),
                       "zooms": s.get("zoom_changes"), "effects": s.get("effects"),
                       "notes_open": s.get("notes_open"), "warnings": len(b.get("warnings") or [])}
    r = _load(job / "report.json")
    if r:
        out["build"] = {"timeline": r.get("timeline"), "ok": r.get("ok"), "failed": r.get("failed") or {},
                        "preview": r.get("preview") or []}
    live = _load(job / "live_edits.json")  # {timeline name: {"edits": [...], ...}}
    if isinstance(live, dict) and r and isinstance(live.get(r.get("timeline")), dict):
        edits = live[r["timeline"]].get("edits") or []
        out["live_edits"] = sum(1 for e in edits if isinstance(e, dict) and not e.get("undone"))
    rn = _load(job / "render.json")
    if rn:
        out["render"] = {"file": rn.get("file"), "status": rn.get("status")}
    return out


def format_status(st: dict) -> str:
    lines = [f"job {st['job']}" + (f" · {Path(st['clip']).name}" if st.get("clip") else "")
             + (f" · {fmt_time(st['duration'])}" if st.get("duration") else "")]
    marks = []
    for step, _, _ in STEPS:
        m = "✓" if step in st["done"] and step not in st["stale"] else ("!" if step in st["stale"] else "·")
        marks.append(f"{m} {step}")
    lines.append("  ".join(marks))
    e = st.get("edit")
    if e and e.get("before") is not None:
        lines.append(f"edit: {e.get('style') or '?'} · {fmt_time(e['before'])} → {fmt_time(e['after'] or 0)}"
                     f" · {e.get('cuts', 0)} cuts · {e.get('captions', 0)} captions · {e.get('zooms', 0)} zooms"
                     f" · {e.get('effects', 0)} effects"
                     + (f" · {e['notes_open']} notes need you" if e.get("notes_open") else "")
                     + (f" · {e['warnings']} warnings" if e.get("warnings") else ""))
    bd = st.get("build")
    if bd:
        lines.append(f"timeline: “{bd['timeline']}”" + ("" if bd.get("ok") else " (finished with problems)")
                     + (" · failed " + ", ".join(f"{k}×{v}" for k, v in bd["failed"].items()) if bd["failed"] else ""))
    if st.get("live_edits"):
        lines.append(f"live edits on it: {st['live_edits']} (fold them into the plan before a rebuild)")
    if st.get("render"):
        lines.append(f"render: {st['render']['status']} · {st['render']['file']}")
    if st["stale"]:
        lines.append(f"out of date: {', '.join(st['stale'])} (an earlier step changed since)")
    if st.get("next"):
        lines.append(f"next: {st['next']}")
    return "\n".join(lines)


def list_jobs(limit: int = 10) -> List[dict]:
    if not paths.JOBS_DIR.exists():
        return []
    latest = paths.LATEST_LINK.resolve() if paths.LATEST_LINK.exists() else None
    rows = []
    for d in paths.JOBS_DIR.iterdir():
        if d.is_symlink() or not d.is_dir() or not (d / "clip.json").exists():
            continue
        st = job_status(d)
        st["latest"] = d.resolve() == latest
        rows.append(st)
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows[:limit]


def format_jobs(rows: List[dict]) -> str:
    if not rows:
        return "no Flow jobs yet (flow find)"
    out = []
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["updated"])) if r["updated"] else ""
        last = r["done"][-1] if r["done"] else "-"
        tl = (r.get("build") or {}).get("timeline") or ""
        out.append(f"{'*' if r.get('latest') else ' '} {r['job']:<32} {when}  {last:<10} {tl}")
    return "\n".join(out)
