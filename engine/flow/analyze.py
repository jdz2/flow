"""Pre-edit analysis: silences, fillers, retake candidates, emphasis, color, frames.

Produces, inside the job folder:
  analysis.json         machine-readable facts for `flow compile`
  review.txt            the transcript laid out for Claude's edit pass
  edit_plan.draft.json  a safe starting plan (high-confidence removals only)
  frames/contact.jpg    four stills side by side, for subject position and color
"""
from __future__ import annotations

import math
import re
import wave
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional

from flow.util import FlowError, fmt_time, read_json, run, which, write_json

try:  # audioop is fast C code; removed in Python 3.13, so keep a fallback.
    import audioop  # type: ignore
except Exception:  # pragma: no cover
    audioop = None

WIN = 0.02  # envelope window (s)

HARD_FILLERS = {
    "um", "umm", "uh", "uhh", "uhm", "erm", "er", "ah", "ahh", "eh", "hmm", "hm", "mm", "mmm",
    "mhm", "yyy", "yy", "eee", "ee", "eem", "yhm", "em", "emm",
}
SOFT_FILLERS = {"like", "basically", "actually", "literally", "jakby", "wiesz", "znaczy", "no"}
SOFT_FILLER_PHRASES = [("you", "know"), ("i", "mean"), ("kind", "of"), ("sort", "of")]
LEGIT_DOUBLES = {"very", "so", "really", "no", "yeah", "yes", "ha", "bye", "go", "tak", "nie",
                 "bardzo", "super", "okay", "ok", "hey", "wow", "more", "that", "had"}
BRIDGE_WORDS = HARD_FILLERS | {"sorry", "so", "like", "i", "mean", "wait", "okay", "ok", "no",
                               "actually", "let", "me", "again", "przepraszam", "czekaj", "znaczy",
                               "jeszcze", "raz", "oh", "well", "and"}
RETAKE_CUES = [
    ("let", "me", "start", "over"), ("let", "me", "try", "that", "again"), ("let", "me", "redo"),
    ("start", "over"), ("scratch", "that"), ("one", "more", "time"), ("take", "two"),
    ("cut", "that"), ("try", "again"), ("from", "the", "top"), ("hold", "on"),
    ("jeszcze", "raz"), ("od", "nowa"), ("od", "początku"), ("przepraszam",), ("sorry",),
]
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for", "is", "it", "i",
    "you", "we", "they", "he", "she", "this", "that", "with", "as", "be", "are", "was", "so",
    "if", "my", "your", "our", "me", "do", "just", "have", "has", "can", "will", "not",
    "i'm", "it's", "that's", "there", "what", "when", "then", "than", "about", "from",
}
TERMINAL = re.compile(r"[.!?…]+[\"')\]]*$")


def norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower(), flags=re.UNICODE)


# ---------------------------------------------------------------- audio
def envelope(wav_path: Path) -> List[float]:
    """dBFS per 20 ms window."""
    with wave.open(str(wav_path), "rb") as w:
        sr, sw, ch, n = w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()
        data = w.readframes(n)
    step = max(1, int(sr * WIN)) * sw * ch
    out = []
    if audioop is not None:
        for i in range(0, len(data), step):
            r = audioop.rms(data[i:i + step], sw)
            out.append(20 * math.log10(max(r, 1) / 32768.0))
    else:  # pragma: no cover
        import array
        arr = array.array("h", data)
        hop = step // sw
        for i in range(0, len(arr), hop):
            chunk = arr[i:i + hop]
            r = math.sqrt(sum(x * x for x in chunk) / max(1, len(chunk)))
            out.append(20 * math.log10(max(r, 1) / 32768.0))
    return out


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = min(len(s) - 1, f + 1)
    return s[f] + (s[c] - s[f]) * (k - f)


def silence_threshold(env: List[float]) -> float:
    noise = percentile(env, 0.10)
    speech = percentile(env, 0.90)
    if speech - noise < 10:
        thr = noise + 3.0
    else:
        thr = noise + 0.3 * (speech - noise)
    return max(-60.0, min(-20.0, thr))


def find_silences(env: List[float], thr: float, min_len: float = 0.3) -> List[List[float]]:
    out = []
    start = None
    for i, db in enumerate(env + [0.0]):
        if db < thr and i < len(env):
            if start is None:
                start = i
        elif start is not None:
            if (i - start) * WIN >= min_len:
                out.append([round(start * WIN, 3), round(i * WIN, 3)])
            start = None
    return out


def word_loudness(env: List[float], start: float, end: float) -> float:
    a, b = int(start / WIN), max(int(start / WIN) + 1, int(math.ceil(end / WIN)))
    seg = env[a:b] or [-90.0]
    # energy average, not dB average
    return 10 * math.log10(sum(10 ** (d / 10) for d in seg) / len(seg))


def refine_words(words: list, env: List[float], thr: float, duration: float) -> None:
    """Snap word edges to the audio so cuts land in real silence.

    Whisper's word times are approximate. Speech onsets right after a pause are
    snapped to the end of that pause; a word ends where the next pause begins
    (or where the next word starts). Also flags quiet, low-confidence "ghost" words.
    """
    fine = find_silences(env, thr, min_len=0.10)
    # starts
    for k, w in enumerate(words):
        s = w["start"]
        floor = words[k - 1]["start"] + 0.04 if k else 0.0
        for a, b in fine:
            if a <= s < b:  # estimate fell inside a pause: speech starts when it ends
                s = b - 0.02
                break
        else:
            before = [(a, b) for a, b in fine if b <= s and s - b < 0.3 and a >= floor]
            if before:
                s = before[-1][1] - 0.02
        w["start"] = round(max(s, floor, 0.0), 3)
    # ends
    for k, w in enumerate(words):
        nxt = words[k + 1]["start"] if k + 1 < len(words) else duration
        e = min(nxt, w["start"] + 2.0)
        for a, b in fine:
            if w["start"] + 0.06 < a < e:
                e = a + 0.03
                break
        w["end"] = round(max(w["start"] + 0.05, min(e, nxt if nxt > w["start"] else e)), 3)
    for w in words:
        w["db"] = round(word_loudness(env, w["start"], w["end"]), 1)
        w["ghost"] = bool(w["db"] < thr and w.get("p", 1.0) < 0.5)


# ---------------------------------------------------------------- language structure
def split_sentences(words: list, gap: float = 0.8, max_words: int = 28) -> list:
    sentences = []
    cur: list = []
    for i, w in enumerate(words):
        cur.append(i)
        nxt = words[i + 1] if i + 1 < len(words) else None
        end_here = (
            TERMINAL.search(w["text"]) is not None
            or nxt is None
            or nxt["start"] - w["end"] >= gap
            or len(cur) >= max_words
        )
        if end_here:
            sentences.append({"w0": cur[0], "w1": cur[-1]})
            cur = []
    for k, s in enumerate(sentences):
        s["i"] = k
        s["start"] = words[s["w0"]]["start"]
        s["end"] = words[s["w1"]]["end"]
        s["text"] = " ".join(words[j]["text"] for j in range(s["w0"], s["w1"] + 1))
        s["complete"] = TERMINAL.search(words[s["w1"]]["text"]) is not None
    return sentences


def find_fillers(words: list) -> tuple:
    hard, soft = [], []
    toks = [norm(w["text"]) for w in words]
    for i, t in enumerate(toks):
        if t in HARD_FILLERS:
            hard.append(i)
        elif t in SOFT_FILLERS:
            soft.append(i)
    for i in range(len(toks) - 1):
        if (toks[i], toks[i + 1]) in SOFT_FILLER_PHRASES:
            soft.extend([i, i + 1])
    return hard, sorted(set(soft))


def find_repeats(words: list) -> list:
    """Phrases said twice in a row ("the first thing… the first thing you need")."""
    toks = [norm(w["text"]) for w in words]
    n = len(toks)
    cands = []
    covered_until = -1
    for i in range(n):
        if i <= covered_until or not toks[i]:
            continue
        hit = None
        for k in (6, 5, 4, 3, 2, 1):
            if i + 2 * k > n:
                continue
            seq = toks[i:i + k]
            if not all(seq):
                continue
            if k == 1 and (seq[0] in LEGIT_DOUBLES or seq[0] in HARD_FILLERS):
                continue
            for j in range(i + k, min(n - k, i + k + 6) + 1):
                if toks[j:j + k] != seq:
                    continue
                between = list(range(i + k, j))
                pause = words[j]["start"] - words[j - 1]["end"]
                bridge_ok = all(toks[b] in BRIDGE_WORDS for b in between)
                if not between or bridge_ok or (pause >= 0.35 and len(between) <= 3):
                    if k == 1 and between:
                        continue  # single words with stuff between are usually fine
                    conf = 0.9 if k >= 3 else 0.8 if k == 2 else 0.7
                    if pause >= 0.35 and k >= 2:
                        conf = min(0.95, conf + 0.05)
                    phrase = " ".join(words[x]["text"] for x in range(i, i + k))
                    hit = {"words": [i, j - 1], "kind": "repeat", "conf": round(conf, 2),
                           "reason": f'said twice: "{phrase}"'}
                    break
            if hit:
                break
        if hit:
            cands.append(hit)
            covered_until = hit["words"][1]
    return cands


def find_restarts(words: list, sentences: list) -> list:
    """A sentence abandoned and started again in one of the next two sentences."""
    cands = []
    toks = [norm(w["text"]) for w in words]
    for a in range(len(sentences)):
        A = sentences[a]
        na = [t for t in toks[A["w0"]:A["w1"] + 1] if t and t not in HARD_FILLERS]
        if len(na) < 2:
            continue
        for b in (a + 1, a + 2):
            if b >= len(sentences):
                break
            B = sentences[b]
            nb = [t for t in toks[B["w0"]:B["w1"] + 1] if t and t not in HARD_FILLERS]
            if len(nb) < 2:
                continue
            cp = 0
            while cp < min(len(na), len(nb)) and na[cp] == nb[cp]:
                cp += 1
            ratio = SequenceMatcher(None, na, nb[:len(na) + 2]).ratio()
            conf = 0.0
            if cp >= 3 or (ratio >= 0.75 and len(na) >= 4):
                conf = 0.85 if (not A["complete"] or len(na) < len(nb)) else 0.7
            elif cp == 2 and (not A["complete"] or len(na) <= 4):
                conf = 0.6
            if b == a + 2:
                mid = sentences[a + 1]
                if mid["w1"] - mid["w0"] > 3:
                    conf = 0.0  # too much in between to be a simple retake
                else:
                    conf -= 0.05
            if conf > 0:
                end = B["w0"] - 1
                cands.append({
                    "words": [A["w0"], end], "kind": "restart", "conf": round(conf, 2),
                    "reason": f'restarted: S{A["i"]:02d} is redone in S{B["i"]:02d}',
                })
                break
    return cands


def find_cues(words: list, sentences: list) -> list:
    """Spoken retake cues ("let me start over") remove the attempt before them."""
    toks = [norm(w["text"]) for w in words]
    sent_of = {}
    for s in sentences:
        for j in range(s["w0"], s["w1"] + 1):
            sent_of[j] = s
    cands = []
    for i in range(len(toks)):
        for cue in RETAKE_CUES:
            L = len(cue)
            if tuple(toks[i:i + L]) != cue:
                continue
            s = sent_of[i]
            start = s["w0"]
            if i == s["w0"] and s["i"] > 0:  # cue opens a sentence: drop the previous one
                prev = sentences[s["i"] - 1]
                start = prev["w0"]
            conf = 0.8 if L >= 2 else 0.55
            cands.append({"words": [start, i + L - 1], "kind": "cue", "conf": conf,
                          "reason": f'retake cue "{" ".join(cue)}"'})
            break
    return cands


def merge_candidates(cands: list) -> list:
    cands = sorted(cands, key=lambda c: (c["words"][0], -c["words"][1]))
    merged: list = []
    for c in cands:
        if merged and c["words"][0] <= merged[-1]["words"][1]:
            m = merged[-1]
            m["words"][1] = max(m["words"][1], c["words"][1])
            m["conf"] = max(m["conf"], c["conf"])
            if c["reason"] not in m["reason"]:
                m["reason"] += "; " + c["reason"]
        else:
            merged.append({**c, "words": list(c["words"])})
    return merged


def find_emphasis(words: list, limit_ratio: float = 0.08) -> list:
    pool = [w for w in words if len(norm(w["text"])) >= 3 and norm(w["text"]) not in STOPWORDS
            and not w.get("ghost")]
    if len(pool) < 5:
        return []
    vals = [w["db"] for w in pool]
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals)) or 1.0
    scored = []
    for w in pool:
        z = (w["db"] - mean) / sd
        bonus = 0.8 if w["text"].rstrip(".,?\"'").endswith("!") or w["text"].isupper() else 0.0
        scored.append((z + bonus, w["i"]))
    scored.sort(reverse=True)
    keep = max(1, int(len(words) * limit_ratio))
    return sorted(i for z, i in scored[:keep] if z > 0.9)


# ---------------------------------------------------------------- video
def color_stats(media: Path, color_range: str = "tv") -> dict:
    ffmpeg = which("ffmpeg")
    vf = "fps=1/2,scale=320:-2,format=yuv444p,signalstats,metadata=mode=print:file=-"
    proc = run([ffmpeg, "-nostdin", "-v", "error", "-i", str(media), "-an", "-vf", vf,
                "-f", "null", "-"], check=False)
    keys = {"YAVG": [], "YLOW": [], "YHIGH": [], "UAVG": [], "VAVG": [], "SATAVG": []}
    for line in proc.stdout.splitlines():
        m = re.search(r"lavfi\.signalstats\.(\w+)=([\d.]+)", line)
        if m and m.group(1) in keys:
            keys[m.group(1)].append(float(m.group(2)))
    if not keys["YAVG"]:
        return {}

    def avg(k):
        return sum(keys[k]) / len(keys[k])

    full = color_range in ("pc", "full", "jpeg")
    y0, ys, cs = (0.0, 255.0, 255.0) if full else (16.0, 219.0, 224.0)
    return {
        "y_avg": round((avg("YAVG") - y0) / ys, 4),
        "y_low": round((avg("YLOW") - y0) / ys, 4),
        "y_high": round((avg("YHIGH") - y0) / ys, 4),
        "cb": round((avg("UAVG") - 128.0) / cs, 4),
        "cr": round((avg("VAVG") - 128.0) / cs, 4),
        "sat_avg": round(avg("SATAVG") / 128.0, 4),
        "range": "full" if full else "video",
        "samples": len(keys["YAVG"]),
    }


def media_color_range(media: Path) -> str:
    ffprobe = which("ffprobe")
    out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=color_range,pix_fmt", "-of", "default=nw=1", str(media)], check=False).stdout
    if "color_range=pc" in out or "yuvj" in out:
        return "pc"
    return "tv"


def export_frames(media: Path, duration: float, out_dir: Path) -> list:
    ffmpeg = which("ffmpeg")
    out_dir.mkdir(parents=True, exist_ok=True)
    stills = []
    for k, frac in enumerate((0.12, 0.38, 0.62, 0.88), start=1):
        t = max(0.0, duration * frac)
        p = out_dir / f"frame_{k}.jpg"
        run([ffmpeg, "-nostdin", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(media),
             "-frames:v", "1", "-vf", "scale=-2:540", "-q:v", "4", str(p)], check=False)
        if p.exists():
            stills.append(str(p))
    if len(stills) >= 2:
        contact = out_dir / "contact.jpg"
        inputs = []
        for s in stills:
            inputs += ["-i", s]
        run([ffmpeg, "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex",
             f"hstack=inputs={len(stills)}", "-q:v", "4", str(contact)], check=False)
        if contact.exists():
            stills.insert(0, str(contact))
    return stills


# ---------------------------------------------------------------- review document
def build_review(clip: dict, words: list, sentences: list, cands: list, hard: list, soft: list,
                 emphasis: list, silences: list, stats: dict) -> str:
    flagged_words = set(hard) | set(emphasis)
    cand_by_sentence: dict = {}
    for k, c in enumerate(cands):
        for s in sentences:
            if s["w0"] <= c["words"][1] and c["words"][0] <= s["w1"]:
                cand_by_sentence.setdefault(s["i"], []).append(k)
                for j in range(c["words"][0], c["words"][1] + 1):
                    flagged_words.add(j)
    lines = [
        f"FLOW REVIEW · {Path(clip['path']).name}",
        f"duration {fmt_time(clip['duration'])} · {clip['fps']:.3f} fps · {clip['width']}x{clip['height']}"
        f" · language {stats.get('language', '?')}",
        f"words {len(words)} · speech {stats['speech_time']:.1f}s · silence {stats['silence_time']:.1f}s"
        f" · est. after auto-cut {fmt_time(stats['est_after_cut'])}",
        "",
        "RETAKE / BAD-TAKE CANDIDATES (C#: word range, confidence, reason)",
    ]
    if not cands:
        lines.append("  none detected")
    for k, c in enumerate(cands):
        a, b = c["words"]
        text = " ".join(words[j]["text"] for j in range(a, b + 1))
        auto = " AUTO" if c["conf"] >= 0.8 else ""
        lines.append(f"  C{k}: w{a}-{b} conf {c['conf']:.2f}{auto} · {c['reason']}")
        lines.append(f"      “{text}”")
    lines += ["", f"FILLERS (auto-removed unless kept): {', '.join(f'w{i}' for i in hard) or 'none'}",
              f"SOFT FILLERS (kept unless you remove): {', '.join(f'w{i}' for i in soft) or 'none'}",
              "EMPHASIS (loudness peaks): " + (", ".join("w%d %s" % (i, words[i]["text"]) for i in emphasis) or "none"),
              "", "TRANSCRIPT (S#: time, word range | text). Flagged sentences show word indices.", ""]
    for s in sentences:
        gap_before = s["start"] - (words[s["w0"] - 1]["end"] if s["w0"] > 0 else 0.0)
        if gap_before >= 0.6 and s["w0"] > 0:
            lines.append(f"    … {gap_before:.1f}s pause")
        tag = ""
        if s["i"] in cand_by_sentence:
            tag = "  ⚑ " + ",".join(f"C{k}" for k in cand_by_sentence[s["i"]])
        lines.append(f"S{s['i']:02d} {fmt_time(s['start'])}–{fmt_time(s['end'])} w{s['w0']}-{s['w1']} | {s['text']}{tag}")
        if any(j in flagged_words for j in range(s["w0"], s["w1"] + 1)):
            toks = []
            for j in range(s["w0"], s["w1"] + 1):
                mark = "*" if j in emphasis else "~" if j in hard else ""
                toks.append(f"{j}:{words[j]['text']}{mark}")
            lines.append("      " + " ".join(toks))
    lines += ["", "Legend: * emphasis peak · ~ hard filler · ⚑ retake candidate"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main entry
def analyze(job: Path) -> dict:
    clip = read_json(job / "clip.json")
    tr = read_json(job / "words.json")
    words = tr["words"]
    media = Path(clip["path"])
    wav = job / "audio16k.wav"
    if not wav.exists():
        from flow.transcribe import extract_audio
        extract_audio(media, wav)
    if not words:
        raise FlowError("No speech found in this clip. Flow needs spoken audio to edit.")

    env = envelope(wav)
    thr = silence_threshold(env)
    silences = find_silences(env, thr)
    refine_words(words, env, thr, float(clip["duration"]))

    sentences = split_sentences(words)
    hard, soft = find_fillers(words)
    cands = merge_candidates(find_repeats(words) + find_restarts(words, sentences) + find_cues(words, sentences))
    ghosts = [w["i"] for w in words if w.get("ghost")]
    for g in ghosts:
        cands.append({"words": [g, g], "kind": "ghost", "conf": 0.7,
                      "reason": "very quiet, low-confidence word (possible mis-hear)"})
    cands = merge_candidates(cands)
    emphasis = find_emphasis(words)

    speech_time = sum(w["end"] - w["start"] for w in words)
    silence_time = sum(b - a for a, b in silences)
    removed = set(hard)
    for c in cands:
        if c["conf"] >= 0.8:
            removed.update(range(c["words"][0], c["words"][1] + 1))
    est = 0.0
    prev = None
    for w in words:
        if w["i"] in removed:
            prev = None
            continue
        est += w["end"] - w["start"]
        if prev is not None:
            est += min(0.15, max(0.0, w["start"] - prev["end"]))
        prev = w
    stats = {"speech_time": round(speech_time, 2), "silence_time": round(silence_time, 2),
             "est_after_cut": round(est, 2), "language": tr.get("language", "")}

    color = {}
    frames = []
    if clip.get("has_video", True):
        color = color_stats(media, media_color_range(media))
        frames = export_frames(media, clip["duration"], job / "frames")

    analysis = {
        "clip": clip, "threshold_db": round(thr, 1), "silences": silences, "words": words,
        "sentences": sentences, "fillers": hard, "soft_fillers": soft, "candidates": cands,
        "emphasis": emphasis, "color": color, "frames": frames, "stats": stats,
    }
    write_json(job / "analysis.json", analysis)
    (job / "review.txt").write_text(
        build_review(clip, words, sentences, cands, hard, soft, emphasis, silences, stats), encoding="utf-8")

    draft = {
        "version": 1,
        "style": None,
        "font": None,
        "grade": {},  # empty = the style's look; {"look": null} = leave the color as shot
        "remove": [{"words": c["words"], "reason": c["reason"]} for c in cands if c["conf"] >= 0.8],
        "restore": [],
        "keep_fillers": False,
        "emphasis": emphasis,
        "hook": None,
        "cold_open": None,
        "punch_ins": "auto",
        "ramps": [],
        "speed": None,
        "effects": [],
        "masks": [],
        "subject": {"x": 0.5, "y": 0.4},
        "overrides": {},
        "notes": "",
    }
    write_json(job / "edit_plan.draft.json", draft)
    return analysis
