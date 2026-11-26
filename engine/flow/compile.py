"""Turn analysis + Claude's edit_plan.json + a style into build.json.

Pure computation (no Resolve). All timeline numbers in build.json are frames
relative to the start of the new timeline; source numbers are frames relative
to the first frame of the source clip.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flow import luts
from flow.styles import check_style, deep_merge, get_style
from flow.util import FlowError, fmt_time, read_json, write_json

OVERLAY_FX = {"flash", "letterbox", "vignette", "spotlight", "highlight_box", "circle_reveal", "grain",
              "light_leak"}
IMAGE_FX = {"shake", "glow", "halation", "rgb_split", "blur_in", "blur_bg", "zoom", "push_in"}
ALL_FX = OVERLAY_FX | IMAGE_FX
FX_DEFAULT_DURATION = {"shake": 0.35, "flash": 0.2, "blur_in": 0.5, "rgb_split": 0.3, "glow": 1.5,
                       "halation": 2.0, "zoom": 1.5, "push_in": 2.5, "circle_reveal": 0.6,
                       "highlight_box": 1.5, "light_leak": 1.5, "spotlight": 2.0}
TITLE_KINDS = ("title", "chapter", "lower_third", "location", "end_card", "quote")
NOTE_MARKER_COLORS = {"done": "Green", "needs_user": "Red", "skipped": "Cream"}
TERMINAL = re.compile(r"[.!?…]+[\"')\]]*$")
CLAUSE = re.compile(r"[,;:]+[\"')\]]*$")
DANGLING = {"a", "an", "the", "to", "of", "and", "or", "in", "on", "at", "for", "my", "your", "our",
            "with", "but", "if", "from", "into", "by"}


def norm_word(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


RESOLVE_RATES = [23.976, 24.0, 25.0, 29.97, 30.0, 47.952, 48.0, 50.0, 59.94, 60.0, 72.0, 90.0,
                 95.904, 96.0, 100.0, 119.88, 120.0]


class Warnings(list):
    def add(self, msg: str) -> None:
        if msg not in self:
            self.append(msg)


def resolve_rate_string(fps: float) -> str:
    best = min(RESOLVE_RATES, key=lambda r: abs(r - fps))
    s = f"{best:.3f}".rstrip("0").rstrip(".")
    return s


# ---------------------------------------------------------------- plan helpers
def _range(r, n: int, warn: Warnings, what: str) -> Optional[Tuple[int, int]]:
    try:
        a, b = int(r[0]), int(r[1])
    except Exception:
        warn.add(f"Ignored malformed {what} range: {r!r}")
        return None
    if a > b:
        a, b = b, a
    if b < 0 or a >= n:
        warn.add(f"Ignored {what} range outside the transcript: {r!r}")
        return None
    return max(0, a), min(n - 1, b)


def removed_words(plan: dict, analysis: dict, style: dict, warn: Warnings) -> set:
    words = analysis["words"]
    n = len(words)
    removed: set = set()
    for item in plan.get("remove") or []:
        rng = _range(item.get("words") if isinstance(item, dict) else item, n, warn, "remove")
        if rng:
            removed.update(range(rng[0], rng[1] + 1))
    if style["pacing"].get("remove_fillers", True) and not plan.get("keep_fillers"):
        removed.update(analysis.get("fillers") or [])
    for item in plan.get("restore") or []:
        rng = _range(item.get("words") if isinstance(item, dict) else item, n, warn, "restore")
        if rng:
            removed.difference_update(range(rng[0], rng[1] + 1))
    clip_range = analysis["clip"].get("range")
    if clip_range:
        t0, t1 = clip_range
        removed.update(w["i"] for w in words if w["end"] <= t0 or w["start"] >= t1)
    return removed


# ---------------------------------------------------------------- spans and segments
def build_spans(ids: List[int], words: list, pacing: dict, duration: float) -> List[dict]:
    """Group kept words into continuous source spans (silences and removals become cuts)."""
    max_gap = float(pacing.get("max_gap", 0.25))
    pad_in = float(pacing.get("pad_in", 0.05))
    pad_out = float(pacing.get("pad_out", 0.09))
    spans: List[dict] = []
    cur: Optional[dict] = None
    for i in ids:
        w = words[i]
        if cur is not None and i == cur["ids"][-1] + 1 and w["start"] - words[i - 1]["end"] <= max_gap:
            cur["ids"].append(i)
            continue
        cur = {"ids": [i]}
        spans.append(cur)
    for sp in spans:
        first, last = sp["ids"][0], sp["ids"][-1]
        lower = words[first - 1]["end"] if first > 0 else 0.0
        upper = words[last + 1]["start"] if last + 1 < len(words) else duration
        sp["start"] = max(lower, words[first]["start"] - pad_in, 0.0)
        sp["end"] = min(upper, words[last]["end"] + pad_out, duration)
        if sp["end"] <= sp["start"]:
            sp["end"] = min(duration, words[last]["end"])
    return spans


def assign_zoom(spans: List[dict], words: list, style: dict, plan: dict, emphasis: set,
                warn: Warnings) -> Dict[int, float]:
    """Zoom level for every kept word. Hard punch-ins happen where the level changes."""
    zcfg = style.get("zoom", {})
    explicit = plan.get("punch_ins")
    zoom: Dict[int, float] = {}
    all_ids = [i for sp in spans for i in sp["ids"]]
    for i in all_ids:
        zoom[i] = 1.0
    if explicit in (None, "auto"):
        if zcfg.get("mode", "none") == "none":
            return zoom
        on_cuts = float(zcfg.get("on_cuts", 1.0) or 1.0)
        on_emph = float(zcfg.get("on_emphasis", 0.0) or 0.0)
        every = float(zcfg.get("every", 0.0) or 0.0)
        cap = float(zcfg.get("max", 1.4))
        level_hi = min(cap, on_cuts)
        state = False  # False = wide (1.0), True = punched (on_cuts)
        last_emph_t = -99.0
        for k, sp in enumerate(spans):
            if k > 0:
                state = not state
            seg_start_t = words[sp["ids"][0]]["start"]
            emph_until = -1.0
            for i in sp["ids"]:
                t = words[i]["start"]
                if every > 0 and t - seg_start_t >= every and (sp["end"] - t) > every * 0.4:
                    state = not state
                    seg_start_t = t
                level = level_hi if state else 1.0
                if on_emph > 0 and i in emphasis and t - last_emph_t >= 2.0:
                    emph_until = t + 1.5
                    last_emph_t = t
                if t < emph_until:
                    level = min(cap, on_emph)
                    if TERMINAL.search(words[i]["text"]):
                        emph_until = t  # end the emphasis punch at the sentence end
                zoom[i] = round(level, 4)
        return zoom
    if explicit == "none" or not explicit:
        return zoom
    if isinstance(explicit, list):
        id_set = set(all_ids)
        for p in explicit:
            try:
                a = int(p["at_word"])
            except Exception:
                warn.add(f"Ignored punch-in without at_word: {p!r}")
                continue
            z = float(p.get("zoom", zcfg.get("on_cuts", 1.15) or 1.15))
            b = p.get("until_word")
            if b is None:
                sp = next((s for s in spans if a in s["ids"] or (s["ids"][0] <= a <= s["ids"][-1])), None)
                b = sp["ids"][-1] if sp else a
            for i in range(a, int(b) + 1):
                if i in id_set:
                    zoom[i] = round(z, 4)
        return zoom
    warn.add(f"punch_ins must be 'auto', 'none' or a list; got {explicit!r}")
    return zoom


def split_by_zoom(span: dict, words: list, zoom: Dict[int, float]) -> List[dict]:
    parts = []
    cur_ids = [span["ids"][0]]
    for prev, i in zip(span["ids"], span["ids"][1:]):
        if zoom[i] != zoom[prev]:
            parts.append(cur_ids)
            cur_ids = [i]
        else:
            cur_ids.append(i)
    parts.append(cur_ids)
    out = []
    for k, ids in enumerate(parts):
        if k == 0:
            s = span["start"]
        else:
            a, b = words[parts[k - 1][-1]]["end"], words[ids[0]]["start"]
            s = (a + b) / 2 if b > a else b
        if k == len(parts) - 1:
            e = span["end"]
        else:
            a, b = words[ids[-1]]["end"], words[parts[k + 1][0]]["start"]
            e = (a + b) / 2 if b > a else b
        out.append({"kind": "speech", "ids": ids, "start": s, "end": e, "zoom": zoom[ids[0]]})
    return out


def ramp_steps(src_frames: int, peak_pct: float, steps: int) -> List[Tuple[int, int]]:
    """Split a ramp into (src_frames, rec_frames) steps with a bell-shaped speed curve."""
    steps = max(1, steps)
    peak = max(1.0, peak_pct / 100.0)
    prof = [1.0 + (peak - 1.0) * math.sin(math.pi * (k + 0.5) / steps) for k in range(steps)]
    total = sum(prof)
    raw = [src_frames * p / total for p in prof]
    src = [int(math.floor(x)) for x in raw]
    rem = src_frames - sum(src)
    order = sorted(range(steps), key=lambda k: raw[k] - src[k], reverse=True)
    for k in order[:rem]:
        src[k] += 1
    out = []
    for s, p in zip(src, prof):
        if s <= 0:
            continue
        rec = max(1, int(round(s / p)))
        out.append((s, rec))
    return out


def auto_ramps(spans: List[dict], removed: set, words: list, rcfg: dict) -> List[dict]:
    """Ramp through long natural pauses instead of cutting them (Cinematic)."""
    out = []
    min_gap = float(rcfg.get("min_gap", 1.0))
    for a, b in zip(spans, spans[1:]):
        last, first = a["ids"][-1], b["ids"][0]
        if first != last + 1:
            continue  # removed speech sits in between: that's a real cut, not a pause
        gap = b["start"] - a["end"]
        if gap >= min_gap:
            out.append({"start": a["end"], "end": b["start"], "peak": float(rcfg.get("peak", 300)),
                        "auto": True})
    return out


# ---------------------------------------------------------------- split edits (J / L cuts)
SPLIT_DEFAULTS = {"mode": "none", "type": "J", "max": 0.2, "min_frames": 3, "mid_sentence": "none"}


def vrange(s: dict) -> tuple:
    """(rec_f0, rec_len, src_f0, src_f1) of a segment's PICTURE (differs from its sound at split edits)."""
    v = s.get("video")
    if v:
        return v["rec_f0"], v["rec_len"], v["src_f0"], v["src_f1"]
    return s["rec_f0"], s["rec_len"], s["src_f0"], s["src_f1"]


def apply_split_edits(segments: List[dict], words: list, plan: dict, style: dict, fps: float,
                      duration: float, warn: Warnings) -> int:
    """Offset the picture cut from the sound cut at jump cuts.

    J cut: the next sound starts while the previous picture is still up (picture cut comes later).
    L cut: the previous sound carries over the next picture (picture cut comes earlier).
    The sound edit never moves (captions and rhythm stay exact); the picture borrows `d` frames of
    source "handle". Handles must be free of speech so no one is seen talking to the wrong words.
    """
    cfg = dict(SPLIT_DEFAULTS)
    cfg.update(style.get("split_edits") or {})
    explicit = plan.get("split_edits")
    mode = cfg["mode"] if explicit is None else explicit
    if mode in (False, None, "none", []):
        return 0
    if mode is True:
        mode = "auto"
    spans = sorted((w["start"], w["end"]) for w in words)

    def free(t0: float, t1: float) -> bool:
        if t0 < 0 or t1 > duration:
            return False
        return not any(a < t1 - 0.01 and b > t0 + 0.01 for a, b in spans)

    def speechy(s: dict) -> bool:
        return s.get("audio") and s["kind"] in ("speech", "cold_open")

    cuts = [k for k in range(1, len(segments))
            if not segments[k]["contiguous"] and speechy(segments[k]) and speechy(segments[k - 1])]
    wanted: Dict[int, dict] = {}
    if isinstance(mode, list):
        for e in mode:
            try:
                wi = int(e["at_word"])
            except Exception:
                warn.add(f"Ignored split edit without at_word: {e!r}")
                continue
            # the jump cut right before that word (the nearest one at or before it)
            k = next((k for k in reversed(cuts) if segments[k]["words"] and segments[k]["words"][0] <= wi), None)
            if k is None:
                warn.add(f"Split edit at w{wi}: no jump cut right before that word; skipped")
                continue
            wanted[k] = {"type": str(e.get("type", cfg["type"])).upper(),
                         "frames": int(round(float(e.get("sec", cfg["max"])) * fps))
                         if "frames" not in e else int(e["frames"]), "explicit": True}
    else:
        for k in cuts:
            prev_w = segments[k - 1]["words"][-1] if segments[k - 1]["words"] else None
            starts_sentence = prev_w is None or TERMINAL.search(words[prev_w]["text"]) is not None \
                or segments[k - 1]["kind"] != segments[k]["kind"]
            typ = cfg["type"] if starts_sentence else cfg.get("mid_sentence", "none")
            if typ in (None, "none"):
                continue
            if typ == "alternate":
                typ = "J" if len(wanted) % 2 == 0 else "L"
            wanted[k] = {"type": str(typ).upper(), "frames": int(round(float(cfg["max"]) * fps)),
                         "explicit": False}

    done = 0
    min_f = int(cfg["min_frames"])
    for k, want in sorted(wanted.items()):
        a, b = segments[k - 1], segments[k]
        typ = want["type"] if want["type"] in ("J", "L") else "J"
        av = dict(a.get("video") or {"rec_f0": a["rec_f0"], "rec_len": a["rec_len"],
                                     "src_f0": a["src_f0"], "src_f1": a["src_f1"]})
        bv = dict(b.get("video") or {"rec_f0": b["rec_f0"], "rec_len": b["rec_len"],
                                     "src_f0": b["src_f0"], "src_f1": b["src_f1"]})
        fa = float(a.get("speed_pct", 100.0)) / 100.0
        fb = float(b.get("speed_pct", 100.0)) / 100.0
        got = 0
        for d in range(want["frames"], min_f - 1, -1):
            if typ == "J":  # previous picture runs d frames longer, next picture starts d later
                h0, h1 = av["src_f1"] / fps, (av["src_f1"] + d * fa) / fps
                ok = free(h0, h1) and bv["rec_len"] - d >= max(6, d)
            else:          # next picture starts d earlier from its own lead-in, previous ends d sooner
                h0, h1 = (bv["src_f0"] - d * fb) / fps, bv["src_f0"] / fps
                ok = free(h0, h1) and av["rec_len"] - d >= max(6, d)
            if ok:
                got = d
                break
        if not got:
            if want["explicit"]:
                warn.add(f"{typ} cut at {fmt_time(b['rec_f0'] / fps)}: no silent handle in the source; kept a straight cut")
            continue
        d = got
        if typ == "J":
            av["src_f1"] += int(round(d * fa))
            av["rec_len"] += d
            bv["src_f0"] += int(round(d * fb))
            bv["rec_f0"] += d
            bv["rec_len"] -= d
        else:
            av["src_f1"] -= int(round(d * fa))
            av["rec_len"] -= d
            bv["src_f0"] -= int(round(d * fb))
            bv["rec_f0"] -= d
            bv["rec_len"] += d
        a["video"], b["video"] = av, bv
        a["split_out"] = b["split_in"] = {"type": typ, "frames": d}
        done += 1
    return done


# ---------------------------------------------------------------- captions
_KEEP_CAPS = re.compile(r"^([A-Z](?:\.[A-Z])+\.?|I|I'm|I've|I'll|I'd|I’m|I’ve|I’ll|I’d|[A-Z0-9]{2,}[a-z]?)$")


def _lower_word(w: str) -> str:
    """Lower-case one word, but keep the pronoun I and acronyms (AI, NASA, USB) as written."""
    core = w.strip("\"“”'‘’()[]!?.,;:…—–-")
    return w if core and _KEEP_CAPS.match(core) else w.lower()


def transform_text(text: str, case: str, strip: bool) -> str:
    t = text.strip()
    if strip:
        if not re.search(r"\w\.\w", t):          # leave "U.S." / "e.g." alone
            t = re.sub(r"[.,;:…—–]+([\"')\]]*)$", r"\1", t)
        else:
            t = re.sub(r"[,;:…—–]+([\"')\]]*)$", r"\1", t)
        t = t.strip("\"“”")
    if case == "upper":
        t = t.upper()
    elif case == "lower":
        t = " ".join(_lower_word(w) for w in t.split(" "))
    return t


def chunk_captions(rec_words: List[dict], ccfg: dict, emphasis: set, fps: float,
                   total_frames: int) -> List[dict]:
    max_words = int(ccfg.get("max_words", 3))
    max_chars = int(ccfg.get("max_chars", 20))
    case = ccfg.get("case", "none")
    strip = bool(ccfg.get("strip_punctuation", True))
    highlight = bool(ccfg.get("highlight_emphasis")) and bool(ccfg.get("accent"))
    # an emphasised word gets its own caption, so the accent colour lands on that word only
    isolate = highlight and max_words >= 2
    gap_break = int(round(0.35 * fps))
    chunks: List[dict] = []
    cur: List[dict] = []

    def flush():
        if cur:
            chunks.append({"words": [w["i"] for w in cur],
                           "text": " ".join(transform_text(w["text"], case, strip) for w in cur).strip(),
                           "f0": cur[0]["f0"], "last_f1": cur[-1]["f1"],
                           "accent": highlight and any(w["i"] in emphasis for w in cur)})
            cur.clear()

    carry: List[dict] = []
    for w in rec_words:
        t = transform_text(w["text"], case, strip)
        if not t:
            continue
        if cur:
            prev = cur[-1]
            joined = len(" ".join(transform_text(x["text"], case, strip) for x in cur + [w]))
            hard = (w["f0"] - prev["f1"] > gap_break or TERMINAL.search(prev["text"]) is not None
                    or (CLAUSE.search(prev["text"]) is not None and (len(cur) >= 2 or max_words <= 3)))
            full = len(cur) >= max_words or joined > max_chars
            if isolate and (w["i"] in emphasis or prev["i"] in emphasis):
                hard = True
            if hard or full:
                # don't strand "to / the / a" at the end of a caption: carry it forward
                if full and not hard and len(cur) > 1 and norm_word(prev["text"]) in DANGLING:
                    carry = [cur.pop()]
                flush()
                cur.extend(carry)
                carry = []
        cur.append(w)
    flush()

    hold = int(round(float(ccfg.get("hold", 0.25)) * fps))
    min_frames = int(ccfg.get("min_frames", 6))
    fill = int(round(0.5 * fps))
    for k, c in enumerate(chunks):
        nxt = chunks[k + 1]["f0"] if k + 1 < len(chunks) else total_frames
        end = c["last_f1"] + hold
        if nxt - end < fill:
            end = nxt
        end = max(end, c["f0"] + min_frames)
        end = min(end, nxt, total_frames)
        if end <= c["f0"]:
            end = min(total_frames, c["f0"] + 2)
        c["f1"] = end
        c["handoff"] = end == nxt and k + 1 < len(chunks)   # next caption starts right here: no fade-out
        del c["last_f1"]
    return [c for c in chunks if c["f1"] > c["f0"]]


def wrap_lines(text: str, width: int) -> str:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- effects
def assign_lanes(items: List[dict]) -> int:
    """Greedy interval coloring: give each overlay a lane so none overlap."""
    lanes_end: List[int] = []
    for it in sorted(items, key=lambda x: x["f0"]):
        for k, end in enumerate(lanes_end):
            if it["f0"] >= end:
                it["lane"] = k
                lanes_end[k] = it["f1"]
                break
        else:
            it["lane"] = len(lanes_end)
            lanes_end.append(it["f1"])
    return len(lanes_end)


def fx_entry(kind: str, f0: int, f1: int, params: dict, source: str) -> dict:
    return {"type": kind, "f0": int(f0), "f1": int(f1), "params": params, "source": source}


# ---------------------------------------------------------------- main
def compile_job(job: Path) -> dict:
    analysis = read_json(job / "analysis.json")
    plan_path = job / "edit_plan.json"
    if not plan_path.exists():
        raise FlowError(f"No edit plan yet: write {plan_path} (start from edit_plan.draft.json).")
    plan = read_json(plan_path)
    warn = Warnings()

    clip = analysis["clip"]
    words = analysis["words"]
    n = len(words)
    fps = float(clip["fps"]) or 30.0
    duration = float(clip["duration"])

    if not plan.get("style"):
        raise FlowError("edit_plan.json needs a style (e.g. \"talking-head\").")
    style = get_style(plan["style"])
    style = deep_merge(style, plan.get("overrides") or {})
    check_style(style, f"{plan['style']} + plan overrides")
    font = plan.get("font")
    if font:
        fam, fstyle = (font.get("family"), font.get("style")) if isinstance(font, dict) else (font, None)
        style["captions"]["font"] = fam
        if fstyle:
            style["captions"]["font_style"] = fstyle
        if not (plan.get("overrides") or {}).get("hook", {}).get("font"):
            style["hook"]["font"] = fam

    pacing = dict(style["pacing"])
    speech_speed = float(plan.get("speed") or pacing.get("speed", 1.0) or 1.0)
    speech_speed = max(0.5, min(2.0, speech_speed))
    emphasis = set(int(i) for i in (plan.get("emphasis") or []) if 0 <= int(i) < n)

    removed = removed_words(plan, analysis, style, warn)

    # cold open
    co = plan.get("cold_open") or None
    co_ids: List[int] = []
    co_mode = "move"
    if co:
        rng = _range(co.get("words"), n, warn, "cold_open")
        co_mode = co.get("mode", "move")
        if rng:
            co_ids = [i for i in range(rng[0], rng[1] + 1) if i not in removed]
    co_set = set(co_ids) if co_mode == "move" else set()
    main_ids = [i for i in range(n) if i not in removed and i not in co_set]
    if not main_ids and not co_ids:
        raise FlowError("The edit plan removes every word; nothing left to build.")

    co_spans = build_spans(co_ids, words, pacing, duration) if co_ids else []
    main_spans = build_spans(main_ids, words, pacing, duration)

    zoom = assign_zoom(co_spans + main_spans, words, style, plan, emphasis, warn)
    if co_spans and style.get("zoom", {}).get("mode", "none") != "none":
        lvl = float(style["zoom"].get("on_emphasis") or style["zoom"].get("on_cuts") or 1.1)
        cap = float(style["zoom"].get("max", 1.4))
        for sp in co_spans:
            for i in sp["ids"]:
                zoom[i] = max(zoom[i], round(min(lvl, cap), 4))

    # ramps (non-speech only)
    ramps_cfg = style.get("ramps", {})
    ramps: List[dict] = []
    if ramps_cfg.get("auto"):
        ramps += auto_ramps(main_spans, removed, words, ramps_cfg)
    for r in plan.get("ramps") or []:
        try:
            s, e = float(r["start"]), float(r["end"])
        except Exception:
            warn.add(f"Ignored ramp without start/end seconds: {r!r}")
            continue
        overlap = [w["i"] for w in words if w["i"] not in removed and w["end"] > s + 0.02 and w["start"] < e - 0.02]
        if overlap:
            warn.add(f"Ramp {s:.2f}-{e:.2f}s skipped: it overlaps kept speech (w{overlap[0]}). "
                     "Ramps only run over non-speech moments.")
            continue
        ramps.append({"start": s, "end": e, "peak": float(r.get("peak", ramps_cfg.get("peak", 250))),
                      "auto": False})
    for r in plan.get("slowmo") or []:
        try:
            s, e = float(r["start"]), float(r["end"])
        except Exception:
            warn.add(f"Ignored slowmo without start/end seconds: {r!r}")
            continue
        overlap = [w["i"] for w in words if w["i"] not in removed and w["end"] > s + 0.02 and w["start"] < e - 0.02]
        if overlap:
            warn.add(f"Slow-mo {s:.2f}-{e:.2f}s skipped: it overlaps kept speech (w{overlap[0]}).")
            continue
        ramps.append({"start": s, "end": e, "slowmo": max(5.0, min(99.0, float(r.get("speed", 50)))),
                      "auto": False})
    if ramps:
        # drop main-span padding that would overlap a ramp
        for r in ramps:
            for sp in main_spans:
                if sp["end"] > r["start"] and sp["start"] < r["start"]:
                    sp["end"] = r["start"]
                if sp["start"] < r["end"] and sp["end"] > r["end"]:
                    sp["start"] = r["end"]

    # ordered raw segments
    raw: List[dict] = []
    for sp in co_spans:
        for part in split_by_zoom(sp, words, zoom):
            part["kind"] = "cold_open"
            raw.append(part)
    timeline_items: List[Tuple[float, dict]] = []
    for sp in main_spans:
        for part in split_by_zoom(sp, words, zoom):
            timeline_items.append((part["start"], part))
    for r in ramps:
        if "slowmo" in r:
            timeline_items.append((r["start"], {"kind": "slowmo", "ids": [], "start": r["start"], "end": r["end"],
                                                "zoom": 1.0, "speed": r["slowmo"]}))
        else:
            timeline_items.append((r["start"], {"kind": "ramp", "ids": [], "start": r["start"], "end": r["end"],
                                                "zoom": 1.0, "peak": r["peak"], "auto": r["auto"]}))
    timeline_items.sort(key=lambda x: x[0])
    raw += [p for _, p in timeline_items]

    # quantize to frames and lay out the record side
    def tf(t: float) -> int:
        return int(round(t * fps))

    segments: List[dict] = []
    rec = 0
    prev_src_f1 = None
    for p in raw:
        f0, f1 = tf(p["start"]), tf(p["end"])
        if f1 - f0 < 2:
            if p["kind"] != "ramp" and p["ids"]:
                warn.add(f"Dropped a {f1 - f0}-frame sliver at {fmt_time(p['start'])}")
            continue
        if p["kind"] == "ramp":
            steps = ramp_steps(f1 - f0, p["peak"], int(ramps_cfg.get("steps", 5)))
            s0 = f0
            for k, (sf, rf) in enumerate(steps):
                segments.append({
                    "kind": "ramp", "src_f0": s0, "src_f1": s0 + sf, "rec_f0": rec, "rec_len": rf,
                    "speed_pct": round(100.0 * sf / rf, 3), "zoom": 1.0, "audio": False,
                    "contiguous": prev_src_f1 == s0, "ramp_step": k, "words": [],
                })
                rec += rf
                prev_src_f1 = s0 + sf
                s0 += sf
            continue
        if p["kind"] == "slowmo":
            src_len = f1 - f0
            rec_len = max(2, int(round(src_len * 100.0 / p["speed"])))
            segments.append({
                "kind": "slowmo", "src_f0": f0, "src_f1": f1, "rec_f0": rec, "rec_len": rec_len,
                "speed_pct": round(100.0 * src_len / rec_len, 3), "zoom": 1.0, "audio": False,
                "contiguous": prev_src_f1 == f0, "words": [],
            })
            rec += rec_len
            prev_src_f1 = f1
            continue
        src_len = f1 - f0
        rec_len = max(1, int(round(src_len / speech_speed)))
        segments.append({
            "kind": p["kind"], "src_f0": f0, "src_f1": f1, "rec_f0": rec, "rec_len": rec_len,
            "speed_pct": round(100.0 * src_len / rec_len, 3) if speech_speed != 1.0 else 100.0,
            "zoom": float(p["zoom"]), "audio": True, "contiguous": prev_src_f1 == f0,
            "words": list(p["ids"]),
        })
        rec += rec_len
        prev_src_f1 = f1
    total = rec
    if total <= 0:
        raise FlowError("Nothing left after cutting; check the edit plan.")
    split_count = apply_split_edits(segments, words, plan, style, fps, duration, warn)

    # framing: subject-aware pan for 16:9 -> 9:16 crops, plus zoom-around-subject
    fr = style["framing"]
    tw, th = int(fr.get("width", 1080)), int(fr.get("height", 1920))
    subj = plan.get("subject") or {}
    sx = float(subj.get("x", 0.5))
    sy = float(subj.get("y", 0.42))
    sw, sh = int(clip.get("width") or tw), int(clip.get("height") or th)
    scale = max(tw / sw, th / sh)  # scaleToCrop fill
    img_w, img_h = sw * scale, sh * scale
    max_pan = max(0.0, (img_w - tw) / 2)
    max_tilt = max(0.0, (img_h - th) / 2)
    reframe = fr.get("reframe", "subject")
    base_pan = 0.0
    base_tilt = 0.0
    if reframe in ("subject", "smart"):
        base_pan = max(-max_pan, min(max_pan, (0.5 - sx) * img_w))
        base_tilt = max(-max_tilt, min(max_tilt, (sy - 0.5) * img_h))
    for s in segments:
        z = s["zoom"]
        # keep the subject point steady while zooming in
        px = (sx - 0.5) * img_w + base_pan
        py = (0.5 - sy) * img_h - base_tilt
        pan = base_pan - (z - 1.0) * px
        tilt = base_tilt + (z - 1.0) * -py
        lim_x = max(0.0, (img_w * z - tw) / 2)
        lim_y = max(0.0, (img_h * z - th) / 2)
        s["pan"] = round(max(-lim_x, min(lim_x, pan)), 2)
        s["tilt"] = round(max(-lim_y, min(lim_y, tilt)), 2)

    # re-time kept words onto the record timeline
    rec_words: List[dict] = []
    seg_of_word: Dict[int, dict] = {}
    for s in segments:
        if not s["words"]:
            continue
        factor = s["rec_len"] / float(s["src_f1"] - s["src_f0"])
        for i in s["words"]:
            w = words[i]
            a = max(w["start"] * fps, s["src_f0"])
            b = min(w["end"] * fps, s["src_f1"])
            r0 = s["rec_f0"] + int(round((a - s["src_f0"]) * factor))
            r1 = s["rec_f0"] + int(round((b - s["src_f0"]) * factor))
            r1 = max(r1, r0 + 1)
            rec_words.append({"i": i, "text": w["text"], "f0": r0, "f1": r1})
            seg_of_word[i] = s

    def src_to_rec(t: float) -> Optional[int]:
        """Map a source time (s) to an output frame; times inside cuts snap to the next kept frame."""
        f = t * fps
        best = None
        for s in segments:
            if s["src_f0"] <= f < s["src_f1"]:
                factor = s["rec_len"] / float(s["src_f1"] - s["src_f0"])
                return s["rec_f0"] + int(round((f - s["src_f0"]) * factor))
            if s["src_f0"] >= f and (best is None or s["src_f0"] < best["src_f0"]):
                best = s
        return best["rec_f0"] if best else None

    # captions + hook
    ccfg = style["captions"]
    captions = chunk_captions(rec_words, ccfg, emphasis, fps, total) if ccfg.get("enabled", True) else []
    hcfg = style["hook"]
    hook = None
    phook = plan.get("hook")
    hook_enabled = bool(hcfg.get("enabled")) if phook is None else bool(phook)
    if hook_enabled:
        text = (phook or {}).get("text") if isinstance(phook, dict) else None
        if not text:
            warn.add("Hook is on for this style but the plan has no hook text, so no hook was added.")
        else:
            dur = float((phook or {}).get("duration") or hcfg.get("duration", 2.5))
            t = transform_text(text, hcfg.get("case", "none"), False)
            hook = {"text": wrap_lines(t, int(hcfg.get("max_chars_per_line", 18))), "f0": 0,
                    "f1": min(total, int(round(dur * fps)))}

    # effects & masks
    fx: List[dict] = []
    ecfg = style.get("effects", {})
    mcfg = style.get("masks", {})
    max_pm = float(ecfg.get("max_per_minute", 8) or 8)
    min_spacing = int(round(60.0 / max_pm * fps))

    def word_frame(i: int, end: bool = False) -> Optional[int]:
        for rw in rec_words:
            if rw["i"] == i:
                return rw["f1"] if end else rw["f0"]
        # removed word: use the nearest kept word after it
        for rw in sorted(rec_words, key=lambda x: x["i"]):
            if rw["i"] > i:
                return rw["f0"]
        return None

    for rule in list(ecfg.get("auto") or []) + list(mcfg.get("auto") or []):
        kind, _, where = rule.partition("@")
        if kind not in ALL_FX:
            warn.add(f"Unknown auto effect '{kind}' in style")
            continue
        if where == "all":
            fx.append(fx_entry(kind, 0, total, {"_global": True}, "style"))
        elif where == "start":
            fx.append(fx_entry(kind, 0, min(total, int(round(FX_DEFAULT_DURATION.get(kind, 0.5) * fps))), {}, "style"))
        elif where == "hook_end":
            at = hook["f1"] if hook else (sum(s["rec_len"] for s in segments if s["kind"] == "cold_open") or None)
            if at and at < total:
                d = int(round(FX_DEFAULT_DURATION.get(kind, 0.2) * fps))
                fx.append(fx_entry(kind, max(0, at - d // 2), min(total, at + d - d // 2), {}, "style"))
        elif where == "emphasis":
            last = -10 ** 9
            for i in sorted(emphasis):
                f = word_frame(i)
                if f is None or i in removed or f - last < min_spacing:
                    continue
                d = int(round(FX_DEFAULT_DURATION.get(kind, 0.35) * fps))
                fx.append(fx_entry(kind, f, min(total, f + d), {}, "style"))
                last = f
        else:
            warn.add(f"Unknown auto effect position '@{where}'")

    for e in list(plan.get("effects") or []) + list(plan.get("masks") or []):
        kind = e.get("type")
        if kind not in ALL_FX:
            warn.add(f"Unknown effect type {kind!r}; known: {', '.join(sorted(ALL_FX))}")
            continue
        if "at_src" in e or "from_src" in e:
            f0 = src_to_rec(float(e.get("at_src", e.get("from_src"))))
            if f0 is None:
                warn.add(f"Effect {kind} anchored after the end of the edit was skipped")
                continue
            if "to_src" in e:
                f1 = src_to_rec(float(e["to_src"])) or total
            else:
                f1 = f0 + max(2, int(round(float(e.get("duration", FX_DEFAULT_DURATION.get(kind, 0.5))) * fps)))
        elif "at" in e or "at_word" in e:
            f0 = int(round(float(e["at"]) * fps)) if "at" in e else word_frame(int(e["at_word"]))
            if f0 is None:
                warn.add(f"Effect {kind} at a removed/unknown word was skipped")
                continue
            d = int(round(float(e.get("duration", FX_DEFAULT_DURATION.get(kind, 0.5))) * fps))
            f1 = f0 + max(2, d)
        elif "from_word" in e:
            f0 = word_frame(int(e["from_word"]))
            f1 = word_frame(int(e.get("to_word", e["from_word"])), end=True)
            if f0 is None or f1 is None:
                warn.add(f"Effect {kind} over removed words was skipped")
                continue
        elif "from" in e:
            f0 = int(round(float(e["from"]) * fps))
            f1 = int(round(float(e.get("to", e["from"] + 1.0)) * fps))
        else:
            f0, f1 = 0, total
        f0, f1 = max(0, f0), min(total, f1)
        if f1 <= f0:
            continue
        params = {k: v for k, v in e.items() if k not in ("type", "at", "at_word", "from_word", "to_word",
                                                           "from", "to", "duration", "at_src", "from_src",
                                                           "to_src", "note")}
        params.setdefault("center", [sx, 1.0 - sy])  # Fusion space: y up
        fx.append(fx_entry(kind, f0, f1, params, "plan"))

    for e in fx:
        e["params"].setdefault("center", [sx, 1.0 - sy])
        e["params"].setdefault("intensity", float(ecfg.get("intensity", 1.0) or 1.0))
        if e["type"] == "letterbox":
            e["params"].update({"_w": tw, "_h": th})
        if e["type"] == "push_in":
            e["params"].setdefault("zoom_to", e["params"].get("to", 1.08))

    # titles system (title, chapter, lower_third, location, end_card, quote)
    titles: List[dict] = []
    for t in plan.get("titles") or []:
        kind = t.get("kind", "title")
        if kind not in TITLE_KINDS:
            warn.add(f"Unknown title kind {kind!r}; use one of {', '.join(TITLE_KINDS)}")
            kind = "title"
        if not t.get("text"):
            warn.add(f"Title without text skipped: {t!r}")
            continue
        from flow.captions import TITLE_DEFAULTS
        dur = float(t.get("duration") or TITLE_DEFAULTS[kind].get("duration", 3.0))
        if "at_src" in t:
            f0 = src_to_rec(float(t["at_src"]))
        elif "at_word" in t:
            f0 = word_frame(int(t["at_word"]))
        elif "at" in t:
            f0 = int(round(float(t["at"]) * fps))
        elif kind == "end_card":
            f0 = max(0, total - int(round(dur * fps)))
        else:
            f0 = 0
        if f0 is None:
            warn.add(f"Title {t.get('text')!r} is anchored after the end of the edit; skipped")
            continue
        f1 = min(total, f0 + max(6, int(round(dur * fps))))
        if f1 <= f0:
            continue
        case = t.get("case") or TITLE_DEFAULTS[kind].get("case", "none")
        style_over = {k: t[k] for k in ("font", "font_style", "size", "color", "position", "animation",
                                        "box", "align", "tracking") if k in t}
        titles.append({"kind": kind, "text": transform_text(t["text"], case, False),
                       "subtext": t.get("subtext"), "f0": f0, "f1": f1, "style": style_over,
                       "note": t.get("note")})
    title_lanes = assign_lanes(titles)

    overlays = [e for e in fx if e["type"] in OVERLAY_FX]
    lanes = assign_lanes(overlays)
    image_fx = [e for e in fx if e["type"] in IMAGE_FX]
    for s in segments:
        s["fx"] = []
        va, vl, _, _ = vrange(s)
        a, b = va, va + vl
        for e in image_fx:
            lo, hi = max(a, e["f0"]), min(b, e["f1"])
            if hi > lo:
                s["fx"].append({"type": e["type"], "lf0": lo - a, "lf1": hi - a, "uf0": e["f0"] - a,
                                "uf1": e["f1"] - a, "params": e["params"]})

    # transitions at jump cuts
    tcfg = style.get("transitions", {})
    ttype = tcfg.get("type", "none")
    if ttype not in (None, "none"):
        frames = int(tcfg.get("frames", 4))
        for k, s in enumerate(segments):
            if k == 0 or s["contiguous"] or s["kind"] in ("ramp", "slowmo") or segments[k - 1]["kind"] in ("ramp", "slowmo"):
                continue
            if s.get("split_in"):
                continue  # a J/L cut already smooths this cut
            if ttype == "smooth_cut" and abs(float(s.get("zoom", 1.0)) - float(segments[k - 1].get("zoom", 1.0))) > 1e-3:
                continue  # Smooth Cut morphs between the two frames; across a punch-in it warps the face
            if s["rec_len"] < frames + 2 or segments[k - 1]["rec_len"] < frames + 2:
                continue
            s["transition_in"] = {"type": {"smooth_cut": "Smooth Cut", "dissolve": "Cross Dissolve"}.get(ttype, ttype),
                                  "frames": frames}

    # B-roll
    from flow import broll as broll_mod
    hook_end = hook["f1"] if hook else int(round(float(style.get("broll", {}).get("hook_guard", 1.5)) * fps))
    cut_frames = [vrange(sg)[0] for sg in segments[1:] if not sg["contiguous"]]
    broll_items = broll_mod.compile_broll(
        plan.get("broll"), style.get("broll", {}), broll_mod.load_index(job),
        {"fps": fps, "total": total, "hook_end": hook_end, "word_frame": word_frame, "src_to_rec": src_to_rec,
         "tw": tw, "th": th, "sentences": analysis.get("sentences") or [], "words": words,
         "removed": removed, "cut_frames": cut_frames, "segments": segments, "vrange": vrange}, warn)
    for bi in broll_items:  # the look follows the B-roll; image FX on V1 under it are hidden anyway
        bi.setdefault("fx", [])
    covered_cuts = sum(1 for c in cut_frames if any(bi["rec_f0"] < c < bi["rec_f0"] + bi["rec_len"]
                                                     for bi in broll_items))

    # markers for every AI decision worth reviewing
    markers = []
    for bi in broll_items:
        markers.append({"frame": bi["rec_f0"], "color": "Orange", "name": f"B-roll {bi['id']}",
                        "note": f"{bi['name']} · {bi['rec_len'] / fps:.1f}s · {bi['mode']}"
                                + (f" · {bi['note']}" if bi.get("note") else "")})
    for item in plan.get("remove") or []:
        if not isinstance(item, dict) or not item.get("reason"):
            continue
        rng = _range(item.get("words"), n, warn, "remove")
        if not rng:
            continue
        after = next((rw for rw in sorted(rec_words, key=lambda x: x["i"]) if rw["i"] > rng[1]), None)
        if after is None:
            continue
        text = " ".join(words[j]["text"] for j in range(rng[0], rng[1] + 1))
        markers.append({"frame": after["f0"], "color": "Red", "name": "Flow cut",
                        "note": f"{item['reason']} | removed: {text[:120]}"})
    notes_index = {}
    try:
        notes_index = {nn["id"]: nn for nn in read_json(job / "notes.json").get("notes", [])}
    except Exception:
        notes_index = {}
    for rec_note in plan.get("notes") or []:
        nid = rec_note.get("id")
        status = rec_note.get("status", "done")
        src = notes_index.get(nid, {})
        frame = None
        if src.get("src_t") is not None:
            frame = src_to_rec(float(src["src_t"]))
        if frame is None and rec_note.get("at") is not None:
            frame = int(round(float(rec_note["at"]) * fps))
        if frame is None:
            frame = 0
        label = {"done": "✓", "needs_user": "Flow needs you", "skipped": "Skipped"}.get(status, status)
        markers.append({"frame": max(0, min(total - 1, frame)), "color": NOTE_MARKER_COLORS.get(status, "Sky"),
                        "name": f"{label} {nid}".strip(),
                        "note": f"{rec_note.get('action') or ''} {rec_note.get('reason') or ''} | note: "
                                f"{(src.get('text') or '')[:120]}".strip()})
    if co_spans:
        markers.append({"frame": 0, "color": "Yellow", "name": "Cold open",
                        "note": "Moved to the front: " + " ".join(words[i]["text"] for i in co_ids)[:160]})
    for s in segments:
        if s["kind"] == "ramp" and s.get("ramp_step") == 0:
            markers.append({"frame": s["rec_f0"], "color": "Purple", "name": "Speed ramp", "note": "Flow speed ramp"})
        if s["kind"] == "slowmo":
            markers.append({"frame": s["rec_f0"], "color": "Purple", "name": "Slow-mo",
                            "note": f"Flow slow motion {s['speed_pct']:.0f}%"})

    # grade
    gplan = plan.get("grade") or {}
    gcfg = deep_merge(style.get("grade", {}), gplan)
    look_key = None
    user_lut = gcfg.get("lut")
    if user_lut:
        if not Path(user_lut).expanduser().exists():
            warn.add(f"LUT file not found: {user_lut}")
            user_lut = None
    else:
        try:
            look_key = luts.normalize_look(gcfg.get("look"))
        except ValueError as exc:
            warn.add(str(exc))
    cdl = None
    if gcfg.get("balance", True) and analysis.get("color") and (look_key or user_lut or gcfg.get("look")):
        cdl = luts.balance_from_stats(analysis["color"], float(gcfg.get("balance_strength", 0.6)))
    grade = {"look": look_key, "intensity": float(gcfg.get("intensity", 0.8)), "cdl": cdl,
             "user_lut": str(Path(user_lut).expanduser()) if user_lut else None, "lut_file": None,
             "baked_lut_file": None}
    if look_key:
        lut = job / "grade.cube"
        luts.write_cube(lut, luts.build_transform(look_key, grade["intensity"]),
                        f"Flow {look_key} {int(grade['intensity'] * 100)}")
        grade["lut_file"] = str(lut)
        if cdl:
            baked = job / "grade_baked.cube"
            luts.write_cube(baked, luts.build_transform(look_key, grade["intensity"], cdl),
                            f"Flow {look_key} balanced")
            grade["baked_lut_file"] = str(baked)

    jump_cuts = sum(1 for s in segments[1:] if not s["contiguous"])
    punch_changes = sum(1 for a, b in zip(segments, segments[1:]) if a["zoom"] != b["zoom"])
    stats = {
        "source_duration": round(duration, 2),
        "output_duration": round(total / fps, 2),
        "removed_words": len(removed),
        "retakes_removed": len([r for r in plan.get("remove") or [] if isinstance(r, dict)]),
        "jump_cuts": jump_cuts,
        "segments": len(segments),
        "captions": len(captions),
        "zoom_changes": punch_changes,
        "ramps": len([s for s in segments if s["kind"] == "ramp" and s.get("ramp_step") == 0]),
        "slowmo": len([s for s in segments if s["kind"] == "slowmo"]),
        "effects": len(fx),
        "titles": len(titles),
        "notes_done": len([x for x in plan.get("notes") or [] if x.get("status") == "done"]),
        "notes_open": len([x for x in plan.get("notes") or [] if x.get("status") == "needs_user"]),
        "transitions": len([s for s in segments if s.get("transition_in")]),
        "split_edits": split_count,
        "j_cuts": len([s for s in segments if (s.get("split_in") or {}).get("type") == "J"]),
        "l_cuts": len([s for s in segments if (s.get("split_in") or {}).get("type") == "L"]),
        "broll": len(broll_items),
        "broll_seconds": round(sum(bi["rec_len"] for bi in broll_items) / fps, 2),
        "cuts_hidden_by_broll": covered_cuts,
    }
    build = {
        "version": 1,
        "job": str(job),
        "media": clip["path"],
        "clip_name": clip.get("name") or Path(clip["path"]).stem,
        "resolve_item": clip.get("resolve"),
        "fps": fps,
        "timeline": {"name": f"Flow · {clip.get('name') or Path(clip['path']).stem} · {style['name']}",
                     "width": tw, "height": th, "fps_str": resolve_rate_string(fps),
                     "frames": total, "reframe": reframe, "fx_lanes": lanes, "title_lanes": title_lanes,
                     "broll_lanes": 1 if broll_items else 0},
        "style": {"id": style["id"], "name": style["name"], "captions": ccfg, "hook": hcfg,
                  "titles": style.get("titles", {}),
                  "audio": style.get("audio", {}), "zoom": style.get("zoom", {})},
        "segments": segments,
        "framing": {"tw": tw, "th": th, "img_w": round(img_w, 3), "img_h": round(img_h, 3),
                    "base_pan": round(base_pan, 3), "base_tilt": round(base_tilt, 3),
                    "px": round((sx - 0.5) * img_w + base_pan, 3), "py": round((0.5 - sy) * img_h - base_tilt, 3)},
        "broll": broll_items,
        "captions": captions,
        "hook": hook,
        "overlays": overlays,
        "titles": titles,
        "grade": grade,
        "markers": markers,
        "stats": stats,
        "warnings": list(warn),
    }
    write_json(job / "build.json", build)
    (job / "summary.txt").write_text(summary_text(build), encoding="utf-8")
    return build


def summary_text(b: dict) -> str:
    s = b["stats"]
    g = b["grade"]
    look = g["look"] or ("custom LUT" if g.get("user_lut") else "none")
    lines = [
        f"{b['timeline']['name']}",
        f"{fmt_time(s['source_duration'])} → {fmt_time(s['output_duration'])}"
        f" · {s['jump_cuts']} cuts · {s['removed_words']} words removed ({s['retakes_removed']} retakes/bad takes)",
        f"{s['captions']} captions · {s['zoom_changes']} punch-ins · {s['ramps']} speed ramps"
        f" · {s['effects']} effects/masks · {s.get('titles', 0)} titles · {s['transitions']} transitions",
        f"grade: {look}" + (f" @ {int(g['intensity'] * 100)}%" if g["look"] else "")
        + (" + auto-balance" if g.get("cdl") else ""),
    ]
    if s.get("split_edits"):
        lines.append(f"split edits: {s.get('j_cuts', 0)} J-cuts, {s.get('l_cuts', 0)} L-cuts")
    if s.get("broll"):
        lines.append(f"B-roll: {s['broll']} clips, {s['broll_seconds']:.1f}s"
                     f" ({s.get('cuts_hidden_by_broll', 0)} jump cuts hidden)")
    if b.get("hook"):
        lines.append(f"hook: {b['hook']['text']!r}")
    if s.get("notes_done") or s.get("notes_open"):
        lines.append(f"production notes: {s.get('notes_done', 0)} done, {s.get('notes_open', 0)} need you")
    for w in b.get("warnings", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines) + "\n"
