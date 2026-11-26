# Flow

**AI video editor for DaVinci Resolve, driven by Claude.** Type `/flow` and pick a clip. Flow cuts the
silences and bad takes, then adds captions, punch-in zooms, a hook, titles, a color look, speed ramps
and effects. The result is a normal, fully editable Resolve timeline.

Flow can also **finish an existing timeline**. It reads the production notes you left (markers,
placeholder titles and clips, clip metadata, a notes file), then completes them on a copy of the
timeline: titles, cinematic touches, slow-mo, looks and B-roll.

```
/flow → "Flow is ready. What are we editing?"
      → finds your clip in Resolve → style · font · grade · extras
      → transcribes locally (whisper.cpp) → Claude reviews takes, writes the edit
      → "Done. Silences and bad takes cut, captions placed, color graded. Your reel is on the timeline."
```

## Requirements
- macOS (Apple Silicon recommended) and **DaVinci Resolve Studio** 21.1.2+, with Preferences → System →
  General → **External scripting using: Local**
- Claude (desktop app or Claude Code)
- Homebrew. The installer adds `ffmpeg`, `whisper-cpp` and `zstd` if they're missing.

## Install
In Terminal:
```bash
git clone https://github.com/jdz2/flow.git ~/Flow
cd ~/Flow && ./install.sh
```
Then:
1. In DaVinci Resolve Studio, set Preferences → System → General → **External scripting using: Local**,
   and restart Resolve.
2. Restart Claude (desktop app or Claude Code), open a project in Resolve, and type `/flow`.

To update later: `cd ~/Flow && git pull && ./install.sh`. Use one install method: `install.sh` sets up
the engine, the `/flow` skill and the `/flow-…` commands, so you don't also need the plugin.

The installer:
- downloads the Whisper model (`ggml-large-v3-turbo-q5_0`, ~570 MB) to `~/.flow/models`
- writes the Flow looks into Resolve's `LUT/Flow` folder
- adds a **Workspace → Scripts → Flow Build** menu item in Resolve
- links the skill to `~/.claude/skills/flow`, so `/flow` works in Claude
- links the slash commands into `~/.claude/commands` as `/flow-reel`, `/flow-tweak`, `/flow-render`, …

Check your setup any time with:
```bash
~/.flow/flow doctor
```

## Slash commands
`/flow` does everything, and asks. These jump straight to one job (installed as `/flow:<name>` with the
plugin, or `/flow-<name>` by `install.sh`):

| command | does |
|---|---|
| `/flow:reel [clip] [style]` | cut a clip into a reel, skipping the "what are we editing?" step |
| `/flow:finish [notes file]` | finish the open timeline from its markers and placeholders |
| `/flow:tweak <change>` | live edit the reel in place: "punch in here", "captions yellow", "warmer" |
| `/flow:undo [n]` | undo the last live edit(s) |
| `/flow:render [h264\|h265\|prores] [folder]` | render the reel (default H.264 MP4 in `~/Movies/Flow`) plus an `.srt` |
| `/flow:status [all]` | where the edit stands and what's next |
| `/flow:broll [folder\|bin]` | index and describe B-roll for the current job |
| `/flow:styles [id]` | list styles, looks and fonts |
| `/flow:teach <name>` | learn a style from a timeline you cut by hand |
| `/flow:check` | check the setup and fix what's missing |

## What Flow does
**Reel mode** (one clip → a vertical reel):
- **Cuts:** silences, filler words, and retakes / false starts / restarted sentences (detected, then
  judged by Claude). Every cut snaps to real silence.
- **Captions:** Text+ captions timed to the word, with emphasis highlighting and pop/fade animations,
  in any installed font.
- **Motion:** punch-in zooms that alternate on cuts, emphasis punch-ins, smooth push-ins, and a
  subject-aware 9:16 reframe (Studio Smart Reframe optional).
- **Hook and titles:** an AI-written hook, a cold open, and title, chapter, lower-third, location,
  end-card and quote titles.
- **Look:** auto balance (an editable CDL on node 1) plus a look LUT. Looks: Natural, Orange & Teal,
  Film, Moody, Warm, Cool, B&W, Vintage, Nightlife (club and neon light), or your own `.cube`.
- **Cinematic:** speed ramps through pauses, slow-mo, letterbox, grain, halation, light leaks.
- **Effects:** shake, flash, glow, RGB split, blur-in.
- **Masks:** spotlight, background blur, vignette, highlight box, circle reveal.
- **Audio:** normalized to −14 LUFS.

**Finish mode** (an existing timeline): reads your markers and placeholders and completes them on a
duplicate:
- titles and lower thirds
- a look that leaves already-graded clips alone
- slow-mo that keeps the clip's slot, so music sync holds
- zooms, push-ins and effects
- B-roll picked from your media pool
- every note re-marked ✓ green or "Flow needs you" red

See `skills/flow/reference/notes.md` for how to leave notes.

**Styles:** Clean · Talking Head · Talking Head+ · Business · Shorts · Cinematic, plus your own.
**Teach Flow** your style from a timeline you cut by hand: `/flow` → Teach, or
`flow learn-style "My Style"`.

## Engine CLI
`~/.flow/flow <command>`:

| command | does |
|---|---|
| `doctor` | checks the setup and shows the fixes |
| `find [--path f]` | finds the clip in Resolve (selection or playhead) and opens a job |
| `transcribe` · `analyze` · `notes` | prep: words, silences, retakes, emphasis, color, frames, production notes |
| `compile` | turns `edit_plan.json` into `build.json` (all frame math) |
| `build` | builds the timeline in Resolve |
| `finish-scan` · `finish` | Finish mode |
| `styles` · `style-save` · `learn-style` | styles |
| `fonts` · `luts [--install]` | resources |
| `frames` | exports check stills |
| `status` · `jobs` | where a job stands and the next step; recent jobs (offline) |
| `render [--codec h264\|h265\|prores] [--out dir]` | renders the job's timeline via the Deliver page, with an `.srt` |
| `srt [--out file]` | writes the job's captions as SubRip (offline) |
| `probe --switch` | tests the Resolve API in a "Flow Sandbox" project |

Jobs live in `~/.flow/jobs/<clip>/`: `analysis.json`, `review.txt`, `edit_plan.json`, `build.json`,
`report.json` and `preview/`.

## Free edition of Resolve
External scripting needs Studio. On the free edition, run everything up to `flow compile`, then in
Resolve choose **Workspace → Scripts → Flow Build**.

## Limits
These can't be done through Resolve's scripting API:
- Magic Mask (text behind the subject)
- cutting to music beats
- trimming adjustment clips, or putting a grade or Fusion effects on them (Flow uses per-clip grades
  and Fusion clips instead)

## Tests
```bash
python3 -m unittest discover -s tests -v
```
`test_pipeline.py` runs the real whisper pipeline on a synthetic clip (macOS `say` plus ffmpeg). Resolve
behavior was verified with `flow probe` and sandbox builds against Resolve Studio 21.1.2.
