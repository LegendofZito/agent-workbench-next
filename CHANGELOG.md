# Changelog

All notable changes to Agent Workbench Next are documented here.  
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased] - 2026-06-14

### Features

- **Tabbed multi-agent workspaces** — open any number of independent agent sessions in tabs; each tab runs its own CLI process with isolated history.
- **Per-tab agent picker** — choose the agent (Claude Code, Codex, Gemini CLI, Ollama) when opening a new tab; switch without closing the window.
- **Local Ollama model switching** — inline dropdown lists all locally pulled Ollama models; switching takes effect on the next message with no restart required.
- **MCP direct connectors** — "Direct" tab in the connect panel accepts API tokens for MCP-compatible endpoints; tokens are stored locally under `~/.config/agent-workbench/` and never leave the machine.
- **Correct provider logos** — each agent tab displays the official logo/icon for its provider; no generic placeholders.

### Improvements

- **Ctrl+scroll / Ctrl+± zoom** — font size in the conversation pane scales live with the standard keyboard/mouse zoom shortcuts.
- **Centered modal dialogs** — all dialogs (new-session, connect, settings) open centred on the window rather than anchored to a corner.
- **Sidebar dropdown overflow fix** — long model or session names in the sidebar no longer overflow their container; they truncate with a visible ellipsis.
- **Single-instance file attach** — attaching a file to a message no longer duplicates the attachment if the user clicks the button twice before the dialog closes.
- **Escape-to-close keyboard handler** — pressing Escape dismisses any open dialog or popover, consistent with platform conventions.
- **Accessibility keyboard handlers** — interactive non-button elements that accept click events now also respond to Enter/Space for keyboard navigation.
- **Headless sidecar stability** — the `awbench_server.py` HTTP bridge (port 8765) is now resilient to race conditions when multiple tabs start simultaneously.
