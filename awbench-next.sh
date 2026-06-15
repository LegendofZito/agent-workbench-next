#!/usr/bin/env bash
# awbench-next.sh — launch Agent Workbench Next (the modern ui-next app).
# Starts the headless send-server (awbench_server.py) if it isn't already up,
# then opens the Svelte/Tauri dev window. One command brings the whole thing back.
set -e

# KDE launches .desktop files with a minimal PATH — make sure pnpm/node/cargo resolve.
export PATH="$HOME/.cargo/bin:$HOME/.npm-global/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/usr/sbin:$PATH"

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure the send-server is up. The installed user service is preferred because
# it restarts independently if the bridge crashes.
if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
  echo "→ send-server already running on :8765"
elif systemctl --user start agent-workbench-next-server.service >/dev/null 2>&1; then
  echo "→ started managed send-server service on :8765"
  sleep 1
else
  echo "→ managed service unavailable; starting fallback send-server on :8765"
  nohup python3 "$PROJ/awbench_server.py" > /tmp/awbench-server.log 2>&1 &
  sleep 1
fi

echo "→ launching Agent Workbench Next window (ui-next)…"
cd "$PROJ/ui-next"
exec pnpm tauri dev
