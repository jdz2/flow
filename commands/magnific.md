---
description: Bring Magnific creations straight into Resolve through a watched folder (media pool bin Flow › Magnific, optionally at the playhead)
argument-hint: "[folder | playhead | bin | off | status | undo | sync <Magnific project> | what to make]"
---

Magnific into DaVinci Resolve. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Follow the `flow` skill's section
"4. Magnific". In short:
- nothing, or `on`: `flow magnific status`. If no folder is set yet, ask once which folder (offer
  `~/Movies/Flow/Magnific`) and run `flow magnific folder "<dir>"`. Then `flow magnific start` if the watcher
  is off. Say in one line where new creations land.
- a folder path: `flow magnific folder "<path>"`, then `flow magnific start`.
- `playhead` or `bin`: `flow magnific place playhead` (new creations also go onto the open timeline at the
  playhead, on their own track) or `flow magnific place bin` (media pool only, the default). A running
  watcher picks it up by itself.
- `off`: `flow magnific stop`. `status`: `flow magnific status`. `undo`: `flow magnific undo`.
- `sync <Magnific project or folder>`: bring that project's new creations in (skill step 5), then
  `flow magnific sync` if the watcher is off.
- anything else is something to make: generate it with the Magnific tools, price first and wait for a yes,
  check the result, then deliver it into the folder (skill steps 3 and 4).
