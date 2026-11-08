"""Locate the clip to edit (in Resolve or by path) and open a Flow job for it."""
from __future__ import annotations

import datetime as _dt
import hashlib
import re
from pathlib import Path
from typing import Any, Optional

from flow import paths
from flow.util import FlowError, ffprobe, write_json


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")[:40] or "clip"


def open_job(media: Path, resolve_info: Optional[dict] = None,
             src_range: Optional[list] = None) -> Path:
    media = media.expanduser().resolve()
    if not media.exists():
        raise FlowError(f"Media file not found: {media}")
    info = ffprobe(media)
    if not info["has_audio"]:
        raise FlowError("This clip has no audio track. Flow cuts by speech, so it needs audio.")
    st = media.stat()
    h = hashlib.sha1(f"{media}|{st.st_size}".encode()).hexdigest()[:6]
    if src_range:
        h += f"-{int(src_range[0] * 10)}-{int(src_range[1] * 10)}"
    job = paths.job_dir(f"{_slug(media.stem)}-{h}")
    job.mkdir(parents=True, exist_ok=True)
    clip = {**info, "name": media.stem, "range": src_range, "resolve": resolve_info,
            "created": _dt.datetime.now().isoformat(timespec="seconds")}
    write_json(job / "clip.json", clip)
    paths.set_latest(job)
    return job


def _file_path(mpi: Any) -> Optional[str]:
    try:
        p = mpi.GetClipProperty("File Path")
        if isinstance(p, dict):
            p = p.get("File Path")
        return p or None
    except Exception:
        return None


def _is_video_item(item: Any) -> bool:
    try:
        return item.GetType() == "video"
    except Exception:
        return item.GetMediaPoolItem() is not None


def from_resolve(resolve: Any) -> tuple:
    """Return (media_path, resolve_info, src_range) for the clip the user means.

    Order: selected timeline clip > clip under the playhead > selected media pool clip.
    """
    from flow.resolve_bridge import current
    project, media_pool, timeline = current(resolve)
    candidates = []
    if timeline is not None:
        try:
            sel = [i for i in (timeline.GetSelectedClips() or []) if _is_video_item(i)]
        except Exception:
            sel = []
        if sel:
            candidates.append(("timeline-selection", sel[0]))
        else:
            item = timeline.GetCurrentVideoItem()
            if item is not None:
                candidates.append(("playhead", item))
    for how, item in candidates:
        mpi = item.GetMediaPoolItem()
        path = _file_path(mpi) if mpi else None
        if not path:
            continue
        fps = float(timeline.GetSetting("timelineFrameRate") or 0) or None
        rng = None
        try:
            s, e = float(item.GetSourceStartTime()), float(item.GetSourceEndTime())
            if e > s:
                rng = [round(s, 3), round(e, 3)]
        except Exception:
            pass
        info = {"how": how, "media_pool_id": mpi.GetUniqueId(), "timeline": timeline.GetName(),
                "item": item.GetName(), "timeline_fps": fps, "project": project.GetName()}
        return path, info, rng
    try:
        pool_sel = media_pool.GetSelectedClips() or []
    except Exception:
        pool_sel = []
    for mpi in pool_sel:
        path = _file_path(mpi)
        if path:
            return path, {"how": "media-pool-selection", "media_pool_id": mpi.GetUniqueId(),
                          "project": project.GetName()}, None
    raise FlowError("Flow couldn't find a clip. Select it in the timeline or media pool "
                    "(or park the playhead on it), or give me the file path.")


def find_media_pool_item(media_pool: Any, path: str, unique_id: Optional[str] = None) -> Any:
    """Search the whole media pool for a clip by unique id or file path."""
    target = str(Path(path).resolve()) if path else None
    stack = [media_pool.GetRootFolder()]
    while stack:
        folder = stack.pop()
        for clip in folder.GetClipList() or []:
            try:
                if unique_id and clip.GetUniqueId() == unique_id:
                    return clip
                fp = _file_path(clip)
                if fp and target and str(Path(fp).resolve()) == target:
                    return clip
            except Exception:
                continue
        stack.extend(folder.GetSubFolderList() or [])
    return None
