---
description: Change the Flow reel in place while you watch ("punch in here", "captions yellow", "warmer")
argument-hint: "<what to change>"
---

Make this change to the open Flow timeline with a **live edit**: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`).

1. Run `flow edit where` to see what is under the playhead (clip N, caption, nodes, grade). "Here", "this
   shot" and "that caption" mean what it reports.
2. Map the request to `flow edit` calls, using the table in the `flow` skill (section 1, step 9):
   `zoom --zoom`, `captions --color/--size/--font/--accent/--case [--from --to]`, `caption-text --text`,
   `grade --exposure/--contrast/--saturation/--warmth/--tint/--vignette [--look --intensity] [--all|--clip N]`,
   `fx --type --duration`, `broll --broll-clip --duration`. Several changes are several calls.
3. Say what changed in one line.

If the request is structural (cut or keep a take, a new style, re-timing, a new hook), don't live edit:
change `edit_plan.json`, then `flow compile` and `flow build` for a new version, and say so. If the open
timeline wasn't built by Flow, offer `/flow:finish` (or `/flow-finish`) instead.
