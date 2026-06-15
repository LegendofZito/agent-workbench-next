#!/usr/bin/env bash
# install.sh — build and install Agent Workbench Next on the current user's machine.
# Usage:  ./install.sh
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── colour helpers ────────────────────────────────────────────────────────────
red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

# ── prerequisite checks ───────────────────────────────────────────────────────
missing=0

check_cmd() {
    local cmd="$1" hint_apt="$2" hint_dnf="$3"
    if ! command -v "$cmd" &>/dev/null; then
        red "MISSING: $cmd"
        echo "  Debian/Ubuntu:  sudo apt install $hint_apt"
        echo "  Fedora/RHEL:    sudo dnf install $hint_dnf"
        missing=$((missing + 1))
    else
        echo "  ✓ $cmd ($(command -v "$cmd"))"
    fi
}

bold "Checking required tools…"
check_cmd node  "nodejs"  "nodejs"
check_cmd pnpm  "nodejs npm && npm install -g pnpm"  "nodejs npm && npm install -g pnpm"
check_cmd cargo "cargo (via rustup: curl https://sh.rustup.rs -sSf | sh)"  "cargo (via rustup: curl https://sh.rustup.rs -sSf | sh)"
check_cmd python3 "python3"  "python3"

bold "Checking WebKit2GTK (required for Tauri)…"
if ! pkg-config --exists webkit2gtk-4.1 2>/dev/null; then
    red "MISSING: webkit2gtk-4.1 (pkg-config check failed)"
    echo "  Debian/Ubuntu:  sudo apt install libwebkit2gtk-4.1-dev"
    echo "  Fedora/RHEL:    sudo dnf install webkit2gtk4.1-devel"
    missing=$((missing + 1))
else
    echo "  ✓ webkit2gtk-4.1 found"
fi

if [ "$missing" -gt 0 ]; then
    red ""
    red "Install the $missing missing prerequisite(s) above, then re-run ./install.sh"
    exit 1
fi

echo ""
bold "All prerequisites satisfied."
echo ""

# ── install JS dependencies ───────────────────────────────────────────────────
bold "Installing Node dependencies (pnpm install)…"
pnpm -C "$root/ui-next" install

# ── build Tauri release binary ────────────────────────────────────────────────
bold "Building Tauri release binary (this may take a few minutes)…"
pnpm -C "$root/ui-next" tauri build

# ── locate the produced binary ────────────────────────────────────────────────
release_dir="$root/ui-next/src-tauri/target/release"
binary=""
for candidate in \
    "$release_dir/agent-workbench-next" \
    "$release_dir/Agent Workbench Next" \
    "$release_dir/agent-workbench" \
    ; do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
        binary="$candidate"
        break
    fi
done

# fallback: grab the first non-build-script executable in the release dir
if [ -z "$binary" ]; then
    binary="$(find "$release_dir" -maxdepth 1 -type f -executable \
              ! -name '*.d' ! -name 'build' | head -n1)"
fi

if [ -z "$binary" ]; then
    red "Could not locate the built binary under $release_dir"
    echo "Run 'ls $release_dir' to inspect and install manually."
    exit 1
fi

bold "Found binary: $binary"

# ── install binary ────────────────────────────────────────────────────────────
install_dir="${HOME}/.local/bin"
install_path="$install_dir/agent-workbench-next"

mkdir -p "$install_dir"
install -m 755 "$binary" "$install_path"
green "Binary installed → $install_path"

# ── install icon ──────────────────────────────────────────────────────────────
icon_src="$root/ui-next/src-tauri/icons/icon.png"
icon_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/icons/hicolor/256x256/apps"
mkdir -p "$icon_dir"
if [ -f "$icon_src" ]; then
    cp "$icon_src" "$icon_dir/agent-workbench-next.png"
    green "Icon installed → $icon_dir/agent-workbench-next.png"
else
    echo "  (no icon found at $icon_src — skipping)"
fi

# ── install .desktop entry ────────────────────────────────────────────────────
desktop_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
mkdir -p "$desktop_dir"
cat > "$desktop_dir/agent-workbench-next.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Agent Workbench Next
Comment=Unified desktop workbench for Claude Code, Codex, Gemini CLI, and Ollama
Exec=${install_path}
Icon=agent-workbench-next
Terminal=false
Categories=Development;IDE;
Keywords=AI;agent;claude;codex;gemini;ollama;llm;
StartupWMClass=agent-workbench-next
EOF
green ".desktop installed → $desktop_dir/agent-workbench-next.desktop"

# update desktop database if tool is available
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$desktop_dir" 2>/dev/null || true
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
green "Installed. Sign into your CLIs, then launch Agent Workbench Next."
bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Run:       agent-workbench-next"
echo "  Or:        launch 'Agent Workbench Next' from your application menu"
echo ""
echo "  Optionally install and authenticate your agent CLIs:"
echo "    Claude Code:  npm install -g @anthropic-ai/claude-code && claude login"
echo "    Codex:        npm install -g @openai/codex"
echo "    Gemini:       npm install -g @google/gemini-cli && gemini auth login"
echo "    Ollama:       https://ollama.com/download"
echo ""
