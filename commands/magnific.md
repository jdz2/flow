---
description: Bring Magnific creations straight into Resolve through a watched folder, and build them into the edit (slots, new versions as takes)
argument-hint: "[folder | slots | auto | playhead | bin | off | status | undo | put <file> <slot> | fill | sync <Magnific project> | what to make]"
---

Magnific into DaVinci Resolve. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Follow the `flow` skill's section
"4. Magnific". In short:
- nothing, or `on`: `flow magnific status`. If no folder is set yet, ask once which folder (offer
  `~/Movies/Flow/Magnific`) and run `flow magnific folder "<dir>"`. Then `flow magnific start` if the watcher
  is off. Say in one line where new creations land.
- a folder path: `flow magnific folder "<path>"`, then `flow magnific start`.
- `slots`: `flow magnific slots`, the open timeline's slots (S1, S2 …: B-roll markers, B-roll placeholders,
  Magnific clips already in the edit). Show them as a short list: id, time, what it asks for, open or filled.
- `auto`, `playhead` or `bin`: where new creations go. `flow magnific place slots` (the default: a creation
  made for a slot goes straight into it, the rest waits in the media pool), `flow magnific place playhead`
  (the rest also goes onto the open timeline at the playhead, on its own track) or `flow magnific place bin`
  (media pool only). A running watcher picks it up by itself.
- `put <file> <slot>`: `flow magnific put "<file>" --slot S3` builds that creation into the slot now (a new
  clip in an open slot, a new take on the clip of a filled one).
- `off`: `flow magnific stop`. `status`: `flow magnific status`. `undo`: `flow magnific undo`.
- `fill` (or "make B-roll for the open slots"): direct the open slots (skill step 6): a prompt per slot from
  its words, one price for the batch, one yes, then each creation pulled with `--slot` and put in.
- `sync <Magnific project or folder>`: bring that project's new creations in (skill step 5), then
  `flow magnific sync` if the watcher is off.
- anything else is something to make: generate it with the Magnific tools, price first and wait for a yes,
  check the result, then deliver it into the folder (skill steps 3 and 4). Made for a slot ("for S3", "for
  the drone marker")? Pull it with `--slot S3`. A new version of a creation in the edit? `--version-of <id>`.
