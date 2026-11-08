"""End-to-end offline pipeline on a synthetic clip: transcribe -> analyze -> notes -> compile.

Needs ffmpeg, macOS `say`, whisper.cpp and the model (skips otherwise). No Resolve required.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from flow import paths  # noqa: E402
from flow.transcribe import whisper_binary  # noqa: E402

READY = bool(shutil.which("ffmpeg") and shutil.which("say") and whisper_binary() and paths.model_path().exists())


@unittest.skipUnless(READY, "needs ffmpeg, say, whisper.cpp and the model")
class TestPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tests"))
        import make_fixture
        cls.clip = ROOT / "tests" / "fixtures" / "flow_test_clip.mp4"
        if not cls.clip.exists():
            make_fixture.main(cls.clip)
        from flow.analyze import analyze
        from flow.find_clip import open_job
        from flow.notes import collect_for_job
        from flow.transcribe import transcribe
        cls.job = open_job(cls.clip)
        clip = json.loads((cls.job / "clip.json").read_text())
        transcribe(cls.clip, cls.job, clip["duration"])
        cls.analysis = analyze(cls.job)
        notes_file = Path(tempfile.mkdtemp()) / "notes.md"
        notes_file.write_text("0:03 location: Berlin · Nov 2026\n0:22.6-0:24.6 slow-mo\n")
        cls.notes = collect_for_job(cls.job, notes_file=str(notes_file), connect=False)

    def test_transcript(self):
        text = " ".join(w["text"] for w in self.analysis["words"]).lower()
        self.assertIn("edit reels in seconds", text)
        self.assertGreater(len(self.analysis["words"]), 45)

    def test_retake_detected(self):
        words = self.analysis["words"]
        hits = [c for c in self.analysis["candidates"] if c["conf"] >= 0.8 and
                " ".join(words[i]["text"] for i in range(c["words"][0], c["words"][1] + 1)).lower().startswith("the first thing")]
        self.assertTrue(hits, self.analysis["candidates"])

    def test_filler_and_silences(self):
        self.assertTrue(any(self.analysis["words"][i]["text"].lower().startswith("um") for i in self.analysis["fillers"]))
        self.assertGreaterEqual(len(self.analysis["silences"]), 5)

    def test_words_snap_to_silence(self):
        sil = self.analysis["silences"]
        for w in self.analysis["words"]:  # no word may start deep inside a detected pause
            for a, b in sil:
                self.assertFalse(a + 0.1 < w["start"] < b - 0.1, (w, a, b))

    def test_notes_and_compile(self):
        self.assertEqual([n["kind"] for n in self.notes["notes"]], ["location", "slowmo"])
        draft = json.loads((self.job / "edit_plan.draft.json").read_text())
        draft.update({"style": "cinematic", "hook": {"text": "Edit in seconds"},
                      "titles": [{"kind": "location", "text": "Berlin · Nov 2026", "at_src": 3.0}],
                      "slowmo": [{"start": 22.6, "end": 24.6, "speed": 50}],
                      "notes": [{"id": "N1", "status": "done"}, {"id": "N2", "status": "done"}]})
        (self.job / "edit_plan.json").write_text(json.dumps(draft))
        from flow.compile import compile_job
        b = compile_job(self.job)
        self.assertLess(b["stats"]["output_duration"], 26.3)
        self.assertEqual(b["titles"][0]["text"], "BERLIN · NOV 2026")
        self.assertTrue(any(s["kind"] == "slowmo" for s in b["segments"]))
        self.assertTrue(b["grade"]["lut_file"])
        self.assertEqual(sum(1 for m in b["markers"] if m["color"] == "Green"), 2)


if __name__ == "__main__":
    unittest.main()
