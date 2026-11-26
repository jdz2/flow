"""Mask lab: learn what the window params inside a .drx do, by applying edited copies of the Flow node
template to a grey card in Resolve and measuring the exported frame.

    python3 tests/drx_lab.py <out_dir> [variant ...]

Needs Resolve running with a project open. Works on its own timeline "Flow Mask Lab" (made once, with a
mid-grey card), never on the user's timelines. Each variant edits the Vignette window, sets the node's
CDL to slope 0 (pure black where the window applies), exports the frame and prints where the black is.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from flow import drx, nodetree, resolve_bridge as rb  # noqa: E402

W, H = 3840, 2160
LAB = "Flow Mask Lab"
P_A, P_B = 139460613, 139460614                       # float params (template: 480, 3.4)
P_I1, P_I2, P_I3, P_M = 2286944264, 2286944265, 2286944266, 2286944272

VARIANTS = {
    "base": {},
    "a240": {P_A: 240.0},
    "a960": {P_A: 960.0},
    "b0": {P_B: 0.0},
    "b20": {P_B: 20.0},
    "i1_0": {P_I1: 0},
    "i2_0": {P_I2: 0},
    "i3_1": {P_I3: 1},
    "mtx_tx": {P_M: [1, 0, 0.25, 0, 1, 0, 0, 0, 1]},
    "mtx_tx2": {P_M: [1, 0, 0, 0, 1, 0, 0.25, 0, 1]},
    "mtx_sx": {P_M: [2, 0, 0, 0, 1, 0, 0, 0, 1]},
    "mtx_ty": {P_M: [1, 0, 0, 0, 1, 0.25, 0, 0, 1]},
    # hard edge, not inverted: the black disc is exactly the window
    "hard": {P_A: 1.0, P_I2: 0},
    "hard_b1": {P_A: 1.0, P_I2: 0, P_B: 1.0},
    "hard_b2": {P_A: 1.0, P_I2: 0, P_B: 2.0},
    "hard_b6": {P_A: 1.0, P_I2: 0, P_B: 6.0},
    "hard_tx500": {P_A: 1.0, P_I2: 0, P_M: [1, 0, 500, 0, 1, 0, 0, 0, 1]},
    "hard_ty300": {P_A: 1.0, P_I2: 0, P_M: [1, 0, 0, 0, 1, 300, 0, 0, 1]},
    "hard_txy05": {P_A: 1.0, P_I2: 0, P_M: [1, 0, 0.5, 0, 1, 0.5, 0, 0, 1]},
    "hard_sx3": {P_A: 1.0, P_I2: 0, P_M: [3, 0, 0, 0, 1, 0, 0, 0, 1]},
    "hard_sy05": {P_A: 1.0, P_I2: 0, P_M: [1, 0, 0, 0, 0.5, 0, 0, 0, 1]},
    "soft100": {P_A: 100.0, P_I2: 0},
}


def card(path: Path) -> Path:
    if not path.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x808080:s={W}x{H}",
                        "-frames:v", "1", str(path)], check=True)
    return path


def lab_timeline(project, mp, img: Path):
    for i in range(1, project.GetTimelineCount() + 1):
        t = project.GetTimelineByIndex(i)
        if t.GetName() == LAB:
            return t
    root = mp.GetRootFolder()
    items = mp.ImportMedia([str(img)]) or []
    tl = mp.CreateEmptyTimeline(LAB)
    project.SetCurrentTimeline(tl)
    mp.AppendToTimeline([{"mediaPoolItem": items[0], "startFrame": 0, "endFrame": 47}])
    return tl


def measure(jpg: Path) -> dict:
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(jpg), "-vf", "scale=192:108,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(108, 192).astype(float) / 255
    ref = 0.5
    m = np.clip(1 - a / max(ref, 1e-3), 0, 1)       # 1 = fully corrected (black), 0 = untouched grey
    rows, cols = m.mean(axis=1), m.mean(axis=0)
    inside = m > 0.5
    ys, xs = np.nonzero(inside)
    box = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if len(xs) else None
    return {"mean": round(float(m.mean()), 3), "black_box_192x108": box,
            "row_profile": [round(float(v), 2) for v in rows[::12]],
            "col_profile": [round(float(v), 2) for v in cols[::16]],
            "center_val": round(float(m[54, 96]), 2)}


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    names = sys.argv[2:] or list(VARIANTS)
    resolve = rb.connect()
    project, mp, cur = rb.current(resolve)
    prev = cur
    tl = lab_timeline(project, mp, card(out / "grey_card.png"))
    project.SetCurrentTimeline(tl)
    item = tl.GetItemListInTrack("video", 1)[0]
    xml, grade = drx.read_grade(nodetree.TEMPLATE)
    results = {}
    for name in names:
        edits = VARIANTS[name]
        root = drx.grade_root(grade)
        if edits:
            root = drx.edit_window(root, "Vignette", lambda cur_: [(pid, edits.get(pid, v)) for pid, v in cur_])
        path = drx.write_grade(xml, drx.rebuild_grade(grade, root), out / f"{name}.drx")
        g = item.GetNodeGraph()
        ok = g.ApplyGradeFromDRX(str(path), 0)
        time.sleep(0.1)
        g = item.GetNodeGraph()
        cdl = item.SetCDL({"NodeIndex": "6", "Slope": "0 0 0", "Offset": "0 0 0", "Power": "1 1 1",
                           "Saturation": "1"})
        jpg = out / f"{name}.jpg"
        res = item.ExportFrame(10, {"outputType": "file", "filePath": str(jpg), "maxDimension": 960})
        r = {"applied": bool(ok), "nodes": g.GetNumNodes(), "cdl": bool(cdl),
             "exported": bool(res and res.get("success"))}
        if jpg.exists():
            r.update(measure(jpg))
        results[name] = r
        print(name, json.dumps(r))
    (out / "results.json").write_text(json.dumps(results, indent=1))
    if prev is not None:
        project.SetCurrentTimeline(prev)


if __name__ == "__main__":
    main()
