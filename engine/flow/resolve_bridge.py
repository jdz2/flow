"""Connect to a running DaVinci Resolve and small API helpers.

Two ways in:
  * external: `connect()` loads fusionscript.so (needs Resolve Studio with
    Preferences > System > General > External scripting = Local), or
  * internal: the in-Resolve menu script passes its own `resolve` object to
    `use(resolve)`. That route also works in the free edition.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from flow import paths
from flow.util import FlowError

_injected: Any = None


def use(resolve_obj: Any) -> None:
    """Register a Resolve object handed to us by an in-app script."""
    global _injected
    _injected = resolve_obj


def find_resolve_app() -> Path | None:
    for cand in paths.RESOLVE_APP_CANDIDATES:
        if cand.exists():
            return cand
    return None


def _fusionscript_lib() -> Path | None:
    env = os.environ.get("RESOLVE_SCRIPT_LIB")
    if env and Path(env).exists():
        return Path(env)
    app = find_resolve_app()
    if app:
        lib = app / "Contents" / "Libraries" / "Fusion" / "fusionscript.so"
        if lib.exists():
            return lib
    return None


def _load_module():
    lib = _fusionscript_lib()
    if lib is None:
        raise FlowError("DaVinci Resolve is not installed (fusionscript.so not found).")
    os.environ.setdefault("RESOLVE_SCRIPT_API", str(paths.RESOLVE_SCRIPT_API))
    os.environ["RESOLVE_SCRIPT_LIB"] = str(lib)
    modules = str(paths.RESOLVE_SCRIPT_API / "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as bmd  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on host
        raise FlowError(f"Could not load Resolve's scripting module: {exc}") from exc
    return bmd


def resolve_running() -> bool:
    import subprocess
    out = subprocess.run(["pgrep", "-f", "DaVinci Resolve.app/Contents/MacOS/Resolve"],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def connect(retries: int = 2) -> Any:
    """Return the Resolve scripting object or raise FlowError with a fix."""
    if _injected is not None:
        return _injected
    if not resolve_running():
        raise FlowError("DaVinci Resolve isn't running. Open Resolve and your project, then try again.")
    bmd = _load_module()
    resolve = None
    for attempt in range(retries + 1):
        resolve = bmd.scriptapp("Resolve")
        if resolve is not None:
            break
        time.sleep(1.0)
    if resolve is None:
        raise FlowError(
            "Resolve is running but refused the scripting connection.\n"
            "Fix: DaVinci Resolve > Preferences > System > General > External scripting using: Local\n"
            "(External scripting needs DaVinci Resolve Studio. On the free edition, run "
            "Workspace > Scripts > Flow Build inside Resolve instead.)"
        )
    return resolve


def fusion_app(resolve: Any) -> Any:
    try:
        return resolve.Fusion()
    except Exception:
        return None


def is_studio(resolve: Any) -> bool:
    try:
        return "studio" in (resolve.GetProductName() or "").lower()
    except Exception:
        return False


def version_string(resolve: Any) -> str:
    try:
        return resolve.GetVersionString()
    except Exception:
        return "unknown"


def current(resolve: Any):
    """(project, media_pool, timeline) — timeline may be None."""
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None:
        raise FlowError("No project is open in Resolve.")
    return project, project.GetMediaPool(), project.GetCurrentTimeline()


def const(resolve: Any, name: str, default: Any = None) -> Any:
    """Read a resolve.* enum constant, tolerating older builds."""
    try:
        val = getattr(resolve, name)
    except Exception:
        return default
    return default if val is None else val


def timeline_fps(timeline: Any) -> float:
    try:
        return float(timeline.GetSetting("timelineFrameRate"))
    except Exception:
        return 0.0
