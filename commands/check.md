---
description: Check Flow's setup (Resolve, scripting, whisper, model, looks) and fix what's missing
---

Run `~/.flow/flow doctor` (fallback: `${CLAUDE_PLUGIN_ROOT}/bin/flow doctor`).

If everything required passes, say "Flow is ready." plus one line on the optional items that are off.
Otherwise list each failed check with its fix. Offer to run the fixes that are safe to run for them:
`./install.sh` in the Flow folder (installs ffmpeg, whisper.cpp, zstd, the model, looks, menu script,
skill and commands), `flow luts --install`. Never change Resolve preferences yourself; tell the user the
menu path (Preferences > System > General > External scripting using: Local). Only run
`flow probe --switch` after the user says yes, because it saves the project and switches to a sandbox.
