---
description: Cut a clip into a finished vertical reel in DaVinci Resolve (silences, bad takes, captions, zooms, hook, look)
argument-hint: "[clip path] [style, e.g. shorts]"
---

Run Flow's **Reel mode** now, skipping the "What are we editing?" question.

Arguments: `$ARGUMENTS`
- If they include a file path, use `flow find --path "<path>"`. Otherwise use `flow find` (the selected
  or playhead clip in Resolve).
- If they name a style (clean, talking-head, talking-head-plus, business, shorts, cinematic, or a custom
  one from `flow styles`), use it and don't ask about style.

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Follow the `flow` skill's
section "1. Reel mode" step by step: preflight with `flow doctor`, start the prep in the background, ask
only the setup questions the arguments didn't answer, write the plan, compile, build (live if this is the
first build of the session), look at the preview stills and report in one line.
