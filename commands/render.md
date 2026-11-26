---
description: Render the Flow reel to an MP4 (or ProRes) with an .srt captions file next to it
argument-hint: "[h264|h265|prores] [output folder]"
---

Export the reel. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`).

1. `flow status` to see which timeline the latest job built. If nothing was built yet, say so and offer
   `/flow:reel` (or `/flow-reel`). If the user means another timeline ("render this one"), pass `--timeline "<name>"`.
2. Run `flow render [--codec h264|h265|prores] [--out "<folder>"]` in the background (it prints
   `RENDER · n%` lines; relay one or two). Default: H.264 MP4 in `~/Movies/Flow`, at the timeline's
   resolution, with the speech captions as an `.srt` beside it (`--no-srt` skips that).
3. Report the file path (and the .srt) in one line. If Resolve refuses the format, show the codecs it
   listed and offer another.
