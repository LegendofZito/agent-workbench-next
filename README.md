# Agent Workbench Next

A unified desktop workbench for running and comparing multiple AI coding agents side-by-side. Open tabbed sessions for **Claude Code**, **Codex**, **Gemini CLI**, and local **Ollama** models — all from one window. Sessions, credentials, and configuration stay local on your machine; nothing is stored by or sent to this application.

## Screenshots

_screenshots coming soon_

## Requirements

### System build dependencies

**Debian / Ubuntu**

```bash
sudo apt install -y \
  build-essential pkg-config \
  libwebkit2gtk-4.1-dev libssl-dev \
  libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev
```

**Fedora / RHEL**

```bash
sudo dnf install -y \
  gcc pkg-config openssl-devel \
  webkit2gtk4.1-devel gtk3-devel \
  librsvg2-devel libappindicator-gtk3-devel
```

### Language runtimes

| Tool | Version | Notes |
|------|---------|-------|
| Node.js | 18 or later | |
| pnpm | latest | `npm install -g pnpm` |
| Rust / cargo | stable | `curl https://sh.rustup.rs -sSf \| sh` |
| Python | 3.10 or later | ships with most distros |

### Agent CLIs (optional — install and sign in with your own accounts)

Agent Workbench Next ships **no API keys, tokens, or bundled accounts**. Install whichever agents you want to use and authenticate them independently:

- **Claude Code** — `npm install -g @anthropic-ai/claude-code`, then `claude login`
- **Codex** — `npm install -g @openai/codex`, then sign in via the CLI
- **Gemini CLI** — `npm install -g @google/gemini-cli`, then `gemini auth login`
- **Ollama** — <https://ollama.com/download>, pull models with `ollama pull <model>`
- **Qwen (via Ollama)** — `ollama pull qwen3-coder:30b` (or any Qwen variant)

## Install

```bash
git clone https://github.com/your-org/agent-workbench-next.git
cd agent-workbench-next
./install.sh
```

`install.sh` builds the Tauri binary, then installs it to `~/.local/bin/` along with a `.desktop` entry.

## Run

After install:

```bash
agent-workbench-next
```

Or launch **Agent Workbench Next** from your application menu / launcher.

The script also starts the lightweight Python sidecar (`awbench_server.py`, port 8765) if it is not already running; the sidecar bridges terminal I/O between the UI and the agent CLIs.

## Development

```bash
cd ui-next
pnpm install
pnpm tauri dev   # hot-reloading Svelte + Tauri window
```

Type-check the frontend only:

```bash
pnpm check
```

Build a release bundle:

```bash
pnpm tauri build   # output → ui-next/src-tauri/target/release/bundle/
```

## Privacy

All session data — conversation history, credentials, API tokens, and agent configuration — is stored **exclusively on your local machine** under `~/.config/agent-workbench/`. The repository and release binaries contain only application source code. No telemetry, no cloud sync, no account required to use the app itself.
