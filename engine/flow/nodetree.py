"""Node-tree grades: every graded clip gets a labelled colorist node tree instead of one LUT.

    01 Input     the clip's own conversion / fix LUT (camera log, highlight fix), if it had one
    02 Balance   white balance: per-channel offset from Flow's analysis
    03 Exposure  exposure + contrast + saturation (slope / power / sat)
    04 Look      the creative look LUT (Flow look, per-clip nightlife, or the user's LUT)
    05 Skin      empty, ready for a skin qualifier (the API can't key, so it stays for the colorist)
    06 Vignette  inverted soft circular window; strength = how much the edges are pulled down

Resolve's API cannot add nodes, so the tree comes from a template (assets/flow_node_tree.drx, built
once on the Color page) applied with ApplyGradeFromDRX; the nodes are then filled through SetLUT /
SetCDL. Clips that already carry a real multi-node grade are left alone unless replace=True.

Gallery: Flow saves one reference still per graded clip into an album “Flow · <timeline>” (and can
export them as jpgs for review), and the template itself is offered as a PowerGrade “Flow node tree”.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TEMPLATE = Path(__file__).parent / "assets" / "flow_node_tree.drx"
NODES = {"input": 1, "balance": 2, "exposure": 3, "look": 4, "skin": 5, "vignette": 6}
LABELS = ["Input", "Balance", "Exposure", "Look", "Skin", "Vignette"]
COLORS = {"look": "Orange", "skin": "Apricot", "vignette": "Navy"}


def available() -> bool:
    return TEMPLATE.exists()


def _fmt(v) -> str:
    return " ".join(f"{float(x):.4f}" for x in v)


def split_cdl(cdl: Optional[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """Flow's auto-balance CDL -> (Balance node: per-channel offset, Exposure node: slope/power/sat)."""
    if not cdl:
        return None, None
    bal = {"Slope": "1 1 1", "Offset": _fmt(cdl.get("offset", [0, 0, 0])), "Power": "1 1 1", "Saturation": 1.0}
    exp = {"Slope": _fmt(cdl.get("slope", [1, 1, 1])), "Offset": "0 0 0", "Power": _fmt(cdl.get("power", [1, 1, 1])),
           "Saturation": float(cdl.get("saturation", 1.0))}
    return bal, exp


def existing_grade(item: Any) -> Dict[str, Any]:
    """What the clip carries now: node count, node-1 LUT, and whether it is a real grade."""
    g = item.GetNodeGraph()
    if g is None:
        return {"graph": False}
    n = int(g.GetNumNodes() or 0)
    lut = (g.GetLUT(1) or "") if n else ""
    tools = []
    for i in range(1, n + 1):
        try:
            tools += [t for t in (g.GetToolsInNode(i) or []) if not t.startswith("LUT:")]
        except Exception:
            pass
    labels = [g.GetNodeLabel(i) or "" for i in range(1, n + 1)]
    from flow.masks import TREE as MASK_TREE
    return {"graph": True, "nodes": n, "lut1": lut, "tools": tools, "labels": labels,
            "flow_tree": labels[:len(LABELS)] == LABELS or labels[:len(MASK_TREE)] == MASK_TREE}


def can_apply(item: Any, replace: bool = False) -> Tuple[bool, str]:
    st = existing_grade(item)
    if not st.get("graph"):
        return False, "no color graph"
    if replace or st.get("flow_tree"):
        return True, ""
    if st["nodes"] > 1 or st["tools"]:
        return False, f"already graded ({st['nodes']} nodes: {', '.join(st['tools'][:3]) or 'LUTs'})"
    return True, ""


def apply_tree(item: Any, input_lut: Optional[str] = None, balance: Optional[dict] = None,
               exposure: Optional[dict] = None, look_lut: Optional[str] = None, vignette: float = 0.0,
               keep_input: bool = True) -> Dict[str, Any]:
    """Put the Flow tree on one clip and fill it. Returns {ok, done:[...], failed:[...]}."""
    res: Dict[str, Any] = {"ok": False, "done": [], "failed": []}
    if not available():
        res["failed"].append("template missing")
        return res
    before = existing_grade(item)
    if keep_input and not input_lut and before.get("lut1") and not before.get("flow_tree"):
        input_lut = before["lut1"]          # keep the clip's own conversion / fix LUT, now on node 01
    g = item.GetNodeGraph()
    if g is None or not g.ApplyGradeFromDRX(str(TEMPLATE), 0):
        res["failed"].append("ApplyGradeFromDRX")
        return res
    time.sleep(0.05)
    g = item.GetNodeGraph()                 # the old handle goes stale after a DRX apply
    if int(g.GetNumNodes() or 0) != len(LABELS):
        res["failed"].append(f"template gave {g.GetNumNodes()} nodes")
        return res
    for i, lab in enumerate(LABELS, 1):     # labels travel with the DRX, but be sure
        if (g.GetNodeLabel(i) or "") != lab:
            g.SetNodeLabel(i, lab)
    for key, col in COLORS.items():
        try:
            g.SetNodeColor(NODES[key], col)
        except Exception:
            pass

    def lut(node: str, path: Optional[str]) -> None:
        if path:
            (res["done"] if g.SetLUT(NODES[node], str(path)) else res["failed"]).append(f"{node}:lut")

    def cdl(node: str, vals: Optional[dict]) -> None:
        if vals:
            ok = item.SetCDL({"NodeIndex": str(NODES[node]), **{k: str(v) for k, v in vals.items()}})
            (res["done"] if ok else res["failed"]).append(f"{node}:cdl")

    lut("input", input_lut)
    cdl("balance", balance)
    cdl("exposure", exposure)
    lut("look", look_lut)
    v = max(0.0, min(0.8, float(vignette or 0.0)))
    if v > 0:
        s = f"{1.0 - v:.4f}"
        cdl("vignette", {"Slope": f"{s} {s} {s}", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": 1.0})
    res["ok"] = not res["failed"]
    return res


# ---------------------------------------------------------------- what the Color page shows
class Solo:
    """The Color page always works on the TOP clip under the playhead (captions, titles and B-roll
    included) and the API cannot select a clip. Solo(tl).upto(k) turns every video track above k off,
    so the clip on track k is the one shown, graded in view and grabbed as a still. restore() puts
    every track back exactly as it was. Use as a context manager."""

    def __init__(self, tl: Any):
        self.tl = tl
        n = int(tl.GetTrackCount("video") or 0)
        self.orig = {k: bool(tl.GetIsTrackEnabled("video", k)) for k in range(1, n + 1)}
        self.cur = dict(self.orig)

    def upto(self, track: int) -> None:
        for k, was in self.orig.items():
            want = was if k <= track else False
            if self.cur.get(k) != want:
                try:
                    self.tl.SetTrackEnable("video", k, want)
                    self.cur[k] = want
                except Exception:
                    pass

    def restore(self) -> None:
        for k, was in self.orig.items():
            if self.cur.get(k) != was:
                try:
                    self.tl.SetTrackEnable("video", k, was)
                except Exception:
                    pass
        self.cur = dict(self.orig)

    def __enter__(self) -> "Solo":
        return self

    def __exit__(self, *exc) -> None:
        self.restore()


def track_of(item: Any, default: int = 1) -> int:
    try:
        t = item.GetTrackTypeAndIndex()
        return int(t[1])
    except Exception:
        return default


# ---------------------------------------------------------------- gallery
def _album(project: Any, name: str, powergrade: bool = False) -> Any:
    gal = project.GetGallery()
    albums = gal.GetGalleryPowerGradeAlbums() if powergrade else gal.GetGalleryStillAlbums()
    for a in albums or []:
        if gal.GetAlbumName(a) == name:
            return a
    a = gal.CreateGalleryPowerGradeAlbum() if powergrade else gal.CreateGalleryStillAlbum()
    if a is not None:
        gal.SetAlbumName(a, name)
    return a


def offer_powergrade(project: Any) -> bool:
    """Put the Flow tree into a PowerGrade album “Flow” once, so it can be dragged onto any clip."""
    try:
        a = _album(project, "Flow", powergrade=True)
        if a is None:
            return False
        if any((a.GetLabel(s) or "") == "Flow node tree" for s in a.GetStills() or []):
            return True
        before = set(id(s) for s in a.GetStills() or [])
        if not a.ImportStills([str(TEMPLATE)]):
            return False
        for s in a.GetStills() or []:
            if id(s) not in before or not a.GetLabel(s):
                a.SetLabel(s, "Flow node tree")
        return True
    except Exception:
        return False


def grab_stills(resolve: Any, project: Any, tl: Any, shots: List[tuple], fps: float,
                album_name: str, out_dir: Optional[Path] = None, limit: int = 16) -> Dict[str, Any]:
    """One reference still per shot (record frame, label[, track]) into a Gallery album; optional jpg
    export. Tracks above the shot's track are muted while grabbing, so the still is the graded clip and
    not a caption over it. Needs the Color page; returns {album, stills, files}."""
    from flow.util import frames_to_tc
    out: Dict[str, Any] = {"album": album_name, "stills": 0, "files": []}
    try:
        gal = project.GetGallery()
        alb = _album(project, album_name)
        if alb is None:
            return out
        gal.SetCurrentStillAlbum(alb)
        start = tl.GetStartFrame()
        page = resolve.GetCurrentPage()
        if page != "color":
            resolve.OpenPage("color")
            time.sleep(0.8)
        step = max(1, -(-len(shots) // limit))
        grabbed = []
        solo = Solo(tl)
        try:
            for shot in shots[::step][:limit]:
                f, label = shot[0], shot[1]
                solo.upto(int(shot[2]) if len(shot) > 2 else 1)
                tl.SetCurrentTimecode(frames_to_tc(start + int(f), fps))
                time.sleep(0.25)
                st = tl.GrabStill()
                if st is not None:
                    try:
                        alb.SetLabel(st, label[:60])
                    except Exception:
                        pass
                    grabbed.append(st)
        finally:
            solo.restore()
        out["stills"] = len(grabbed)
        if out_dir and grabbed:
            out_dir.mkdir(parents=True, exist_ok=True)
            for old_f in out_dir.glob("still*"):   # one set per build
                old_f.unlink()
            if alb.ExportStills(grabbed, str(out_dir), "still", "jpg"):
                for extra in out_dir.glob("still*.drx"):
                    extra.unlink()
                out["files"] = sorted(str(p) for p in out_dir.glob("still*.jpg"))
        if page and page != "color":
            resolve.OpenPage(page)
    except Exception as exc:
        out["error"] = str(exc)
    return out
