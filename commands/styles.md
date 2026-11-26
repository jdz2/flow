---
description: List Flow's styles, color looks and caption fonts
argument-hint: "[style id to show in full]"
---

Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`).
- With a style id: `flow styles --show <id>` and describe it in plain words (captions, pacing, zooms,
  look, extras).
- Otherwise: `flow styles`, `flow luts` and `flow fonts --offline`, and present them as three short
  lists: styles (one line each), looks, and the recommended installed fonts.
