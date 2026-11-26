"""Magnific watch folder (flow magnific): what counts as new, pull, import into the media pool bin,
placement at the playhead, undo, the watcher loop and the CLI. Resolve is faked; no network.
ffmpeg makes the test media (tests that need it are skipped without it)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

from flow import magnific as mg, paths  # noqa: E402
from flow.util import FlowError, frames_to_tc  # noqa: E402

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def media(path: Path, kind: str, seconds: float = 2.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "video":
        args = ["-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=24:duration={seconds}", "-pix_fmt", "yuv420p"]
    elif kind == "image":
        args = ["-f", "lavfi", "-i", "testsrc2=size=320x180", "-frames:v", "1"]
    else:
        args = ["-f", "lavfi", "-i", f"sine=duration={seconds}"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(path)], check=True)
    return path


# ---------------------------------------------------------------- a small fake Resolve
class FakeMPI:
    def __init__(self, path, uid):
        self.path, self.uid = path, uid
        self.meta, self.third, self.color = {}, {}, None

    def GetClipProperty(self, k=None):
        ext = Path(self.path).suffix.lower()
        d = {"File Path": self.path, "Clip Name": Path(self.path).name,
             "Frames": "" if ext in (".png", ".wav") else "48", "FPS": "" if ext == ".wav" else "24.000"}
        return d if k is None else d.get(k)

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return Path(self.path).name

    def SetClipColor(self, c):
        self.color = c
        return True

    def SetMetadata(self, k, v):
        self.meta[k] = v
        return True

    def SetThirdPartyMetadata(self, k, v):
        self.third[k] = v
        return True


class FakeFolder:
    def __init__(self, name):
        self.name, self.clips, self.subs = name, [], []

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return "bin-" + self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subs)


class FakeItem:
    n = 0

    def __init__(self, mpi, start, dur):
        FakeItem.n += 1
        self.mpi, self.start, self.dur, self.ro, self.uid = mpi, start, dur, 0, f"ti{FakeItem.n}"
        self.color = None

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + self.dur

    def GetDuration(self):
        return self.dur

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.mpi.GetName()

    def GetRightOffset(self):
        return self.ro

    def SetRightOffset(self, v):
        self.dur -= v - self.ro
        self.ro = v
        return True

    def SetClipColor(self, c):
        self.color = c
        return True


class FakeTL:
    def __init__(self, name="My Edit", fps=24.0, start=86400, playhead=48):
        self.name, self.fps, self.start, self.playhead = name, fps, start, playhead
        self.tracks = {"video": [{"name": "Video 1", "items": []}], "audio": [{"name": "Audio 1", "items": []}]}

    def GetName(self):
        return self.name

    def GetSetting(self, k):
        return str(self.fps) if k == "timelineFrameRate" else None

    def GetStartFrame(self):
        return self.start

    def GetCurrentTimecode(self):
        return frames_to_tc(self.start + self.playhead, self.fps)

    def SetCurrentTimecode(self, tc):
        from flow.util import tc_to_frames
        self.playhead = tc_to_frames(tc, self.fps) - self.start
        return True

    def GetTrackCount(self, kind):
        return len(self.tracks[kind])

    def GetTrackName(self, kind, k):
        return self.tracks[kind][k - 1]["name"]

    def SetTrackName(self, kind, k, name):
        self.tracks[kind][k - 1]["name"] = name
        return True

    def AddTrack(self, kind, sub=None):
        self.tracks[kind].append({"name": f"{kind} {len(self.tracks[kind]) + 1}", "items": []})
        return True

    def GetItemListInTrack(self, kind, k):
        return list(self.tracks[kind][k - 1]["items"])

    def DeleteClips(self, items, ripple):
        for t in self.tracks.values():
            for tr in t:
                tr["items"] = [i for i in tr["items"] if i not in items]
        return True


class FakeMP:
    def __init__(self, tl):
        self.root = FakeFolder("Master")
        self.cur, self.tl, self.n, self.imports, self.appends = self.root, tl, 0, [], []

    def GetRootFolder(self):
        return self.root

    def AddSubFolder(self, parent, name):
        f = FakeFolder(name)
        parent.subs.append(f)
        self.cur = f  # like Resolve 21.2 (live test): a bin a script makes is opened in the media pool
        return f

    def GetCurrentFolder(self):
        return self.cur

    def SetCurrentFolder(self, f):
        self.cur = f
        return True

    def ImportMedia(self, files):
        self.imports.append((self.cur.name, list(files)))
        got = []
        for f in files:
            self.n += 1
            it = FakeMPI(f, f"mp{self.n}")
            self.cur.clips.append(it)
            got.append(it)
        return got

    def AppendToTimeline(self, infos):
        out = []
        for info in infos:
            self.appends.append(info)
            kind = "audio" if info.get("mediaType") == 2 else "video"
            it = FakeItem(info["mediaPoolItem"], info["recordFrame"], info["endFrame"] - info["startFrame"])
            self.tl.tracks[kind][info["trackIndex"] - 1]["items"].append(it)
            self.tl.playhead = it.GetEnd() - self.tl.start  # like Resolve: the playhead jumps to the new clip's end
            out.append(it)
        return out


class FakeProject:
    def __init__(self, mp, tl):
        self.mp, self.tl = mp, tl

    def GetMediaPool(self):
        return self.mp

    def GetCurrentTimeline(self):
        return self.tl

    def GetName(self):
        return "Client Job"


class FakeResolve:
    def __init__(self, tl=None):
        self.tl = tl if tl is not None else FakeTL()
        self.mp = FakeMP(self.tl)
        self.project = FakeProject(self.mp, self.tl)

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project


class Base(unittest.TestCase):
    """FLOW_HOME in a temp dir, and a fake clock: sleeping (Flow's settle waits, the media pool UI pauses
    in build._import_into) only moves the clock forward, so the tests run instantly."""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="flow-mg-"))
        self.home = self.td / "home"
        self.folder = self.td / "Magnific"
        self.now = [1000.0]

        def fake_sleep(s):
            self.now[0] += float(s)
        self.patches = [mock.patch.object(paths, "FLOW_HOME", self.home),
                        mock.patch.object(mg, "_clock", lambda: self.now[0]),
                        mock.patch("time.sleep", fake_sleep)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.td, ignore_errors=True)


# ---------------------------------------------------------------- pure parts
class Look(Base):
    def test_only_media_and_no_partial_or_hidden_files(self):
        f = self.folder
        for name in ("a.mov", "b.png", "c.wav", "d.webp", "sub/e.mp4", "notes.txt", ".hidden.mov",
                     "f.mov.crdownload", "g.part", "Safari.download/h.mov", ".flow/i.mov"):
            (f / name).parent.mkdir(parents=True, exist_ok=True)
            (f / name).write_bytes(b"x")
        got = [p.relative_to(f).as_posix() for p in mg.files_in(f)]
        self.assertEqual(got, ["a.mov", "b.png", "c.wav", "d.webp", "sub/e.mp4"])
        self.assertEqual(mg.kind_of(Path("x.MOV")), "video")
        self.assertEqual(mg.kind_of(Path("x.tiff")), "image")
        self.assertEqual(mg.kind_of(Path("x.webp")), "convert")

    def test_new_file_waits_until_it_stops_changing(self):
        p = self.folder / "clip.mov"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"12345")
        st = {"files": {}, "pending": {}}
        ready, waiting = mg.look(self.folder, st, {}, now=100.0)
        self.assertEqual((ready, waiting), ([], [p]))          # first sight: wait
        p.write_bytes(b"1234567890")                            # still growing
        ready, waiting = mg.look(self.folder, st, {}, now=103.0)
        self.assertEqual(ready, [])
        ready, waiting = mg.look(self.folder, st, {}, now=104.0)  # unchanged, but only 1 s
        self.assertEqual(ready, [])
        ready, waiting = mg.look(self.folder, st, {}, now=105.5)
        self.assertEqual(ready, [p])
        st["files"]["clip.mov"] = {"sig": mg.sig(p), "status": "imported"}
        self.assertEqual(mg.look(self.folder, st, {}, now=200.0), ([], []))
        p.unlink()
        mg.look(self.folder, st, {}, now=201.0)
        self.assertEqual(st["pending"], {})

    def test_pulled_file_is_ready_at_once(self):
        p = self.folder / "x.png"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"png")
        ready, _ = mg.look(self.folder, {}, {mg._key(p): {"sig": mg.sig(p)}}, now=1.0)
        self.assertEqual(ready, [p])

    def test_free_spot(self):
        spans = [(0, 100), (150, 200), (300, 400)]
        self.assertEqual(mg.free_spot(spans, 50, 10), 100)    # inside a clip: right after it
        self.assertEqual(mg.free_spot(spans, 100, 50), 100)   # fits exactly in the gap
        self.assertEqual(mg.free_spot(spans, 100, 60), 200)   # too long for the gap
        self.assertEqual(mg.free_spot(spans, 120, 200), 400)
        self.assertEqual(mg.free_spot([], 7, 1000), 7)
        self.assertEqual(mg.free_spot(spans, -5, 10), 100)

    def test_slug_is_plain_ascii(self):
        self.assertEqual(mg._slug("Drohne über Köln, Nacht!"), "Drohne-uber-Koln-Nacht")
        self.assertEqual(mg._slug("   "), "magnific")

    def test_extension_from_link(self):
        self.assertEqual(mg._ext_for("https://x/y/clip.MOV?sig=1", "application/octet-stream", None), ".mov")
        self.assertEqual(mg._ext_for("https://x/y/download", "image/png", None), ".png")
        self.assertEqual(mg._ext_for("https://x/y/download", "application/octet-stream", "shot.wav"), ".wav")
        self.assertEqual(mg._ext_for("https://x/y/download", "application/octet-stream", None), "")


class Settings(Base):
    def test_folder_baseline_and_place(self):
        (self.folder / "old.png").parent.mkdir(parents=True)
        (self.folder / "old.png").write_bytes(b"x")
        res = mg.set_folder(str(self.folder))
        self.assertEqual(res["baseline"], 1)
        self.assertEqual(mg.config()["folder"], str(self.folder.resolve()))
        self.assertEqual(mg.status()["left_alone"], 1)
        self.assertIn("playhead", mg.set_place("playhead", still_seconds=3)["say"])
        self.assertEqual((mg.config()["place"], mg.config()["still_seconds"]), ("playhead", 3))
        with self.assertRaises(FlowError):
            mg.set_place("timeline")
        with self.assertRaises(FlowError):
            mg.set_folder(str(Path.home()))


# ---------------------------------------------------------------- pull
@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class Pull(Base):
    def setUp(self):
        super().setUp()
        mg.set_folder(str(self.folder))
        self.src = self.td / "src"

    def test_pull_file_keeps_what_made_it(self):
        res = mg.pull(file=str(media(self.src / "render.mp4", "video")), name="Drone über Berlin",
                      creation="c-1", prompt="drone over berlin at night", model="kling-3")
        f = Path(res["file"])
        self.assertTrue(f.exists() and f.parent == self.folder.resolve())
        self.assertTrue(f.name.endswith("_Drone-uber-Berlin.mp4"))
        self.assertEqual(res["kind"], "video")
        meta = json.loads((self.home / "magnific" / "pulled.json").read_text())[mg._key(f)]
        self.assertEqual((meta["creation"], meta["model"], meta["prompt"]), ("c-1", "kling-3", "drone over berlin at night"))
        self.assertEqual(meta["sig"], mg.sig(f))
        self.assertEqual([p.name for p in self.folder.iterdir()], [f.name])   # no .part left behind
        again = mg.pull(file=str(self.src / "render.mp4"), creation="c-1")
        self.assertTrue(again["skipped"])
        self.assertEqual(mg.status()["pulled_creations"], ["c-1"])

    def test_pull_url_names_the_file_from_what_it_is(self):
        png = media(self.src / "x.png", "image").read_bytes()

        class Resp(io.BytesIO):
            headers = mock.Mock()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        Resp.headers.get = lambda k, d=None: "application/octet-stream" if k == "Content-Type" else d
        Resp.headers.get_filename = lambda: None
        with mock.patch.object(mg.urllib.request, "urlopen", lambda req, timeout=0: Resp(png)):
            res = mg.pull(url="https://cdn.example/abc/download", name="Poster")
        self.assertTrue(res["file"].endswith("_Poster.png"))
        self.assertEqual(res["kind"], "image")

    def test_pull_refuses_bad_input(self):
        with self.assertRaises(FlowError):
            mg.pull(url="http://insecure.example/x.mov")
        bad = self.src / "page.mov"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("<html>expired</html>")
        with self.assertRaises(FlowError):
            mg.pull(file=str(bad))
        with self.assertRaises(FlowError):
            mg.pull(file=str(media(self.src / "s.wav", "audio")), kind="video")
        self.assertEqual(list(self.folder.iterdir()), [])


# ---------------------------------------------------------------- folder → Resolve
@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class SyncToResolve(Base):
    def setUp(self):
        super().setUp()
        media(self.folder / "before.png", "image")
        mg.set_folder(str(self.folder))
        self.rs = FakeResolve()
        self.connect = lambda: self.rs

    def test_imports_into_flow_magnific_bin_with_metadata(self):
        self.rs.mp.cur = viewing = FakeFolder("Selects")
        res_pull = mg.pull(file=str(media(self.td / "g.mp4", "video")), name="City", creation="c-9",
                           prompt="neon city in the rain", model="veo-3")
        media(self.folder / "from-browser.png", "image")
        res = mg.sync(connect=self.connect)
        names = sorted(Path(i["file"]).name for i in res["imported"])
        self.assertEqual(names, sorted([Path(res_pull["file"]).name, "from-browser.png"]))
        self.assertNotIn("before.png", str(self.rs.mp.imports))             # baseline left alone
        flow = next(f for f in self.rs.mp.root.subs if f.name == "Flow")
        bin_ = next(f for f in flow.subs if f.name == "Magnific")
        self.assertEqual(len(bin_.clips), 2)
        self.assertEqual({x[0] for x in self.rs.mp.imports}, {"Magnific"})
        self.assertIs(self.rs.mp.cur, viewing)                               # the user's bin view is back
        city = next(c for c in bin_.clips if c.path == res_pull["file"])
        self.assertEqual(city.color, "Purple")
        self.assertEqual(city.meta["Comments"], "neon city in the rain")
        self.assertEqual(city.meta["Keywords"], "Magnific, veo-3")
        self.assertEqual(city.meta["Description"], "City")
        self.assertEqual(city.third["Magnific Creation"], "c-9")
        self.assertEqual(self.rs.mp.appends, [])                             # bin mode: timeline untouched
        self.assertEqual(mg.sync(connect=self.connect)["imported"], [])      # nothing twice
        self.assertEqual(mg.status()["imported"], 2)

    def test_playhead_places_on_own_track_never_over_clips_and_undo(self):
        mg.set_place("playhead", still_seconds=2)
        tl = self.rs.tl
        a = mg.pull(file=str(media(self.td / "a.mp4", "video")), name="A")
        b = mg.pull(file=str(media(self.td / "b.png", "image")), name="B")
        c = mg.pull(file=str(media(self.td / "c.wav", "audio")), name="C")
        res = mg.sync(connect=self.connect)
        self.assertEqual(len(res["placed"]), 3)
        self.assertEqual(tl.playhead, 48)                                     # the user's playhead stays put
        v = tl.tracks["video"]
        self.assertEqual([t["name"] for t in v], ["Video 1", "Flow Magnific"])
        self.assertEqual(v[0]["items"], [])                                   # the user's track: untouched
        clip_a, clip_b = v[1]["items"]
        self.assertEqual(clip_a.GetStart() - tl.start, 48)                    # at the playhead
        self.assertEqual(clip_a.GetDuration(), 48)                            # 2 s at 24 fps
        self.assertEqual(clip_b.GetStart() - tl.start, 96)                    # first free spot after A
        self.assertEqual(clip_b.GetDuration(), 48)                            # still: 2 s
        aud = tl.tracks["audio"]
        self.assertEqual(aud[1]["name"], "Flow Magnific Audio")
        self.assertEqual(aud[1]["items"][0].GetStart() - tl.start, 48)
        modes = [x.get("mediaType") for x in self.rs.mp.appends]
        self.assertEqual(modes, [1, 1, 2])
        self.assertTrue(all(it.color == "Purple" for it in v[1]["items"]))
        # undo takes them off one by one, newest first; the media pool keeps them
        self.assertIn(Path(c["file"]).name, mg.undo(connect=self.connect)["say"])
        self.assertEqual(aud[1]["items"], [])
        self.assertIn(Path(b["file"]).name, mg.undo(connect=self.connect)["say"])
        self.assertEqual(v[1]["items"], [clip_a])
        tl.name = "Other"
        with self.assertRaises(FlowError):
            mg.undo(connect=self.connect)
        tl.name = "My Edit"
        mg.undo(connect=self.connect)
        with self.assertRaises(FlowError):
            mg.undo(connect=self.connect)
        self.assertTrue(Path(a["file"]).exists())

    def test_playhead_without_timeline_stays_in_media_pool(self):
        self.rs.project.tl = None
        mg.set_place("playhead")
        mg.pull(file=str(media(self.td / "a.mp4", "video")))
        res = mg.sync(connect=self.connect)
        self.assertEqual(len(res["imported"]), 1)
        self.assertEqual(res["placed"], [])
        self.assertTrue(any("no timeline" in n for n in res["notes"]))

    def test_resolve_closed_files_wait_and_come_later(self):
        mg.pull(file=str(media(self.td / "a.mp4", "video")))

        def closed():
            raise FlowError("DaVinci Resolve isn't running.")
        res = mg.sync(connect=closed)
        self.assertTrue(res["blocked"])
        self.assertEqual(len(res["waiting"]), 1)
        self.assertIn("until Resolve", mg.format_sync(res))
        self.assertEqual(len(mg.sync(connect=self.connect)["imported"]), 1)

    def test_already_in_media_pool_is_not_imported_again(self):
        p = media(self.folder / "dragged-in.mov", "video")
        self.rs.mp.root.clips.append(FakeMPI(str(p.resolve()), "mine"))
        res = mg.sync(connect=self.connect)
        self.assertTrue(res["imported"][0]["already"])
        self.assertEqual(self.rs.mp.imports, [])

    def test_webp_becomes_png(self):
        p = self.folder / "web.webp"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x64", "-frames:v", "1",
                        str(p)], check=True)
        res = mg.sync(connect=self.connect)
        self.assertEqual([i["file"] for i in res["imported"]], ["web.png"])
        self.assertTrue((self.folder / "web.png").exists())
        self.assertEqual(mg.sync(connect=self.connect)["imported"], [])

    def test_all_imports_what_was_there_before(self):
        res = mg.sync(all_=True, connect=self.connect)
        self.assertEqual([i["file"] for i in res["imported"]], ["before.png"])

    def test_watch_one_look(self):
        mg.pull(file=str(media(self.td / "a.mp4", "video")))
        lines = []
        mg.watch(ticks=1, connect=self.connect, out=lines.append)
        self.assertTrue(any("→ media pool Flow › Magnific" in x for x in lines), lines)
        self.assertFalse((self.home / "magnific" / "watch.pid").exists())


class Watcher(Base):
    def test_stale_pid_is_not_running(self):
        (self.home / "magnific").mkdir(parents=True)
        (self.home / "magnific" / "watch.pid").write_text("999999")
        self.assertIsNone(mg.running())
        self.assertFalse(mg.stop()["stopped"])

    def test_own_process_is_not_mistaken_for_a_watcher(self):
        (self.home / "magnific").mkdir(parents=True)
        (self.home / "magnific" / "watch.pid").write_text(str(os.getpid()))
        self.assertIsNone(mg.running())  # this test runner isn't "magnific watch"


class Cli(Base):
    def run_cli(self, *argv):
        from flow.cli import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv))
        return rc, buf.getvalue()

    def test_status_folder_place(self):
        rc, out = self.run_cli("magnific", "folder", str(self.folder))
        self.assertEqual(rc, 0)
        self.assertIn("Magnific folder", out)
        rc, out = self.run_cli("magnific", "place", "playhead", "--still", "4")
        self.assertIn("playhead", out)
        rc, out = self.run_cli("magnific", "status", "--json", "--offline")
        st = json.loads(out)
        self.assertEqual((st["place"], st["still_seconds"], st["watcher"]), ("playhead", 4.0, None))
        rc, out = self.run_cli("magnific", "status", "--offline")
        self.assertIn("watcher: off", out)

    def test_status_shows_the_open_timeline_shape(self):
        rs = FakeResolve()
        rs.tl.GetSetting = lambda k: {"timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920",
                                      "timelineFrameRate": "30"}.get(k)
        st = mg.status(resolve=True, connect=lambda: rs)
        self.assertEqual((st["resolve"]["timeline"], st["resolve"]["shape"]), ("My Edit", "9:16"))
        self.assertIn("(9:16)", mg.format_status(st))

        def closed():
            raise FlowError("not running")
        self.assertIsNone(mg.status(resolve=True, connect=closed)["resolve"])


if __name__ == "__main__":
    unittest.main()
