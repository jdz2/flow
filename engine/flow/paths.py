"""Filesystem locations used by Flow."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_STYLES_DIR = REPO_ROOT / "styles"

FLOW_HOME = Path(os.environ.get("FLOW_HOME", Path.home() / ".flow"))
JOBS_DIR = FLOW_HOME / "jobs"
CACHE_DIR = FLOW_HOME / "cache"
MODELS_DIR = FLOW_HOME / "models"
USER_STYLES_DIR = FLOW_HOME / "styles"
CAPABILITIES_FILE = FLOW_HOME / "capabilities.json"
LATEST_LINK = JOBS_DIR / "latest"

DEFAULT_MODEL = "ggml-large-v3-turbo-q5_0.bin"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" + DEFAULT_MODEL

# Resolve install locations (checked in order).
RESOLVE_APP_CANDIDATES = [
    Path("/Applications/DaVinci Resolve/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve Studio.app"),
    Path("/Applications/DaVinci Resolve Studio/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve.app"),
    Path("/Applications/DaVinci Resolve Studio.app"),
]
RESOLVE_SUPPORT = Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve")
RESOLVE_SCRIPT_API = RESOLVE_SUPPORT / "Developer" / "Scripting"
RESOLVE_LUT_DIR = RESOLVE_SUPPORT / "LUT"
FLOW_LUT_DIR = RESOLVE_LUT_DIR / "Flow"
RESOLVE_USER_SCRIPTS = (
    Path.home() / "Library" / "Application Support" / "Blackmagic Design"
    / "DaVinci Resolve" / "Fusion" / "Scripts"
)


def model_path() -> Path:
    override = os.environ.get("FLOW_WHISPER_MODEL")
    return Path(override) if override else MODELS_DIR / DEFAULT_MODEL


def ensure_dirs() -> None:
    for d in (FLOW_HOME, JOBS_DIR, CACHE_DIR, MODELS_DIR, USER_STYLES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def resolve_job(job: str | None) -> Path:
    """Accept a job id, a job directory path, or None (= latest)."""
    if job is None or job == "latest":
        if not LATEST_LINK.exists():
            raise SystemExit("No Flow job yet. Run `flow find` first.")
        return LATEST_LINK.resolve()
    p = Path(job).expanduser()
    if p.is_dir():
        return p.resolve()
    d = job_dir(job)
    if d.is_dir():
        return d
    raise SystemExit(f"Unknown job: {job}")


def set_latest(job_path: Path) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if LATEST_LINK.is_symlink() or LATEST_LINK.exists():
        LATEST_LINK.unlink()
    LATEST_LINK.symlink_to(job_path)
