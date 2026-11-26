"""Unit tests for Flow's pure logic (no Resolve, no whisper needed).

    python3 -m unittest discover -s tests -v
"""
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
os.environ.setdefault("FLOW_HOME", tempfile.mkdtemp(prefix="flow-test-home-"))

from flow import analyze, compile as fc, luts, notes, styles  # noqa: E402


def W(spec):
    """[(text, start, end), ...] -> word dicts like analyze produces."""
    return [{"i": i, "text": t, "start": s, "end": e, "p": 1.0, "db": -18.0, "ghost": False}
            for i, (t, s, e) in enumerate(spec)]


def make_job(words, plan, fps=30.0, duration=None, extra=None):
    d = Path(tempfile.mkdtemp(prefix="flow-job-"))
    duration = duration or (words[-1]["end"] + 1.0)
    clip = {"path": str(d / "clip.mp4"), "name": "clip", "fps": fps, "duration": duration,
            "width": 1920, "height": 1080, "has_video": True, "range": None}
    analysis = {"clip": clip, "words": words, "fillers": [w["i"] for w in words if w["text"].lower().strip(",.") in ("um", "uh")],
                "candidates": [], "emphasis": [], "color": {"y_avg": 0.4, "y_low": 0.08, "y_high": 0.85,
                                                             "cb": 0.02, "cr": 0.03, "sat_avg": 0.2}}
    if extra:
        analysis.update(extra)
    (d / "analysis.json").write_text(json.dumps(analysis))
    (d / "edit_plan.json").write_text(json.dumps(plan))
    return d


SPEECH = W([("Hey,", 0.10, 0.40), ("welcome", 0.50, 0.90), ("back.", 0.90, 1.30),
            ("The", 3.00, 3.20), ("first", 3.20, 3.50), ("thing.", 3.50, 3.90),
            ("The", 4.60, 4.80), ("first", 4.80, 5.10), ("thing", 5.10, 5.40), ("you", 5.40, 5.55),
            ("need", 5.55, 5.80), ("is", 5.80, 5.95), ("a", 5.95, 6.05), ("plan.", 6.05, 6.50),
            ("Um,", 8.00, 8.30), ("it", 8.70, 8.85), ("really", 8.85, 9.20), ("matters.", 9.20, 9.80)])


class TestAnalyze(unittest.TestCase):
    def test_repeat_and_restart_found(self):
        sents = analyze.split_sentences(SPEECH)
        cands = analyze.merge_candidates(analyze.find_repeats(SPEECH) + analyze.find_restarts(SPEECH, sents))
        self.assertTrue(any(c["words"][0] == 3 and c["words"][1] >= 5 and c["conf"] >= 0.8 for c in cands), cands)

    def test_fillers(self):
        hard, soft = analyze.find_fillers(SPEECH)
        self.assertEqual(hard, [14])

    def test_parallelism_not_flagged(self):
        words = W([("we", 0, .2), ("need", .2, .4), ("to", .4, .5), ("do", .5, .6), ("this", .6, .8),
                   ("and", .8, .9), ("we", .9, 1.0), ("need", 1.0, 1.2), ("to", 1.2, 1.3), ("do", 1.3, 1.4),
                   ("that.", 1.4, 1.7)])
        self.assertEqual(analyze.find_repeats(words), [])

    def test_retake_cue(self):
        words = W([("So", 0, .2), ("the", .2, .3), ("idea", .3, .6), ("sorry,", .9, 1.2), ("let", 1.3, 1.4),
                   ("me", 1.4, 1.5), ("start", 1.5, 1.7), ("over.", 1.7, 2.0), ("The", 2.5, 2.6), ("idea", 2.6, 2.9)])
        c = analyze.find_cues(words, analyze.split_sentences(words))
        self.assertTrue(any(x["words"][1] == 7 for x in c), c)

    def test_silences(self):
        env = [-60.0] * 50 + [-15.0] * 50 + [-60.0] * 30 + [-15.0] * 10
        thr = analyze.silence_threshold(env)
        sil = analyze.find_silences(env, thr, 0.3)
        self.assertEqual(len(sil), 2)
        self.assertAlmostEqual(sil[1][0], 2.0, places=2)


class TestCompile(unittest.TestCase):
    def plan(self, **kw):
        p = {"style": "talking-head", "remove": [{"words": [3, 5], "reason": "restart"}], "hook": False,
             "punch_ins": "auto", "subject": {"x": 0.5, "y": 0.4}, "grade": {"look": None}}
        p.update(kw)
        return p

    def test_cuts_and_frames(self):
        job = make_job(SPEECH, self.plan())
        b = fc.compile_job(job)
        segs = b["segments"]
        kept = [i for s in segs for i in s["words"]]
        self.assertNotIn(3, kept)
        self.assertNotIn(14, kept)  # filler auto-removed
        self.assertEqual(kept, sorted(kept))
        rec = 0
        for s in segs:  # record side is gapless
            self.assertEqual(s["rec_f0"], rec)
            rec += s["rec_len"]
            self.assertGreaterEqual(s["src_f1"] - s["src_f0"], 2)
        self.assertEqual(rec, b["timeline"]["frames"])

    def test_no_audio_overlap_between_segments(self):
        b = fc.compile_job(make_job(SPEECH, self.plan()))
        srcs = sorted((s["src_f0"], s["src_f1"]) for s in b["segments"])
        for (a0, a1), (b0, b1) in zip(srcs, srcs[1:]):
            self.assertLessEqual(a1, b0)

    def test_fps_variants(self):
        for fps in (23.976, 29.97, 30.0, 60.0):
            b = fc.compile_job(make_job(SPEECH, self.plan(), fps=fps))
            for s in b["segments"]:
                self.assertIsInstance(s["src_f0"], int)
            self.assertIn(b["timeline"]["fps_str"], ("23.976", "29.97", "30", "60"))

    def test_word_retiming_monotonic(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(style="clean")))
        starts = [c["f0"] for c in b["captions"]]
        self.assertEqual(starts, sorted(starts))
        for a, c in zip(b["captions"], b["captions"][1:]):
            self.assertLessEqual(a["f1"], c["f0"])

    def test_caption_chunking_respects_max_words(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(style="talking-head-plus")))
        for c in b["captions"]:
            self.assertLessEqual(len(c["text"].split()), 2)
            self.assertEqual(c["text"], c["text"].upper())

    def test_zoom_alternates_on_cuts(self):
        b = fc.compile_job(make_job(SPEECH, self.plan()))
        jumps = [(a["zoom"], s["zoom"]) for a, s in zip(b["segments"], b["segments"][1:]) if not s["contiguous"]]
        self.assertTrue(all(x != y for x, y in jumps), jumps)

    def test_ramp_steps_cover_source(self):
        steps = fc.ramp_steps(90, 300, 5)
        self.assertEqual(sum(s for s, _ in steps), 90)
        speeds = [s / r for s, r in steps]
        self.assertGreater(max(speeds), speeds[0])
        self.assertLess(sum(r for _, r in steps), 90)

    def test_ramp_rejected_over_speech(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(ramps=[{"start": 4.7, "end": 5.6}])))
        self.assertTrue(any("overlaps kept speech" in w for w in b["warnings"]))

    def test_slowmo_and_titles_and_notes(self):
        job = make_job(SPEECH, self.plan(slowmo=[{"start": 6.6, "end": 7.6, "speed": 50}],
                                         titles=[{"kind": "location", "text": "Berlin", "at_src": 4.8},
                                                 {"kind": "end_card", "text": "Bye"}],
                                         notes=[{"id": "N1", "status": "done", "action": "title"}]))
        (job / "notes.json").write_text(json.dumps({"notes": [{"id": "N1", "src_t": 4.8, "text": "title: Berlin"}]}))
        b = fc.compile_job(job)
        slow = [s for s in b["segments"] if s["kind"] == "slowmo"]
        self.assertEqual(len(slow), 1)
        self.assertAlmostEqual(slow[0]["speed_pct"], 50, delta=2)
        self.assertFalse(slow[0]["audio"])
        self.assertEqual(len(b["titles"]), 2)
        end = [t for t in b["titles"] if t["kind"] == "end_card"][0]
        self.assertEqual(end["f1"], b["timeline"]["frames"])
        self.assertTrue(any(m["name"].startswith("✓ N1") and m["color"] == "Green" for m in b["markers"]))

    def test_cold_open_moves_to_front(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(cold_open={"words": [15, 17]})))
        self.assertEqual(b["segments"][0]["kind"], "cold_open")
        self.assertEqual(b["segments"][0]["words"][0], 15)

    def test_effects_split_across_segments(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(effects=[{"type": "glow", "from": 0.0, "to": 99}])))
        with_fx = [s for s in b["segments"] if s["fx"]]
        self.assertEqual(len(with_fx), len(b["segments"]))
        for s in with_fx:
            for f in s["fx"]:
                self.assertTrue(0 <= f["lf0"] < f["lf1"] <= fc.vrange(s)[1])


class TestSplitEdits(unittest.TestCase):
    def plan(self, **kw):
        p = {"style": "talking-head", "remove": [{"words": [3, 5], "reason": "restart"}], "hook": False,
             "punch_ins": "none", "subject": {"x": 0.5, "y": 0.4}, "grade": {"look": None}}
        p.update(kw)
        return p

    def check_layout(self, b):
        segs = b["segments"]
        for a, c in zip(segs, segs[1:]):  # picture track stays gapless and non-overlapping
            va, vl, _, _ = fc.vrange(a)
            self.assertEqual(va + vl, fc.vrange(c)[0])
        for s in segs:
            r0, rl, s0, s1 = fc.vrange(s)
            self.assertGreater(rl, 0)
            self.assertEqual(s1 - s0, rl)  # 100% speed: picture source length matches its record length

    def test_auto_j_cut_at_sentence_start(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(split_edits="auto")))
        js = [s for s in b["segments"] if (s.get("split_in") or {}).get("type") == "J"]
        self.assertTrue(js, b["warnings"])
        self.check_layout(b)
        for s in js:  # sound edit did not move, picture comes later
            self.assertGreater(s["video"]["rec_f0"], s["rec_f0"])
        self.assertEqual(b["stats"]["j_cuts"], len(js))

    def test_handles_never_show_speech(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(split_edits="auto")))
        kept = {i for s in b["segments"] for i in s["words"]}
        for s in b["segments"]:
            if not s.get("video"):
                continue
            v0, v1 = s["video"]["src_f0"] / 30.0, s["video"]["src_f1"] / 30.0
            for w in SPEECH:  # any word visible in the picture must be one this segment speaks
                if w["start"] < v1 - 0.01 and w["end"] > v0 + 0.01:
                    self.assertIn(w["i"], s["words"], (w, s["video"]))

    def test_explicit_l_cut(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(split_edits=[{"at_word": 15, "type": "L", "sec": 0.2}])))
        ls = [s for s in b["segments"] if (s.get("split_in") or {}).get("type") == "L"]
        self.assertEqual(len(ls), 1)
        self.assertLess(ls[0]["video"]["rec_f0"], ls[0]["rec_f0"])
        self.check_layout(b)

    def test_none_keeps_straight_cuts(self):
        b = fc.compile_job(make_job(SPEECH, self.plan(split_edits="none")))
        self.assertFalse(any(s.get("video") for s in b["segments"]))


class TestBroll(unittest.TestCase):
    def job_with_library(self, plan):
        job = make_job(SPEECH, plan, extra={"sentences": [
            {"i": 0, "w0": 0, "w1": 2, "start": 0.1, "end": 1.3, "text": "Hey, welcome back."},
            {"i": 1, "w0": 6, "w1": 13, "start": 4.6, "end": 6.5, "text": "The first thing you need is a plan."},
            {"i": 2, "w0": 14, "w1": 17, "start": 8.0, "end": 9.8, "text": "Um, it really matters."}]})
        lib = {"clips": [
            {"id": "B1", "path": "/tmp/plan.mov", "name": "notebook plan.mov", "duration": 6.0, "fps": 60.0,
             "width": 1920, "height": 1080, "has_audio": True, "keywords": ["notebook", "plan"],
             "description": "hands writing a plan in a notebook", "tags": []},
            {"id": "B2", "path": "/tmp/city.mov", "name": "city.mov", "duration": 1.0, "fps": 30.0,
             "width": 3840, "height": 2160, "has_audio": False, "keywords": ["city"], "description": "", "tags": []}]}
        (job / "broll.json").write_text(json.dumps(lib))
        return job

    def plan(self, **kw):
        p = {"style": "talking-head", "hook": False, "punch_ins": "none", "grade": {"look": None},
             "split_edits": "none"}
        p.update(kw)
        return p

    def test_word_anchored_broll_frames(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll=[
            {"clip": "B1", "from_word": 6, "to_word": 13, "src_in": 1.0}])))
        self.assertEqual(len(b["broll"]), 1)
        e = b["broll"][0]
        cap = next(c for c in b["captions"] if 6 in c["words"])
        self.assertEqual(e["rec_f0"], cap["f0"])
        self.assertEqual(e["src_f0"], 60)  # 1.0 s at the clip's own 60 fps
        self.assertAlmostEqual((e["src_f1"] - e["src_f0"]) / 60.0, e["rec_len"] / 30.0, delta=0.05)
        self.assertTrue(e["fx"] and e["fx"][0]["type"] == "push_in")
        self.assertEqual(b["timeline"]["broll_lanes"], 1)

    def test_short_clip_is_shortened_and_overlaps_resolved(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll=[
            {"clip": "B1", "at": 1.5, "duration": 3.0}, {"clip": "B2", "at": 2.5, "duration": 3.0}])))
        e1, e2 = b["broll"]
        self.assertLessEqual(e1["rec_f0"] + e1["rec_len"], e2["rec_f0"])
        self.assertLessEqual(e2["rec_len"], 30)
        self.assertTrue(any("shortened" in w for w in b["warnings"]))

    def test_auto_matches_by_description(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll="auto")))
        self.assertEqual([e["id"] for e in b["broll"]], ["B1"])

    def test_nat_sound_j_lead(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll=[
            {"clip": "B1", "from_word": 6, "to_word": 10, "src_in": 2.0, "audio": "nat", "j": 0.3}])))
        e = b["broll"][0]
        self.assertEqual(e["nat"]["j"], 9)
        self.assertEqual(e["nat"]["rec_f0"], e["rec_f0"] - 9)
        self.assertEqual(e["nat"]["src_f0"], e["src_f0"] - 18)

    def test_pip_mode(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll=[
            {"clip": "B1", "at": 2.0, "duration": 1.5, "mode": "pip", "pos": [0.5, 0.25], "size": 0.8}])))
        e = b["broll"][0]
        self.assertEqual(e["mode"], "pip")
        self.assertAlmostEqual(e["zoom"], 0.8)
        self.assertGreater(e["tilt"], 0)

    def test_pip_card_stays_inside_frame(self):
        b = fc.compile_job(self.job_with_library(self.plan(broll=[
            {"clip": "B1", "at": 2.0, "duration": 1.5, "mode": "pip", "pos": [0.5, 0.02], "size": 0.9}])))
        e = b["broll"][0]
        card_h = 1080 * (1080 / 1920) * 0.9  # 16:9 card fitted to a 1080-wide frame
        top = 960 - e["tilt"] - card_h / 2
        self.assertGreaterEqual(top, 0)


class TestBrollLibrary(unittest.TestCase):
    def test_tokens_and_label(self):
        from flow import broll
        self.assertEqual(broll.tokens("DJI_0042 Drone over Harbour.mov"), ["drone", "harbour"])
        p = broll.label_image("B12", Path(tempfile.mkdtemp()) / "l.ppm")
        self.assertTrue(p.read_bytes().startswith(b"P6 150 180 255"))


class TestAIShots(unittest.TestCase):
    def restyle_job(self, plan, window=(0.0, 9.9), scale=1.0):
        job = make_job(SPEECH, plan)
        lib = {"clips": [{"id": "R1", "path": "/tmp/r1.mp4", "name": "R1.mp4", "duration": (window[1] - window[0]) * scale,
                          "fps": 30.0, "width": 1920, "height": 1080, "has_audio": False, "keywords": [],
                          "description": "anime restyle", "tags": [], "source": "ai:restyle",
                          "ai": {"kind": "restyle", "window": list(window), "prompt": "anime", "time_scale": scale}}]}
        (job / "broll.json").write_text(json.dumps(lib))
        return job

    def plan(self, **kw):
        p = {"style": "talking-head", "hook": False, "punch_ins": "none", "grade": {"look": None},
             "split_edits": "none"}
        p.update(kw)
        return p

    def test_restyle_follows_every_cut(self):
        b = fc.compile_job(self.restyle_job(self.plan(broll=[{"clip": "R1"}])))
        segs = [s for s in b["segments"] if s["src_f1"] > s["src_f0"]]
        pieces = b["broll"]
        self.assertEqual(len(pieces), len(segs))
        for s, e in zip(segs, pieces):
            r0, rlen, s0, s1 = fc.vrange(s)
            self.assertEqual(e["rec_f0"], r0)
            self.assertEqual(e["rec_len"], rlen)
            self.assertEqual(e["src_f0"], s0)          # window starts at 0 s, same fps
            self.assertTrue(e.get("sync") and e["ai"] == "restyle")
            self.assertEqual((e["zoom"], e["pan"], e["tilt"]), (s["zoom"], s["pan"], s["tilt"]))
            self.assertFalse(e.get("fx"))

    def test_restyle_keeps_punch_in_framing(self):
        b = fc.compile_job(self.restyle_job(self.plan(broll=[{"clip": "R1"}], punch_ins=[{"at_word": 6, "zoom": 1.2}])))
        zooms = {e["zoom"] for e in b["broll"]}
        self.assertIn(1.2, zooms)

    def test_restyle_window_part_and_time_scale(self):
        b = fc.compile_job(self.restyle_job(self.plan(broll=[{"clip": "R1", "src_range": [4.0, 7.0]}]),
                                            window=(2.0, 9.0), scale=1.1))
        first = b["broll"][0]
        # source 4.6 s (first kept frame after 4.0) is (4.6 - 2.0) * 1.1 s into the AI clip
        seg = next(s for s in b["segments"] if s["src_f0"] <= 4.6 * 30 < s["src_f1"])
        expect = (max(seg["src_f0"], 4.0 * 30) / 30.0 - 2.0) * 1.1
        self.assertAlmostEqual(first["src_f0"] / 30.0, expect, delta=0.05)
        self.assertAlmostEqual(first["speed_pct"], 110.0, delta=0.5)

    def test_validate_catches_bad_shots(self):
        from flow import aishots
        clip = {"duration": 20.0, "width": 1920, "height": 1080}
        self.assertEqual(aishots.validate({"id": "R1", "kind": "restyle", "window": [1, 6], "prompt": "x"}, clip), [])
        self.assertTrue(aishots.validate({"id": "R1", "kind": "restyle", "window": [1, 2], "prompt": "x"}, clip))
        self.assertTrue(aishots.validate({"id": "G1", "kind": "restyle", "window": [1, 6], "prompt": "x"}, clip))
        self.assertTrue(aishots.validate({"id": "G1", "kind": "generate", "duration": 40, "prompt": "x"}, clip))
        self.assertTrue(aishots.validate({"id": "R1", "kind": "restyle", "window": [1, 6], "prompt": ""}, clip))

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "needs ffmpeg")
    def test_prepare_and_import_roundtrip(self):
        import subprocess
        from flow import aishots, broll
        job = Path(tempfile.mkdtemp(prefix="flow-ai-"))
        src = job / "src.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=5",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
        (job / "clip.json").write_text(json.dumps({"path": str(src), "duration": 5.0, "fps": 30.0,
                                                    "width": 1280, "height": 720}))
        (job / "aishots_plan.json").write_text(json.dumps({"shots": [
            {"id": "R1", "kind": "restyle", "window": [0.5, 4.0], "prompt": "claymation"},
            {"id": "G1", "kind": "generate", "duration": 5, "prompt": "city at night", "ref_frame": 1.0}]}))
        req = aishots.prepare(job)
        r1 = next(r for r in req["requests"] if r["id"] == "R1")
        self.assertEqual(r1["tool"], "video_modify")
        cut = Path(r1["uploads"][0]["path"])
        from flow.util import ffprobe
        self.assertAlmostEqual(ffprobe(cut)["duration"], 3.5, delta=0.1)
        g1 = next(r for r in req["requests"] if r["id"] == "G1")
        self.assertTrue(Path(g1["uploads"][0]["path"]).exists())
        res = aishots.import_result(job, "R1", file=str(cut))
        self.assertEqual(res["id"], "R1")
        c = broll.find_clip(broll.load_index(job), "R1")
        self.assertEqual(c["ai"]["window"], [0.5, 4.0])
        self.assertEqual(c["ai"]["time_scale"], 1.0)
        # a later B-roll rescan keeps the AI shot
        data = broll.scan(job, files=[], use_resolve=False)
        self.assertTrue(any(x["id"] == "R1" for x in data["clips"]))


class TestLive(unittest.TestCase):
    class FakeTL:
        def __init__(self):
            self.tcs = []
        def SetCurrentTimecode(self, tc):
            self.tcs.append(tc)
            return True

    class FakeResolve:
        def __init__(self):
            self.pages = []
        def OpenPage(self, name):
            self.pages.append(name)
            return True

    def test_disabled_live_does_nothing(self):
        from flow import live
        tl, rs = self.FakeTL(), self.FakeResolve()
        lv = live.Live(False)
        lv.attach(rs, None, tl, 24.0, 86400)
        lv.page("color"); lv.show(10); lv.say("x")
        self.assertEqual(lv.play(0, 48), "none")
        self.assertEqual((tl.tcs, rs.pages), ([], []))

    def test_scrub_plays_in_real_time_and_in_order(self):
        import time as _t
        from flow import live
        from flow.util import tc_to_frames
        tl, rs = self.FakeTL(), self.FakeResolve()
        lv = live.Live(True, pace=0)
        lv.attach(rs, None, tl, 24.0, 86400)
        t0 = _t.time()
        self.assertEqual(lv.play(0, 24), "scrub")
        took = _t.time() - t0
        self.assertAlmostEqual(took, 1.0, delta=0.25)
        frames = [tc_to_frames(tc, 24.0) - 86400 for tc in tl.tcs]
        self.assertEqual(frames, sorted(frames))
        self.assertEqual(frames[0], 0)
        self.assertEqual(rs.pages, ["edit"])

    def test_page_switch_once(self):
        from flow import live
        rs = self.FakeResolve()
        lv = live.Live(True, pace=0)
        lv.attach(rs, None, self.FakeTL(), 24.0, 0)
        lv.page("color"); lv.page("color"); lv.page("edit"); lv.page("nope")
        self.assertEqual(rs.pages, ["color", "edit"])


class TestNodeTree(unittest.TestCase):
    class Graph:
        def __init__(self, item, nodes=1, lut1="", tools=None, stale_after_apply=True):
            self.item, self.n, self.luts, self.labels = item, nodes, {1: lut1} if lut1 else {}, {}
            self.tools = tools or {}
            self.stale = False
            self.stale_after_apply = stale_after_apply
            self.colors = {}
        def _check(self):
            if self.stale:
                raise RuntimeError("stale graph handle")
        def GetNumNodes(self): self._check(); return self.n
        def GetLUT(self, i): self._check(); return self.luts.get(i, "")
        def SetLUT(self, i, path): self._check(); self.luts[i] = path; return True
        def GetToolsInNode(self, i): self._check(); return list(self.tools.get(i, [])) + ([f"LUT: {self.luts[i]}"] if i in self.luts else [])
        def GetNodeLabel(self, i): self._check(); return self.labels.get(i, "")
        def SetNodeLabel(self, i, l): self._check(); self.labels[i] = l; return True
        def SetNodeColor(self, i, c): self._check(); self.colors[i] = c; return True
        def ApplyGradeFromDRX(self, path, mode):
            self._check()
            from flow import nodetree
            fresh = TestNodeTree.Graph(self.item, nodes=6)
            fresh.labels = {i: l for i, l in enumerate(nodetree.LABELS, 1)}
            self.item.graph = fresh
            self.stale = self.stale_after_apply
            return True

    class Item:
        def __init__(self, **kw):
            self.graph = TestNodeTree.Graph(self, **kw)
            self.cdls = []
        def GetNodeGraph(self): return self.graph
        def SetCDL(self, d): self.cdls.append(d); return True
        def GetName(self): return "clip"

    def test_split_cdl(self):
        from flow import nodetree
        bal, exp = nodetree.split_cdl({"slope": [1.1, 1.1, 1.1], "offset": [-0.04, -0.05, -0.01],
                                       "power": [1.06, 1.06, 1.06], "saturation": 0.95})
        self.assertEqual(bal["Slope"], "1 1 1")
        self.assertTrue(bal["Offset"].startswith("-0.0400"))
        self.assertTrue(exp["Slope"].startswith("1.1000") and exp["Offset"] == "0 0 0")
        self.assertEqual(nodetree.split_cdl(None), (None, None))

    def test_never_overwrites_a_real_grade(self):
        from flow import nodetree
        self.assertTrue(nodetree.can_apply(self.Item(nodes=1))[0])
        self.assertTrue(nodetree.can_apply(self.Item(nodes=1, lut1="fix.cube"))[0])
        self.assertFalse(nodetree.can_apply(self.Item(nodes=3))[0])
        self.assertFalse(nodetree.can_apply(self.Item(nodes=1, tools={1: ["Primary Balance"]}))[0])
        self.assertTrue(nodetree.can_apply(self.Item(nodes=3), replace=True)[0])

    @unittest.skipUnless(__import__("flow.nodetree", fromlist=["x"]).available(), "template missing")
    def test_apply_fills_nodes_and_keeps_input_lut(self):
        from flow import nodetree
        it = self.Item(nodes=1, lut1="Gaywerk_fix.cube")
        res = nodetree.apply_tree(it, balance={"Slope": "1 1 1", "Offset": "0.01 0 0", "Power": "1 1 1", "Saturation": 1.0},
                                  look_lut="/x/look.cube", vignette=0.25)
        self.assertTrue(res["ok"], res)
        g = it.GetNodeGraph()
        self.assertEqual(g.luts[1], "Gaywerk_fix.cube")   # the clip's own LUT moved to 01 Input
        self.assertEqual(g.luts[4], "/x/look.cube")
        idx = [c["NodeIndex"] for c in it.cdls]
        self.assertEqual(idx, ["2", "6"])
        self.assertEqual(it.cdls[1]["Slope"], "0.7500 0.7500 0.7500")

    def test_solo_mutes_above_and_restores(self):
        from flow import nodetree

        class TL:
            def __init__(self):
                self.en = {1: True, 2: True, 3: False, 4: True}
            def GetTrackCount(self, kind): return 4
            def GetIsTrackEnabled(self, kind, k): return self.en[k]
            def SetTrackEnable(self, kind, k, v): self.en[k] = v; return True
        tl = TL()
        with nodetree.Solo(tl) as solo:
            solo.upto(1)
            self.assertEqual(tl.en, {1: True, 2: False, 3: False, 4: False})
            solo.upto(2)
            self.assertEqual(tl.en, {1: True, 2: True, 3: False, 4: False})
        self.assertEqual(tl.en, {1: True, 2: True, 3: False, 4: True})


class TestLiveEdit(unittest.TestCase):
    def test_only_flow_timelines(self):
        from flow import liveedit as le
        self.assertTrue(le.is_flow_timeline("Flow · clip · Talking Head+ · v12"))
        self.assertTrue(le.is_flow_timeline("Halloween Gaywerk Aftermovie · Flow v9"))
        self.assertFalse(le.is_flow_timeline("Halloween Gaywerk Aftermovie RC v5"))
        self.assertEqual(le._version_base("Flow · clip · Shorts · v3"), "Flow · clip · Shorts")

    def test_zoom_keeps_subject_like_compile(self):
        from flow import liveedit as le
        fr = {"tw": 1080, "th": 1920, "img_w": 3413.333, "img_h": 1920.0, "base_pan": 0.0, "base_tilt": 0.0,
              "px": 0.0, "py": 192.0}
        self.assertEqual(le.zoom_framing(fr, 1.0), (0.0, 0.0))
        pan, tilt = le.zoom_framing(fr, 1.25)
        self.assertEqual(pan, 0.0)
        self.assertAlmostEqual(tilt, -48.0, places=1)       # 0.25 * 192, clamped inside the zoomed image
        self.assertEqual(le.zoom_framing(None, 1.2, {"zoom": 1.0, "pan": 10.0, "tilt": -5.0}), (12.0, -6.0))

    def test_grade_deltas_add_up_and_undo_cleanly(self):
        from flow import liveedit as le
        st = le.apply_delta(le.NEUTRAL, {"exposure": "0.3", "warmth": 0.2, "saturation": 1.2})
        st = le.apply_delta(st, {"exposure": 0.3, "saturation": 1.1, "vignette": 2})
        self.assertAlmostEqual(st["exposure"], 0.6)
        self.assertAlmostEqual(st["saturation"], 1.32)
        self.assertEqual(st["vignette"], 0.8)
        self.assertEqual(le.apply_delta(st, {"exposure": "=0"})["exposure"], 0.0)
        cd = le.grade_cdls({"slope": [1.1] * 3, "offset": [-0.04, -0.05, -0.01], "power": [1.06] * 3,
                            "saturation": 0.95}, {"exposure": 1.0, "warmth": 1.0, "contrast": 1.0, "saturation": 1.0})
        self.assertTrue(cd["exposure"]["Slope"].startswith("2.2000"))     # one stop up
        r, g, b = (float(x) for x in cd["balance"]["Offset"].split())
        self.assertGreater(r, -0.04)
        self.assertLess(b, -0.01)                                         # warmer: more red, less blue
        neutral = le.grade_cdls(None, le.NEUTRAL)
        self.assertEqual(neutral["vignette"]["Slope"], "1.0000 1.0000 1.0000")

    def test_plan_fold_and_unfold_round_trip(self):
        import copy
        from flow import liveedit as le
        plan = {"grade": {"look": "orange-teal", "intensity": 0.8}, "overrides": {}}
        orig = copy.deepcopy(plan)
        b1 = le.fold_patch(plan, {"overrides": {"captions": {"color": "#FFD400"}}})
        b2 = le.fold_patch(plan, {"grade": {"look": "film", "intensity": 0.6}})
        self.assertEqual(plan["grade"]["look"], "film")
        le.unfold_patch(plan, b2)
        le.unfold_patch(plan, b1)
        self.assertEqual(plan, orig)
        empty = {}
        b = le.fold_patch(empty, {"overrides": {"captions": {"size": 0.09}}})
        le.unfold_patch(empty, json.loads(json.dumps(b)))               # survives the JSON log
        self.assertEqual(empty, {})


class TestLuts(unittest.TestCase):
    def test_identity_roundtrip(self):
        p = Path(tempfile.mkdtemp()) / "id.cube"
        luts.write_cube(p, luts.build_transform(None, 1.0), "id", size=9)
        size, data = luts.read_cube(p)
        self.assertEqual(size, 9)
        self.assertEqual(len(data), 9 ** 3)
        self.assertEqual(data[1], (0.125, 0.0, 0.0))  # red varies fastest
        for r, g, b in data[:50]:
            self.assertTrue(0.0 <= min(r, g, b) and max(r, g, b) <= 1.0)

    def test_looks_clamped_and_distinct(self):
        probe = (0.35, 0.55, 0.75)
        outs = {k: luts.build_transform(k, 1.0)(probe) for k in luts.LOOKS}
        for k, o in outs.items():
            self.assertTrue(all(0.0 <= v <= 1.0 for v in o), k)
        self.assertGreater(len({tuple(round(v, 3) for v in o) for o in outs.values()}), 6)

    def test_intensity_zero_is_identity(self):
        c = (0.2, 0.4, 0.6)
        self.assertEqual(tuple(round(v, 6) for v in luts.build_transform("film", 0.0)(c)), c)

    def test_balance_neutralizes_cast(self):
        cdl = luts.balance_from_stats({"y_avg": 0.4, "y_low": 0.05, "y_high": 0.9, "cb": -0.05, "cr": 0.06,
                                       "sat_avg": 0.2})
        self.assertLess(cdl["offset"][0], cdl["offset"][2])  # warm/red cast -> pull red down vs blue

    def test_aliases(self):
        self.assertEqual(luts.normalize_look("Orange & Teal"), "orange-teal")
        self.assertIsNone(luts.normalize_look("Leave it"))


class TestNightlife(unittest.TestCase):
    @staticmethod
    def hue(c):
        import colorsys
        h, l, s = colorsys.rgb_to_hls(*c)
        return h * 360, s

    def test_skin_never_purple(self):
        tf = luts.nightlife_transform()
        for skin in [(0.78, 0.62, 0.55), (0.72, 0.58, 0.62), (0.6, 0.45, 0.5), (0.85, 0.7, 0.66), (0.55, 0.42, 0.40)]:
            h, s = self.hue(tf(skin))
            self.assertTrue(h < 45 or h > 352, (skin, h))  # orange/red skin line, never violet/magenta
            self.assertGreater(s, 0.15, skin)

    def test_nothing_comes_out_purple(self):
        import colorsys
        for wm in (0.0, 1.0):
            tf = luts.nightlife_transform(warm=wm)
            for hd in range(230, 345, 5):          # every violet/purple/magenta input
                for s in (0.08, 0.2, 0.4, 0.7, 1.0):
                    for l in (0.3, 0.5, 0.7):
                        h, ll, ss = colorsys.rgb_to_hls(*tf(colorsys.hls_to_rgb(hd / 360, l, s)))
                        self.assertFalse(250 <= h * 360 <= 330 and ss > 0.15 and ll > 0.15,
                                         (hd, s, l, wm, h * 360, ss))

    def test_blue_light_goes_teal_not_brighter(self):
        tf = luts.nightlife_transform()
        blue = (0.15, 0.2, 0.85)
        out = tf(blue)
        h, _ = self.hue(out)
        self.assertTrue(170 <= h <= 215, h)
        self.assertLessEqual(luts.luma(*out), luts.luma(*blue) * 1.25)

    def test_keep_green(self):
        g = (0.2, 0.75, 0.35)
        self.assertLess(self.hue(luts.nightlife_transform(keep_green=True)(g))[0], 160)
        self.assertGreater(self.hue(luts.nightlife_transform()(g))[0], 160)

    def test_warm_correction_cools_cast(self):
        warm_light = (0.9, 0.55, 0.2)
        a = luts.nightlife_transform(warm=0.0)(warm_light)
        b = luts.nightlife_transform(warm=1.0)(warm_light)
        self.assertLess(b[0] - b[2], a[0] - a[2])

    def test_soft_teal_soft_orange(self):
        import colorsys
        hard, soft = luts.nightlife_transform(), luts.nightlife_transform(soft=1.0)
        for skin in [(0.78, 0.55, 0.45), (0.75, 0.45, 0.6)]:     # skin stays orange, but gentler
            h, s = self.hue(soft(skin))
            self.assertTrue(h < 45 or h > 352, h)
            self.assertLess(s, self.hue(hard(skin))[1])
            self.assertGreater(s, 0.2)
        for neon in [(0.1, 0.25, 0.95), (0.15, 0.9, 0.2), (0.1, 0.75, 0.8)]:   # blue, green, cyan light
            h, s = self.hue(soft(neon))
            self.assertTrue(160 <= h <= 210, h)                   # -> teal
            self.assertLess(s, 0.5)                               # -> soft
            self.assertLess(s, self.hue(hard(neon))[1])
        for hd in range(230, 345, 5):                             # still nothing purple
            for s in (0.2, 0.5, 1.0):
                h, ll, ss = colorsys.rgb_to_hls(*soft(colorsys.hls_to_rgb(hd / 360, 0.5, s)))
                self.assertFalse(250 <= h * 360 <= 330 and ss > 0.15, (hd, s, h * 360, ss))


class TestStylesAndNotes(unittest.TestCase):
    def test_builtin_styles_resolve(self):
        for row in styles.list_styles():
            st = styles.get_style(row["id"])
            for sec in styles.REQUIRED_SECTIONS:
                self.assertIn(sec, st)

    def test_aliases(self):
        self.assertEqual(styles.match_style_id("YT Shorts"), "shorts")
        self.assertEqual(styles.match_style_id("Simplicity"), "clean")
        self.assertEqual(styles.match_style_id("Talking Head+"), "talking-head-plus")

    def test_custom_style_extends(self):
        p = styles.save_user_style({"name": "Test Loud", "captions": {"size": 0.1}}, base="shorts")
        st = styles.get_style(p.stem)
        self.assertEqual(st["captions"]["size"], 0.1)
        self.assertEqual(st["captions"]["case"], "upper")  # inherited

    def test_classify_notes(self):
        self.assertEqual(notes.classify("title: Berlin 2026"), "title")
        self.assertEqual(notes.classify("slow-mo here"), "slowmo")
        self.assertEqual(notes.classify("lower third: DJ Kasia"), "lower_third")
        self.assertEqual(notes.classify("bad take, cut"), "cut")
        self.assertEqual(notes.classify("something", "Purple"), "cinematic")

    def test_notes_file(self):
        p = Path(tempfile.mkdtemp()) / "notes.md"
        p.write_text("# Notes\n0:12-0:18 slow-mo on the confetti\n1:05 lower third: DJ Kasia\n- cinematic look\n")
        rows = notes.read_notes_file(p)
        self.assertEqual(rows[0]["t"], 12.0)
        self.assertEqual(rows[0]["t_end"], 18.0)
        self.assertEqual(rows[1]["t"], 65.0)
        self.assertIsNone(rows[2]["t"])
        self.assertEqual(rows[2]["text"], "cinematic look")

    def test_placeholder_text(self):
        for t in ("TITLE", "[guest name]", "TODO: write intro", "XXX", "Your title here", "Flow: catchy title"):
            self.assertTrue(notes.PLACEHOLDER_TEXT.match(t) or "todo" in t.lower() or t.lower().startswith("flow:"), t)
        self.assertFalse(notes.PLACEHOLDER_TEXT.match("Welcome to Berlin"))


if __name__ == "__main__":
    unittest.main()
