"""Small shared helpers: subprocess, JSON, media probing, time formatting."""
from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


class FlowError(RuntimeError):
    """An error with a message meant for the user."""


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
        raise FlowError(f"Command failed ({proc.returncode}): {' '.join(cmd[:3])} …\n" + "\n".join(tail))
    return proc


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        p = Path(prefix) / name
        if p.exists():
            return str(p)
    return None


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def parse_rate(rate: str | float | int | None) -> float:
    """'30000/1001' -> 29.97002997, '25' -> 25.0."""
    if rate is None:
        return 0.0
    if isinstance(rate, (int, float)):
        return float(rate)
    rate = str(rate).strip()
    if not rate or rate in ("0/0", "N/A"):
        return 0.0
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return float(rate)


def ffprobe(path: Path) -> dict:
    """Return the media facts Flow needs: fps, size, duration, audio presence."""
    exe = which("ffprobe")
    if not exe:
        raise FlowError("ffprobe not found. Install ffmpeg: brew install ffmpeg")
    out = run([exe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]).stdout
    data = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})
    fps = parse_rate(video.get("avg_frame_rate")) if video else 0.0
    if video and (fps <= 0 or fps > 240):
        fps = parse_rate(video.get("r_frame_rate"))
    width = int(video.get("width", 0)) if video else 0
    height = int(video.get("height", 0)) if video else 0
    rotation = 0
    if video:
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])
        rotation = int(video.get("tags", {}).get("rotate", rotation))
    if abs(rotation) in (90, 270):
        width, height = height, width
    return {
        "path": str(path),
        "duration": float(fmt.get("duration") or (video or {}).get("duration") or 0.0),
        "fps": round(fps, 6),
        "width": width,
        "height": height,
        "has_video": video is not None,
        "has_audio": audio is not None,
        "audio_rate": int(audio.get("sample_rate", 0)) if audio else 0,
        "video_codec": (video or {}).get("codec_name", ""),
    }


def fmt_time(seconds: float) -> str:
    """12.345 -> '00:12.3'; 75.2 -> '01:15.2'."""
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m:02d}:{s:04.1f}"


def frames_to_tc(frame: int, fps: float) -> str:
    """Non-drop-frame timecode for an absolute frame number."""
    base = int(round(fps))
    ff = frame % base
    total_s = frame // base
    return f"{total_s // 3600:02d}:{(total_s // 60) % 60:02d}:{total_s % 60:02d}:{ff:02d}"


def tc_to_frames(tc: str, fps: float) -> int:
    base = int(round(fps))
    parts = tc.replace(";", ":").split(":")
    h, m, s, f = (int(p) for p in parts)
    return ((h * 60 + m) * 60 + s) * base + f
