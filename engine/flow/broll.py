"""B-roll for reels: a library Flow can see, and placement on the reel.

    flow broll [--folder DIR] [--bin NAME] [--file F]   index B-roll for the current job
    flow broll-describe B3 "drone over the harbour at dusk"   remember what a clip shows

Sources: folders on disk, media pool bins (by default any bin named like "B-Roll", "Cutaways",
"Inserts"), single files. Each clip gets an id (B1, B2, ...), 4 thumbnails on a labelled contact
sheet, and keywords from its file name, folder and Resolve metadata. Descriptions written with
`broll-describe` are cached per file in ~/.flow/broll/library.json, so a clip is only looked at once.

Placement (compile_broll) turns plan entries into exact record/source frames on a B-roll track
above the speaker: cover mode (fills the frame) or pip (the whole frame as a floating card),
optional push-in, slow motion, cross-fades and natural sound with J/L lead-in/out.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from flow import paths
from flow.util import FlowError, ffprobe, fmt_time, read_json, which, write_json

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts", ".webm", ".mpg", ".mpeg"}
BIN_PATTERN = re.compile(r"b[\s_-]*roll|cutaway|insert|stock|footage", re.I)
LIB_DIR = paths.FLOW_HOME / "broll"
LIB_FILE = LIB_DIR / "library.json"
THUMB_W, THUMB_H, LABEL_W = 320, 180, 150
THUMB_POS = (0.08, 0.36, 0.64, 0.92)
ROWS_PER_SHEET = 6
STOP = set("""a an the and or but if of to in on at for with from into by is are was were be been being
this that these those it its i you he she we they me my your our their them his her as so do does did
not no just very really then than there here what when where which who how all any some more most one
two get got going gonna want like know think make made take go let lets about over out up down back
video clip shot final copy edit export new old mov mp4 take dji gopro img dsc mvi pxl vid gx gh""".split())

DEFAULTS = {"mode": "full", "motion": "push_in", "push": 1.06, "min": 1.0, "max": 3.5, "tail": 0.12,
            "every": 5.0, "max_coverage": 0.4, "avoid_hook": True, "audio": "mute", "nat_db": -20.0,
            "fade": 0, "pip_size": 0.86, "pip_pos": [0.5, 0.3]}


# ---------------------------------------------------------------- library
def _key(path: Path) -> str:
    st = path.stat()
    return hashlib.sha1(f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def _library() -> dict:
    try:
        return read_json(LIB_FILE)
    except Exception:
        return {}


def tokens(text: str) -> List[str]:
    out = []
    for t in re.split(r"[^0-9A-Za-zÀ-ÿ]+", (text or "").lower()):
        if len(t) < 3 or t in STOP or t.isdigit() or BIN_PATTERN.fullmatch(t):
            continue
        if re.fullmatch(r"(c|dsc|img|mvi|gx|gh|dji|pxl|vid|clip)?\d+[a-z]?", t):
            continue  # camera file numbers
        out.append(stem(t))
    return out


def stem(t: str) -> str:
    for suf in ("ings", "ing", "ies", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)] + ("y" if suf == "ies" else "")
    return t


def _mp_props(mpi: Any) -> Dict[str, str]:
    out = {}
    for k in ("Keywords", "Comments", "Description", "Scene", "Shot", "Clip Name"):
        try:
            v = mpi.GetClipProperty(k)
            if isinstance(v, dict):
                v = v.get(k)
            if v:
                out[k] = str(v)
        except Exception:
            continue
    return out


def _bin_clips(mp: Any, bins: List[str]) -> List[tuple]:
    """(path, media_pool_id, bin name, props) for every video clip in matching bins (recursive)."""
    from flow.find_clip import _file_path
    want = [b.lower() for b in bins]
    out = []
    stack = [(mp.GetRootFolder(), False, "")]
    while stack:
        folder, inside, label = stack.pop()
        name = folder.GetName() or ""
        hit = inside or (any(w == name.lower() or w in name.lower() for w in want) if want
                         else bool(BIN_PATTERN.search(name)))
        here = name if hit and not inside else label
        if hit:
            for clip in folder.GetClipList() or []:
                fp = _file_path(clip)
                if fp and Path(fp).suffix.lower() in VIDEO_EXT:
                    out.append((fp, clip.GetUniqueId(), here, _mp_props(clip)))
        for sub in folder.GetSubFolderList() or []:
            stack.append((sub, hit, here))
    return out


def _thumb(path: Path, t: float, out: Path) -> bool:
    if out.exists():
        return True
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
          f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2:color=0x202020")
    subprocess.run([which("ffmpeg"), "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(path),
                    "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)], capture_output=True)
    return out.exists()


# 5x7 bitmap glyphs for the contact-sheet labels (ffmpeg here has no drawtext)
GLYPHS = {
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
}


def label_image(text: str, out: Path, w: int = LABEL_W, h: int = THUMB_H) -> Path:
    scale = 5 if len(text) <= 4 else 4
    gw, gh = 5 * scale, 7 * scale
    total_w = len(text) * (gw + scale) - scale
    x0, y0 = max(2, (w - total_w) // 2), (h - gh) // 2
    px = bytearray([255, 210, 0] * (w * h))  # Flow yellow background
    for n, ch in enumerate(text):
        g = GLYPHS.get(ch)
        if not g:
            continue
        for r, row in enumerate(g):
            for c, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        x = x0 + n * (gw + scale) + c * scale + dx
                        y = y0 + r * scale + dy
                        if 0 <= x < w and 0 <= y < h:
                            i = 3 * (y * w + x)
                            px[i:i + 3] = b"\x10\x10\x10"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(f"P6 {w} {h} 255\n".encode() + bytes(px))
    return out


def _sheet(rows: List[tuple], out: Path) -> bool:
    """rows: [(label_ppm, [4 thumb jpgs])] -> one jpg, one row per clip."""
    args, filt, names = [which("ffmpeg"), "-nostdin", "-v", "error", "-y"], [], []
    n = 0
    for k, (lab, thumbs) in enumerate(rows):
        ins = []
        for p in [lab] + thumbs:
            args += ["-i", str(p)]
            ins.append(f"[{n}:v]")
            n += 1
        filt.append("".join(ins) + f"hstack=inputs={len(ins)}[r{k}]")
        names.append(f"[r{k}]")
    if len(rows) > 1:
        filt.append("".join(names) + f"vstack=inputs={len(rows)}[out]")
        last = "[out]"
    else:
        last = "[r0]"
    args += ["-filter_complex", ";".join(filt), "-map", last, "-frames:v", "1", "-q:v", "4", str(out)]
    subprocess.run(args, capture_output=True)
    return out.exists()


def scan(job: Path, folders: List[str] = (), bins: Optional[List[str]] = None, files: List[str] = (),
         use_resolve: bool = True) -> dict:
    """Index B-roll for a job: ids, facts, thumbnails, contact sheets, keywords, cached descriptions."""
    if not which("ffmpeg"):
        raise FlowError("ffmpeg not found. Install it: brew install ffmpeg")
    clip = read_json(job / "clip.json") if (job / "clip.json").exists() else {}
    own = str(Path(clip["path"]).resolve()) if clip.get("path") else None
    found: Dict[str, dict] = {}

    def add(path: str, source: str, mp_id: Optional[str] = None, props: Optional[dict] = None):
        p = Path(path).expanduser()
        if not p.exists() or p.suffix.lower() not in VIDEO_EXT:
            return
        rp = str(p.resolve())
        if rp == own or rp in found:
            if rp in found and mp_id:
                found[rp]["media_pool_id"] = mp_id
            return
        found[rp] = {"path": rp, "source": source, "media_pool_id": mp_id, "props": props or {}}

    for d in folders or []:
        root = Path(d).expanduser()
        if not root.is_dir():
            raise FlowError(f"Not a folder: {root}")
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                add(str(p), f"folder:{root.name}")
    for f in files or []:
        add(f, "file")
    # AI shots (flow aishots) live in the same library and survive a rescan
    try:
        prev_clips = read_json(job / "broll.json").get("clips", [])
    except Exception:
        prev_clips = []
    ai_prev = {c["path"]: c for c in prev_clips if c.get("ai")}
    bin_note = None
    if use_resolve and (bins is None or bins):
        try:
            from flow import resolve_bridge as rb
            _, mp, _ = rb.current(rb.connect())
            hits = _bin_clips(mp, list(bins or []))
            for fp, mid, bname, props in hits:
                add(fp, f"bin:{bname}", mid, props)
            if not hits:
                bin_note = ("no media pool bin matched " + (", ".join(bins) if bins else
                                                             "B-roll/Cutaways/Inserts names"))
        except FlowError as exc:
            bin_note = f"media pool not read: {exc}"
    if not found and not ai_prev:
        raise FlowError("No B-roll found. Point Flow at a folder (--folder), a media pool bin (--bin), "
                        "or put clips in a bin named “B-Roll”." + (f" ({bin_note})" if bin_note else ""))

    lib = _library()
    # ids stay stable across rescans (plans refer to them); new clips get the next free number
    prev_ids = {c["path"]: c["id"] for c in prev_clips if not c.get("ai")}
    next_n = max([int(i[1:]) for i in prev_ids.values() if i[1:].isdigit()] + [0]) + 1
    clips = []
    order = sorted(found.items(), key=lambda kv: (kv[0] not in prev_ids, prev_ids.get(kv[0], ""), kv[0].lower()))
    for rp, info in order:
        if rp in ai_prev:
            continue
        if rp in prev_ids:
            cid = prev_ids[rp]
        else:
            cid = f"B{next_n}"
            next_n += 1
        c = clip_entry(Path(rp), cid, info["source"], info["media_pool_id"], info["props"], lib)
        if c:
            clips.append(c)
    for rp, old in ai_prev.items():
        if Path(rp).exists():
            c = clip_entry(Path(rp), old["id"], old["source"], old.get("media_pool_id"), {}, lib, ai=old["ai"])
            if c:
                clips.append(c)
    return write_index(job, clips, bin_note)


def clip_entry(p: Path, cid: str, source: str, mp_id: Optional[str], props: dict, lib: dict,
               ai: Optional[dict] = None, description: str = "") -> Optional[dict]:
    """Facts, thumbnails and keywords for one library clip (None when it is not usable video)."""
    try:
        facts = ffprobe(p)
    except FlowError:
        return None
    if not facts["has_video"] or facts["duration"] <= 0.3:
        return None
    key = _key(p)
    thumbs, times = [], []
    for j, frac in enumerate(THUMB_POS):
        t = max(0.0, min(facts["duration"] - 0.05, facts["duration"] * frac))
        tp = LIB_DIR / "thumbs" / f"{key}_{j}.jpg"
        if _thumb(p, t, tp):
            thumbs.append(str(tp))
            times.append(round(t, 2))
    kw_text = " ".join([p.stem, p.parent.name] + list(props.values()) + ([ai.get("prompt", "")] if ai else []))
    entry = lib.get(key, {})
    c = {
        "id": cid, "path": str(p.resolve()), "name": p.name, "key": key, "source": source,
        "media_pool_id": mp_id, "duration": round(facts["duration"], 3),
        "fps": facts["fps"], "width": facts["width"], "height": facts["height"],
        "has_audio": facts["has_audio"], "thumbs": thumbs, "thumb_times": times,
        "keywords": sorted(set(tokens(kw_text))), "props": props,
        "description": entry.get("description", "") or description, "tags": entry.get("tags", []),
    }
    if ai:
        c["ai"] = ai
    return c


def _id_key(cid: str):
    return (cid[:1], int(cid[1:]) if cid[1:].isdigit() else 0)


def write_index(job: Path, clips: List[dict], note: Optional[str] = None) -> dict:
    """Write broll.json, the labelled contact sheets and broll_review.txt."""
    out_dir = job / "broll"
    out_dir.mkdir(parents=True, exist_ok=True)
    clips.sort(key=lambda c: _id_key(c["id"]))
    sheets = []
    for s in range(0, len(clips), ROWS_PER_SHEET):
        rows = []
        for c in clips[s:s + ROWS_PER_SHEET]:
            lab = label_image(c["id"], out_dir / f"label_{c['id']}.ppm")
            th = (c["thumbs"] + c["thumbs"][-1:] * 4)[:4] if c["thumbs"] else []
            if len(th) == 4:
                rows.append((lab, th))
        sp = out_dir / f"sheet_{s // ROWS_PER_SHEET + 1}.jpg"
        if rows and _sheet(rows, sp):
            sheets.append({"file": str(sp), "ids": [c["id"] for c in clips[s:s + ROWS_PER_SHEET]]})
    data = {"scanned": time.strftime("%Y-%m-%dT%H:%M:%S"), "clips": clips, "sheets": sheets, "note": note}
    write_json(job / "broll.json", data)
    (job / "broll_review.txt").write_text(format_library(data), encoding="utf-8")
    return data


def format_library(data: dict) -> str:
    clips = data.get("clips", [])
    lines = [f"B-ROLL LIBRARY · {len(clips)} clips. Each sheet row: label, then frames at "
             + ", ".join(f"{int(f * 100)}%" for f in THUMB_POS) + " of the clip."]
    for sh in data.get("sheets", []):
        lines.append(f"  sheet {sh['file']}  ({', '.join(sh['ids'])})")
    if data.get("note"):
        lines.append(f"  note: {data['note']}")
    lines.append("")
    for c in clips:
        desc = c.get("description") or "— not described yet (look at the sheet, then flow broll-describe)"
        lines.append(f"{c['id']}: {c['name']} · {fmt_time(c['duration'])} · {c['fps']:.2f} fps · "
                     f"{c['width']}x{c['height']}{' · audio' if c['has_audio'] else ''} · {c['source']}")
        lines.append(f"     {desc}")
        ai = c.get("ai")
        if ai:
            what = (f"restyle of source {ai['window'][0]:.2f}–{ai['window'][1]:.2f}s (follows the edit)"
                    if ai.get("kind") == "restyle" else "generated shot")
            lines.append(f"     AI {what} · {ai.get('model') or 'auto'} · “{ai.get('prompt', '')[:90]}”")
        if c.get("keywords"):
            lines.append(f"     keywords: {', '.join(c['keywords'][:12])}")
        lines.append(f"     frames at {', '.join(f'{t:.1f}s' for t in c.get('thumb_times', []))}")
    return "\n".join(lines) + "\n"


def describe(job: Path, items: Dict[str, Any]) -> dict:
    """items: {"B3": "text"} or {"B3": {"description": ..., "tags": [...]}} (ids, names or paths)."""
    data = read_json(job / "broll.json")
    lib = _library()
    changed = []
    for ref, val in items.items():
        c = find_clip(data, ref)
        if c is None:
            raise FlowError(f"Unknown B-roll clip {ref!r}")
        if isinstance(val, str):
            val = {"description": val}
        c["description"] = val.get("description", c.get("description", ""))
        if val.get("tags"):
            c["tags"] = list(val["tags"])
        lib[c["key"]] = {"path": c["path"], "description": c["description"], "tags": c.get("tags", []),
                         "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
        changed.append(c["id"])
    write_json(LIB_FILE, lib)
    write_json(job / "broll.json", data)
    (job / "broll_review.txt").write_text(format_library(data), encoding="utf-8")
    return {"described": changed}


def find_clip(data: dict, ref: str) -> Optional[dict]:
    ref = str(ref).strip()
    clips = data.get("clips", [])
    for c in clips:
        if c["id"].lower() == ref.lower():
            return c
    for c in clips:
        if c["name"].lower() == ref.lower() or Path(c["name"]).stem.lower() == ref.lower():
            return c
    try:
        rp = str(Path(ref).expanduser().resolve())
        return next((c for c in clips if c["path"] == rp), None)
    except Exception:
        return None


def load_index(job: Path) -> dict:
    try:
        return read_json(job / "broll.json")
    except Exception:
        return {"clips": []}


# ---------------------------------------------------------------- automatic matching
def auto_pick(sentences: List[dict], words: list, removed: set, clips: List[dict], cfg: dict,
              rec_of_word: Callable[[int, bool], Optional[int]], fps: float, total: int,
              hook_end: int, cut_frames: List[int]) -> List[dict]:
    """Keyword match: sentences ↔ clip descriptions/keywords. Prefers windows that hide a jump cut.
    Claude's own picks (an explicit list in the plan) are better; this is the fallback."""
    clip_terms = {c["id"]: set(tokens(" ".join([c.get("description", ""), " ".join(c.get("tags", [])),
                                                 " ".join(c.get("keywords", []))]))) for c in clips}
    cands = []
    for s in sentences:
        ids = [i for i in range(s["w0"], s["w1"] + 1) if i not in removed]
        if not ids:
            continue
        f0, f1 = rec_of_word(ids[0], False), rec_of_word(ids[-1], True)
        if f0 is None or f1 is None or f1 - f0 < cfg["min"] * fps:
            continue
        if cfg["avoid_hook"] and f0 < hook_end:
            continue
        terms = set(tokens(" ".join(words[i]["text"] for i in ids)))
        for cid, ct in clip_terms.items():
            score = len(terms & ct)
            if score == 0:
                continue
            covers = any(f0 < c < f1 for c in cut_frames)
            cands.append((score + (0.5 if covers else 0.0), f0, f1, cid, ids))
    cands.sort(key=lambda x: (-x[0], x[1]))
    picked, used = [], set()
    budget = cfg["max_coverage"] * total
    spent = 0
    for score, f0, f1, cid, ids in cands:
        if cid in used:
            continue
        f1 = min(f1, f0 + int(cfg["max"] * fps))
        if any(not (f1 + cfg["every"] * fps * 0.5 <= p["f0"] or f0 >= p["f1"] + cfg["every"] * fps * 0.5)
               for p in picked):
            continue
        if spent + (f1 - f0) > budget:
            continue
        picked.append({"f0": f0, "f1": f1, "clip": cid, "score": score, "from_word": ids[0]})
        used.add(cid)
        spent += f1 - f0
    return sorted(picked, key=lambda p: p["f0"])


# ---------------------------------------------------------------- placement
def compile_broll(plan_broll: Any, cfg_style: dict, index: dict, ctx: dict, warn: Any) -> List[dict]:
    """ctx: fps, total, hook_end, word_frame(i, end), src_to_rec(t), tw, th, sentences, words,
    removed, cut_frames. Returns entries with frames on the record timeline (one B-roll lane)."""
    cfg = dict(DEFAULTS)
    cfg.update(cfg_style or {})
    fps, total = ctx["fps"], ctx["total"]
    if plan_broll in (None, False, "none", []):
        return []
    clips = index.get("clips", [])
    if not clips:
        warn.add("The plan asks for B-roll but no library is indexed for this job (run flow broll).")
        return []
    if plan_broll == "auto":
        picks = auto_pick(ctx["sentences"], ctx["words"], ctx["removed"], clips, cfg, ctx["word_frame"],
                          fps, total, ctx["hook_end"], ctx["cut_frames"])
        raw = [{"clip": p["clip"], "at": p["f0"] / fps, "duration": (p["f1"] - p["f0"]) / fps,
                "note": "auto"} for p in picks]
        if not raw:
            warn.add("Automatic B-roll found no clip whose description matches what is said.")
    elif isinstance(plan_broll, list):
        raw = plan_broll
    else:
        warn.add(f"broll must be 'auto', 'none' or a list; got {plan_broll!r}")
        return []

    raw = expand_source_sync(raw, index, ctx, warn)
    tail = int(round(float(cfg["tail"]) * fps))
    out: List[dict] = []
    for e in raw:
        sync = bool(e.get("_sync"))
        c = find_clip(index, e.get("clip", ""))
        if c is None:
            warn.add(f"B-roll clip {e.get('clip')!r} is not in the library; skipped")
            continue
        # where on the reel
        f0 = f1 = None
        if "from_word" in e:
            f0 = ctx["word_frame"](int(e["from_word"]), False)
            if "to_word" in e:
                g = ctx["word_frame"](int(e["to_word"]), True)
                f1 = g + tail if g is not None else None
        elif "at_word" in e:
            f0 = ctx["word_frame"](int(e["at_word"]), False)
        elif "at_src" in e:
            f0 = ctx["src_to_rec"](float(e["at_src"]))
        elif "at" in e:
            f0 = int(round(float(e["at"]) * fps))
        if f0 is None:
            warn.add(f"B-roll {c['id']} is anchored to a removed word or outside the edit; skipped")
            continue
        f0 = max(0, f0 - int(round(float(e.get("lead", 0.0)) * fps)))
        if f1 is None:
            f1 = f0 + int(round(float(e.get("duration", (cfg["min"] + cfg["max"]) / 2)) * fps))
        f1 = min(total, f1)
        if cfg["avoid_hook"] and not e.get("over_hook") and not sync and f0 < ctx["hook_end"] < f1:
            warn.add(f"B-roll {c['id']} moved to start after the hook")
            f0 = ctx["hook_end"]
        speed = max(5.0, min(400.0, float(e.get("speed", 100.0))))
        cf = float(c.get("fps") or fps)
        avail_src = c["duration"] - float(e.get("src_in", 0.0) if "src_in" in e else 0.0)
        max_rec = (int(round(avail_src * 100.0 / speed * fps)) if sync
                   else int(avail_src * 100.0 / speed * fps) - 1)
        if f1 - f0 > max_rec:
            warn.add(f"B-roll {c['id']} is only {c['duration']:.1f}s long; shortened")
            f1 = f0 + max_rec
        if f1 - f0 < (1 if sync else int(round(0.5 * fps))):
            warn.add(f"B-roll {c['id']} at {fmt_time(f0 / fps)} would be shorter than 0.5s; skipped")
            continue
        need_s = (f1 - f0) / fps * speed / 100.0
        if "src_in" in e:
            src_in = max(0.0, min(float(e["src_in"]), c["duration"] - need_s))
        else:  # skip the shaky first moments of a handheld clip, stay centred if there is room
            src_in = max(0.0, min(max(0.4, (c["duration"] - need_s) * 0.3), c["duration"] - need_s))
        mode = e.get("mode", cfg["mode"])
        if mode not in ("full", "pip"):
            warn.add(f"B-roll mode {mode!r} unknown; using full")
            mode = "full"
        ent = {"id": c["id"], "path": c["path"], "name": c["name"], "media_pool_id": c.get("media_pool_id"),
               "rec_f0": int(f0), "rec_len": int(f1 - f0), "src_f0": int(round(src_in * cf)),
               "src_f1": int(round((src_in + need_s) * cf)), "clip_fps": cf, "speed_pct": speed,
               "mode": mode, "fade": int(e.get("fade", cfg["fade"]) or 0), "note": e.get("note"),
               "audio": e.get("audio", cfg["audio"]) if c.get("has_audio") else "mute",
               "nat_db": float(e.get("nat_db", cfg["nat_db"]))}
        if c.get("ai"):
            ent["ai"] = c["ai"].get("kind", "generate")
        if ent["src_f1"] <= ent["src_f0"]:
            ent["src_f1"] = ent["src_f0"] + 1
        # framing
        tw, th = ctx["tw"], ctx["th"]
        cw, ch = c.get("width") or tw, c.get("height") or th
        if sync:  # same framing as the speaker shot underneath (punch-ins included)
            ent["zoom"], ent["pan"], ent["tilt"] = e["_frame"]
            ent["sync"] = True
            if ent["src_f1"] - ent["src_f0"] < 1:
                ent["src_f1"] = ent["src_f0"] + 1
        elif mode == "full":
            sc = max(tw / cw, th / ch)
            img_w, img_h = cw * sc, ch * sc
            fx_ = float(e.get("x", 0.5))
            fy_ = float(e.get("y", 0.5))
            ent["pan"] = round(max(-(img_w - tw) / 2, min((img_w - tw) / 2, (0.5 - fx_) * img_w)), 2)
            ent["tilt"] = round(max(-(img_h - th) / 2, min((img_h - th) / 2, (fy_ - 0.5) * img_h)), 2)
            ent["zoom"] = float(e.get("zoom", 1.0))
        else:
            size = float(e.get("size", cfg["pip_size"]))
            px, py = e.get("pos", cfg["pip_pos"])
            fs = min(tw / cw, th / ch)  # Scaling = fit: the whole frame is visible
            card_w, card_h = cw * fs * size, ch * fs * size
            margin = 0.03 * tw
            cx = min(max(float(px) * tw, card_w / 2 + margin), tw - card_w / 2 - margin) if card_w + 2 * margin < tw else tw / 2
            cy = min(max(float(py) * th, card_h / 2 + margin), th - card_h / 2 - margin) if card_h + 2 * margin < th else th / 2
            ent["zoom"] = size
            ent["pan"] = round(cx - tw / 2, 2)
            ent["tilt"] = round(th / 2 - cy, 2)
        motion = "none" if sync else e.get("motion", cfg["motion"])
        if motion in ("push_in", "pull_out"):
            amt = float(e.get("push", cfg["push"]))
            a, b = (1.0, amt) if motion == "push_in" else (amt, 1.0)
            ent["fx"] = [{"type": "push_in", "lf0": 0, "lf1": ent["rec_len"],
                          "params": {"zoom_from": a, "zoom_to": b, "center": [0.5, 0.5]}}]
        elif motion not in (None, "none", False):
            warn.add(f"B-roll motion {motion!r} unknown (push_in, pull_out, none)")
        # natural sound with J (lead-in) / L (tail) offsets
        if ent["audio"] == "nat":
            if abs(speed - 100.0) > 0.01:
                warn.add(f"B-roll {c['id']}: natural sound needs 100% speed; muted")
                ent["audio"] = "mute"
            else:
                j = int(round(float(e.get("j", 0.0)) * fps))
                l_ = int(round(float(e.get("l", 0.0)) * fps))
                j = min(j, ent["rec_f0"], int(ent["src_f0"] * fps / cf))
                l_ = min(l_, total - (ent["rec_f0"] + ent["rec_len"]),
                         max(0, int((c["duration"] - need_s - src_in) * fps)))
                ent["nat"] = {"rec_f0": ent["rec_f0"] - j, "rec_len": ent["rec_len"] + j + l_,
                              "src_f0": ent["src_f0"] - int(round(j * cf / fps)),
                              "src_f1": ent["src_f1"] + int(round(l_ * cf / fps)), "j": j, "l": l_}
        out.append(ent)

    out.sort(key=lambda x: x["rec_f0"])
    kept: List[dict] = []
    for ent in out:  # one lane: a later B-roll cuts the earlier one short
        if kept and ent["rec_f0"] < kept[-1]["rec_f0"] + kept[-1]["rec_len"]:
            prev = kept[-1]
            new_len = ent["rec_f0"] - prev["rec_f0"]
            if new_len < (1 if prev.get("sync") else int(round(0.5 * fps))):
                warn.add(f"B-roll {prev['id']} overlapped {ent['id']} and was dropped")
                kept.pop()
            else:
                ratio = new_len / prev["rec_len"]
                prev["src_f1"] = prev["src_f0"] + max(1, int(round((prev["src_f1"] - prev["src_f0"]) * ratio)))
                prev["rec_len"] = new_len
                for f in prev.get("fx", []):
                    f["lf1"] = new_len
                n = prev.get("nat")
                if n:  # keep the lead-in, end the sound with the picture
                    n["l"] = 0
                    n["rec_len"] = new_len + n["j"]
                    n["src_f1"] = prev["src_f1"]
        kept.append(ent)
    cover = sum(k["rec_len"] for k in kept if not k.get("sync"))  # a restyle is the speaker, not a cutaway
    if total and cover / total > max(0.6, cfg["max_coverage"] + 0.2):
        warn.add(f"B-roll covers {100 * cover / total:.0f}% of the reel; the speaker is barely on screen")
    return kept


def expand_source_sync(raw: List[dict], index: dict, ctx: dict, warn: Any) -> List[dict]:
    """An AI restyle of a source window replaces the speaker shot itself: it is cut exactly like the
    edit underneath. Each kept segment overlapping the window becomes one piece on the B-roll lane,
    with the same timing (speed ramps included) and the same framing (punch-ins included)."""
    segs = ctx.get("segments")
    if not segs:
        return raw
    vr = ctx.get("vrange") or (lambda s: (s["rec_f0"], s["rec_len"], s["src_f0"], s["src_f1"]))
    fps = ctx["fps"]
    out: List[dict] = []
    for e in raw:
        c = find_clip(index, e.get("clip", ""))
        ai = (c or {}).get("ai") or {}
        anchored = any(k in e for k in ("from_word", "at_word", "at_src", "at"))
        if not c or ai.get("kind") != "restyle" or (anchored and e.get("sync") != "source") or e.get("sync") == "none":
            out.append(e)
            continue
        w0, w1 = float(ai["window"][0]), float(ai["window"][1])
        if "src_range" in e:  # only part of the restyled window
            w0, w1 = max(w0, float(e["src_range"][0])), min(w1, float(e["src_range"][1]))
        scale = float(ai.get("time_scale") or 1.0)
        pieces = 0
        for s in segs:
            r0, rlen, s0, s1 = vr(s)
            a, b = max(s0, w0 * fps), min(s1, w1 * fps)
            if b - a < 1 or s1 <= s0:
                continue
            factor = rlen / float(s1 - s0)
            f0 = r0 + int(round((a - s0) * factor))
            f1 = r0 + int(round((b - s0) * factor))
            if f1 <= f0:
                continue
            out.append({"clip": c["id"], "at": f0 / fps, "duration": (f1 - f0) / fps,
                        "src_in": (a / fps - ai["window"][0]) * scale,
                        "speed": 100.0 * scale / factor, "mode": "full", "fade": 0, "audio": "mute",
                        "note": e.get("note") or "AI restyle", "_sync": True,
                        "_frame": (float(s.get("zoom", 1.0)), float(s.get("pan", 0.0)), float(s.get("tilt", 0.0)))})
            pieces += 1
        if not pieces:
            warn.add(f"AI restyle {c['id']} covers source {w0:.2f}–{w1:.2f}s, which the edit cut out entirely")
    return out
