"""`flow doctor`: is everything Flow needs in place? Each failed check comes with its fix."""
from __future__ import annotations

import os
from typing import List

from flow import paths, resolve_bridge as rb
from flow.transcribe import whisper_binary
from flow.util import FlowError, which


def _check(rows: List[dict], name: str, ok: bool, detail: str = "", fix: str = "", required: bool = True):
    rows.append({"check": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix,
                 "required": required})


def run_checks(connect_resolve: bool = True) -> dict:
    rows: List[dict] = []
    _check(rows, "ffmpeg", bool(which("ffmpeg") and which("ffprobe")), which("ffmpeg") or "",
           "brew install ffmpeg")
    wb = whisper_binary()
    _check(rows, "whisper.cpp", bool(wb), wb or "", "brew install whisper-cpp")
    mp = paths.model_path()
    _check(rows, "whisper model", mp.exists(),
           f"{mp.name} ({mp.stat().st_size // 1_000_000} MB)" if mp.exists() else str(mp),
           f"./install.sh (downloads {paths.DEFAULT_MODEL})")
    from flow import drx
    _check(rows, "zstd (mask grades)", drx.zstd_available(), which("zstd") or ("python zstandard" if drx.zstd_available() else ""),
           "brew install zstd", required=False)
    app = rb.find_resolve_app()
    _check(rows, "DaVinci Resolve installed", app is not None, str(app or ""), "Install DaVinci Resolve Studio")
    lut_ok = paths.RESOLVE_LUT_DIR.exists() and os.access(paths.RESOLVE_LUT_DIR, os.W_OK)
    _check(rows, "Resolve LUT folder writable", lut_ok, str(paths.RESOLVE_LUT_DIR),
           f"sudo chmod a+w '{paths.RESOLVE_LUT_DIR}'")
    looks = list(paths.FLOW_LUT_DIR.glob("Flow *.cube")) if paths.FLOW_LUT_DIR.exists() else []
    _check(rows, "Flow looks installed", bool(looks), f"{len(looks)} looks", "flow luts --install",
           required=False)
    menu = paths.RESOLVE_USER_SCRIPTS / "Edit" / "Flow Build.py"
    _check(rows, "Resolve menu script", menu.exists(), str(menu), "./install.sh", required=False)
    skill = os.path.expanduser("~/.claude/skills/flow/SKILL.md")
    _check(rows, "/flow skill installed", os.path.exists(skill), skill, "./install.sh", required=False)
    caps = paths.CAPABILITIES_FILE
    _check(rows, "Resolve API probe", caps.exists(), str(caps), "flow probe --switch", required=False)

    info = {}
    if connect_resolve and app is not None:
        running = rb.resolve_running()
        _check(rows, "Resolve running", running, "", "Open DaVinci Resolve")
        if running:
            try:
                r = rb.connect(retries=1)
                studio = rb.is_studio(r)
                info = {"product": r.GetProductName(), "version": rb.version_string(r)}
                _check(rows, "External scripting", True, f"{info['product']} {info['version']}")
                _check(rows, "Studio edition", studio, info["product"],
                       "Smart Reframe, Speed Warp and some effects need DaVinci Resolve Studio", required=False)
                try:
                    project, _, tl = rb.current(r)
                    info["project"] = project.GetName()
                    info["timeline"] = tl.GetName() if tl else None
                    _check(rows, "Project open", True, f"{info['project']} · timeline {info['timeline']}")
                except FlowError as exc:
                    _check(rows, "Project open", False, str(exc), "Open a project in Resolve")
            except FlowError as exc:
                _check(rows, "External scripting", False, str(exc).splitlines()[0],
                       "Resolve > Preferences > System > General > External scripting using: Local")
    ok = all(r["ok"] for r in rows if r["required"])
    return {"ok": ok, "checks": rows, "resolve": info}


def format_report(rep: dict) -> str:
    lines = ["Flow is ready." if rep["ok"] else "Flow needs a few things:"]
    for r in rep["checks"]:
        mark = "✓" if r["ok"] else ("✗" if r["required"] else "·")
        line = f"  {mark} {r['check']}"
        if r["detail"]:
            line += f" — {r['detail']}"
        if r["fix"]:
            line += f"\n      fix: {r['fix']}"
        lines.append(line)
    return "\n".join(lines)
