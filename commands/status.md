---
description: Show where the current Flow edit stands and what to do next
argument-hint: "[job id, or 'all']"
---

Show the state of Flow's work. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Both commands are offline.
- No argument: `flow status` (the latest job).
- `all`: `flow jobs`, then `flow status --job <id>` for the one the user picks.
- A job id: `flow status --job <id>`.

Summarize in two or three lines: the clip, the last timeline built and its stats, anything out of date or
needing the user (open notes, warnings, live edits not yet in the plan), and the next step.
