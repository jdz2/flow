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
class FakeMarkers:
    """Markers like Resolve's: one per frame, hidden data in customData."""
    def _m(self):
        if not hasattr(self, "markers"):
            self.markers = {}
        return self.markers

    def GetMarkers(self):
        return {f: dict(i) for f, i in self._m().items()}

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if frame in self._m():
            return False
        self._m()[frame] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self._m().pop(frame, None) is not None

    def UpdateMarkerCustomData(self, frame, data):
        if frame not in self._m():
            return False
        self._m()[frame]["customData"] = data
        return True

    def GetMarkerByCustomData(self, data):
        return {f: dict(i) for f, i in self._m().items() if i.get("customData") == data}


class FakeMPI:
    def __init__(self, path, uid):
        self.path, self.uid = path, uid
        self.meta, self.third, self.color = {}, {}, None
        self.frames = None

    def _frames(self):
        if self.frames is None:
            try:
                from flow.util import ffprobe
                f = ffprobe(Path(self.path))
                self.frames = str(int(round(f["duration"] * (f["fps"] or 24))))
            except Exception:
                self.frames = "48"
        return self.frames

    def GetClipProperty(self, k=None):
        ext = Path(self.path).suffix.lower()
        d = {"File Path": self.path, "Clip Name": Path(self.path).name,
             "Frames": "" if ext in (".png", ".wav") else self._frames(), "FPS": "" if ext == ".wav" else "24.000"}
        return d if k is None else d.get(k)

    def GetThirdPartyMetadata(self, k=None):
        return dict(self.third) if k is None else self.third.get(k, "")

    def GetMetadata(self, k=None):
        return dict(self.meta) if k is None else self.meta.get(k, "")

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


class FakeItem(FakeMarkers):
    n = 0

    def __init__(self, mpi, start, dur, lo=0, name=None):
        FakeItem.n += 1
        self.mpi, self.start, self.dur, self.ro, self.uid = mpi, start, dur, 0, f"ti{FakeItem.n}"
        self.color, self.lo, self.name, self.enabled, self.props = None, lo, name, True, {}
        self.takes, self.sel = [], 0

    def GetType(self):
        return "video"

    def GetMediaPoolItem(self):
        return self.mpi

    def GetLeftOffset(self):
        return self.lo

    def SetName(self, name):
        self.name = name
        return True

    def GetClipEnabled(self):
        return self.enabled

    def SetClipEnabled(self, on):
        self.enabled = bool(on)
        return True

    def SetProperty(self, k, v):
        self.props[k] = v
        return True

    # takes, like Resolve 21.2 (live test): the first AddTake makes the clip a take selector (its own clip is
    # take 1); selecting a shorter take shrinks the clip for good, a longer one keeps its length
    def AddTake(self, mpi, s=None, e=None):
        if not self.takes:
            self.takes, self.sel = [(self.mpi, self.lo, self.lo + self.dur)], 1
        s = 0 if s is None else s
        e = int(mpi.GetClipProperty("Frames") or 0) if e is None else e
        self.takes.append((mpi, s, e))
        return True

    def GetTakesCount(self):
        return len(self.takes)

    def GetSelectedTakeIndex(self):
        return self.sel

    def SelectTakeByIndex(self, n):
        if not 1 <= n <= len(self.takes):
            return False
        mpi, s, e = self.takes[n - 1]
        self.mpi, self.sel, self.lo = mpi, n, s
        self.dur = min(self.dur, e - s)
        return True

    def DeleteTakeByIndex(self, n):
        if not 1 <= n <= len(self.takes) or n == self.sel:
            return False
        self.takes.pop(n - 1)
        if self.sel > n:
            self.sel -= 1
        return True

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + self.dur

    def GetDuration(self):
        return self.dur

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.name or self.mpi.GetName()

    def GetRightOffset(self):
        return self.ro

    def SetRightOffset(self, v):
        self.dur -= v - self.ro
        self.ro = v
        return True

    def SetClipColor(self, c):
        self.color = c
        return True


class FakeTL(FakeMarkers):
    def __init__(self, name="My Edit", fps=24.0, start=86400, playhead=48):
        self.name, self.fps, self.start, self.playhead = name, fps, start, playhead
        self.tracks = {"video": [{"name": "Video 1", "items": []}], "audio": [{"name": "Audio 1", "items": []}]}
        self.settings = {"timelineFrameRate": str(fps), "timelineResolutionWidth": "1920",
                         "timelineResolutionHeight": "1080"}
        self.insert_ok = True          # AddTrack("video", {"index": n}) puts a track in between (Resolve 21)

    def GetName(self):
        return self.name

    def GetSetting(self, k):
        return self.settings.get(k)

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
        new = {"name": f"{kind} {len(self.tracks[kind]) + 1}", "items": []}
        if isinstance(sub, dict) and "index" in sub and self.insert_ok:
            self.tracks[kind].insert(int(sub["index"]) - 1, new)
        else:
            self.tracks[kind].append(new)
        return True

    def DeleteTrack(self, kind, k):
        if not 1 <= k <= len(self.tracks[kind]):
            return False
        self.tracks[kind].pop(k - 1)
        return True

    def put(self, k, it):
        """A clip of the user's on video track k (made if needed)."""
        while len(self.tracks["video"]) < k:
            self.tracks["video"].append({"name": f"Video {len(self.tracks['video']) + 1}", "items": []})
        self.tracks["video"][k - 1]["items"].append(it)
        return it

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
            it = FakeItem(info["mediaPoolItem"], info["recordFrame"], info["endFrame"] - info["startFrame"],
                          lo=info["startFrame"])
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

    def test_default_is_slots_and_only_choices_are_kept(self):
        self.assertEqual(mg.config()["place"], "slots")
        mg.set_place("bin")
        cfg_file = self.home / "magnific" / "config.json"
        self.assertEqual(json.loads(cfg_file.read_text()), {"place": "bin", "place_set": True})
        mg.save_config(interval=5.0)
        self.assertEqual(mg.config()["place"], "bin")                       # a choice sticks
        # a config an earlier Flow wrote, its default "bin" saved with it, is not a choice
        cfg_file.write_text(json.dumps({"folder": None, "place": "bin", "interval": 3.0, "still_seconds": 5.0}))
        self.assertEqual(mg.config()["place"], "slots")
        cfg_file.write_text(json.dumps({"place": "playhead", "still_seconds": 4}))
        self.assertEqual(mg.config()["place"], "playhead")                  # playhead was always chosen


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
        self.assertEqual(self.rs.mp.appends, [])                             # none made for a slot: timeline untouched
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


# ---------------------------------------------------------------- slots: creations built into the edit
from flow import magnific_slots as ms  # noqa: E402


class SlotRules(Base):
    def test_slot_ids_in_names(self):
        self.assertEqual(ms.slot_in_name("S3 · harbour drone"), "S3")
        self.assertEqual(ms.slot_in_name("[S12] city"), "S12")
        self.assertEqual(ms.slot_in_name(None, "20261126-101500_S3-Harbour-dusk.mov"), "S3")
        self.assertIsNone(ms.slot_in_name("S01E02 pilot"))
        self.assertIsNone(ms.slot_in_name("Sunset over S3"))
        self.assertEqual((ms.norm_id("s4"), ms.norm_id(" 7 ")), ("S4", "S7"))
        with self.assertRaises(FlowError):
            ms.norm_id("harbour")

    def test_match_uses_sure_signals_only(self):
        recs = {"S1": {"creation": "c-1", "status": "filled"},
                "S2": {"creation": "c-9", "versions": ["c-5"], "status": "filled"},
                "S3": {"creation": "c-7", "status": "gone"}}
        self.assertEqual(ms.match({"slot": "s2"}, "x.mov", recs), ("S2", "made for it"))
        self.assertEqual(ms.match({"title": "S1 · harbour"}, "x.mov", recs)[0], "S1")
        self.assertEqual(ms.match({"version_of": "c-1"}, "x.mov", recs)[0], "S1")
        self.assertEqual(ms.match({"version_of": "c-5"}, "x.mov", recs)[0], "S2")   # a version of an older one
        self.assertEqual(ms.match({"version_of": "c-7"}, "x.mov", recs), (None, None))  # its slot is gone
        self.assertEqual(ms.match({"title": "harbour at dusk"}, "harbour.mov", recs), (None, None))

    def test_fit(self):
        self.assertEqual(ms.fit("video", 48, 24.0, 3.0, 24.0), "ok")
        self.assertEqual(ms.fit("video", 48, 24.0, 1.9, 24.0), "slow")      # within 10 %: slowed to fit
        self.assertEqual(ms.fit("video", 48, 24.0, 1.5, 24.0), "short")
        self.assertEqual(ms.fit("image", 480, 24.0, 0.0, 0.0), "ok")
        self.assertEqual(ms.need_frames(48, 24.0, 30.0), 60)                  # 2 s of a 30 fps clip
        self.assertEqual(ms.need_frames(50, 25.0, 23.976), 48)

    def test_what_reads_as_a_broll_ask(self):
        for t in ("B-roll: drone, slow push in", "drone over the harbour", "[b roll] city", "Insert shot: hands"):
            self.assertTrue(ms.reads_as_broll(t), t)
        for t in ("cut the drone shot", "slow push in", "Placeholder title", ""):
            self.assertFalse(ms.reads_as_broll(t), t)
        self.assertTrue(ms._broll_marker({"color": "Lavender", "name": "", "note": ""}))
        self.assertFalse(ms._broll_marker({"color": "Lavender", "customData": "flow"}))       # Finish mode's own
        self.assertTrue(ms._broll_marker({"color": "Green", "customData": "flow:slot:S2"}))   # filled: still a slot


class SlotScan(Base):
    def setUp(self):
        super().setUp()
        self.rs = FakeResolve()
        self.tl = tl = self.rs.tl
        tl.put(1, FakeItem(FakeMPI("/media/aroll.mov", "a1"), tl.start, 240))
        tl.AddMarker(48, "Lavender", "", "harbour at dusk", 48)               # a B-roll marker, 2 s
        tl.AddMarker(120, "Blue", "B-roll: city lights", "", 1)               # says B-roll; no length: 2 s
        tl.AddMarker(200, "Red", "cut this", "", 1)                           # not a slot
        tl.AddMarker(220, "Lavender", "✓ B-roll", "done by Finish", 1, "flow")  # Flow's own note: not a slot
        self.ph = tl.put(2, FakeItem(FakeMPI("/media/solid.mov", "s1"), tl.start + 150, 30, name="B-roll: shop front"))
        mag = FakeMPI("/media/mag.mov", "m1")
        mag.third["Magnific Creation"] = "c-old"
        tl.put(2, FakeItem(mag, tl.start + 200, 40))
        self.connect = lambda: self.rs

    def test_finds_slots_and_ids_stick(self):
        res = mg.slots(connect=self.connect)
        got = [(s["id"], s["kind"], s["status"], s["f0"], s["f1"]) for s in res["slots"]]
        self.assertEqual(got, [("S1", "marker", "open", 48, 96), ("S2", "marker", "open", 120, 168),
                               ("S3", "placeholder", "open", 150, 180), ("S4", "clip", "filled", 200, 240)])
        self.assertEqual(res["marked"], 3)
        self.assertEqual(self.tl.markers[48]["customData"], "flow:slot:S1")
        self.assertEqual(self.tl.markers[120]["customData"], "flow:slot:S2")
        self.assertEqual(self.ph.markers[0]["customData"], "flow:slot:S3")    # a small marker on the placeholder
        self.assertEqual((self.tl.markers[200]["customData"], self.tl.markers[220]["customData"]), ("", "flow"))
        text = ms.format_slots(res)
        self.assertIn("S4   filled", text)
        self.assertIn("new versions become takes", text)
        # again: the same ids and nothing to mark; a moved marker keeps its id (it carries it)
        self.tl.markers[132] = self.tl.markers.pop(120)
        res = mg.slots(connect=self.connect)
        self.assertEqual([(s["id"], s["f0"]) for s in res["slots"]], [("S1", 48), ("S2", 132), ("S3", 150), ("S4", 200)])
        self.assertEqual(res["marked"], 0)
        # a copy of a slot's marker gets an id of its own; a deleted one is gone
        self.tl.AddMarker(300, "Lavender", "", "harbour again", 24, "flow:slot:S1")
        del self.tl.markers[132]
        res = mg.slots(connect=self.connect)
        self.assertEqual([s["id"] for s in res["slots"]], ["S1", "S3", "S4", "S5"])
        self.assertEqual(self.tl.markers[300]["customData"], "flow:slot:S5")
        self.assertEqual(ms.load("Client Job")["slots"]["S2"]["status"], "gone")

    def test_read_only_and_no_timeline(self):
        res = mg.slots(mark=False, connect=self.connect)
        self.assertEqual((len(res["slots"]), res["marked"]), (4, 0))
        self.assertEqual(self.tl.markers[48]["customData"], "")
        self.assertEqual(self.ph.markers, {})
        self.rs.project.tl = None
        with self.assertRaises(FlowError):
            mg.slots(connect=self.connect)


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class SlotBuild(Base):
    def setUp(self):
        super().setUp()
        mg.set_folder(str(self.folder))
        self.rs = FakeResolve()
        self.tl = tl = self.rs.tl
        tl.playhead = 10
        tl.put(1, FakeItem(FakeMPI("/media/aroll.mov", "a1"), tl.start, 240))
        self.connect = lambda: self.rs
        self.src = self.td / "src"

    def video(self, name, seconds):
        return str(media(self.src / name, "video", seconds))

    def items(self, k):
        return self.tl.tracks["video"][k - 1]["items"]

    def names(self):
        return [t["name"] for t in self.tl.tracks["video"]]

    def test_slots_mode_fills_a_marker_slot_and_undo(self):
        tl = self.tl
        tl.AddMarker(48, "Lavender", "", "harbour at dusk", 48)
        self.assertEqual([s["id"] for s in mg.slots(connect=self.connect)["slots"]], ["S1"])
        mg.set_place("slots")
        self.rs.mp.cur = viewing = FakeFolder("Selects")
        p = mg.pull(file=self.video("v1.mp4", 3), name="Harbour dusk", creation="c-1", slot="S1")
        self.assertIn("_S1-Harbour-dusk", p["name"])                       # the file says which slot it is for
        res = mg.sync(connect=self.connect)
        self.assertEqual([x["op"] for x in res["placed"]], ["fill"])
        self.assertEqual(self.names(), ["Video 1", "Flow B-roll"])
        (it,) = self.items(2)
        self.assertEqual((it.GetStart() - tl.start, it.GetDuration(), it.lo), (48, 48, 12))  # the middle of 3 s
        self.assertEqual((it.GetName(), it.color), ("S1 · Harbour dusk", "Purple"))
        self.assertEqual((tl.markers[48]["color"], tl.markers[48]["customData"]), ("Green", "flow:slot:S1"))
        self.assertEqual(tl.playhead, 10)                                   # the user's playhead stays
        self.assertIs(self.rs.mp.cur, viewing)                              # and the bin they had open
        self.assertEqual(len(self.items(1)), 1)                             # the A-roll: untouched
        self.assertIn("→ S1", mg.format_sync(res))
        self.assertEqual(mg.sync(connect=self.connect)["placed"], [])       # nothing twice
        s1 = mg.slots(connect=self.connect)["slots"][0]
        self.assertEqual((s1["status"], s1["creation"]), ("filled", "c-1"))
        # undo: the clip and its track go, the marker is Lavender again, the slot is open
        self.assertIn("took", mg.undo(connect=self.connect)["say"])
        self.assertEqual(self.names(), ["Video 1"])
        self.assertEqual(tl.markers[48]["color"], "Lavender")
        self.assertEqual(mg.slots(connect=self.connect)["slots"][0]["status"], "open")

    def test_put_fills_a_placeholder_and_switches_it_off(self):
        tl = self.tl
        tl.tracks["video"][0]["items"] = [
            FakeItem(FakeMPI("/media/aroll.mov", "a1"), tl.start, 96),
            FakeItem(FakeMPI("/media/solid.mov", "s1"), tl.start + 96, 48, name="B-roll: shop front"),
            FakeItem(FakeMPI("/media/aroll.mov", "a1"), tl.start + 144, 96)]
        ph = self.items(1)[1]
        self.assertEqual(mg.slots(connect=self.connect)["slots"][0]["kind"], "placeholder")
        res = mg.put(self.video("shop.mp4", 2), "s1", connect=self.connect)   # bin mode: put is the user's ask
        self.assertEqual((res["op"], res["slot"]), ("fill", "S1"))
        self.assertIn("is in S1", res["say"])
        self.assertFalse(ph.enabled)                                          # switched off, not deleted
        self.assertEqual(len(self.items(1)), 3)
        (it,) = self.items(2)
        self.assertEqual((it.GetStart() - tl.start, it.GetDuration()), (96, 48))
        self.assertEqual(mg.slots(connect=self.connect)["slots"][0]["status"], "filled")
        self.assertIn("placeholder is back on", mg.undo(connect=self.connect)["say"])
        self.assertTrue(ph.enabled)
        self.assertEqual(self.names(), ["Video 1"])

    def test_a_fill_never_covers_the_users_clips(self):
        tl = self.tl
        tl.put(2, FakeItem(FakeMPI("/media/title.mov", "t1"), tl.start + 40, 60, name="Title"))
        tl.SetTrackName("video", 2, "Titles")
        tl.AddMarker(48, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        f = self.video("h.mp4", 2)
        tl.insert_ok = False                          # a Resolve that adds tracks only on top
        with self.assertRaises(FlowError) as cm:
            mg.put(f, "S1", connect=self.connect)
        self.assertIn("cover V2 Titles", str(cm.exception))
        self.assertEqual(self.names(), ["Video 1", "Titles"])                 # no track left behind
        self.assertEqual(tl.playhead, 10)
        tl.insert_ok = True                           # Resolve 21: a new track right above the slot
        res = mg.put(f, "S1", connect=self.connect)
        self.assertEqual(self.names(), ["Video 1", "Flow B-roll", "Titles"])  # the title stays on top
        self.assertEqual((res["track"], len(self.items(2)), len(self.items(3))), (2, 1, 1))

    def test_new_versions_become_takes_on_their_clip(self):
        tl = self.tl
        tl.AddMarker(48, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        mg.set_place("slots")
        mg.pull(file=self.video("v1.mp4", 3), name="Harbour", creation="c-1", slot="S1")
        mg.sync(connect=self.connect)
        (it,) = self.items(2)
        # a new version of c-1: a take on the same clip, same spot, same length
        mg.pull(file=self.video("v2.mp4", 2.5), name="Harbour v2", creation="c-2", version_of="c-1")
        res = mg.sync(connect=self.connect)
        self.assertEqual([x["op"] for x in res["placed"]], ["take"])
        self.assertIn("new take", mg.format_sync(res))
        self.assertEqual((it.GetTakesCount(), it.GetSelectedTakeIndex()), (2, 2))
        self.assertEqual((it.GetStart() - tl.start, it.GetDuration()), (48, 48))
        self.assertIn("Harbour-v2", it.GetMediaPoolItem().GetName())
        rec = ms.load("Client Job")["slots"]["S1"]
        self.assertEqual((rec["creation"], rec["versions"]), ("c-2", ["c-1"]))
        # too short for its slot: it waits in the media pool, and says why
        mg.pull(file=self.video("v3.mp4", 1), name="Harbour v3", creation="c-3", version_of="c-2")
        res = mg.sync(connect=self.connect)
        self.assertEqual(res["placed"], [])
        self.assertTrue(any("S1 needs 2.00s" in n for n in res["notes"]), res["notes"])
        st = mg.status()
        self.assertEqual(st["unplaced"][-1]["slot"], "S1")
        self.assertIn("new, not placed", mg.format_status(st))
        self.assertEqual(it.GetTakesCount(), 2)
        # a little short (1.9 s for 2 s): a slowed copy that fits exactly
        mg.pull(file=self.video("v4.mp4", 1.9), name="Harbour v4", creation="c-4", slot="S1")
        res = mg.sync(connect=self.connect)
        self.assertEqual([x["op"] for x in res["placed"]], ["take"])
        self.assertEqual((it.GetTakesCount(), it.GetDuration()), (3, 48))
        slowed = it.GetMediaPoolItem()
        self.assertEqual(Path(slowed.path).parent, self.home / "magnific" / "fit")
        self.assertEqual(slowed.GetClipProperty("Frames"), "48")
        self.assertIn("slowed to fit S1", slowed.meta["Description"])
        # undo: back to the version before, which stays one click away
        self.assertIn("earlier version", mg.undo(connect=self.connect)["say"])
        self.assertEqual((it.GetTakesCount(), it.GetSelectedTakeIndex()), (2, 2))
        self.assertEqual(ms.load("Client Job")["slots"]["S1"]["creation"], "c-2")

    def test_a_take_that_would_change_the_clip_is_taken_back(self):
        self.tl.AddMarker(48, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        mg.put(self.video("v1.mp4", 2), "S1", connect=self.connect)
        (it,) = self.items(2)
        select = FakeItem.SelectTakeByIndex

        def moving(item, n):              # a Resolve that shifts the clip when a take is picked
            ok = select(item, n)
            if n > 1:
                item.start += 1
            return ok
        with mock.patch.object(FakeItem, "SelectTakeByIndex", moving):
            with self.assertRaises(FlowError) as cm:
                mg.put(self.video("v2.mp4", 2), "S1", connect=self.connect)
        self.assertIn("taken back", str(cm.exception))
        self.assertEqual((it.GetTakesCount(), it.GetSelectedTakeIndex()), (1, 1))

    def test_playhead_mode_puts_slot_creations_in_their_slot(self):
        tl = self.tl
        tl.AddMarker(120, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        mg.set_place("playhead", still_seconds=2)
        a = mg.pull(file=self.video("s1.mp4", 2), name="S1 · harbour")          # named for its slot
        b = mg.pull(file=self.video("free.mp4", 2), name="Something else")
        c = mg.pull(file=self.video("short.mp4", 1), name="Short", slot="S1")   # too short for S1
        res = mg.sync(connect=self.connect)
        ops = {Path(x["file"]).name: x["op"] for x in res["placed"]}
        self.assertEqual(ops, {a["name"]: "fill", b["name"]: "playhead"})
        self.assertNotIn(c["name"], ops)                                     # waits; not at the playhead either
        self.assertEqual(set(self.names()), {"Video 1", "Flow B-roll", "Flow Magnific"})
        self.assertEqual(tl.playhead, 10)

    def test_bin_mode_says_where_it_fits_and_put_places_it(self):
        mg.set_place("bin")
        self.tl.AddMarker(48, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        p = mg.pull(file=self.video("v1.mp4", 2), name="Harbour", slot="S1")
        res = mg.sync(connect=self.connect)
        self.assertEqual(res["placed"], [])
        self.assertEqual(self.names(), ["Video 1"])                          # bin mode: the timeline is untouched
        self.assertTrue(any("fits S1" in n and "flow magnific put" in n for n in res["notes"]), res["notes"])
        self.assertEqual([u["slot"] for u in mg.status()["unplaced"]], ["S1"])
        out = mg.put(p["name"], "S1", connect=self.connect)                 # a name in the folder is enough
        self.assertEqual(out["op"], "fill")
        self.assertEqual(mg.status()["unplaced"], [])
        mg.set_place("slots")
        self.assertEqual(mg.sync(connect=self.connect)["placed"], [])        # the watcher doesn't put it in again

    def test_a_slot_on_another_timeline_waits(self):
        self.tl.AddMarker(48, "Lavender", "", "harbour", 48)
        mg.slots(connect=self.connect)
        other = FakeTL(name="Other cut")
        self.rs.project.tl = self.rs.mp.tl = other
        mg.set_place("slots")
        mg.pull(file=self.video("v1.mp4", 2), name="Harbour", slot="S1")
        res = mg.sync(connect=self.connect)
        self.assertEqual(res["placed"], [])
        self.assertTrue(any("on “My Edit”: open that timeline" in n for n in res["notes"]), res["notes"])
        with self.assertRaises(FlowError):
            mg.put(self.video("v2.mp4", 2), "S1", connect=self.connect)
        with self.assertRaises(FlowError):
            mg.put(self.video("v3.mp4", 2), "S9", connect=self.connect)

    def test_cli_slots_pull_and_put(self):
        from flow import resolve_bridge as rb
        from flow.cli import main
        self.tl.AddMarker(48, "Lavender", "", "harbour", 48)

        def cli(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = main(list(argv))
            return rc, buf.getvalue()
        with mock.patch.object(rb, "_injected", self.rs):
            rc, out = cli("magnific", "slots")
            self.assertEqual(rc, 0)
            self.assertIn("S1   open", out)
            rc, out = cli("magnific", "slots", "--json", "--no-mark")
            self.assertEqual(json.loads(out)["slots"][0]["id"], "S1")
            rc, out = cli("magnific", "pull", "--file", self.video("v1.mp4", 2), "--name", "Harbour", "--slot", "S1")
            self.assertEqual(rc, 0)
            name = out.split(" is in ")[0].strip()
            self.assertIn("_S1-Harbour", name)
            rc, out = cli("magnific", "put", name, "--slot", "S1", "--in", "0")
            self.assertEqual(rc, 0)
            self.assertIn("is in S1", out)
            rc, out = cli("magnific", "place", "slots")
            self.assertIn("go straight into it", out)


if __name__ == "__main__":
    unittest.main()
