---
description: Teach Flow your editing style from a timeline you cut by hand, or from a description
argument-hint: "<style name> [--base talking-head]"
---

Teach Flow a custom style. Arguments: `$ARGUMENTS`

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Follow the `flow` skill's
section "3. Teach mode":
- With a hand-cut timeline open in Resolve: `flow learn-style "<name>" --base <closest built-in> --dry-run`,
  show what was measured (captions, pacing, zooms, look), and after the user confirms run it again
  without `--dry-run`.
- From a description: interview briefly, write the differences per `reference/styles.md`, and pipe them
  to `flow style-save --base <closest>`.

Finish with `flow styles` so the user sees it listed, and tell them `/flow:reel <clip> <style id>` (or `/flow-reel <clip> <style id>`) uses it.
