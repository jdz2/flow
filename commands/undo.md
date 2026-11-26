---
description: Undo the last live edit(s) on the Flow reel
argument-hint: "[how many, default 1]"
---

Undo live edits on the open Flow timeline. Count: `$ARGUMENTS` (default 1).

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Run `flow edit undo` that many
times (stop early if it reports nothing left to undo), then `flow edit log` and tell the user in one line
what was undone and what is still applied.
