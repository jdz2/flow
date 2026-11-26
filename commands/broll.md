---
description: Index a folder or bin of B-roll for the current Flow job and describe each clip
argument-hint: "[folder or media pool bin]"
---

Build the B-roll library for the latest job. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`).
1. A path → `flow broll --folder "<path>"`; a bin name → `flow broll --bin "<bin>"`; nothing → `flow broll`
   (bins named B-Roll, Cutaways or Inserts).
2. Read `broll_review.txt` and look at every `broll/sheet_N.jpg` in the job folder. For each clip without a
   description write one line (subject, place, action, shot type, mood) into a JSON file
   `{"B1": "…"}` and run `flow broll-describe --file <it>`.
3. Say how many clips are ready, and suggest where a few would fit in the reel (by what is said). The next
   build uses them when the plan's `broll` lists them.
