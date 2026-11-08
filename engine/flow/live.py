"""Live mode: build the reel in front of the user, inside the open Resolve window.

    flow build --live [--pace 1.0] [--play scrub|keys|none]

The normal build is a fast background job. Live mode drives Resolve's own UI while it works, so the
user watches the edit happen: Resolve comes to the front, the new timeline opens on the Edit page,
the playhead follows each cut, caption and B-roll as it lands, the Color page opens and steps through
every clip while its grade is applied (viewer and scopes update), and at the end the finished reel is
played back from the top. Every step is also printed as a one-line narration (LIVE · …) for the chat.

Resolve's scripting API has no transport (play/stop), so playback is done one of two ways:
  scrub  (default) the playhead runs through the timeline at real-time speed (picture, no sound)
  keys   real playback with sound: Resolve is brought to the front and sent the space bar through
         System Events. This needs Accessibility permission for the app running Flow; when it is
         missing Flow falls back to scrub.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from flow.util import frames_to_tc

PAGES = ("media", "cut", "edit", "fusion", "color", "fairlight", "deliver")


class Live:
    """Pacing + UI control. A disabled Live does nothing, so build code can call it unconditionally."""

    def __init__(self, enabled: bool = False, pace: float = 1.0, play: str = "scrub",
                 log: Optional[Path] = None):
        self.enabled = enabled
        self.pace = max(0.0, float(pace))
        self.play_mode = play if play in ("scrub", "keys", "none") else "scrub"
        self.log = log
        self.resolve = self.project = self.tl = None
        self.fps = 24.0
        self.start = 0
        self._page = None
        self._t0 = time.time()

    # ---- wiring
    def attach(self, resolve: Any, project: Any, tl: Any, fps: float, start: int) -> None:
        self.resolve, self.project, self.tl, self.fps, self.start = resolve, project, tl, fps, start

    # ---- narration
    def say(self, msg: str) -> None:
        if not self.enabled:
            return
        line = f"LIVE {time.time() - self._t0:5.1f}s · {msg}"
        print(line, flush=True)
        if self.log:
            try:
                with open(self.log, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass

    def beat(self, seconds: float) -> None:
        if self.enabled and self.pace > 0 and seconds > 0:
            time.sleep(seconds * self.pace)

    # ---- UI
    def front(self) -> None:
        """Bring Resolve to the front so the user sees the work (no special permission needed)."""
        if not self.enabled:
            return
        try:
            subprocess.run(["osascript", "-e", 'tell application "DaVinci Resolve" to activate'],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    def page(self, name: str, hold: float = 0.6) -> None:
        if not self.enabled or self.resolve is None or name not in PAGES:
            return
        if self._page != name:
            try:
                self.resolve.OpenPage(name)
                self._page = name
            except Exception:
                return
            self.beat(hold)

    def show(self, frame: int, hold: float = 0.15, msg: Optional[str] = None) -> None:
        """Move the playhead to a record frame (0 = timeline start) so the viewer shows it."""
        if not self.enabled or self.tl is None:
            return
        try:
            self.tl.SetCurrentTimecode(frames_to_tc(self.start + max(0, int(frame)), self.fps))
        except Exception:
            pass
        if msg:
            self.say(msg)
        self.beat(hold)

    # ---- playback
    def play(self, f0: int, f1: int) -> str:
        """Play [f0, f1) of the timeline. Returns the mode actually used."""
        if not self.enabled or self.tl is None or self.play_mode == "none" or f1 <= f0:
            return "none"
        self.page("edit", 0.3)
        if self.play_mode == "keys" and self._play_keys(f0, f1):
            return "keys"
        self._scrub(f0, f1)
        return "scrub"

    def _scrub(self, f0: int, f1: int) -> None:
        step = 2 if self.fps <= 30 else 4
        t_start = time.time()
        f = f0
        while f < f1:
            try:
                self.tl.SetCurrentTimecode(frames_to_tc(self.start + f, self.fps))
            except Exception:
                return
            # stay on real time: skip ahead if Resolve was slow to draw
            due = t_start + (f - f0 + step) / self.fps
            lag = due - time.time()
            if lag > 0:
                time.sleep(lag)
                f += step
            else:
                f = f0 + int((time.time() - t_start) * self.fps) + step

    def _play_keys(self, f0: int, f1: int) -> bool:
        self.show(f0, 0.2)
        script = ['tell application "DaVinci Resolve" to activate', 'delay 0.3',
                  'tell application "System Events" to key code 49']  # space
        try:
            res = subprocess.run(["osascript"] + sum([["-e", s] for s in script], []),
                                 capture_output=True, text=True, timeout=10)
        except Exception:
            return False
        if res.returncode != 0:
            self.say("real playback needs Accessibility permission for this app; scrubbing instead")
            return False
        time.sleep((f1 - f0) / self.fps)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 49'],
                       capture_output=True, timeout=10)
        return True


NULL = Live(False)


def from_args(args: Any, job: Path) -> Live:
    if not getattr(args, "live", False):
        return NULL
    log = job / "live.log"
    try:
        log.write_text("", encoding="utf-8")
    except OSError:
        log = None
    return Live(True, pace=getattr(args, "pace", 1.0) or 1.0, play=getattr(args, "play", "scrub") or "scrub",
                log=log)
