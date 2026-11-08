#!/bin/sh
# Flow installer: whisper.cpp + model, Flow looks (LUTs), Resolve menu script, the /flow skill.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
FLOW_HOME="${FLOW_HOME:-$HOME/.flow}"
MODEL="ggml-large-v3-turbo-q5_0.bin"
RESOLVE_SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Edit"

say() { printf '\033[1m→ %s\033[0m\n' "$1"; }

say "Flow home: $FLOW_HOME"
mkdir -p "$FLOW_HOME/models" "$FLOW_HOME/jobs" "$FLOW_HOME/styles" "$FLOW_HOME/cache"
ln -sf "$ROOT/bin/flow" "$FLOW_HOME/flow"
printf '%s\n' "$ROOT/engine" > "$FLOW_HOME/engine_path"
chmod +x "$ROOT/bin/flow"

command -v ffmpeg >/dev/null 2>&1 || { say "Installing ffmpeg"; brew install ffmpeg; }
if ! command -v whisper-cli >/dev/null 2>&1; then
  say "Installing whisper.cpp"
  brew install whisper-cpp
fi
if [ ! -f "$FLOW_HOME/models/$MODEL" ]; then
  say "Downloading Whisper model ($MODEL, ~570 MB)"
  curl -L --fail -o "$FLOW_HOME/models/$MODEL.part" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"
  mv "$FLOW_HOME/models/$MODEL.part" "$FLOW_HOME/models/$MODEL"
fi

say "Installing Flow looks into Resolve's LUT folder"
"$ROOT/bin/flow" luts --install || echo "  (LUT folder not writable; looks will be installed per job)"

say "Installing the Resolve menu script (Workspace > Scripts > Flow Build)"
mkdir -p "$RESOLVE_SCRIPTS"
cp "$ROOT/resolve_scripts/Flow Build.py" "$RESOLVE_SCRIPTS/Flow Build.py"

say "Installing the /flow skill for Claude"
mkdir -p "$HOME/.claude/skills"
if [ -e "$HOME/.claude/skills/flow" ] && [ ! -L "$HOME/.claude/skills/flow" ]; then
  echo "  ~/.claude/skills/flow exists and is not a link; leaving it alone"
else
  ln -sfn "$ROOT/skills/flow" "$HOME/.claude/skills/flow"
fi

say "Checking setup"
"$ROOT/bin/flow" doctor || true
cat <<'EOF'

Done. In DaVinci Resolve Studio set:
  Preferences > System > General > External scripting using: Local
Then open Claude and type /flow
EOF
