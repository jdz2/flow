---
description: Finish the open Resolve timeline from its markers, placeholders and notes (on a duplicate)
argument-hint: "[notes file]"
---

Run Flow's **Finish mode** on the timeline that is open in Resolve. Flow works on a duplicate, never on
the original.

Arguments: `$ARGUMENTS` (optional path to a notes file; pass it as `--file`).

The engine is `~/.flow/flow` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow`). Follow the `flow` skill's
section "2. Finish mode": `flow doctor`, `flow finish-scan [--file …]`, read `finish_review.txt`, look at
`flow frames --count 6`, ask only what the notes and footage don't answer, write `finish_plan.json` per
`reference/finish_plan.md` covering every note, run `flow finish`, look at the stills, and report note by
note what was done and what still needs the user.
