"""Local speech-to-text with word timestamps (whisper.cpp)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from flow import paths
from flow.util import FlowError, read_json, run, which, write_json

AUDIO_NAME = "audio16k.wav"
NOISE_TOKENS = re.compile(r"^[\[\(\*♪].*[\]\)\*♪]$")
PUNCT_ONLY = re.compile(r"^[^\w']+$", re.UNICODE)


def whisper_binary() -> str | None:
    return which("whisper-cli") or which("whisper-cpp")


def extract_audio(media: Path, out_wav: Path) -> Path:
    if out_wav.exists() and out_wav.stat().st_mtime >= media.stat().st_mtime:
        return out_wav
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise FlowError("ffmpeg not found. Install it: brew install ffmpeg")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(media), "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)])
    return out_wav


def cache_key(media: Path, model: Path, language: str) -> str:
    st = media.stat()
    raw = f"{media.resolve()}|{st.st_size}|{int(st.st_mtime)}|{model.name}|{language}|v3-dtw"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


DTW_PRESETS = {  # model file fragment -> whisper.cpp --dtw preset
    "large-v3-turbo": "large.v3.turbo", "large-v3": "large.v3", "large-v2": "large.v2",
    "large-v1": "large.v1", "medium.en": "medium.en", "medium": "medium", "small.en": "small.en",
    "small": "small", "base.en": "base.en", "base": "base", "tiny.en": "tiny.en", "tiny": "tiny",
}
DTW_LAG = 0.10  # DTW token times trail the real onset slightly; analyze snaps them to the audio


def dtw_preset(model: Path) -> str | None:
    name = model.name.lower()
    for frag, preset in DTW_PRESETS.items():
        if frag in name:
            return preset
    return None


def _parse_whisper_json(data: dict) -> tuple[list, str]:
    """whisper-cli -ojf output with -ml 1 -sow: one segment per word.

    Word starts come from DTW token times when available (much more accurate
    than segment offsets); ends are provisional and refined in analyze.
    """
    words = []
    for seg in data.get("transcription", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if NOISE_TOKENS.match(text) or text.upper() in ("[BLANK_AUDIO]", "[MUSIC]"):
            continue
        off = seg.get("offsets", {})
        start = float(off.get("from", 0)) / 1000.0
        end = float(off.get("to", 0)) / 1000.0
        toks = [t for t in seg.get("tokens", []) if not str(t.get("text", "")).startswith("[_")]
        probs = [float(t.get("p", 1.0)) for t in toks if "p" in t]
        p = sum(probs) / len(probs) if probs else 1.0
        dtw = [float(t["t_dtw"]) for t in toks if float(t.get("t_dtw", -1)) >= 0
               and not PUNCT_ONLY.match(str(t.get("text", "")).strip() or ".")]
        if PUNCT_ONLY.match(text) and words:
            words[-1]["text"] += text  # glue stray punctuation to the previous word
            continue
        w = {"text": text, "start": start, "end": end, "p": round(p, 3)}
        if dtw:
            w["start"] = max(0.0, dtw[0] / 100.0 - DTW_LAG)
            w["end"] = max(w["start"] + 0.05, dtw[-1] / 100.0 + 0.1)
            w["dtw"] = True
        words.append(w)
    lang = (data.get("result") or {}).get("language") or data.get("params", {}).get("language", "")
    return words, lang


def _sanitize(words: list, duration: float) -> list:
    """Monotonic, non-overlapping, minimum-length word times."""
    out = []
    prev_start = 0.0
    for w in words:
        s = max(w["start"], prev_start)
        if duration:
            s = min(s, max(0.0, duration - 0.05))
        out.append({**w, "start": round(s, 3)})
        prev_start = s + 0.01
    for k, w in enumerate(out):
        nxt = out[k + 1]["start"] if k + 1 < len(out) else (duration or w["end"])
        e = min(max(w["end"], w["start"] + 0.04), max(nxt, w["start"] + 0.04))
        w["end"] = round(e, 3)
        w["i"] = k
    return out


def transcribe(media: Path, job: Path, duration: float, language: str = "auto",
               model: Path | None = None, threads: int | None = None) -> dict:
    model = model or paths.model_path()
    exe = whisper_binary()
    if not exe:
        raise FlowError("whisper.cpp is not installed. Run: brew install whisper-cpp "
                        "(or ./install.sh in the Flow folder).")
    if not model.exists():
        raise FlowError(f"Whisper model missing: {model}\nRun ./install.sh or download {paths.MODEL_URL}")

    key = cache_key(media, model, language)
    cached = paths.CACHE_DIR / key / "words.json"
    if cached.exists():
        result = read_json(cached)
        shutil.copy2(cached, job / "words.json")
        result["cached"] = True
        return result

    wav = extract_audio(media, job / AUDIO_NAME)
    threads = threads or max(4, (os.cpu_count() or 8) - 2)
    with tempfile.TemporaryDirectory(prefix="flow-whisper-") as td:
        out_base = Path(td) / "out"
        cmd = [exe, "-m", str(model), "-f", str(wav), "-l", language, "-t", str(threads),
               "-ml", "1", "-sow", "-ojf", "-of", str(out_base), "-np"]
        preset = dtw_preset(model)
        if preset:  # token-level alignment; needs flash attention off
            cmd += ["-nfa", "-dtw", preset]
        proc = run(cmd, check=False)
        out_json = out_base.with_suffix(".json")
        if proc.returncode != 0 or not out_json.exists():
            tail = (proc.stderr or "").strip().splitlines()[-10:]
            raise FlowError("whisper.cpp failed:\n" + "\n".join(tail))
        raw = json.loads(out_json.read_text(encoding="utf-8", errors="replace"))

    words, lang = _parse_whisper_json(raw)
    words = _sanitize(words, duration)
    result = {"language": lang or language, "model": model.name, "duration": duration,
              "words": words}
    write_json(cached, result)
    write_json(job / "words.json", result)
    result["cached"] = False
    return result
