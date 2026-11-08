"""Flow Build — DaVinci Resolve menu script (Workspace > Scripts > Edit > Flow Build).

Builds the latest compiled Flow job (~/.flow/jobs/latest/build.json) from inside Resolve.
Use it on the free edition (no external scripting) or whenever Claude can't reach Resolve.
"""
import os
import sys
import traceback

home = os.path.expanduser("~")
engine = None
try:
    with open(os.path.join(home, ".flow", "engine_path")) as f:
        engine = f.read().strip()
except Exception:
    engine = os.path.join(home, "Desktop", "Flow", "engine")
if engine not in sys.path:
    sys.path.insert(0, engine)

try:
    from flow import paths, resolve_bridge
    from flow import build

    _r = globals().get("resolve")
    if _r is None:
        try:
            _r = app.GetResolve()  # noqa: F821  (provided by Resolve's script host)
        except Exception:
            import DaVinciResolveScript as _bmd
            _r = _bmd.scriptapp("Resolve")
    resolve_bridge.use(_r)
    report = build.build_job(paths.resolve_job("latest"))
    print(build.format_report(report))
except Exception:
    print("Flow Build failed:")
    traceback.print_exc()
