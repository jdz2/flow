"""Magnific creations straight into Resolve, through a folder Flow watches.

    flow magnific status                 folder, watcher, where new creations go, recent imports
    flow magnific folder [DIR] [--all]   show or set the folder (default ~/Movies/Flow/Magnific)
    flow magnific pull --url U | --file F [--name --creation --prompt --model]
                                         save a finished creation into the folder (Claude runs this
                                         after a Magnific generation)
    flow magnific sync [--all]           import what is new in the folder into Resolve, now
    flow magnific start | stop           the watcher in the background: it looks every few seconds
    flow magnific watch                  the watcher in the foreground (what `start` runs)
    flow magnific place bin|playhead     where new creations go (bin, the media pool, is the default)
    flow magnific undo                   take the last placed creation off the timeline again
    flow magnific selftest --switch      check all of this in the running Resolve (Flow Sandbox project)

The folder is the contract. Whatever lands in it goes to Resolve: results Claude pulls from the
Magnific MCP, downloads from the Magnific web app, exports of a Magnific automation. Flow has no
Magnific key; generations run in the Claude session (Flow skill, "4. Magnific").

New files are imported into the media pool bin "Flow › Magnific", purple like AI shots, with prompt,
model and creation id in the clip's metadata (so `flow broll --bin Magnific` can match them by what
they show). With `place playhead` each new creation also goes onto the open timeline at the
playhead, on its own track ("Flow Magnific"; audio on "Flow Magnific Audio") at the first free spot
of that track. Nothing already on the timeline is moved, trimmed or covered, and `undo` removes it.

A file is imported once it has stopped changing (same size and time on two looks a few seconds
apart) and reads as media; partial downloads (.part, .crdownload, Safari's .download) and hidden
files are ignored. Files that were in the folder before Flow watched it are left alone unless --all.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from flow import paths
from flow.broll import VIDEO_EXT
from flow.util import FlowError, ffprobe, read_json, run, tc_to_frames, which, write_json

DEFAULT_FOLDER = Path.home() / "Movies" / "Flow" / "Magnific"
BIN = "Magnific"
TRACK = {"video": "Flow Magnific", "audio": "Flow Magnific Audio"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".dpx", ".bmp", ".psd"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".aif", ".aiff", ".flac"}
CONVERT_EXT = {".webp"}            # Resolve can't read WebP; Flow puts a PNG next to it
PARTIAL_EXT = {".part", ".crdownload", ".download", ".tmp", ".partial", ".opdownload"}
SETTLE = 2.0                       # seconds a new file must stay unchanged before it is imported
GIVE_UP = 120.0                    # a file that still doesn't read as media after this is skipped
DEFAULTS = {"folder": None, "place": "bin", "interval": 3.0, "still_seconds": 5.0}
PLACES = ("bin", "playhead")
CONTENT_TYPES = {
    "video/quicktime": ".mov", "video/mp4": ".mp4", "video/x-m4v": ".m4v", "video/webm": ".webm",
    "video/x-matroska": ".mkv", "image/png": ".png", "image/jpeg": ".jpg", "image/tiff": ".tif",
    "image/webp": ".webp", "image/x-exr": ".exr", "image/vnd.adobe.photoshop": ".psd",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/flac": ".flac",
    "audio/aiff": ".aif", "audio/x-aiff": ".aif", "application/zip": ".zip",
}


# ---------------------------------------------------------------- files Flow keeps
def _home() -> Path:
    d = paths.FLOW_HOME / "magnific"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(name: str, default: Any) -> Any:
    try:
        data = read_json(_home() / name)
    except (OSError, FlowError):
        return default
    return data if isinstance(data, type(default)) else default


def _save(name: str, data: Any) -> None:
    write_json(_home() / name, data)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


_clock = time.time  # seconds, for "has this file stopped changing" (tests swap in their own clock)


class _Lock:
    """An exclusive lock file under ~/.flow/magnific. wait=False gives up at once if it is taken."""

    def __init__(self, name: str, wait: bool = True):
        self.name, self.wait, self.got, self.fh = name, wait, False, None

    def __enter__(self) -> bool:
        import fcntl
        self.fh = open(_home() / self.name, "a+")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB))
            self.got = True
        except OSError:
            self.got = False
        return self.got

    def __exit__(self, *exc) -> bool:
        import fcntl
        if self.got:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


def config() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(_load("config.json", {}))
    return cfg


def save_config(**changes: Any) -> dict:
    cfg = config()
    cfg.update({k: v for k, v in changes.items() if v is not None})
    if cfg["place"] not in PLACES:
        raise FlowError(f"place must be {' or '.join(PLACES)}")
    _save("config.json", cfg)
    return cfg


def folder(cfg: Optional[dict] = None) -> Path:
    cfg = cfg or config()
    return Path(cfg.get("folder") or DEFAULT_FOLDER).expanduser()


# ---------------------------------------------------------------- looking at the folder (pure)
def kind_of(p: Path) -> Optional[str]:
    ext = p.suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in CONVERT_EXT:
        return "convert"
    return None


def _skipped_name(name: str) -> bool:
    return name.startswith(".") or Path(name).suffix.lower() in PARTIAL_EXT


def files_in(root: Path) -> List[Path]:
    """Media files under root (subfolders too), without hidden files and partial downloads."""
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skipped_name(d))
        for fn in filenames:
            p = Path(dirpath) / fn
            if not _skipped_name(fn) and kind_of(p):
                out.append(p)
    return sorted(out)


def sig(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def look(root: Path, state: dict, pulled: dict, now: float, settle: float = SETTLE) -> Tuple[List[Path], List[Path]]:
    """One look at the folder: (ready, waiting). Ready files are new and haven't changed for `settle`
    seconds (or were written whole by `flow magnific pull`); waiting ones are new but just appeared
    or are still being written. Updates state["pending"]."""
    handled = state.setdefault("files", {})
    pending = state.setdefault("pending", {})
    ready, waiting, seen = [], [], set()
    for p in files_in(root):
        rel = _rel(root, p)
        seen.add(rel)
        try:
            s = sig(p)
        except OSError:
            continue
        if (handled.get(rel) or {}).get("sig") == s:
            continue                                  # imported, left alone, or given up on
        if (pulled.get(_key(p)) or {}).get("sig") == s:
            ready.append(p)                           # pull renames a complete file into place
            continue
        pe = pending.get(rel)
        if pe and pe.get("sig") == s and now - float(pe.get("since", now)) >= settle:
            ready.append(p)
            continue
        if not pe or pe.get("sig") != s:
            pending[rel] = {"sig": s, "since": now, "first": (pe or {}).get("first", now)}
        waiting.append(p)
    for rel in list(pending):
        if rel not in seen:
            pending.pop(rel)
    return ready, waiting


def free_spot(spans: List[Tuple[int, int]], f0: int, length: int) -> int:
    """First frame at or after f0 where [f, f + length) overlaps none of the (start, end) spans."""
    f = max(0, int(f0))
    for s, e in sorted(spans):
        if e <= f:
            continue
        if s >= f + length:
            break
        f = e
    return f


def _baseline(root: Path, state: dict, pulled: dict) -> int:
    n = 0
    for p in files_in(root):
        if _key(p) in pulled:
            continue
        state["files"][_rel(root, p)] = {"sig": sig(p), "status": "baseline", "at": _now()}
        n += 1
    return n


def _state(cfg: dict, pulled: dict, all_: bool = False) -> Tuple[dict, int]:
    """The folder's state. A folder Flow hasn't watched before starts with a baseline: files already
    in it are left alone (unless all_), so setting a full Downloads folder doesn't flood the bin."""
    root = folder(cfg)
    st = _load("state.json", {})
    if st.get("folder") == str(root):
        st.setdefault("files", {})
        st.setdefault("pending", {})
        return st, 0
    st = {"folder": str(root), "files": {}, "pending": {}, "since": _now()}
    return st, (0 if all_ else _baseline(root, st, pulled))


# ---------------------------------------------------------------- pull (Claude → folder)
def _slug(s: str, n: int = 48) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^0-9A-Za-z]+", "-", s).strip("-")
    return s[:n].strip("-") or "magnific"


def _key(p: Path) -> str:
    """How pulled files are looked up: the resolved path, in one Unicode form (macOS may hand back
    either form of an accented name)."""
    return unicodedata.normalize("NFC", str(p.resolve()))


def _unique(p: Path) -> Path:
    if not p.exists():
        return p
    for i in range(2, 1000):
        cand = p.with_name(f"{p.stem}-{i}{p.suffix}")
        if not cand.exists():
            return cand
    raise FlowError(f"Too many files named {p.name}")


def _known_ext(ext: str) -> bool:
    ext = (ext or "").lower()
    return bool(kind_of(Path("x" + ext))) or ext == ".zip"


def _ext_for(url: str, ctype: str, filename: Optional[str]) -> str:
    for cand in (Path(filename or "").suffix, Path(urllib.parse.urlparse(url).path).suffix):
        if _known_ext(cand):
            return cand.lower()
    return CONTENT_TYPES.get(ctype, "")


def _fetch(url: str, root: Path, stem: str, ext: Optional[str]) -> Tuple[Path, str]:
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": "flow"})
    tmp = root / f".{stem}.part"                         # hidden: the watcher ignores it
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            try:
                fname = r.headers.get_filename()
            except Exception:
                fname = None
            suffix = (ext or _ext_for(url, ctype, fname)).lower()
            with open(tmp, "wb") as fh:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    fh.write(b)
    except urllib.error.HTTPError as exc:
        if tmp.exists():
            tmp.unlink()
        raise FlowError(f"The link answered HTTP {exc.code}. Magnific links expire: deliver the creation "
                        "again for a fresh one.") from None
    except (urllib.error.URLError, OSError) as exc:
        if tmp.exists():
            tmp.unlink()
        raise FlowError(f"Download failed: {getattr(exc, 'reason', exc)}") from None
    return tmp, suffix


FORMAT_EXT = {"png_pipe": ".png", "jpeg_pipe": ".jpg", "tiff_pipe": ".tif", "webp_pipe": ".webp",
              "exr_pipe": ".exr", "psd_pipe": ".psd", "bmp_pipe": ".bmp", "dpx_pipe": ".dpx", "wav": ".wav",
              "mp3": ".mp3", "aiff": ".aif", "flac": ".flac", "aac": ".aac", "matroska,webm": ".mkv"}


def _sniff(p: Path) -> str:
    """The extension a file should have, from what ffprobe reads in it (for links that don't say)."""
    exe = which("ffprobe")
    if not exe:
        return ""
    proc = run([exe, "-v", "error", "-print_format", "json", "-show_format", str(p)], check=False)
    try:
        fmt = json.loads(proc.stdout or "{}").get("format", {})
    except ValueError:
        return ""
    name = fmt.get("format_name", "")
    if "mov" in name.split(",") or "mp4" in name.split(","):
        return ".mov" if (fmt.get("tags") or {}).get("major_brand", "").strip() == "qt" else ".mp4"
    return FORMAT_EXT.get(name, "")


def pull(url: Optional[str] = None, file: Optional[str] = None, name: Optional[str] = None,
         creation: Optional[str] = None, prompt: Optional[str] = None, model: Optional[str] = None,
         kind: Optional[str] = None, ext: Optional[str] = None, force: bool = False) -> dict:
    """Save a finished creation into the folder, complete, with what made it (for the clip metadata)."""
    if not url and not file:
        raise FlowError("Give --url (the finished creation's https link) or --file.")
    root = folder()
    root.mkdir(parents=True, exist_ok=True)
    if creation and not force:
        for p, m in _load("pulled.json", {}).items():
            if m.get("creation") == str(creation) and Path(p).exists():
                return {"file": p, "name": Path(p).name, "skipped": True,
                        "say": f"{Path(p).name} is already in the folder (creation {creation})"}
    if ext and not ext.startswith("."):
        ext = "." + ext
    base = name or (Path(file).stem if file else "") or (f"creation-{creation}" if creation else "")
    stem = f"{time.strftime('%Y%m%d-%H%M%S')}_{_slug(base)}"
    if file:
        src = Path(file).expanduser()
        if not src.is_file():
            raise FlowError(f"No such file: {src}")
        if not url and src.resolve().parent == root.resolve():
            raise FlowError(f"{src.name} is already in the Magnific folder; the watcher (or sync) imports it.")
        suffix = (ext or src.suffix).lower()
        tmp = root / f".{stem}.part"
        shutil.copyfile(src, tmp)
    else:
        if not url.startswith("https://"):
            raise FlowError("The creation's URL must be https.")
        tmp, suffix = _fetch(url, root, stem, ext)
    try:
        if not _known_ext(suffix):
            suffix = _sniff(tmp) or suffix
        if suffix == ".zip":
            raise FlowError("That's a zip (an image sequence). Deliver video as ProRes 4444 to keep "
                            "transparency, or unzip it into the folder yourself.")
        k = kind_of(Path("x" + suffix))
        if k is None:
            raise FlowError(f"{suffix or 'This file type'} isn't a format Resolve imports. Deliver video as "
                            "ProRes or H.264, images as PNG or TIFF, audio as WAV (or pass --ext).")
        if kind and kind != ("image" if k == "convert" else k):
            raise FlowError(f"Expected {kind}, but the file is {'an image' if k == 'convert' else k} ({suffix}).")
        try:
            facts = ffprobe(tmp)
        except FlowError:
            raise FlowError("The download isn't a readable media file (an expired link or an error page?).") from None
        if k in ("video", "image", "convert") and not facts["has_video"]:
            raise FlowError("The download has no picture in it.")
        if k == "audio" and not facts["has_audio"]:
            raise FlowError("The download has no sound in it.")
        with _Lock("pulled.lock"):
            final = _unique(root / f"{stem}{suffix}")
            # recorded before the file appears, so the watcher always finds its prompt and model
            # (a rename keeps size and mtime, so the signature is the same)
            pulled = _load("pulled.json", {})
            pulled[_key(final)] = {"sig": sig(tmp), "creation": str(creation) if creation else None,
                                   "prompt": prompt, "model": model, "title": name, "kind": k,
                                   "pulled": _now(), "source": "file" if file else "url"}
            _save("pulled.json", pulled)
            os.replace(tmp, final)
    finally:
        if tmp.exists():
            tmp.unlink()
    on = running() is not None
    return {"file": str(final), "name": final.name, "kind": k, "size": final.stat().st_size,
            "width": facts["width"], "height": facts["height"], "duration": round(facts["duration"], 2),
            "watcher": on,
            "say": f"{final.name} is in {root}" + (" · the watcher imports it in a moment" if on
                                                    else " · run `flow magnific sync` to import it")}


# ---------------------------------------------------------------- folder → Resolve
def _media_ok(p: Path, k: str) -> bool:
    if not which("ffprobe"):
        return True
    try:
        f = ffprobe(p)
    except FlowError:
        return False
    return f["has_audio"] if k == "audio" else f["has_video"]


def _convert(p: Path) -> Path:
    out = _unique(p.with_suffix(".png"))
    exe = which("ffmpeg")
    if not exe:
        raise FlowError("ffmpeg not found (needed to turn WebP into PNG). Install it: brew install ffmpeg")
    run([exe, "-v", "error", "-y", "-i", str(p), "-frames:v", "1", str(out)])
    return out


def _sub_bin(mp: Any, parent: Any, name: str) -> Any:
    for f in parent.GetSubFolderList() or []:
        if f.GetName() == name:
            return f
    sub = mp.AddSubFolder(parent, name)
    time.sleep(1.0)  # let the media pool UI settle before importing into a new bin (see build._import_into)
    return sub or parent


def restore_view(mp: Any, prev: Any, res: Optional[dict] = None) -> bool:
    """Put the media pool back on the bin the user had open. Resolve opens a bin when a script makes it or
    imports into it, and its media pool can lag, so check, and try again after a moment."""
    if prev is None:
        return True
    try:
        want = prev.GetUniqueId()
    except Exception:
        return False

    def back() -> bool:
        try:
            cur = mp.GetCurrentFolder()
            return cur is not None and cur.GetUniqueId() == want
        except Exception:
            return False
    for _ in range(3):
        if back():
            return True
        try:
            mp.SetCurrentFolder(prev)
        except Exception:
            pass
        time.sleep(0.5)
    if back():
        return True
    if res is not None:
        try:
            name = prev.GetName()
        except Exception:
            name = "you had open"
        res["notes"].append(f"the media pool stayed on Flow › {BIN} (Resolve didn't go back to the bin {name})")
    return False


def _tag(mpi: Any, meta: dict) -> None:
    """Purple like AI shots, and what made it in the metadata: searchable in Resolve, and keywords for
    Flow's B-roll matching."""
    def attempt(fn_name: str, *a: Any) -> bool:
        try:
            return bool(getattr(mpi, fn_name)(*a))
        except Exception:
            return False
    attempt("SetClipColor", "Purple")
    fields = {"Keywords": ", ".join(x for x in ("Magnific", meta.get("model")) if x)}
    if meta.get("prompt"):
        fields["Comments"] = str(meta["prompt"])[:2000]
    if meta.get("title"):
        fields["Description"] = str(meta["title"])
    for key, val in fields.items():
        attempt("SetMetadata", key, val) or attempt("SetClipProperty", key, val)
    if meta.get("creation"):
        attempt("SetThirdPartyMetadata", "Magnific Creation", str(meta["creation"]))


def _track(tl: Any, kind: str) -> int:
    name = TRACK[kind]
    for k in range(1, int(tl.GetTrackCount(kind) or 0) + 1):
        if (tl.GetTrackName(kind, k) or "") == name:
            return k
    ok = tl.AddTrack(kind, "stereo") if kind == "audio" else tl.AddTrack(kind)
    k = int(tl.GetTrackCount(kind) or 0)
    if not ok or not k:
        raise FlowError("Resolve didn't add a track for the Magnific creation.")
    try:
        tl.SetTrackName(kind, k, name)
    except Exception:
        pass
    return k


def _num(v: Any) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def place(mp: Any, tl: Any, mpi: Any, path: Path, kind: str, still_seconds: float) -> dict:
    """Put one imported creation on the open timeline at the playhead, on its own track, at the first
    spot of that track that is free for its whole length. Never touches another clip."""
    from flow.build import _caps
    fps = _num(tl.GetSetting("timelineFrameRate")) or 24.0
    start = int(tl.GetStartFrame() or 0)
    tc0 = tl.GetCurrentTimecode()
    ph = max(0, tc_to_frames(tc0, fps) - start)
    lane = "audio" if kind == "audio" else "video"
    if kind == "image":
        src_frames = max(1, int(round(float(still_seconds or DEFAULTS["still_seconds"]) * fps)))
        rec_len = src_frames
    else:
        clip_fps = _num(mpi.GetClipProperty("FPS")) or fps
        src_frames = int(_num(mpi.GetClipProperty("Frames")))
        if src_frames <= 0:
            src_frames = int(round(ffprobe(path)["duration"] * clip_fps))
        if src_frames <= 0:
            raise FlowError(f"{path.name} has no length Resolve reports.")
        rec_len = max(1, int(round(src_frames / clip_fps * fps)))
    k = _track(tl, lane)
    spans = [(it.GetStart() - start, it.GetEnd() - start) for it in tl.GetItemListInTrack(lane, k) or []]
    f0 = free_spot(spans, ph, rec_len)
    end_inclusive = bool(_caps().get("end_frame_inclusive", False))
    info = {"mediaPoolItem": mpi, "startFrame": 0, "endFrame": src_frames - (1 if end_inclusive else 0),
            "trackIndex": k, "recordFrame": start + f0, "mediaType": 2 if lane == "audio" else 1}
    it = (mp.AppendToTimeline([info]) or [None])[0]
    if it is None:
        raise FlowError(f"Resolve didn't put {path.name} on the timeline.")
    d = int(it.GetDuration() or 0)
    if d > rec_len:  # same trim as build: Resolve's end frame can be off by one (or a still too long)
        it.SetRightOffset((it.GetRightOffset() or 0) + (d - rec_len))
    try:
        it.SetClipColor("Purple")
    except Exception:
        pass
    try:  # Resolve moves the playhead to the end of a clip it adds; put the user's back
        if tc0 and tl.GetCurrentTimecode() != tc0:
            tl.SetCurrentTimecode(tc0)
    except Exception:
        pass
    return {"timeline": tl.GetName(), "item": it.GetUniqueId(), "lane": lane, "track": k,
            "track_name": TRACK[lane], "frame": f0, "seconds": round(f0 / fps, 2),
            "length": round(int(it.GetDuration() or rec_len) / fps, 2), "file": str(path),
            "moved": f0 != ph}


def _import(ready: List[Path], root: Path, st: dict, pulled: dict, where: str, cfg: dict,
            connect: Optional[Callable[[], Any]], res: dict) -> None:
    from flow import resolve_bridge as rb
    from flow.build import _flow_bin, _import_into
    from flow.find_clip import _file_path, find_media_pool_item
    handled, pending = st["files"], st["pending"]
    # reach Resolve first: if it is closed (or no project is open) nothing in the folder changes
    resolve = (connect or rb.connect)()
    project, mp, tl = rb.current(resolve)
    now = _clock()
    batch: List[Tuple[str, Path, str, dict]] = []   # (rel of the file that arrived, file to import, kind, meta)
    for p in ready:
        rel, k = _rel(root, p), kind_of(p)
        meta = pulled.get(_key(p)) or {}
        if not _media_ok(p, "image" if k == "convert" else k):
            first = float((pending.get(rel) or {}).get("first", now))
            if now - first > GIVE_UP:
                handled[rel] = {"sig": sig(p), "status": "failed", "at": _now(),
                                "note": "doesn't read as a picture or sound file"}
                res["failed"].append({"file": rel, "why": "not a readable media file"})
            else:
                res["waiting"].append(rel)
            continue
        if k == "convert":
            try:
                out = _convert(p)
            except FlowError as exc:
                handled[rel] = {"sig": sig(p), "status": "failed", "at": _now(), "note": str(exc)}
                res["failed"].append({"file": rel, "why": str(exc)})
                continue
            handled[rel] = {"sig": sig(p), "status": "converted", "at": _now(), "to": _rel(root, out)}
            batch.append((_rel(root, out), out, "image", meta))
        else:
            batch.append((rel, p, k, meta))
    if not batch:
        return
    prev = mp.GetCurrentFolder()  # first: Resolve opens a bin it just made (and the bin it imports into)
    target = _sub_bin(mp, _flow_bin(mp), BIN)
    items: Dict[str, Tuple[Any, bool]] = {}
    todo = []
    for rel, p, k, meta in batch:
        mpi = find_media_pool_item(mp, str(p))
        if mpi is not None:
            items[rel] = (mpi, True)
        else:
            todo.append((rel, p))
    if todo:
        got = _import_into(mp, target, [str(p) for _, p in todo])
        by_path = {}
        for it in got:
            fp = _file_path(it)
            if fp:
                by_path[_key(Path(fp))] = it
        for rel, p in todo:
            it = by_path.get(_key(p))
            if it is not None:
                items[rel] = (it, False)
    restore_view(mp, prev, res)
    pname = project.GetName()
    for rel, p, k, meta in batch:
        if rel not in items:
            handled[rel] = {"sig": sig(p), "status": "failed", "at": _now(), "project": pname,
                            "note": "Resolve didn't import it"}
            res["failed"].append({"file": rel, "why": "Resolve didn't import it"})
            continue
        mpi, already = items[rel]
        if not already:
            _tag(mpi, meta)
        entry = {"sig": sig(p), "status": "imported", "at": _now(), "project": pname, "kind": k,
                 "media_pool_id": mpi.GetUniqueId()}
        res["imported"].append({"file": rel, "kind": k, "already": already, "prompt": meta.get("prompt")})
        if where == "playhead":
            if tl is None:
                res["notes"].append("no timeline is open, so new creations stay in the media pool")
            else:
                try:
                    pl = place(mp, tl, mpi, p, k, float(cfg.get("still_seconds") or DEFAULTS["still_seconds"]))
                except FlowError as exc:
                    res["notes"].append(str(exc))
                else:
                    entry["placed"] = {x: pl[x] for x in ("timeline", "item", "seconds")}
                    res["placed"].append({**pl, "file": rel})
                    placed = _load("placed.json", [])
                    placed.append({**pl, "file": str(p), "at": _now()})
                    _save("placed.json", placed[-200:])
        handled[rel] = entry
        pending.pop(rel, None)


def sync(all_: bool = False, where: Optional[str] = None, wait: bool = True,
         connect: Optional[Callable[[], Any]] = None) -> dict:
    """One pass: import what is new and settled in the folder (and place it, in playhead mode).
    wait=True (a one-off run) looks twice, a moment apart, so a file that just arrived counts too."""
    cfg = config()
    root = folder(cfg)
    root.mkdir(parents=True, exist_ok=True)
    where = where or cfg["place"]
    if where not in PLACES:
        raise FlowError(f"place must be {' or '.join(PLACES)}")
    res: Dict[str, Any] = {"folder": str(root), "place": where, "imported": [], "placed": [], "waiting": [],
                           "failed": [], "baseline": 0, "notes": [], "busy": False}
    try:
        os.listdir(root)
    except PermissionError:
        raise FlowError(f"macOS doesn't let Flow read {root}. Allow it in System Settings › Privacy & Security › "
                        "Files and Folders, or use a folder in ~/Movies.") from None
    with _Lock("sync.lock", wait=False) as got:
        if not got:
            res["busy"] = True
            res["notes"].append("another Magnific import is running; it picks these files up")
            return res
        pulled = _load("pulled.json", {})
        st, res["baseline"] = _state(cfg, pulled, all_)
        before = json.dumps(st, sort_keys=True)
        if all_:
            for rel in [r for r, h in st["files"].items() if h.get("status") in ("baseline", "failed")]:
                st["files"].pop(rel)
        ready, waiting = look(root, st, pulled, _clock())
        if wait and waiting:
            time.sleep(SETTLE + 0.2)
            more, waiting = look(root, st, pulled, _clock())
            ready += [p for p in more if p not in ready]
        res["waiting"] = [_rel(root, p) for p in waiting]
        try:
            if ready:
                _import(ready, root, st, pulled, where, cfg, connect, res)
        except FlowError as exc:  # Resolve closed, no project open…: the files wait for the next look
            res["notes"].append(str(exc))
            res["blocked"] = True
            res["waiting"] += [_rel(root, p) for p in ready
                               if (st["files"].get(_rel(root, p)) or {}).get("sig") != sig(p)]
        finally:
            if json.dumps(st, sort_keys=True) != before:  # the watcher looks every few seconds: write only changes
                _save("state.json", st)
    res["waiting"] = list(dict.fromkeys(res["waiting"]))
    return res


# ---------------------------------------------------------------- undo
def undo(connect: Optional[Callable[[], Any]] = None) -> dict:
    with _Lock("sync.lock"):  # not while the watcher is placing something
        return _undo(connect)


def _undo(connect: Optional[Callable[[], Any]]) -> dict:
    from flow import resolve_bridge as rb
    placed = _load("placed.json", [])
    live = [e for e in placed if not e.get("undone")]
    if not live:
        raise FlowError("Nothing to undo: no Magnific creation was placed on a timeline.")
    e = live[-1]
    resolve = (connect or rb.connect)()
    _, _, tl = rb.current(resolve)
    if tl is None or tl.GetName() != e["timeline"]:
        raise FlowError(f"The last creation went onto “{e['timeline']}”. Open that timeline, then undo again.")
    lane = e.get("lane", "video")
    hits = []
    for k in range(1, int(tl.GetTrackCount(lane) or 0) + 1):
        hits += [it for it in tl.GetItemListInTrack(lane, k) or [] if it.GetUniqueId() == e["item"]]
    name = Path(e["file"]).name
    if hits and not tl.DeleteClips(hits, False):
        raise FlowError(f"Resolve didn't remove {name} from the timeline.")
    e["undone"] = True
    _save("placed.json", placed)
    say = (f"took {name} off “{e['timeline']}” (it stays in the media pool bin Flow › {BIN})" if hits
           else f"{name} was already gone from “{e['timeline']}”")
    return {"undid": e, "say": say}


# ---------------------------------------------------------------- the watcher
def _pid_file() -> Path:
    return _home() / "watch.pid"


def log_file() -> Path:
    return _home() / "watch.log"


def _read_pid() -> Optional[int]:
    try:
        return int(_pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def _is_watcher(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return True  # can't tell; trust the pid file
    return "magnific" in out and "watch" in out


def running() -> Optional[int]:
    pid = _read_pid()
    if not pid:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return pid if _is_watcher(pid) else None


def _lines(res: dict) -> List[str]:
    out = []
    for i in res.get("imported", []):
        out.append(f"MAGNIFIC · {i['file']} → media pool Flow › {BIN}"
                   + (" (was already there)" if i.get("already") else ""))
    for p in res.get("placed", []):
        out.append(f"MAGNIFIC · {Path(p['file']).name} on “{p['timeline']}” at {p['seconds']:.2f}s, "
                   f"track {p['track_name']}" + (" (the playhead spot was taken; first free spot after it)"
                                                 if p.get("moved") else ""))
    for f in res.get("failed", []):
        out.append(f"MAGNIFIC · skipped {f['file']}: {f['why']}")
    return out


def watch(ticks: Optional[int] = None, connect: Optional[Callable[[], Any]] = None,
          out: Callable[[str], None] = print) -> int:
    """Look at the folder every few seconds and import what is new, until stopped. Settings are read
    on every look, so `place` / `folder` changes apply without a restart."""
    me = os.getpid()
    _pid_file().write_text(str(me))
    stop = {"now": False}
    old = None
    try:
        old = signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    except ValueError:  # not the main thread (tests)
        pass
    out(f"MAGNIFIC · watching {folder()} (new creations → {config()['place']})")
    last_note, n = None, 0
    try:
        while not stop["now"] and _read_pid() == me:
            try:
                res = sync(wait=False, connect=connect)
            except FlowError as exc:
                res = {"notes": [str(exc)]}
            except Exception as exc:  # a Resolve API surprise must not end the watcher
                res = {"notes": [f"import failed: {type(exc).__name__}: {exc}"]}
            for line in _lines(res):
                out(line)
            note = "; ".join(res.get("notes") or []) or None
            if note != last_note:
                if note:
                    out(f"MAGNIFIC · {note}")
                last_note = note
            n += 1
            if ticks and n >= ticks:
                break
            time.sleep(float(config().get("interval") or DEFAULTS["interval"]))
    finally:
        if _read_pid() == me:
            _pid_file().unlink()
        if old is not None:
            signal.signal(signal.SIGTERM, old)
    return 0


def start(where: Optional[str] = None, interval: Optional[float] = None,
          still_seconds: Optional[float] = None) -> dict:
    cfg = save_config(place=where, interval=interval, still_seconds=still_seconds)
    root = folder(cfg)
    root.mkdir(parents=True, exist_ok=True)
    pid = running()
    if pid:
        return {"running": True, "pid": pid, "folder": str(root), "place": cfg["place"], "started": False,
                "say": f"The Magnific watcher is already running (pid {pid}); it uses these settings from its next look."}
    with _Lock("sync.lock"):  # baseline now, so files already in the folder stay out
        pulled = _load("pulled.json", {})
        st, base = _state(cfg, pulled)
        _save("state.json", st)
    env = os.environ.copy()
    engine = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = engine + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:  # keep the log small: one old copy
        if log_file().stat().st_size > 1_000_000:
            os.replace(log_file(), log_file().with_suffix(".log.1"))
    except OSError:
        pass
    with open(log_file(), "a", encoding="utf-8") as log:
        log.write(f"--- {_now()} start\n")
        log.flush()
        proc = subprocess.Popen([sys.executable, "-u", "-m", "flow", "magnific", "watch"], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                                cwd=str(_home()))
    _pid_file().write_text(str(proc.pid))
    time.sleep(0.5)
    if proc.poll() is not None:
        raise FlowError(f"The watcher stopped right away. See {log_file()}")
    return {"running": True, "pid": proc.pid, "folder": str(root), "place": cfg["place"], "started": True,
            "baseline": base, "log": str(log_file()),
            "say": f"Watching {root} · new creations → " + ("media pool bin Flow › Magnific" if cfg["place"] == "bin"
                                                            else "media pool + the open timeline at the playhead")
                   + (f" · {base} file(s) already there were left alone (`flow magnific sync --all` imports them)"
                      if base else "")}


def stop() -> dict:
    pid = running()
    if not pid:
        try:
            _pid_file().unlink()
        except OSError:
            pass
        return {"stopped": False, "say": "The Magnific watcher isn't running."}
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):  # it finishes the import it is in, then quits
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    try:
        _pid_file().unlink()
    except OSError:
        pass
    return {"stopped": True, "pid": pid,
            "say": "Magnific watcher stopped. New files wait in the folder until you start it again or run sync."}


# ---------------------------------------------------------------- settings + status
def set_folder(path: str, all_: bool = False) -> dict:
    root = Path(path).expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise FlowError(f"Not a folder: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if root in (Path.home().resolve(), Path("/")):
        raise FlowError("Pick a folder for Magnific creations, not your whole home folder.")
    cfg = save_config(folder=str(root))
    with _Lock("sync.lock"):
        pulled = _load("pulled.json", {})
        st, base = _state(cfg, pulled, all_)
        _save("state.json", st)
    return {"folder": str(root), "baseline": base, "all": all_,
            "say": f"Magnific folder: {root}"
                   + (f" · {base} file(s) already there were left alone (`flow magnific sync --all` imports them)"
                      if base else "")}


def set_place(where: str, still_seconds: Optional[float] = None) -> dict:
    if where not in PLACES:
        raise FlowError(f"place must be {' or '.join(PLACES)}")
    cfg = save_config(place=where, still_seconds=still_seconds)
    say = ("New creations go to the media pool bin Flow › Magnific only; timelines stay untouched."
           if where == "bin" else
           "New creations go to the media pool and onto the open timeline at the playhead, on the track "
           f"“{TRACK['video']}” (stills {cfg['still_seconds']:g} s). `flow magnific undo` takes the last one off.")
    return {"place": where, "still_seconds": cfg["still_seconds"], "say": say}


def _shape(w: int, h: int) -> str:
    if not w or not h:
        return ""
    for name, r in (("9:16", 9 / 16), ("16:9", 16 / 9), ("1:1", 1.0), ("4:5", 4 / 5), ("2.39:1", 2.39)):
        if abs(w / h - r) < 0.02:
            return name
    return f"{w}x{h}"


def open_timeline(connect: Optional[Callable[[], Any]] = None) -> Optional[dict]:
    """The project and timeline open in Resolve, with its shape (so a generation can match it).
    None when Resolve can't be reached."""
    from flow import resolve_bridge as rb
    try:
        project, _, tl = rb.current((connect or rb.connect)())
    except FlowError:
        return None
    out: Dict[str, Any] = {"project": project.GetName(), "timeline": None}
    if tl is not None:
        w = int(_num(tl.GetSetting("timelineResolutionWidth")))
        h = int(_num(tl.GetSetting("timelineResolutionHeight")))
        out.update({"timeline": tl.GetName(), "width": w, "height": h,
                    "fps": _num(tl.GetSetting("timelineFrameRate")), "shape": _shape(w, h)})
    return out


def status(resolve: bool = False, connect: Optional[Callable[[], Any]] = None) -> dict:
    cfg = config()
    root = folder(cfg)
    st = _load("state.json", {})
    files = st.get("files", {}) if st.get("folder") == str(root) else {}
    imported = sorted(((r, h) for r, h in files.items() if h.get("status") == "imported"),
                      key=lambda x: x[1].get("at", ""))
    waiting = [_rel(root, p) for p in files_in(root)
               if (files.get(_rel(root, p)) or {}).get("sig") != sig(p)] if root.is_dir() else []
    pulled = _load("pulled.json", {})
    placed = [e for e in _load("placed.json", []) if not e.get("undone")]
    pid = running()
    return {"folder": str(root), "folder_set": bool(cfg.get("folder")), "exists": root.is_dir(),
            "place": cfg["place"], "still_seconds": cfg["still_seconds"], "interval": cfg["interval"],
            "watcher": pid, "log": str(log_file()), "imported": len(imported),
            "recent": [{"file": r, "at": h.get("at"), "project": h.get("project"),
                        "placed": h.get("placed")} for r, h in imported[-5:]],
            "waiting": waiting, "left_alone": sum(1 for h in files.values() if h.get("status") == "baseline"),
            "failed": [{"file": r, "why": h.get("note")} for r, h in files.items() if h.get("status") == "failed"],
            "pulled_creations": sorted({m["creation"] for m in pulled.values() if m.get("creation")}),
            "last_placed": placed[-1] if placed else None,
            "resolve": open_timeline(connect) if resolve else None}


def format_status(s: dict) -> str:
    lines = [f"MAGNIFIC · folder {s['folder']}" + ("" if s["folder_set"] else " (default)")
             + ("" if s["exists"] else " (doesn't exist yet)"),
             f"watcher: {'on (pid %d)' % s['watcher'] if s['watcher'] else 'off'} · new creations → "
             + ("media pool bin Flow › Magnific" if s["place"] == "bin"
                else f"media pool + timeline at the playhead (track {TRACK['video']}, stills {s['still_seconds']:g} s)"),
             f"{s['imported']} imported"
             + (f" · {len(s['waiting'])} waiting" if s["waiting"] else "")
             + (f" · {s['left_alone']} left alone (were there before)" if s["left_alone"] else "")
             + (f" · {len(s['failed'])} skipped" if s["failed"] else "")]
    rv = s.get("resolve")
    if rv:
        lines.append(f"open in Resolve: “{rv['project']}”"
                     + (f" · timeline “{rv['timeline']}” {rv['width']}x{rv['height']}"
                        + (f" ({rv['shape']})" if rv.get("shape") and "x" not in rv["shape"] else "")
                        if rv.get("timeline") else " · no timeline open"))
    for r in s["recent"]:
        pl = r.get("placed")
        lines.append(f"  {r['at']}  {r['file']}" + (f"  → “{pl['timeline']}” at {pl['seconds']:.2f}s" if pl else ""))
    for w in s["waiting"][:5]:
        lines.append(f"  waiting: {w}")
    for f in s["failed"][:5]:
        lines.append(f"  skipped: {f['file']} ({f['why']})")
    return "\n".join(lines)


def format_sync(res: dict) -> str:
    lines = _lines(res)
    n = len(res["waiting"])
    if res.get("blocked"):
        lines.append(f"MAGNIFIC · {n} file(s) wait in {res['folder']} until Resolve can take them")
    elif not res["imported"] and not res["failed"]:
        lines.append("MAGNIFIC · nothing new in " + res["folder"] + (f" ({n} still being written)" if n else ""))
    elif n:
        lines.append(f"MAGNIFIC · {n} more still being written")
    if res.get("baseline"):
        lines.append(f"MAGNIFIC · {res['baseline']} file(s) already in the folder were left alone "
                     "(`flow magnific sync --all` imports them)")
    for n in dict.fromkeys(res.get("notes") or []):
        lines.append(f"MAGNIFIC · {n}")
    return "\n".join(lines)
