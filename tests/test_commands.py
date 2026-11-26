"""status / jobs / srt / render (fake Resolve), friendly CLI errors, zstd fallback, and the plugin's
slash command files. No Resolve, whisper or network needed."""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from flow import paths, render, status  # noqa: E402
from flow.util import FlowError  # noqa: E402

BUILD = {
    "fps": 30.0, "clip_name": "demo",
    "hook": {"text": "Big hook", "f0": 0, "f1": 60},
    "captions": [
        {"words": [0, 1], "text": "HEY THERE", "f0": 2, "f1": 30},
        {"words": [2], "text": "FRIENDS", "f0": 30, "f1": 3700},
        {"words": [], "text": "  ", "f0": 40, "f1": 50},
    ],
    "stats": {"source_duration": 40.0, "output_duration": 30.0, "jump_cuts": 4, "captions": 2,
              "zoom_changes": 3, "effects": 1, "notes_open": 1},
    "style": {"id": "shorts", "name": "Shorts"}, "warnings": ["w"],
}
ANALYSIS = {"words": [{"text": "Hey"}, {"text": "there,"}, {"text": "friends!"}]}


def make_job(root: Path, name="demo-abc", files=("clip.json", "words.json", "analysis.json")) -> Path:
    job = root / "jobs" / name
    job.mkdir(parents=True)
    for f in files:
        data = {"path": "/tmp/demo.mp4", "duration": 40.0} if f == "clip.json" else {}
        if f == "analysis.json":
            data = ANALYSIS
        if f == "build.json":
            data = BUILD
        (job / f).write_text(json.dumps(data))
    return job


class Srt(unittest.TestCase):
    def test_time_format(self):
        self.assertEqual(render.srt_time(0), "00:00:00,000")
        self.assertEqual(render.srt_time(3723.4567), "01:02:03,457")

    def test_captions_use_spoken_words_and_skip_hook_and_blanks(self):
        text = render.captions_srt(BUILD, ANALYSIS["words"])
        cues = text.strip().split("\n\n")
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].splitlines(), ["1", "00:00:00,067 --> 00:00:01,000", "Hey there,"])
        self.assertEqual(cues[1].splitlines()[1], "00:00:01,000 --> 00:02:03,333")
        self.assertNotIn("Big hook", text)

    def test_falls_back_to_caption_text(self):
        self.assertIn("HEY THERE", render.captions_srt(BUILD))

    def test_write_srt(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td), files=("clip.json", "analysis.json", "build.json"))
            res = render.write_srt(job)
            self.assertEqual(res["cues"], 2)
            self.assertTrue(Path(res["file"]).read_text().startswith("1\n"))


class Status(unittest.TestCase):
    def test_next_step_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td))
            st = status.job_status(job)
            self.assertEqual(st["done"], ["find", "transcribe", "analyze"])
            self.assertIn("edit_plan.json", st["next"])
            (job / "edit_plan.json").write_text("{}")
            (job / "build.json").write_text(json.dumps(BUILD))
            st = status.job_status(job)
            self.assertEqual(st["next"], "flow build")
            self.assertEqual(st["edit"]["style"], "Shorts")
            text = status.format_status(st)
            self.assertIn("00:40.0 → 00:30.0", text)
            self.assertIn("1 notes need you", text)

    def test_stale_build_is_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td), files=("clip.json", "words.json", "analysis.json", "edit_plan.json",
                                            "build.json"))
            old = job / "build.json"
            os.utime(old, (1_000_000, 1_000_000))
            st = status.job_status(job)
            self.assertEqual(st["stale"], ["compile"])
            self.assertEqual(st["next"], "flow compile")

    def test_live_edits_counted_for_built_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td), files=("clip.json", "build.json"))
            (job / "report.json").write_text(json.dumps({"timeline": "demo · v2", "ok": True}))
            (job / "live_edits.json").write_text(json.dumps(
                {"demo · v2": {"edits": [{"op": "zoom"}, {"op": "fx", "undone": True}]}}))
            st = status.job_status(job)
            self.assertEqual(st["live_edits"], 1)
            self.assertIn("“demo · v2”", status.format_status(st))

    def test_list_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = make_job(root, "a-1")
            make_job(root, "b-2")
            (root / "jobs" / "latest").symlink_to(a)
            with mock.patch.object(paths, "JOBS_DIR", root / "jobs"), \
                    mock.patch.object(paths, "LATEST_LINK", root / "jobs" / "latest"):
                rows = status.list_jobs()
            self.assertEqual(sorted(r["job"] for r in rows), ["a-1", "b-2"])
            self.assertEqual([r["job"] for r in rows if r["latest"]], ["a-1"])


class FakeTL:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name

    def GetSetting(self, k):
        return {"timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"}.get(k)


class FakeProject:
    def __init__(self, timelines, codecs=None, statuses=None):
        self.tls = [FakeTL(n) for n in timelines]
        self.current = self.tls[0] if self.tls else None
        self.codecs = codecs if codecs is not None else {"H.264": "H264", "H.265": "H265"}
        self.statuses = list(statuses or [{"JobStatus": "Rendering", "CompletionPercentage": 40},
                                          {"JobStatus": "Complete", "CompletionPercentage": 100}])
        self.settings = None
        self.fmt = None
        self.started = None

    def GetTimelineCount(self):
        return len(self.tls)

    def GetTimelineByIndex(self, i):
        return self.tls[i - 1]

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, tl):
        self.current = tl
        return True

    def GetMediaPool(self):
        return None

    def GetName(self):
        return "P"

    def GetRenderCodecs(self, fmt):
        return self.codecs

    def SetCurrentRenderMode(self, m):
        return True

    def SetCurrentRenderFormatAndCodec(self, fmt, codec):
        self.fmt = (fmt, codec)
        return True

    def SetRenderSettings(self, s):
        self.settings = s
        return True

    def AddRenderJob(self):
        return "job-1"

    def StartRendering(self, jid):
        self.started = jid
        return True

    def GetRenderJobStatus(self, jid):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def IsRenderingInProgress(self):
        return False


class FakeResolve:
    def __init__(self, project):
        self.p = project

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.p

    def OpenPage(self, name):
        return True


class Render(unittest.TestCase):
    def test_renders_jobs_timeline_with_srt(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td), files=("clip.json", "analysis.json", "build.json"))
            (job / "report.json").write_text(json.dumps({"timeline": "demo · v2"}))
            proj = FakeProject(["Other", "demo · v2"])
            out = Path(td) / "out"
            res = render.render(job, out_dir=out, resolve=FakeResolve(proj), poll=0, log=lambda *_: None)
            self.assertEqual(res["status"], "Complete")
            self.assertEqual(proj.current.GetName(), "demo · v2")
            self.assertEqual(proj.fmt, ("mp4", "H264"))
            self.assertEqual(proj.settings["TargetDir"], str(out))
            self.assertEqual(proj.settings["CustomName"], "demo - v2")
            self.assertEqual((proj.settings["FormatWidth"], proj.settings["FormatHeight"]), (1080, 1920))
            self.assertTrue(Path(res["srt"]).exists())
            self.assertEqual(json.loads((job / "render.json").read_text())["status"], "Complete")

    def test_prores_codec_matched_by_description(self):
        proj = FakeProject(["T"], codecs={"Apple ProRes 422 HQ": "ProRes422HQ"})
        self.assertEqual(render._pick_codec(proj, "prores"), ("mov", "ProRes422HQ"))

    def test_unavailable_codec_lists_options(self):
        proj = FakeProject(["T"], codecs={"H.264": "H264"})
        with self.assertRaises(FlowError) as cm:
            render._pick_codec(proj, "h265")
        self.assertIn("H.264", str(cm.exception))

    def test_failed_render_raises(self):
        proj = FakeProject(["T"], statuses=[{"JobStatus": "Failed", "Error": "disk full"}])
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FlowError) as cm:
                render.render(None, out_dir=Path(td), resolve=FakeResolve(proj), poll=0, log=lambda *_: None)
        self.assertIn("disk full", str(cm.exception))

    def test_unknown_timeline(self):
        proj = FakeProject(["T"])
        with self.assertRaises(FlowError):
            render.render(None, timeline="Nope", out_dir=Path(tempfile.gettempdir()),
                          resolve=FakeResolve(proj), poll=0)


class CliErrors(unittest.TestCase):
    def run_cli(self, *argv):
        from flow import cli
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = cli.main(list(argv))
        return rc, err.getvalue()

    def test_missing_step_and_bad_json_are_friendly(self):
        with tempfile.TemporaryDirectory() as td:
            job = make_job(Path(td), files=("clip.json",))
            rc, err = self.run_cli("compile", "--job", str(job))
            self.assertEqual(rc, 2)
            self.assertIn("Run `flow analyze` first", err)
            (job / "analysis.json").write_text('{"clip": ')
            rc, err = self.run_cli("compile", "--job", str(job))
            self.assertEqual(rc, 2)
            self.assertIn("not valid JSON", err)
            self.assertNotIn("Traceback", err)


class ZstdFallback(unittest.TestCase):
    def test_python_module_round_trip(self):
        from flow import drx
        zs = drx._zstd_module()
        if zs is None:
            self.skipTest("zstandard module not installed")
        data = os.urandom(64) + b"flow" * 500
        with mock.patch.object(drx, "_zstd_exe", return_value=None):
            packed = drx._zstd(data, False)
            self.assertTrue(packed[4] & 0x20)  # single-segment frame, as Resolve needs
            self.assertEqual(drx._zstd(packed, True), data)


class SlashCommands(unittest.TestCase):
    def test_every_command_has_frontmatter_and_uses_real_engine_commands(self):
        from flow.cli import build_parser
        sub = next(a for a in build_parser()._actions if a.dest == "cmd")
        known = set(sub.choices)
        files = sorted((ROOT / "commands").glob("*.md"))
        self.assertGreaterEqual(len(files), 8)
        for f in files:
            text = f.read_text()
            m = re.match(r"---\n(.*?)\n---\n", text, re.S)
            self.assertTrue(m, f.name)
            self.assertRegex(m.group(1), r"(?m)^description: \S", f.name)
            for cmd in re.findall(r"`flow ([a-z][a-z-]+)", text):
                self.assertIn(cmd, known, f"{f.name} uses unknown `flow {cmd}`")


if __name__ == "__main__":
    unittest.main()
