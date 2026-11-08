"""Generate a synthetic talking clip with known pauses, a filler and a retake.

    python3 tests/make_fixture.py [out.mp4]

Uses macOS `say` for speech and ffmpeg for a 1920x1080 30 fps test picture with a
warm color cast (so auto-balance has something to fix).
"""
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = (
    "Hey, welcome back. [[slnc 1400]] "
    "Today I'm going to show you how to edit reels in seconds. [[slnc 900]] "
    "The first thing, [[slnc 650]] the first thing you need is a plan. [[slnc 1200]] "
    "Um, [[slnc 350]] it is really important. [[slnc 1600]] "
    "Most people skip this step, and they lose hours every single week. [[slnc 700]] "
    "So let me show you exactly how it works. [[slnc 2200]] "
    "Let's go!"
)

# Ground truth for tests: the retake is the first "The first thing,".
EXPECT = {"retake_phrase": "the first thing", "filler": "um", "min_silences": 5}


def main(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "speech.aiff"
        subprocess.run(["say", "-r", "175", "-o", str(aiff), SCRIPT], check=True)
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(aiff)],
            capture_output=True, text=True, check=True).stdout.strip()) + 0.6
        subprocess.run([
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={dur:.2f}",
            "-i", str(aiff),
            "-filter_complex", "[0:v]colorchannelmixer=rr=1.08:bb=0.86,format=yuv420p[v];[1:a]apad,aresample=48000[a]",
            "-map", "[v]", "-map", "[a]", "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
            str(out)], check=True)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "fixtures" / "flow_test_clip.mp4"
    print(main(target))
