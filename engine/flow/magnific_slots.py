"""Slots: the places in an edit where new Magnific visuals go, and building creations into them.

    flow magnific slots [--no-mark]          the open timeline's slots (S1, S2 …), marked so their ids stick
    flow magnific put FILE --slot S3 [--in SEC]
                                             build one creation into a slot now: an open slot gets it as a
                                             new clip, a filled one gets it as a new version (a take)

A slot is one of
  * a B-roll marker: Lavender, or a marker whose words read as B-roll ("drone over the harbour"), on the
    timeline or on a clip, over the marker's duration (2 s when it has none). Filled by a new clip on a
    Flow B-roll track above it.
  * a B-roll placeholder: a clip named "B-roll: …" or "Placeholder …", or Text+ placeholder text that reads
    as B-roll (the same notes Finish mode reads). Filled the same way; the placeholder is switched off,
    not deleted.
  * a Magnific clip that is already in the edit (Flow knows its creation): a new version goes onto it as a
    take, so it keeps its spot and length and the old version stays one click away.

Ids are per Resolve project and stick: Flow writes "flow:slot:S3" into the marker's hidden data (and puts a
small marker on a placeholder that has none); ~/.flow/magnific/slots/<project>.json keeps the rest.

Which slot a new creation belongs to (sure signals only; anything else stays in the media pool):
  1. `flow magnific pull --slot S3`, or a title or file name that starts with the slot id ("S3 · harbour")
  2. `flow magnific pull --version-of <creation>`: a new version of a creation that is in the edit

Nothing of the user's moves: no ripple, no clip covered (a fill only goes onto a Flow B-roll track with
nothing in between, or onto a new track right above the slot), the playhead and the media pool view stay,
and `flow magnific undo` takes a fill or a take back. Resolve keeps a take's own length and never grows a
clip back, so a version up to 10 % shorter than its slot comes in as a slowed copy that fits exactly;
anything shorter waits in the media pool.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flow import magnific as mg
from flow.notes import _marker_text, classify, placeholder_reason
from flow.util import FlowError, ffprobe, fmt_time, read_json, run, which, write_json

TAG = "flow:slot:"
TRACK_FILL = "Flow B-roll"
FLOW_BROLL = re.compile(r"^\s*flow\s+b-?roll", re.IGNORECASE)
SLOT_RE = re.compile(r"^\s*[\[(]?S(\d{1,3})[\])]?(?=$|[\s·:_\-.,|])", re.IGNORECASE)
DATE_PREFIX = re.compile(r"^\d{8}-\d{6}_")      # how `flow magnific pull` names files
MARKER_SECONDS = 2.0                             # a marker without a duration asks for this much
FIT_MIN = 0.9                                    # shortest version (of its slot) Flow slows down to fit
DONE_COLOR = "Green"
# words that open a B-roll ask whatever follows ("B-roll: drone, slow push in" is B-roll, not a slow-mo note)
BROLL_CUE = re.compile(r"^\W*(b-?\s?roll|insert shot|cutaway|needs? (footage|a shot|b-?roll))\b", re.IGNORECASE)


# ---------------------------------------------------------------- names → slot ids (pure)
def slot_in_name(title: Optional[str] = None, file: Optional[str] = None) -> Optional[str]:
    """The slot id a creation names at its start: "S3 · harbour", "[S3] harbour", "20261126-101500_S3-harbour.mov"."""
    cands = [str(title)] if title else []
    if file:
        cands.append(DATE_PREFIX.sub("", Path(str(file)).stem))
    for c in cands:
        m = SLOT_RE.match(c)
        if m:
            return f"S{int(m.group(1))}"
    return None


def norm_id(s: str) -> str:
    m = re.fullmatch(r"\s*S?(\d{1,3})\s*", str(s or ""), re.IGNORECASE)
    if not m:
        raise FlowError(f"{s!r} isn't a slot id; slots are S1, S2 … (`flow magnific slots` lists them)")
    return f"S{int(m.group(1))}"


def match(meta: dict, file: Optional[str], recs: Dict[str, dict]) -> Tuple[Optional[str], Optional[str]]:
    """(slot id, why) for a new creation, from sure signals only. recs: this project's slots."""
    if meta.get("slot"):
        return norm_id(meta["slot"]), "made for it"
    sid = slot_in_name(meta.get("title"), file)
    if sid:
        return sid, "named for it"
    parent = meta.get("version_of")
    if parent:
        for sid, r in recs.items():
            if r.get("status") != "gone" and (r.get("creation") == str(parent)
                                              or str(parent) in (r.get("versions") or [])):
                return sid, "a new version of what is there"
    return None, None


# ---------------------------------------------------------------- the project's slot file
def _file(project: str) -> Path:
    d = mg._home() / "slots"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{mg._slug(project, 80)}.json"


def load(project: str) -> dict:
    try:
        data = read_json(_file(project))
    except (OSError, FlowError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("slots"), dict):
        data = {"project": project, "next": 1, "slots": {}}
    data.setdefault("next", 1)
    return data


def save(project: str, data: dict) -> None:
    write_json(_file(project), data)


def _new_id(data: dict) -> str:
    n = int(data.get("next") or 1)
    while f"S{n}" in data["slots"]:
        n += 1
    data["next"] = n + 1
    return f"S{n}"


# ---------------------------------------------------------------- the timeline
def _uid(x: Any) -> Optional[str]:
    try:
        return x.GetUniqueId()
    except Exception:
        return None


def _num(v: Any) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _fps(tl: Any) -> float:
    return _num(tl.GetSetting("timelineFrameRate")) or 24.0


def _items(tl: Any, k: int) -> List[Any]:
    out = []
    for it in tl.GetItemListInTrack("video", k) or []:
        try:
            if it.GetType() == "transition":
                continue
        except Exception:
            pass
        out.append(it)
    return out


def _all_items(tl: Any) -> List[Tuple[int, Any]]:
    return [(k, it) for k in range(1, int(tl.GetTrackCount("video") or 0) + 1) for it in _items(tl, k)]


def _enabled(it: Any) -> bool:
    try:
        return bool(it.GetClipEnabled())
    except Exception:
        return True


def _overlaps(it: Any, start: int, f0: int, f1: int) -> bool:
    return it.GetStart() - start < f1 and it.GetEnd() - start > f0


def _occupied(tl: Any, k: int, start: int, f0: int, f1: int) -> bool:
    return any(_overlaps(it, start, f0, f1) for it in _items(tl, k))


def _shows(tl: Any, k: int, start: int, f0: int, f1: int) -> bool:
    """Something on track k is visible over [f0, f1): a new clip above it would cover it."""
    return any(_overlaps(it, start, f0, f1) and _enabled(it) for it in _items(tl, k))


def _find(tl: Any, uid: Optional[str]) -> Optional[Tuple[int, Any]]:
    if not uid:
        return None
    for k, it in _all_items(tl):
        if _uid(it) == uid:
            return k, it
    return None


def _at(tl: Any, k: int, frame_abs: int) -> Optional[Any]:
    return next((it for it in _items(tl, k) if it.GetStart() == frame_abs), None)


def _markers(host: Any) -> Dict[int, dict]:
    try:
        return {int(f): (i or {}) for f, i in (host.GetMarkers() or {}).items()}
    except Exception:
        return {}


def _tag_of(info: dict) -> Optional[str]:
    cd = str(info.get("customData") or "")
    return cd[len(TAG):] if cd.startswith(TAG) else None


def _item_offset(it: Any, frame: int) -> Optional[int]:
    """Where an item marker sits, in frames from the clip's left edge. Flow reads item markers as offsets from
    the left edge (notes.py); a frame outside the clip is tried as an offset from the source start instead."""
    d = int(it.GetDuration() or 0)
    if 0 <= frame < d:
        return frame
    try:
        lo = int(it.GetLeftOffset() or 0)
    except Exception:
        lo = 0
    return frame - lo if 0 <= frame - lo < d else None


def reads_as_broll(text: Optional[str]) -> bool:
    """Words that ask for B-roll: they open with a B-roll cue, or Finish mode reads them as B-roll (the
    first kind of note they name is B-roll: "drone over the harbour")."""
    t = (text or "").strip()
    return bool(t) and (bool(BROLL_CUE.match(t)) or classify(t) == "broll")


def _broll_marker(info: dict) -> bool:
    cd = str(info.get("customData") or "")
    if cd.startswith(TAG):
        return True
    if cd == "flow" or cd.startswith("flow:"):   # Flow's own markers (Finish mode's ✓ / "Flow needs you", probes)
        return False
    return info.get("color") == "Lavender" or reads_as_broll(_marker_text(info))


def _broll_placeholder(it: Any) -> Optional[str]:
    why = placeholder_reason(it)
    return why if why and reads_as_broll(why) else None


def creation_of(it: Any, known: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The Magnific creation in a timeline clip: its "Magnific Creation" metadata, or a file Flow brought
    in from the Magnific folder (known: media pool id → creation, "mp:<id>" when it came without one)."""
    try:
        mpi = it.GetMediaPoolItem()
    except Exception:
        mpi = None
    if mpi is None:
        return None
    try:
        v = mpi.GetThirdPartyMetadata("Magnific Creation")
    except Exception:
        v = None
    if isinstance(v, dict):
        v = v.get("Magnific Creation")
    if v:
        return str(v)
    return (known or {}).get(_uid(mpi) or "")


def known_media() -> Dict[str, str]:
    """Media pool ids of what Flow imported from the Magnific folder → creation id (or "mp:<id>")."""
    st = mg._load("state.json", {})
    pulled = mg._load("pulled.json", {})
    root = Path(st.get("folder") or mg.folder())
    out = {}
    for rel, h in (st.get("files") or {}).items():
        mid = h.get("media_pool_id")
        if h.get("status") != "imported" or not mid:
            continue
        meta = pulled.get(mg._key(root / rel)) or {}
        out[mid] = str(meta.get("creation") or h.get("creation") or f"mp:{mid}")
    return out


def _brief_of_clip(it: Any) -> str:
    try:
        mpi = it.GetMediaPoolItem()
        meta = mpi.GetMetadata() or {} if mpi is not None else {}
    except Exception:
        meta = {}
    if isinstance(meta, dict):
        for key in ("Description", "Comments"):
            if (meta.get(key) or "").strip():
                return str(meta[key]).strip()[:120]
    try:
        return it.GetName() or ""
    except Exception:
        return ""


def _shape(w: int, h: int) -> str:
    return mg._shape(w, h)


def scan(project: Any, tl: Any, mark: bool = True, known: Optional[Dict[str, str]] = None) -> dict:
    """Read the timeline's slots, give new ones ids (and mark them), and save the project's slot file."""
    pname, tname = project.GetName(), tl.GetName()
    fps, start = _fps(tl), int(tl.GetStartFrame() or 0)
    default_len = max(1, int(round(MARKER_SECONDS * fps)))
    data = load(pname)
    slots = data["slots"]
    known = known_media() if known is None else known
    found: List[dict] = []

    def marker_slot(on: str, host: Any, frame: int, info: dict, f0: int, track: int) -> dict:
        dur = int(info.get("duration") or 1)
        return {"sid": _tag_of(info), "kind": "marker", "f0": f0, "f1": f0 + (dur if dur > 1 else default_len),
                "track": track, "brief": _marker_text(info),
                "marker": {"on": on, "item": _uid(host) if on == "item" else None, "frame": frame,
                           "color": info.get("color"), "name": info.get("name") or "", "note": info.get("note") or "",
                           "duration": dur},
                "fp": f"{on}:{_uid(host) if on == 'item' else ''}:{frame}:{info.get('name') or ''}"}

    for frame, info in sorted(_markers(tl).items()):
        if _broll_marker(info):
            found.append(marker_slot("timeline", tl, frame, info, frame, 1))
    clips = []
    for k, it in _all_items(tl):
        s = it.GetStart() - start
        ims = _markers(it)
        why = _broll_placeholder(it)
        if why:
            tagged = next(((f, i) for f, i in ims.items() if _tag_of(i)), None)
            if tagged is None and not _enabled(it):
                continue                    # a placeholder someone switched off
            if mg.kind_of(Path(why.strip())):  # a clip named after its file: "Placeholder shop.mov"
                why = Path(why.strip()).stem
            found.append({"sid": _tag_of(tagged[1]) if tagged else None, "kind": "placeholder",
                          "f0": s, "f1": it.GetEnd() - start, "track": k, "brief": why.strip()[:120],
                          "placeholder": _uid(it),
                          "marker": ({"on": "item", "item": _uid(it), "frame": tagged[0], "color": tagged[1].get("color"),
                                      "name": tagged[1].get("name") or "", "note": tagged[1].get("note") or "",
                                      "duration": int(tagged[1].get("duration") or 1), "flow": True} if tagged else None),
                          "fp": f"placeholder:{_uid(it)}"})
            continue
        for frame, info in sorted(ims.items()):
            off = _item_offset(it, frame)
            if off is not None and _broll_marker(info):
                found.append(marker_slot("item", it, frame, info, s + off, k))
        c = creation_of(it, known)
        if c:
            clips.append((k, it, c))

    mine = {sid: r for sid, r in slots.items() if r.get("timeline") == tname}
    by_fp = {r.get("fp"): sid for sid, r in mine.items() if r.get("fp")}
    seen, claimed, out, new_marks = set(), set(), [], 0
    for c in found:
        sid = c["sid"] if c["sid"] and re.fullmatch(r"S\d+", c["sid"]) else None
        sid = sid or by_fp.get(c["fp"])
        if sid is None or sid in seen:          # new, or a copy of a marker that already carries an id
            sid = _new_id(data)
        rec = slots.setdefault(sid, {"id": sid, "created": mg._now(), "history": []})
        rec.update({k: c[k] for k in ("kind", "f0", "f1", "track", "brief", "fp")})
        marker = c.get("marker")
        if c["kind"] == "placeholder" and marker is None:
            marker = rec.get("marker") if (rec.get("marker") or {}).get("item") == c["placeholder"] else None
        rec.update({"timeline": tname, "marker": marker, "placeholder": c.get("placeholder")})
        if mark and c["sid"] != sid:
            if _mark(tl, c, sid):
                new_marks += 1
                if c["kind"] == "placeholder":
                    rec["marker"] = {"on": "item", "item": c["placeholder"], "frame": 0, "color": "Lavender",
                                     "name": sid, "note": c["brief"], "duration": 1, "flow": True}
        hit = _find(tl, rec.get("item"))
        if hit:
            rec["status"], rec["f0"], rec["f1"] = "filled", hit[1].GetStart() - start, hit[1].GetEnd() - start
            claimed.add(rec["item"])
        else:
            rec.update({"status": "open", "item": None, "creation": None})
        seen.add(sid)
        out.append(rec)
    for k, it, c in clips:
        uid = _uid(it)
        if uid in claimed:
            continue
        sid = next((s for s, r in mine.items() if s not in seen
                    and (r.get("item") == uid or (r.get("kind") == "clip" and r.get("creation") == c))), None)
        if sid is None:
            sid = _new_id(data)
        rec = slots.setdefault(sid, {"id": sid, "created": mg._now(), "history": []})
        rec.update({"kind": "clip", "timeline": tname, "f0": it.GetStart() - start, "f1": it.GetEnd() - start,
                    "track": k, "item": uid, "creation": c, "status": "filled", "brief": _brief_of_clip(it),
                    "marker": None, "placeholder": None, "fp": f"clip:{uid}"})
        seen.add(sid)
        claimed.add(uid)
        out.append(rec)
    for sid, r in mine.items():
        if sid not in seen:
            r["status"] = "gone"
    save(pname, data)
    w = int(_num(tl.GetSetting("timelineResolutionWidth")))
    h = int(_num(tl.GetSetting("timelineResolutionHeight")))
    out.sort(key=lambda r: (r["f0"], r["id"]))
    return {"project": pname, "timeline": tname, "fps": fps, "width": w, "height": h, "shape": _shape(w, h),
            "slots": [_public(r, fps) for r in out], "marked": new_marks}


def _mark(tl: Any, c: dict, sid: str) -> bool:
    """Write the slot id into the marker's hidden data; a placeholder without one gets a small marker."""
    tag = TAG + sid
    try:
        if c["kind"] == "placeholder":
            hit = _find(tl, c["placeholder"])
            return bool(hit and hit[1].AddMarker(0, "Lavender", sid, c["brief"][:200], 1, tag))
        m = c["marker"]
        host = tl if m["on"] == "timeline" else (_find(tl, m["item"]) or (None, None))[1]
        return bool(host is not None and host.UpdateMarkerCustomData(m["frame"], tag))
    except Exception:
        return False


def _public(r: dict, fps: float) -> dict:
    out = {k: r.get(k) for k in ("id", "kind", "status", "timeline", "f0", "f1", "track", "brief", "item",
                                 "creation", "placeholder")}
    out.update({"at": round(r["f0"] / fps, 3), "seconds": round((r["f1"] - r["f0"]) / fps, 3),
                "earlier_versions": len(r.get("versions") or [])})
    return out


def format_slots(res: dict) -> str:
    shape = res.get("shape") or ""
    head = (f"SLOTS · “{res['timeline']}” {res['width']}x{res['height']}"
            + (f" ({shape})" if shape and "x" not in shape else "") + f" · {res['fps']:g} fps")
    lines = [head]
    if not res["slots"]:
        lines.append("  no slots yet: put a Lavender marker (or a marker that says what B-roll goes there) over the "
                     "spot, or a clip named \"B-roll: …\" as a placeholder")
    for s in res["slots"]:
        what = {"marker": "marker", "placeholder": f"placeholder V{s['track']}", "clip": f"Magnific clip V{s['track']}"}[s["kind"]]
        tail = (" · new versions become takes" if s["status"] == "filled" else "")
        lines.append(f"  {s['id']:<4} {s['status']:<7} {fmt_time(s['at'])}–{fmt_time(s['at'] + s['seconds'])}"
                     f" {s['seconds']:4.1f}s  {what} · {s['brief'] or '(no words)'}{tail}")
    if res.get("marked"):
        lines.append(f"  marked {res['marked']} new slot(s) in Resolve, so their ids stick")
    return "\n".join(lines)


# ---------------------------------------------------------------- fitting a creation to its slot
def need_frames(slot_frames: int, tl_fps: float, clip_fps: float) -> int:
    """Source frames that fill slot_frames timeline frames."""
    return max(1, int(math.ceil(slot_frames / tl_fps * clip_fps - 1e-6)))


def fit(kind: str, slot_frames: int, tl_fps: float, seconds: float, clip_fps: float) -> str:
    """"ok", "slow" (a little short: slow it to fit) or "short" (too short to place)."""
    if kind == "image":
        return "ok"
    have = int(round(seconds * (clip_fps or tl_fps)))
    need = need_frames(slot_frames, tl_fps, clip_fps or tl_fps)
    if have >= need:
        return "ok"
    return "slow" if have >= FIT_MIN * need else "short"


def fit_copy(src: Path, slot_frames: int, tl_fps: float) -> Path:
    """A slowed copy of src that lasts exactly slot_frames at the timeline's rate (ProRes 422 HQ, no sound)."""
    exe = which("ffmpeg")
    if not exe:
        raise FlowError("ffmpeg not found (needed to slow a version to fit its slot). Install it: brew install ffmpeg")
    dur = ffprobe(src)["duration"]
    seconds = slot_frames / tl_fps
    if dur <= 0:
        raise FlowError(f"{src.name} has no length")
    out_dir = mg._home() / "fit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = mg._unique(out_dir / f"{src.stem}-fit{slot_frames}.mov")
    # a hair slower than needed, so the cut at exactly slot_frames never runs out of picture
    run([exe, "-v", "error", "-y", "-i", str(src), "-an", "-vf", f"setpts={seconds * 1.01 / dur:.6f}*PTS,fps={tl_fps:g}",
         "-frames:v", str(slot_frames), "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", str(out)])
    return out


def _src_in(src_frames: int, need: int, src_in: Optional[float], clip_fps: float) -> int:
    room = max(0, src_frames - need)
    if src_in is not None:
        return max(0, min(room, int(round(float(src_in) * clip_fps))))
    return room // 2          # the middle: generated shots often settle in their first frames


# ---------------------------------------------------------------- where a fill goes
def _add_track(tl: Any, index: int) -> Tuple[Optional[int], str]:
    """A new empty video track at index (Resolve 21 takes {"index": n}), else on top. (k, how)."""
    n0 = int(tl.GetTrackCount("video") or 0)
    if index <= n0:
        before = [_uid(x) for x in _items(tl, index)]
        try:
            ok = bool(tl.AddTrack("video", {"index": index}))
        except Exception:
            ok = False
        n1 = int(tl.GetTrackCount("video") or 0)
        if ok and n1 == n0 + 1:
            if not _items(tl, index) and [_uid(x) for x in _items(tl, index + 1)] == before:
                return index, "inserted"
            return n1, "top"            # it went on top after all
    try:
        ok = bool(tl.AddTrack("video"))
    except Exception:
        ok = False
    n1 = int(tl.GetTrackCount("video") or 0)
    return (n1, "top") if ok and n1 == n0 + 1 else (None, "none")


def _drop_track(tl: Any, k: int) -> None:
    try:
        if not _items(tl, k):
            tl.DeleteTrack("video", k)
    except Exception:
        pass


def fill_track(tl: Any, base: int, f0: int, f1: int) -> Tuple[int, bool]:
    """The video track a fill for [f0, f1) above track `base` goes on: (track, made). A Flow B-roll track
    with nothing visible between it and the slot, else a new track right above the slot. Never one that
    would put the new clip over something of the user's."""
    start = int(tl.GetStartFrame() or 0)
    n = int(tl.GetTrackCount("video") or 0)
    for k in range(base + 1, n + 1):
        name = tl.GetTrackName("video", k) or ""
        if FLOW_BROLL.match(name) and not _occupied(tl, k, start, f0, f1):
            return k, False
        if _shows(tl, k, start, f0, f1):
            break                        # anything higher would cover this clip
    k, how = _add_track(tl, base + 1)
    if k is None:
        raise FlowError("Resolve didn't add a track for the new clip.")
    if how == "top":
        covered = [j for j in range(base + 1, k) if _shows(tl, j, start, f0, f1)]
        if covered:
            _drop_track(tl, k)
            names = ", ".join(f"V{j} {tl.GetTrackName('video', j) or ''}".strip() for j in covered)
            raise FlowError(f"the new clip would cover {names} there; this Resolve can't add a track in between. "
                            "Make room on a track right above the slot (or name one \"Flow B-roll\") and put it again.")
    try:
        tl.SetTrackName("video", k, TRACK_FILL)
    except Exception:
        pass
    return k, True


# ---------------------------------------------------------------- markers
def _marker_host(tl: Any, m: dict) -> Tuple[Any, Optional[int], dict]:
    """(host, frame, info) of a slot's marker as it is now (found by its hidden data, else where it was)."""
    host = tl if m.get("on") == "timeline" else (_find(tl, m.get("item")) or (None, None))[1]
    if host is None:
        return None, None, {}
    ms = _markers(host)
    sid_tag = next((f for f, i in ms.items() if str(i.get("customData") or "") == m.get("tag")), None) \
        if m.get("tag") else None
    f = sid_tag if sid_tag is not None else (m.get("frame") if m.get("frame") in ms else None)
    return host, f, (ms.get(f) or {}) if f is not None else {}


def recolor(tl: Any, m: Optional[dict], color: str, sid: str) -> Optional[str]:
    """Re-add the slot's marker in another colour (Resolve can't recolour one). Returns its old colour."""
    if not m:
        return None
    host, f, info = _marker_host(tl, {**m, "tag": TAG + sid})
    if host is None or f is None or not info:
        return None
    old = info.get("color")
    if old == color:
        return old
    try:
        if host.DeleteMarkerAtFrame(f):
            host.AddMarker(f, color, info.get("name") or "", info.get("note") or "", int(info.get("duration") or 1),
                           info.get("customData") or "")
    except Exception:
        return None
    return old


# ---------------------------------------------------------------- building a creation in
def _clip_facts(mpi: Any, path: Path, kind: str, tl_fps: float) -> Tuple[int, float]:
    """(source frames, clip fps) as Resolve sees the clip (ffprobe when it doesn't say)."""
    if kind == "image":
        return 0, tl_fps
    clip_fps = _num(mpi.GetClipProperty("FPS")) or tl_fps
    frames = int(_num(mpi.GetClipProperty("Frames")))
    if frames <= 0:
        frames = int(round(ffprobe(path)["duration"] * clip_fps))
    return frames, clip_fps


def _crop_if_other_shape(resolve: Any, it: Any, path: Path, tl: Any) -> bool:
    """A creation in another shape (16:9 into a 9:16 reel) fills the frame instead of getting bars."""
    try:
        f = ffprobe(path)
        w, h = int(_num(tl.GetSetting("timelineResolutionWidth"))), int(_num(tl.GetSetting("timelineResolutionHeight")))
        if not (f["width"] and f["height"] and w and h) or abs(f["width"] / f["height"] - w / h) < 0.02:
            return False
        from flow import resolve_bridge as rb
        crop = rb.const(resolve, "SCALE_CROP")
        return bool(crop is not None and it.SetProperty("Scaling", crop))
    except Exception:
        return False


def _end(frames: int) -> int:
    from flow.build import _caps
    return frames - (1 if _caps().get("end_frame_inclusive") else 0)


def build_in(resolve: Any, project: Any, mp: Any, tl: Any, rec: dict, mpi: Any, path: Path, kind: str,
             meta: dict, src_in: Optional[float] = None) -> dict:
    """Put one imported creation into its slot: a fill when it is open, a take when it is filled.
    Returns what undo needs. Raises FlowError (nothing changed) when it can't."""
    if rec.get("timeline") != tl.GetName():
        raise FlowError(f"{rec['id']} is on “{rec.get('timeline')}”; open that timeline first.")
    if kind not in ("video", "image"):
        raise FlowError(f"{path.name} is sound; slots take pictures (`flow magnific place playhead` puts sound at the playhead).")
    tc0 = tl.GetCurrentTimecode()
    try:
        if rec.get("status") == "filled":
            out = _take(tl, rec, mpi, path, kind, meta, src_in)
        else:
            out = _fill(resolve, mp, tl, rec, mpi, path, kind, meta, src_in)
    finally:
        try:  # Resolve moves the playhead to a clip it adds; put the user's back
            if tc0 and tl.GetCurrentTimecode() != tc0:
                tl.SetCurrentTimecode(tc0)
        except Exception:
            pass
    rec.setdefault("history", []).append({"op": out["op"], "creation": meta.get("creation"), "file": str(path),
                                          "at": mg._now()})
    if out["op"] == "take" and rec.get("creation"):
        rec.setdefault("versions", [])
        if rec["creation"] not in rec["versions"]:
            rec["versions"].append(rec["creation"])
    rec.update({"status": "filled", "item": out["item"], "creation": str(meta.get("creation") or f"mp:{_uid(mpi)}")})
    return out


def _fill(resolve: Any, mp: Any, tl: Any, rec: dict, mpi: Any, path: Path, kind: str, meta: dict,
          src_in: Optional[float]) -> dict:
    fps, start = _fps(tl), int(tl.GetStartFrame() or 0)
    f0, f1 = int(rec["f0"]), int(rec["f1"])
    slot_len = f1 - f0
    src_frames, clip_fps = _clip_facts(mpi, path, kind, fps)
    if kind == "image":
        s0, need = 0, slot_len
    else:
        need = need_frames(slot_len, fps, clip_fps)
        if src_frames < need:
            raise FlowError(f"{path.name} is {src_frames / clip_fps:.2f}s; {rec['id']} needs {slot_len / fps:.2f}s.")
        s0 = _src_in(src_frames, need, src_in, clip_fps)
    k, made = fill_track(tl, int(rec.get("track") or 1), f0, f1)
    it = (mp.AppendToTimeline([{"mediaPoolItem": mpi, "startFrame": s0, "endFrame": s0 + _end(need),
                                "trackIndex": k, "recordFrame": start + f0, "mediaType": 1}]) or [None])[0]
    if it is None or it.GetStart() - start != f0:
        if it is not None:
            tl.DeleteClips([it], False)
        if made:
            _drop_track(tl, k)
        raise FlowError(f"Resolve didn't put {path.name} into {rec['id']}.")
    d = int(it.GetDuration() or 0)
    if d > slot_len:  # Resolve's end frame can be off by one (or a still too long)
        it.SetRightOffset((it.GetRightOffset() or 0) + (d - slot_len))
    for call, args in (("SetClipColor", ("Purple",)), ("SetName", (f"{rec['id']} · {meta.get('title') or path.stem}"[:60],))):
        try:
            getattr(it, call)(*args)
        except Exception:
            pass
    cropped = _crop_if_other_shape(resolve, it, path, tl)
    ph = None
    if rec.get("kind") == "placeholder":
        hit = _find(tl, rec.get("placeholder"))
        if hit:
            try:
                ph = rec["placeholder"] if hit[1].SetClipEnabled(False) else None
            except Exception:
                ph = None
    old_color = recolor(tl, rec.get("marker"), DONE_COLOR, rec["id"])
    return {"op": "fill", "slot": rec["id"], "timeline": tl.GetName(), "item": _uid(it), "lane": "video",
            "track": k, "track_name": tl.GetTrackName("video", k), "made_track": made, "frame": f0,
            "seconds": round(f0 / fps, 2), "length": round(int(it.GetDuration() or slot_len) / fps, 2),
            "placeholder_off": ph, "marker_color": old_color, "cropped": cropped, "file": str(path)}


def _take(tl: Any, rec: dict, mpi: Any, path: Path, kind: str, meta: dict, src_in: Optional[float]) -> dict:
    fps, start = _fps(tl), int(tl.GetStartFrame() or 0)
    hit = _find(tl, rec.get("item"))
    if hit is None and rec.get("creation"):
        hit = next(((k, it) for k, it in _all_items(tl) if creation_of(it) == rec["creation"]), None)
    if hit is None:
        raise FlowError(f"The clip in {rec['id']} isn't on “{tl.GetName()}” any more.")
    k, it = hit
    s_abs, slot_len = it.GetStart(), int(it.GetDuration() or 0)
    src_frames, clip_fps = _clip_facts(mpi, path, kind, fps)
    if kind == "image":
        s0, need = 0, slot_len
    else:
        need = need_frames(slot_len, fps, clip_fps)
        if src_frames < need:
            raise FlowError(f"{path.name} is {src_frames / clip_fps:.2f}s; the clip in {rec['id']} is "
                            f"{slot_len / fps:.2f}s, and a shorter take would shrink it for good.")
        s0 = _src_in(src_frames, need, src_in, clip_fps)
    if not it.AddTake(mpi, s0, s0 + _end(need)):
        raise FlowError(f"Resolve didn't add {path.name} as a take on {rec['id']}.")
    prev = int(it.GetSelectedTakeIndex() or 1)
    n = int(it.GetTakesCount() or 0)
    it.SelectTakeByIndex(n)
    now = _at(tl, k, s_abs) or it
    if int(now.GetDuration() or 0) != slot_len or now.GetStart() != s_abs:
        now.SelectTakeByIndex(prev)
        now.DeleteTakeByIndex(n)
        raise FlowError(f"Resolve changed the length of the clip in {rec['id']}, so the new take was taken back.")
    return {"op": "take", "slot": rec["id"], "timeline": tl.GetName(), "item": _uid(now), "lane": "video",
            "track": k, "track_name": tl.GetTrackName("video", k), "frame": s_abs - start,
            "seconds": round((s_abs - start) / fps, 2), "length": round(slot_len / fps, 2), "take": n,
            "prev_take": prev, "prev_creation": rec.get("creation"), "file": str(path)}


# ---------------------------------------------------------------- undo
def undo_entry(tl: Any, e: dict, project_name: str) -> str:
    """Take back one fill or take. Returns what happened, in words."""
    name = Path(e["file"]).name
    data = load(project_name)
    rec = data["slots"].get(e.get("slot") or "")
    hit = _find(tl, e.get("item"))
    if e["op"] == "take":
        if hit is None:
            start = int(tl.GetStartFrame() or 0)
            it = _at(tl, int(e["track"]), start + int(e["frame"]))
            hit = (e["track"], it) if it is not None else None
        if hit is None:
            return f"the clip with {name} is gone from “{e['timeline']}”"
        it = hit[1]
        it.SelectTakeByIndex(int(e.get("prev_take") or 1))
        it.DeleteTakeByIndex(int(e["take"]))
        if rec is not None:
            rec["creation"] = e.get("prev_creation")
            rec["item"] = _uid(_at(tl, hit[0], it.GetStart()) or it)
        say = f"{e['slot']} is back on its earlier version ({name} stays in the media pool)"
    else:
        if hit is not None and not tl.DeleteClips([hit[1]], False):
            raise FlowError(f"Resolve didn't remove {name} from the timeline.")
        if e.get("made_track"):
            for k in range(int(tl.GetTrackCount("video") or 0), 0, -1):
                if (tl.GetTrackName("video", k) or "") == e.get("track_name", TRACK_FILL) and not _items(tl, k):
                    _drop_track(tl, k)
                    break
        if e.get("placeholder_off"):
            ph = _find(tl, e["placeholder_off"])
            if ph:
                try:
                    ph[1].SetClipEnabled(True)
                except Exception:
                    pass
        if rec is not None:
            if e.get("marker_color"):
                recolor(tl, rec.get("marker"), e["marker_color"], rec["id"])
            rec.update({"status": "open", "item": None, "creation": None})
        say = (f"took {name} out of {e['slot']}" + (" (the placeholder is back on)" if e.get("placeholder_off") else "")
               + " · it stays in the media pool") if hit is not None else f"{name} was already gone from “{e['timeline']}”"
    if rec is not None:
        save(project_name, data)
    return say
