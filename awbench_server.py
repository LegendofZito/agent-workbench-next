#!/usr/bin/env python3
"""Headless backend for Agent Workbench Next.

The Svelte/Tauri application owns presentation. This server owns long-running
client processes and writes the same durable session records as the legacy
Workbench. It intentionally uses only the Python standard library.
"""

import json
import hashlib
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("AWBENCH_PORT", "8765"))
CONFIG_DIR = os.path.expanduser("~/.config/agent-workbench")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SESS_DIR = os.path.join(CONFIG_DIR, "sessions")
GEMINI_TITLE_CACHE_FILE = os.path.join(CONFIG_DIR, "gemini-titles.json")
CUSTOM_PROJECTS_DIR = os.path.expanduser(
    "~/Projects/Open Projects/Agent Workbench Sessions"
)
OLLAMA_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")
LOCAL_AGENT_MAX_STEPS = 24
LOCAL_TOOL_MAX_OUTPUT = 24_000
LOCAL_FILE_MAX_BYTES = 2_000_000
QWEN_MCP_BRIDGE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tools",
    "qwen_mcp_bridge.mjs",
)
QWEN_SETTINGS_FILE = os.path.expanduser("~/.qwen/settings.json")
SUPPORTED_CONNECTORS = {
    "threejs",
    "cloudflare",
    "google-drive",
    "supabase",
    "vercel",
    "gmail",
    "google-calendar",
    "canva",
    "hugging-face",
    "indeed",
    "cocounsel-legal",
    "robinhood",
}

ACTIVE_TURNS = {}
ACTIVE_LOCK = threading.Lock()
LIMIT_CACHE = {}
LIMIT_CACHE_SECONDS = 60
CONNECTOR_CACHE = {"time": 0.0, "value": []}
CONNECTOR_CACHE_SECONDS = 15
HANDOFF_WARNING_PERCENT = 70
HANDOFF_MAX_CHARS = 60_000
CLAUDE_SMART_ORCHESTRATION_PROMPT = """
Agent Workbench orchestration policy:

Use Claude Code's native Agent tool selectively. Do not ask the user whether to
delegate and do not delegate every prompt.

Before taking tools or modifying files on a substantial actionable request,
emit one short visible routing line:
Routing: <agent profile + model/effort> — <brief reason>.
Do not emit a routing line for conversation, clarification, status checks,
simple explanations, or tiny one-step actions.

Route bounded read-only research to Haiku. Route normal coding, editing, tests,
refactors, UI implementation, and routine debugging to Sonnet. Keep
architecture, ambiguous diagnosis, high-consequence judgment, destructive
operations, and final review with Opus.

Use at most one worker by default. Fan out only when independent parallel work
clearly saves time, and state why. Keep delegation inside the current Claude
session, wait for the worker, verify its result, and present one integrated
answer. Never claim worker work succeeded without checking it.
""".strip()
CLAUDE_SMART_AGENTS = {
    "workbench-researcher": {
        "description": "Bounded read-only research, repository discovery, inventories, and fact gathering.",
        "prompt": "Stay read-only, gather the minimum evidence needed, cite exact files or sources, and return a concise factual brief.",
        "tools": ["Read", "Grep", "Glob", "Bash", "WebFetch", "WebSearch"],
        "model": "haiku",
    },
    "workbench-builder": {
        "description": "Well-scoped coding, editing, tests, refactors, UI implementation, and routine debugging.",
        "prompt": "Implement the assigned change surgically, preserve existing behavior, run focused validation, and report changed files and unresolved risks.",
        "model": "sonnet",
        "effort": "medium",
    },
    "workbench-complex-builder": {
        "description": "Difficult implementation with broad coupling or subtle failure modes after architecture is clear.",
        "prompt": "Implement the defined design carefully, preserve invariants, run focused and broader validation, and report remaining uncertainty.",
        "model": "sonnet",
        "effort": "high",
    },
    "workbench-reviewer": {
        "description": "Independent verification after substantial delegated implementation.",
        "prompt": "Review the actual implementation and validation evidence against the requirements. Identify correctness or regression risks.",
        "tools": ["Read", "Grep", "Glob", "Bash"],
        "model": "opus",
        "effort": "high",
    },
}
CLAUDE_SMART_AGENTS_JSON = json.dumps(CLAUDE_SMART_AGENTS, separators=(",", ":"))


def now_iso():
    return datetime.now().astimezone().isoformat()


def authoritative_time_context():
    moment = datetime.now().astimezone()
    zone_name = getattr(moment.tzinfo, "key", None) or moment.tzname() or "local"
    offset = moment.strftime("%z")
    if len(offset) == 5:
        offset = offset[:3] + ":" + offset[3:]
    return (
        "<session_context>\n"
        "Authoritative local system time for this turn:\n"
        f"- Local date and time: {moment.strftime('%A, %B %-d, %Y at %-I:%M:%S %p %Z')}\n"
        f"- ISO 8601: {moment.isoformat()}\n"
        f"- Time zone: {zone_name} (UTC{offset})\n"
        "Use this as the source of truth for today, yesterday, tomorrow, elapsed "
        "dates, deadlines, and any other relative time reference. If conversation "
        "history conflicts with this clock, this clock wins.\n"
        "</session_context>"
    )


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def session_path(session_id):
    if not re.fullmatch(r"[A-Za-z0-9:_-]+", session_id or ""):
        raise ValueError("invalid session id")
    return os.path.join(SESS_DIR, f"{session_id}.json")


def load_session(session_id):
    with open(session_path(session_id), encoding="utf-8") as handle:
        return json.load(handle)


def save_session(session):
    save_json(session_path(session["id"]), session)


def clean_text(value):
    return str(value or "").replace("\x00", "").strip()


def shorten(value, limit=80):
    text = re.sub(r"\s+", " ", clean_text(value))
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def iso_from_unix(timestamp):
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().isoformat()
    except (TypeError, ValueError, OSError):
        return now_iso()


def strip_session_context(text):
    value = clean_text(text)
    match = re.match(
        r"^\s*(<session_context>.*?</session_context>)\s*(.*)$",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return "", value
    return clean_text(match.group(1)), clean_text(match.group(2))


def read_jsonl_lines(path, limit=None):
    try:
        with open(path, encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if limit is not None and index >= limit:
                    break
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def native_session_id(session):
    source_id = clean_text(session.get("source_session_id"))
    if source_id:
        return source_id
    backend = session.get("backend") or {}
    return clean_text(backend.get("session_id") or backend.get("thread_id"))


def deleted_native_refs():
    config = load_json(CONFIG_FILE, {}) or {}
    ids = set()
    paths = set()
    for bucket in (config.get("deleted_session_tombstones") or {}).values():
        if not isinstance(bucket, dict):
            continue
        ids.update(clean_text(value) for value in bucket.get("ids") or [] if clean_text(value))
        paths.update(
            os.path.abspath(os.path.expanduser(clean_text(value)))
            for value in bucket.get("paths") or []
            if clean_text(value)
        )
    return ids, paths


def local_session_records():
    records = []
    try:
        paths = Path(SESS_DIR).glob("*.json")
    except OSError:
        return records
    for path in paths:
        session = load_json(str(path), None)
        if isinstance(session, dict) and session.get("id"):
            records.append((path, session))
    return records


def source_is_deleted(session, deleted_ids, deleted_paths):
    source_path = clean_text(session.get("source_path"))
    return (
        clean_text(session.get("id")) in deleted_ids
        or native_session_id(session) in deleted_ids
        or (
            source_path
            and os.path.abspath(os.path.expanduser(source_path)) in deleted_paths
        )
    )


def claude_context_limit(model):
    value = clean_text(model).lower()
    if re.search(r"(?:opus-4-[678]|sonnet-4-6|fable-5)", value):
        return 1_000_000
    return 200_000


def context_from_claude_usage(usage, model="", compact_count=0):
    if not isinstance(usage, dict):
        return {}
    used = sum(
        int(usage.get(key) or 0)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    if used <= 0:
        return {}
    limit = claude_context_limit(model)
    return {
        "used": used,
        "limit": limit,
        "percent": min(100.0, used * 100.0 / limit),
        "model": clean_text(model),
        "compact_count": int(compact_count or 0),
    }


def claude_model_alias(model):
    value = clean_text(model).lower()
    for alias in ("fable", "opus", "sonnet", "haiku"):
        if alias in value:
            return alias
    return "Default"


def claude_session_metadata(path):
    title = ""
    preview = ""
    cwd = ""
    context = {}
    compact_count = 0
    resumable = False
    for record in read_jsonl_lines(path):
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        cwd = cwd or clean_text(record.get("cwd"))
        record_type = record.get("type")
        if record_type in {"ai-title", "custom-title"}:
            candidate = clean_text(record.get("customTitle") or record.get("aiTitle"))
            if candidate:
                title = candidate
            continue
        if record_type == "system" and record.get("subtype") == "compact_boundary":
            compact_count += 1
            continue
        message = record.get("message") or {}
        if record_type == "user" and not record.get("isMeta"):
            content = message.get("content")
            text_parts = []
            if isinstance(content, str):
                command = re.search(
                    r"<command-name>(.*?)</command-name>",
                    content,
                    flags=re.DOTALL,
                )
                text_parts.append(command.group(1) if command else content)
            elif isinstance(content, list):
                text_parts.extend(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            candidate = clean_text("\n".join(text_parts))
            if candidate:
                resumable = True
                preview = preview or shorten(candidate)
        elif record_type == "assistant":
            model = clean_text(message.get("model"))
            if model and model != "<synthetic>":
                candidate = context_from_claude_usage(
                    message.get("usage") or {},
                    model,
                    compact_count,
                )
                if candidate:
                    context = candidate
    return {
        "title": title,
        "preview": preview,
        "cwd": cwd,
        "context": context,
        "resumable": resumable,
    }


def claude_jsonl_turns(path):
    turns = []
    current = None
    pending_text = []
    seen = set()

    def ensure_turn(timestamp=""):
        nonlocal current
        if current is None:
            current = {
                "id": str(uuid.uuid4()),
                "created_at": clean_text(timestamp) or now_iso(),
                "status": "completed",
                "items": [],
            }
            turns.append(current)
        return current

    def flush_assistant_text(final=False):
        nonlocal pending_text
        if not pending_text:
            return
        turn = ensure_turn()
        progress = pending_text[:-1] if final else pending_text
        turn["items"].extend(
            {"type": "system", "text": text, "tag": "progress"}
            for text in progress
        )
        if final:
            turn["items"].append({"type": "agentMessage", "text": pending_text[-1]})
        pending_text = []

    for record in read_jsonl_lines(path):
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        record_id = clean_text(record.get("uuid"))
        if record_id and record_id in seen:
            continue
        if record_id:
            seen.add(record_id)
        record_type = record.get("type")
        if record_type not in {"user", "assistant"}:
            continue
        message = record.get("message") or {}
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        timestamp = clean_text(record.get("timestamp"))
        if record_type == "user":
            prompt = clean_text(
                "\n".join(
                    clean_text(block.get("text"))
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            )
            if prompt:
                flush_assistant_text(final=True)
                current = {
                    "id": record_id or str(uuid.uuid4()),
                    "created_at": timestamp or now_iso(),
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": prompt}],
                        }
                    ],
                }
                turns.append(current)
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                output = block.get("content")
                if isinstance(output, list):
                    output = "\n".join(
                        clean_text(part.get("text"))
                        for part in output
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                if clean_text(output):
                    ensure_turn(timestamp)["items"].append(
                        {"type": "system", "text": clean_text(output), "tag": "tool"}
                    )
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and clean_text(block.get("text")):
                pending_text.append(clean_text(block.get("text")))
            elif block_type == "thinking" and clean_text(block.get("thinking")):
                ensure_turn(timestamp)["items"].append(
                    {
                        "type": "reasoning",
                        "summary": [clean_text(block.get("thinking"))],
                    }
                )
            elif block_type == "tool_use":
                flush_assistant_text(final=False)
                name = clean_text(block.get("name"))
                tool_input = block.get("input") or {}
                if name == "Bash":
                    command = clean_text(tool_input.get("command"))
                    if command:
                        ensure_turn(timestamp)["items"].append(
                            {"type": "commandExecution", "command": command}
                        )
                elif name in {"Edit", "Write", "NotebookEdit"}:
                    target = clean_text(
                        tool_input.get("file_path")
                        or tool_input.get("notebook_path")
                        or tool_input.get("path")
                    )
                    ensure_turn(timestamp)["items"].append(
                        {
                            "type": "fileChange",
                            "content": target or name,
                            "changes": [{"path": target}] if target else [],
                        }
                    )
                elif name:
                    detail = clean_text(
                        tool_input.get("description")
                        or tool_input.get("query")
                        or tool_input.get("file_path")
                    )
                    ensure_turn(timestamp)["items"].append(
                        {
                            "type": "system",
                            "text": f"Tool: {name}" + (f" — {detail}" if detail else ""),
                            "tag": "tool",
                        }
                    )
    flush_assistant_text(final=True)
    return [turn for turn in turns if turn.get("items")]


def codex_rollout_turns(path):
    if not path or not os.path.isfile(path):
        return []
    turns = []
    current = None
    for record in read_jsonl_lines(path):
        if not isinstance(record, dict) or record.get("type") not in {
            "event_msg",
            "response_item",
        }:
            continue
        payload = record.get("payload") or {}
        event_type = payload.get("type")
        item = None
        timestamp = clean_text(record.get("timestamp"))
        if record.get("type") == "event_msg" and event_type == "user_message":
            text = clean_text(payload.get("message"))
            if not text:
                continue
            current = {
                "id": str(uuid.uuid4()),
                "created_at": timestamp or now_iso(),
                "status": "completed",
                "items": [],
            }
            turns.append(current)
            item = {
                "type": "userMessage",
                "content": [{"type": "text", "text": text}],
            }
        elif record.get("type") == "event_msg" and event_type == "agent_message":
            text = clean_text(payload.get("message"))
            if not text:
                continue
            if current is None:
                current = {
                    "id": str(uuid.uuid4()),
                    "created_at": timestamp or now_iso(),
                    "status": "completed",
                    "items": [],
                }
                turns.append(current)
            item = (
                {"type": "system", "text": text, "tag": "progress"}
                if payload.get("phase") == "commentary"
                else {"type": "agentMessage", "text": text}
            )
        elif record.get("type") == "response_item" and current is not None:
            if event_type == "function_call":
                name = clean_text(payload.get("name"))
                arguments = payload.get("arguments")
                command = ""
                if name == "exec_command" and isinstance(arguments, str):
                    try:
                        command = clean_text((json.loads(arguments) or {}).get("cmd"))
                    except (ValueError, AttributeError):
                        command = clean_text(arguments)
                item = (
                    {"type": "commandExecution", "command": command}
                    if command
                    else {"type": "system", "text": f"Tool: {name}", "tag": "tool"}
                    if name
                    else None
                )
            elif event_type in {"function_call_output", "custom_tool_call_output"}:
                output = payload.get("output")
                if isinstance(output, dict):
                    output = output.get("content") or output.get("output")
                if clean_text(output):
                    item = {
                        "type": "system",
                        "text": clean_text(output),
                        "tag": "system",
                    }
            elif event_type == "reasoning":
                summary = payload.get("summary") or []
                text = clean_text(
                    " ".join(
                        value.get("text", "") if isinstance(value, dict) else str(value)
                        for value in summary
                    )
                )
                if text:
                    item = {"type": "reasoning", "summary": [text], "text": text}
        if item is not None and current is not None:
            current["items"].append(item)
    return [turn for turn in turns if turn.get("items")]


def codex_context_from_rollout(path):
    context = {}
    for record in read_jsonl_lines(path):
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        usage = info.get("last_token_usage") or {}
        used = int(usage.get("input_tokens") or usage.get("total_tokens") or 0)
        limit = int(info.get("model_context_window") or 0)
        if used > 0 and limit > 0:
            context = {
                "used": used,
                "limit": limit,
                "percent": min(100.0, used * 100.0 / limit),
                "model": "Codex",
                "compact_count": 0,
            }
    return context


def gemini_session_index():
    index = {}
    root = Path(os.path.expanduser("~/.gemini"))
    if not root.is_dir():
        return index
    for candidate in root.rglob("session-*.jsonl"):
        try:
            header = next(read_jsonl_lines(str(candidate), limit=1), {})
            session_id = clean_text(header.get("sessionId"))
            if not session_id:
                continue
            mtime = candidate.stat().st_mtime_ns
        except OSError:
            continue
        existing = index.get(session_id)
        if existing and int(existing.get("mtime_ns") or 0) >= mtime:
            continue
        index[session_id] = {
            "path": str(candidate),
            "project_hash": clean_text(header.get("projectHash")),
            "start_time": clean_text(header.get("startTime")),
            "updated_at": iso_from_unix(candidate.stat().st_mtime),
            "mtime_ns": mtime,
        }
    return index


def known_project_paths():
    values = {os.path.abspath(os.path.expanduser("~"))}
    config = load_json(CONFIG_FILE, {}) or {}
    for value in [config.get("cwd"), *(config.get("recent_projects") or [])]:
        if clean_text(value):
            values.add(os.path.abspath(os.path.expanduser(clean_text(value))))
    for workspace in config.get("workspace_tabs") or []:
        if isinstance(workspace, dict) and clean_text(workspace.get("cwd")):
            values.add(os.path.abspath(os.path.expanduser(clean_text(workspace.get("cwd")))))
    for _path, session in local_session_records():
        if clean_text(session.get("cwd")):
            values.add(os.path.abspath(os.path.expanduser(clean_text(session.get("cwd")))))
    return values


def gemini_title_from_jsonl(path):
    for record in read_jsonl_lines(path, limit=500):
        messages = []
        if isinstance(record, dict) and isinstance(record.get("$set"), dict):
            payload = record["$set"].get("messages")
            if isinstance(payload, list):
                messages = payload
        elif isinstance(record, dict):
            messages = [record]
        for message in messages:
            if not isinstance(message, dict) or message.get("type") != "user":
                continue
            prompt = clean_text(
                "\n".join(
                    clean_text(block.get("text"))
                    for block in message.get("content") or []
                    if isinstance(block, dict) and block.get("text")
                )
            )
            _, visible = strip_session_context(prompt)
            if visible:
                return shorten(visible)
    return ""


def gemini_native_titles():
    cached = load_json(GEMINI_TITLE_CACHE_FILE, {}) or {}
    if not isinstance(cached, dict):
        cached = {}
    try:
        if time.time() - os.path.getmtime(GEMINI_TITLE_CACHE_FILE) < 300:
            return cached
    except OSError:
        pass
    try:
        result = subprocess.run(
            [
                "gemini",
                "--list-sessions",
                "--skip-trust",
                "--yolo",
                "--include-directories",
                os.path.expanduser("~"),
            ],
            cwd=os.path.expanduser("~"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cached
    titles = dict(cached)
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    for raw_line in (result.stdout or "").splitlines():
        line = ansi.sub("", raw_line)
        match = re.match(
            r"^\s*\d+\.\s+(.*?)\s+\([^()]*(?:ago)?\)\s+"
            r"\[([0-9a-fA-F-]{36})\]\s*$",
            line,
        )
        if not match:
            continue
        title, session_id = (clean_text(value) for value in match.groups())
        if title and not title.lower().startswith("<session_context>"):
            titles[session_id] = title
    save_json(GEMINI_TITLE_CACHE_FILE, titles)
    return titles


def gemini_jsonl_turns(path):
    messages = []
    seen_ids = set()
    for record in read_jsonl_lines(path):
        if isinstance(record, dict) and isinstance(record.get("$set"), dict):
            payload = record["$set"].get("messages")
            if isinstance(payload, list):
                messages = payload
                seen_ids = {
                    clean_text(message.get("id"))
                    for message in messages
                    if isinstance(message, dict) and clean_text(message.get("id"))
                }
            continue
        if not isinstance(record, dict) or record.get("type") not in {"user", "gemini"}:
            continue
        message_id = clean_text(record.get("id"))
        if message_id and message_id in seen_ids:
            continue
        messages.append(record)
        if message_id:
            seen_ids.add(message_id)
    turns = []
    current = None

    def ensure_turn(timestamp=""):
        nonlocal current
        if current is None:
            current = {
                "id": str(uuid.uuid4()),
                "created_at": clean_text(timestamp) or now_iso(),
                "status": "completed",
                "items": [],
            }
            turns.append(current)
        return current

    def append_work(text, tag="system"):
        if clean_text(text):
            ensure_turn()["items"].append(
                {"type": "system", "text": clean_text(text), "tag": tag}
            )

    for message in messages:
        if not isinstance(message, dict):
            continue
        message_type = message.get("type")
        timestamp = clean_text(message.get("timestamp"))
        content = message.get("content") or []
        if not isinstance(content, list):
            content = []
        if message_type == "user":
            prompt = clean_text(
                "\n".join(
                    clean_text(block.get("text"))
                    for block in content
                    if isinstance(block, dict) and block.get("text")
                )
            )
            hidden, visible = strip_session_context(prompt)
            if hidden:
                append_work(hidden, "system")
            if visible:
                current = {
                    "id": clean_text(message.get("id")) or str(uuid.uuid4()),
                    "created_at": timestamp or now_iso(),
                    "status": "completed",
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": visible}],
                        }
                    ],
                }
                turns.append(current)
            continue
        if message_type != "gemini":
            continue
        ensure_turn(timestamp)
        for thought in message.get("thoughts") or []:
            if isinstance(thought, dict):
                text = clean_text(
                    thought.get("description")
                    or thought.get("thinking")
                    or thought.get("summary")
                    or thought.get("subject")
                )
                if text:
                    current["items"].append(
                        {"type": "reasoning", "summary": [text], "text": text}
                    )
        assistant_text = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if clean_text(block.get("text")):
                assistant_text.append(clean_text(block.get("text")))
            elif isinstance(block.get("functionCall"), dict):
                call = block["functionCall"]
                name = clean_text(call.get("name"))
                arguments = call.get("args") or {}
                detail = clean_text(
                    arguments.get("command")
                    or arguments.get("file_path")
                    or arguments.get("path")
                    or arguments.get("url")
                    or arguments.get("query")
                )
                current["items"].append(
                    {
                        "type": "commandExecution"
                        if name == "run_shell_command"
                        else "system",
                        "command": detail if name == "run_shell_command" else "",
                        "text": (
                            f"Tool: {name}" + (f" — {detail}" if detail else "")
                            if name != "run_shell_command"
                            else ""
                        ),
                        "tag": "tool",
                    }
                )
        text = clean_text("\n".join(assistant_text))
        thought_text, visible_text = split_gemini_visible_text(text)
        if thought_text:
            current["items"].append(
                {"type": "reasoning", "summary": [thought_text], "text": thought_text}
            )
        if visible_text:
            current["items"].append({"type": "agentMessage", "text": visible_text})
    return [turn for turn in turns if turn.get("items")]


def native_session_summaries():
    summaries = []
    deleted_ids, deleted_paths = deleted_native_refs()
    home = os.path.abspath(os.path.expanduser("~"))

    claude_root = Path(os.path.expanduser("~/.claude/projects"))
    if claude_root.is_dir():
        for path in claude_root.glob("*/*.jsonl"):
            session_id = path.stem
            metadata = claude_session_metadata(str(path))
            if not metadata["resumable"]:
                continue
            summary = {
                "id": f"claude-terminal:{session_id}",
                "source_session_id": session_id,
                "agent": "claude",
                "origin": "claude-terminal",
                "cwd": metadata["cwd"] or home,
                "title": metadata["title"] or metadata["preview"] or "Claude session",
                "preview": metadata["preview"] or "Claude session",
                "created_at": iso_from_unix(path.stat().st_ctime),
                "updated_at": iso_from_unix(path.stat().st_mtime),
                "source_path": str(path),
                "source_mtime_ns": path.stat().st_mtime_ns,
                "backend": {
                    "session_id": session_id,
                    "model": claude_model_alias(
                        (metadata.get("context") or {}).get("model")
                    ),
                },
                "context": metadata["context"],
                "turns": [],
                "transcript_loaded": False,
            }
            if not source_is_deleted(summary, deleted_ids, deleted_paths):
                summaries.append(summary)

    database = os.path.expanduser("~/.codex/state_5.sqlite")
    if os.path.isfile(database):
        try:
            import sqlite3

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "select id, rollout_path, cwd, title, preview, first_user_message, "
                "created_at, updated_at, model, reasoning_effort, agent_role "
                "from threads where archived = 0 order by updated_at desc limit 500"
            ).fetchall()
            connection.close()
        except Exception:
            rows = []
        for row in rows:
            if clean_text(row["agent_role"]):
                continue
            path = clean_text(row["rollout_path"])
            try:
                mtime_ns = os.stat(path).st_mtime_ns if path else 0
            except OSError:
                mtime_ns = 0
            session_id = clean_text(row["id"])
            preview = clean_text(
                row["preview"] or row["title"] or row["first_user_message"]
            )
            summary = {
                "id": f"codex-terminal:{session_id}",
                "source_session_id": session_id,
                "agent": "codex",
                "origin": "codex-terminal",
                "cwd": clean_text(row["cwd"]) or home,
                "title": clean_text(row["title"]) or preview or "Codex session",
                "preview": preview or "Codex session",
                "created_at": iso_from_unix(row["created_at"]),
                "updated_at": iso_from_unix(row["updated_at"]),
                "source_path": path,
                "source_mtime_ns": mtime_ns,
                "backend": {
                    "thread_id": session_id,
                    "model": clean_text(row["model"]) or "gpt-5.5",
                    "effort": clean_text(row["reasoning_effort"]) or "medium",
                },
                "context": codex_context_from_rollout(path),
                "turns": [],
                "transcript_loaded": False,
            }
            if not source_is_deleted(summary, deleted_ids, deleted_paths):
                summaries.append(summary)

    project_by_hash = {
        hashlib.sha256(path.encode("utf-8")).hexdigest(): path
        for path in known_project_paths()
    }
    gemini_titles = gemini_native_titles()
    for session_id, metadata in gemini_session_index().items():
        path = metadata["path"]
        title = (
            clean_text(gemini_titles.get(session_id))
            or gemini_title_from_jsonl(path)
            or "Gemini session"
        )
        summary = {
            "id": f"gemini-terminal:{session_id}",
            "source_session_id": session_id,
            "agent": "gemini",
            "origin": "gemini-terminal",
            "cwd": project_by_hash.get(metadata["project_hash"], home),
            "title": title,
            "preview": title,
            "created_at": metadata["start_time"] or metadata["updated_at"],
            "updated_at": metadata["updated_at"],
            "source_path": path,
            "source_mtime_ns": metadata["mtime_ns"],
            "backend": {"session_id": session_id, "model": "Auto"},
            "context": {},
            "turns": [],
            "transcript_loaded": False,
        }
        if not source_is_deleted(summary, deleted_ids, deleted_paths):
            summaries.append(summary)
    return summaries


def sync_native_history():
    existing_records = local_session_records()
    existing_by_id = {session["id"]: (path, session) for path, session in existing_records}
    linked_native_ids = {
        native_session_id(session): session["id"]
        for _path, session in existing_records
        if native_session_id(session)
    }
    discovered_ids = set()
    created = 0
    updated = 0
    for summary in native_session_summaries():
        imported_id = summary["id"]
        source_id = native_session_id(summary)
        linked_id = linked_native_ids.get(source_id)
        if linked_id and linked_id != imported_id:
            continue
        discovered_ids.add(imported_id)
        existing_entry = existing_by_id.get(imported_id)
        if existing_entry:
            _path, existing = existing_entry
            previous_mtime = int(existing.get("source_mtime_ns") or 0)
            current_mtime = int(summary.get("source_mtime_ns") or 0)
            turns = existing.get("turns") or []
            transcript_loaded = bool(existing.get("transcript_loaded"))
            user_title = clean_text(existing.get("title")) if existing.get("user_renamed") else ""
            candidate = dict(existing)
            candidate.update(summary)
            if user_title:
                candidate["title"] = user_title
                candidate["preview"] = user_title
                candidate["user_renamed"] = True
            candidate["turns"] = turns
            candidate["transcript_loaded"] = (
                transcript_loaded and previous_mtime == current_mtime
            )
            if candidate != existing:
                save_session(candidate)
                updated += 1
        else:
            save_session(summary)
            created += 1

    removed = 0
    for path, session in existing_records:
        if session.get("origin") not in {
            "claude-terminal",
            "codex-terminal",
            "gemini-terminal",
        }:
            continue
        if session.get("id") in discovered_ids:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return {"created": created, "updated": updated, "removed": removed}


def load_native_transcript(session_id):
    session = load_session(session_id)
    if session.get("origin") not in {
        "claude-terminal",
        "codex-terminal",
        "gemini-terminal",
    }:
        return session
    source_path = clean_text(session.get("source_path"))
    if not source_path or not os.path.isfile(source_path):
        return session
    source_mtime_ns = os.stat(source_path).st_mtime_ns
    if (
        session.get("transcript_loaded")
        and int(session.get("source_mtime_ns") or 0) == source_mtime_ns
    ):
        return session
    agent = clean_text(session.get("agent"))
    if agent == "claude":
        metadata = claude_session_metadata(source_path)
        session["turns"] = claude_jsonl_turns(source_path)
        session["context"] = metadata["context"]
        if metadata["title"]:
            session["title"] = metadata["title"]
    elif agent == "codex":
        session["turns"] = codex_rollout_turns(source_path)
        session["context"] = codex_context_from_rollout(source_path)
    elif agent == "gemini":
        session["turns"] = gemini_jsonl_turns(source_path)
        title = gemini_title_from_jsonl(source_path)
        if title:
            session["title"] = title
    session["source_mtime_ns"] = source_mtime_ns
    session["transcript_loaded"] = True
    save_session(session)
    return session


def ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=3) as response:
            data = json.load(response)
        return [model["name"] for model in data.get("models", []) if model.get("name")]
    except Exception:
        return []


def orchestration_mode():
    config = load_json(CONFIG_FILE, {}) or {}
    return clean_text((config.get("claude_orchestration") or {}).get("mode") or "smart")


def history_messages(session):
    messages = []
    for turn in session.get("turns", []):
        if turn.get("status") == "running":
            continue
        for item in turn.get("items", []):
            kind = item.get("type")
            if kind == "userMessage":
                content = item.get("content")
                if isinstance(content, list):
                    text = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                elif isinstance(content, dict):
                    text = content.get("text", "")
                else:
                    text = str(content or "")
                if text.strip():
                    messages.append({"role": "user", "content": text})
            elif kind == "agentMessage":
                text = clean_text(item.get("text"))
                if text:
                    messages.append({"role": "assistant", "content": text})
    return messages


def prior_history_messages(session):
    messages = history_messages(session)
    if messages and messages[-1].get("role") == "user":
        return messages[:-1]
    return messages


def pick_backend(session):
    agent = clean_text(session.get("agent")).lower()
    config = load_json(CONFIG_FILE, {}) or {}
    custom_keys = {
        clean_text(client.get("key")).lower()
        for client in (config.get("custom_clients") or [])
        if isinstance(client, dict) and clean_text(client.get("key"))
    }
    backend = session.get("backend") or {}
    custom_key = clean_text(backend.get("custom_client_key")).lower()
    if agent in custom_keys or custom_key in custom_keys:
        return "custom"
    if "claude" in agent:
        return "claude"
    if "codex" in agent or "gpt" in agent:
        return "codex"
    if "gemini" in agent:
        return "gemini"
    return "ollama"


def prompt_with_attachments(prompt, paths):
    readable = [path for path in paths if os.path.isfile(path)]
    if not readable:
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        "The user attached the following local files. Inspect each relevant file "
        "with the appropriate file or image-reading tool before answering:\n- "
        + "\n- ".join(readable)
    )


def safe_artifact_session_key(session_id):
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-")[:48] or "session"
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:8]
    return f"{label}-{digest}"


def artifact_session_dir(session_id):
    path = os.path.join(CONFIG_DIR, "artifacts", safe_artifact_session_key(session_id))
    os.makedirs(path, exist_ok=True)
    return path


def handoff_item_text(item):
    kind = item.get("type", "")
    if kind == "userMessage":
        content = item.get("content")
        if isinstance(content, list):
            return clean_text(
                " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            )
        if isinstance(content, dict):
            return clean_text(content.get("text"))
        return clean_text(content)
    if kind == "fileChange":
        changes = item.get("changes") or []
        if changes:
            return clean_text(
                ", ".join(
                    str(change.get("path") or change.get("filePath") or "")
                    for change in changes
                    if isinstance(change, dict)
                )
            )
    if kind == "reasoning":
        summary = item.get("summary")
        if isinstance(summary, list):
            return clean_text(" ".join(map(str, summary)))
    return clean_text(
        item.get("text")
        or item.get("content")
        or item.get("command")
        or item.get("summary")
    )


def unique_handoff_values(values, limit=None, item_limit=1400):
    result = []
    seen = set()
    for value in values:
        value = clean_text(value)
        normalized = re.sub(r"\s+", " ", value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if len(value) > item_limit:
            value = value[: item_limit - 1].rstrip() + "…"
        result.append(value)
        if limit and len(result) >= limit:
            break
    return result


def handoff_section(name, values, fallback="None recorded.", limit=None, item_limit=1400):
    values = unique_handoff_values(values, limit=limit, item_limit=item_limit)
    body = "\n".join(f"- {value}" for value in values) if values else f"- {fallback}"
    return f"## {name}\n{body}"


def structured_handoff_packet(session):
    title = clean_text(session.get("title") or "Untitled")
    agent = clean_text(session.get("agent") or "agent").title()
    cwd = clean_text(session.get("cwd") or os.path.expanduser("~"))
    context = session.get("context") or {}
    requests = []
    conclusions = []
    upgrades = []
    failures = []
    unresolved = []
    technical = []
    chronology = []

    for turn_index, turn in enumerate(session.get("turns") or [], start=1):
        user_texts = []
        agent_texts = []
        for item in turn.get("items") or []:
            kind = item.get("type", "")
            text = handoff_item_text(item)
            if not text:
                continue
            if kind == "userMessage":
                user_texts.append(text)
                requests.append(text)
                chronology.append(f"{turn_index}. USER: {text}")
            elif kind == "agentMessage":
                agent_texts.append(text)
                conclusions.append(text)
                chronology.append(f"{turn_index}. AGENT: {text}")
                lowered = text.lower()
                if re.search(
                    r"\b(added|built|implemented|upgraded|updated|fixed|created|installed|changed|wired|enabled|completed|now supports)\b",
                    lowered,
                ):
                    upgrades.append(text)
                if re.search(
                    r"\b(failed|failure|error|blocked|unable|could not|couldn't|timeout|timed out|stalled|broken|regression)\b",
                    lowered,
                ):
                    failures.append(text)
                if re.search(
                    r"\b(todo|remaining|unresolved|not yet|still needs|follow[- ]?up|deferred|later|next step|open question)\b",
                    lowered,
                ):
                    unresolved.append(text)
            elif kind in {"commandExecution", "fileChange", "plan", "note", "report"}:
                technical.append(f"{kind.upper()}: {text}")
        if user_texts and not agent_texts:
            unresolved.extend(user_texts)
        if turn.get("status") not in {None, "completed"}:
            failures.append(
                f"Turn {turn_index} ended as {turn.get('status')}: "
                f"{clean_text(turn.get('prompt')) or 'Turn had no saved prompt.'}"
            )

    used = int(context.get("used") or 0)
    limit = int(context.get("limit") or 0)
    context_line = (
        f"{round(used * 100 / limit)}% ({used:,}/{limit:,} tokens)"
        if used > 0 and limit > 0
        else "unknown"
    )
    parts = [
        "# Automatic Session Handoff",
        f"- Source title: {title}",
        f"- Source client: {agent}",
        f"- Project directory: {cwd}",
        f"- Source context usage: {context_line}",
        f"- Generated: {now_iso()}",
        "",
        handoff_section("User Goals And Requests", requests, limit=80, item_limit=900),
        "",
        handoff_section(
            "Decisions And Important Conclusions",
            conclusions[-40:],
            limit=40,
            item_limit=1200,
        ),
        "",
        handoff_section(
            "Upgrades And Completed Changes",
            upgrades[-30:],
            limit=30,
            item_limit=1200,
        ),
        "",
        handoff_section(
            "Failures, Dead Ends, And Blockers",
            failures[-30:],
            limit=30,
            item_limit=1200,
        ),
        "",
        handoff_section(
            "Unresolved Questions And Forgotten Follow-Ups",
            unresolved[-40:],
            limit=40,
            item_limit=1000,
        ),
        "",
        handoff_section(
            "Important Technical Details",
            technical[-60:],
            limit=60,
            item_limit=800,
        ),
        "",
        "## Chronological Conversation Record",
        "\n".join(unique_handoff_values(chronology[-80:], item_limit=700))
        or "- No conversation text was available.",
        "",
        "## Continuation Instructions",
        "- Treat this packet as authoritative context from the previous session.",
        "- Preserve completed work and do not repeat failed approaches without a reason.",
        "- Address unresolved items when they are relevant, even if the prior conversation moved on.",
        "- Inspect the current filesystem before assuming recorded file state is still current.",
        "- Continue the user's latest objective directly after a brief acknowledgment.",
    ]
    packet = "\n".join(parts)
    if len(packet) > HANDOFF_MAX_CHARS:
        packet = packet[: HANDOFF_MAX_CHARS - 160].rstrip() + (
            "\n\n[Handoff truncated at the configured safety limit. "
            "The categorized sections above take priority.]"
        )
    return packet


def handoff_signature(session):
    turns = session.get("turns") or []
    context = session.get("context") or {}
    last_turn = turns[-1] if turns else {}
    return "|".join(
        [
            str(len(turns)),
            clean_text(session.get("updated_at")),
            clean_text(last_turn.get("id")),
            clean_text(last_turn.get("status")),
            str(context.get("used") or 0),
            str(context.get("limit") or 0),
            str(context.get("compact_count") or 0),
        ]
    )


def stage_handoff(session, force=False):
    context = session.get("context") or {}
    used = int(context.get("used") or 0)
    limit = int(context.get("limit") or 0)
    percent = round(used * 100 / limit) if used > 0 and limit > 0 else 0
    compact_count = int(context.get("compact_count") or 0)
    if not force and percent < HANDOFF_WARNING_PERCENT and compact_count <= 0:
        return ""
    signature = handoff_signature(session)
    existing = clean_text(session.get("handoff_stage_artifact"))
    if (
        not force
        and session.get("handoff_stage_ready")
        and session.get("handoff_stage_signature") == signature
        and os.path.isfile(existing)
    ):
        return existing
    path = os.path.join(artifact_session_dir(session["id"]), "HANDOFF.md")
    temporary = f"{path}.tmp-{os.getpid()}-{uuid.uuid4()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(structured_handoff_packet(session))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    session["handoff_stage_ready"] = True
    session["handoff_stage_signature"] = signature
    session["handoff_stage_artifact"] = path
    session["handoff_stage_updated_at"] = now_iso()
    return path


def all_sessions():
    sessions = []
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return sessions
    for name in names:
        if not name.endswith(".json"):
            continue
        session = load_json(os.path.join(SESS_DIR, name))
        if isinstance(session, dict):
            sessions.append(session)
    return sessions


def next_handoff_title(source):
    current = clean_text(source.get("title") or "Session")
    base = clean_text(source.get("handoff_base_title"))
    highest = int(source.get("handoff_sequence") or 1)
    if not base:
        match = re.match(r"^(.*?)\s+(\d+)$", current)
        if match and int(match.group(2)) >= 2:
            base = clean_text(match.group(1))
            highest = int(match.group(2))
        else:
            base = current
    source_cwd = os.path.abspath(source.get("cwd") or "")
    for session in all_sessions():
        if os.path.abspath(session.get("cwd") or "") != source_cwd:
            continue
        match = re.match(
            rf"^{re.escape(base)}(?:\s+(\d+))?$",
            clean_text(session.get("title")),
            flags=re.IGNORECASE,
        )
        if match:
            highest = max(highest, int(match.group(1) or 1))
    return f"{base} {highest + 1}", base, highest + 1


def deploy_handoff(source_id, target_agent, model, effort, automatic=False):
    source = load_session(source_id)
    source_artifact = stage_handoff(source, force=True)
    save_session(source)
    packet = Path(source_artifact).read_text(encoding="utf-8")
    title, base_title, sequence = next_handoff_title(source)
    timestamp = now_iso()
    session = {
        "id": str(uuid.uuid4()),
        "title": title,
        "agent": target_agent,
        "backend": {"model": model},
        "cwd": source.get("cwd") or os.path.expanduser("~"),
        "origin": "local",
        "created_at": timestamp,
        "updated_at": timestamp,
        "turns": [],
        "handoff_from": source_id,
        "handoff_base_title": base_title,
        "handoff_sequence": sequence,
    }
    if target_agent != "gemini" and effort and effort != "Default":
        session["backend"]["effort"] = effort
    target_artifact = os.path.join(artifact_session_dir(session["id"]), "HANDOFF.md")
    Path(target_artifact).write_text(packet, encoding="utf-8")
    session["handoff_artifact"] = target_artifact
    session["handoff_context"] = (
        "Read the authoritative continuation handoff before acting:\n"
        f"{target_artifact}"
    )
    session["handoff_stage_ready"] = True
    session["handoff_stage_signature"] = handoff_signature(source)
    session["handoff_stage_artifact"] = target_artifact
    session["handoff_stage_updated_at"] = timestamp
    save_session(session)
    if automatic:
        source["auto_handoff_signature"] = handoff_signature(source)
        source["auto_handoff_deployed_at"] = timestamp
        source["auto_handoff_target_session_id"] = session["id"]
        save_session(source)
    return session


def default_handoff_target(source_agent):
    config = load_json(CONFIG_FILE, {}) or {}
    preferences = config.get("handoff_preferences") or {}
    mode = clean_text(preferences.get("mode") or "current")
    sequence = [
        clean_text(agent)
        for agent in preferences.get("sequence") or []
        if clean_text(agent)
    ]
    supported = {"claude", "codex", "gemini", "ollama"}
    if mode == "fixed":
        target = clean_text(preferences.get("fixed_agent")) or source_agent
    elif mode == "sequence" and sequence:
        target = (
            sequence[(sequence.index(source_agent) + 1) % len(sequence)]
            if source_agent in sequence
            else sequence[0]
        )
    else:
        target = source_agent
    if target not in supported:
        target = source_agent if source_agent in supported else "claude"
    settings = (config.get("agent_settings") or {}).get(target) or {}
    prefer_cheap = bool(preferences.get("prefer_cheap_models", True))
    cheap_models = {
        "claude": "sonnet",
        "codex": "gpt-5.4-mini",
        "gemini": "gemini-3-flash-preview",
    }
    default_models = {
        "claude": "Default",
        "codex": "gpt-5.5",
        "gemini": "Auto",
        "ollama": (ollama_models() or ["qwen3-coder:30b"])[0],
    }
    model = (
        cheap_models.get(target)
        if prefer_cheap and target in cheap_models
        else clean_text(settings.get("model"))
    ) or default_models[target]
    effort = (
        "low"
        if prefer_cheap and target in {"claude", "codex"}
        else clean_text(settings.get("effort")) or "Default"
    )
    return target, model, effort


def maybe_auto_deploy_handoff(source_id):
    config = load_json(CONFIG_FILE, {}) or {}
    preferences = config.get("handoff_preferences") or {}
    if not preferences.get("auto_deploy"):
        return None
    source = load_session(source_id)
    artifact = stage_handoff(source)
    if not artifact:
        return None
    signature = handoff_signature(source)
    if clean_text(source.get("auto_handoff_signature")) == signature:
        return None
    save_session(source)
    target, model, effort = default_handoff_target(clean_text(source.get("agent")))
    return deploy_handoff(
        source_id,
        target,
        model,
        effort,
        automatic=True,
    )


def split_gemini_visible_text(text):
    thoughts = []

    def replace(match):
        thoughts.append(clean_text(match.group(1)))
        return ""

    visible = re.sub(r"\[Thought:\s*true\](.*?)(?=\[Thought:\s*true\]|\Z)", replace, text, flags=re.S)
    return "\n".join(part for part in thoughts if part), clean_text(visible)


class TurnControl:
    def __init__(self):
        self.cancel = threading.Event()
        self.process = None
        self.lock = threading.Lock()

    def set_process(self, process):
        with self.lock:
            self.process = process

    def stop(self):
        self.cancel.set()
        with self.lock:
            process = self.process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


def run_process(command, cwd, control):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    control.set_process(process)
    return process


def emit_usage(emit, usage):
    if not isinstance(usage, dict):
        return
    input_tokens = int(
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("input")
        or 0
    )
    output_tokens = int(
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("output")
        or 0
    )
    total_tokens = int(
        usage.get("total_tokens")
        or usage.get("totalTokens")
        or input_tokens + output_tokens
    )
    if input_tokens or output_tokens or total_tokens:
        emit(
            {
                "type": "usage",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            }
        )


def update_context(session, usage, model=""):
    if not isinstance(usage, dict):
        return
    used = int(usage.get("input_tokens") or usage.get("inputTokens") or 0)
    limit = int(
        usage.get("model_context_window")
        or usage.get("modelContextWindow")
        or session.get("context", {}).get("limit")
        or 0
    )
    if used <= 0 or limit <= 0:
        return
    compact_count = int(session.get("context", {}).get("compact_count") or 0)
    session["context"] = {
        "used": used,
        "limit": limit,
        "percent": min(100.0, used * 100.0 / limit),
        "model": model,
        "compact_count": compact_count,
    }


def stream_ollama(session, user_text, model, emit, control):
    installed = ollama_models()
    if not model or model in {"Default", "Auto"} or model not in installed:
        model = installed[0] if installed else (model or "qwen2.5-coder:7b")
    emit({"type": "meta", "backend": "ollama", "model": model})
    messages = prior_history_messages(session) + [{"role": "user", "content": user_text}]
    payload = json.dumps(
        {"model": model, "messages": messages, "stream": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    full = []
    with urllib.request.urlopen(request, timeout=600) as response:
        for raw in response:
            if control.cancel.is_set():
                break
            raw = raw.strip()
            if not raw:
                continue
            chunk = json.loads(raw)
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                full.append(piece)
                emit({"type": "delta", "text": piece})
            if chunk.get("done"):
                emit_usage(emit, chunk)
                break
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")
    return "".join(full), model


def bounded_text(value, limit=LOCAL_TOOL_MAX_OUTPUT):
    text = str(value or "")
    if len(text) <= limit:
        return text
    removed = len(text) - limit
    return text[:limit] + f"\n\n[truncated {removed:,} characters]"


def resolved_local_path(value, cwd):
    raw = clean_text(value)
    if not raw:
        return os.path.abspath(cwd)
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    return os.path.abspath(expanded)


def strip_ansi(value):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value or ""))


def run_qwen_mcp_bridge(arguments, cwd=None, timeout=90):
    if not os.path.isfile(QWEN_MCP_BRIDGE):
        raise RuntimeError(f"Qwen MCP bridge is missing: {QWEN_MCP_BRIDGE}")
    if not shutil.which("node"):
        raise RuntimeError("Node.js is required for direct MCP connectors.")
    result = subprocess.run(
        ["node", QWEN_MCP_BRIDGE, *arguments],
        cwd=cwd or os.path.expanduser("~"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = clean_text(result.stdout)
    if result.returncode != 0:
        error = clean_text(strip_ansi(result.stderr)) or output
        raise RuntimeError(error or f"Qwen MCP bridge exited {result.returncode}")
    try:
        return json.loads(output or "{}")
    except ValueError as error:
        raise RuntimeError(
            f"Qwen MCP bridge returned invalid JSON: {bounded_text(output, 2_000)}"
        ) from error


def connector_statuses(force=False):
    now = time.monotonic()
    if (
        not force
        and CONNECTOR_CACHE["value"]
        and now - CONNECTOR_CACHE["time"] < CONNECTOR_CACHE_SECONDS
    ):
        return CONNECTOR_CACHE["value"]
    payload = run_qwen_mcp_bridge(["list"], timeout=60)
    value = payload.get("connectors") or []
    CONNECTOR_CACHE.update({"time": now, "value": value})
    return value


def configure_connector_oauth(connector, client_id="", client_secret=""):
    connector = clean_text(connector).lower()
    if connector not in SUPPORTED_CONNECTORS:
        raise RuntimeError(f"Unsupported connector: {connector}")
    try:
        settings = json.loads(Path(QWEN_SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not read Qwen settings: {error}") from error
    server = (settings.get("mcpServers") or {}).get(connector)
    if not isinstance(server, dict):
        raise RuntimeError(f"Connector is not configured in Qwen Code: {connector}")
    oauth = dict(server.get("oauth") or {})
    oauth["enabled"] = True
    if clean_text(client_id):
        oauth["clientId"] = clean_text(client_id)
    if clean_text(client_secret):
        oauth["clientSecret"] = clean_text(client_secret)
    server["oauth"] = oauth
    temporary = f"{QWEN_SETTINGS_FILE}.awbench-{uuid.uuid4().hex}.tmp"
    Path(temporary).write_text(
        json.dumps(settings, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, QWEN_SETTINGS_FILE)
    CONNECTOR_CACHE.update({"time": 0.0, "value": []})
    return oauth


def set_connector_token(connector, token):
    connector = clean_text(connector).lower()
    if connector not in SUPPORTED_CONNECTORS:
        raise RuntimeError(f"Unsupported connector: {connector}")
    try:
        settings = json.loads(Path(QWEN_SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Could not read Qwen settings: {error}") from error
    server = (settings.get("mcpServers") or {}).get(connector)
    if not isinstance(server, dict):
        raise RuntimeError(f"Connector is not configured in Qwen Code: {connector}")
    token = token.strip() if isinstance(token, str) else ""
    if token:
        server.setdefault("headers", {})["Authorization"] = "Bearer " + token
    else:
        headers = server.get("headers") or {}
        headers.pop("Authorization", None)
        if not headers:
            server.pop("headers", None)
        else:
            server["headers"] = headers
    temporary = f"{QWEN_SETTINGS_FILE}.awbench-{uuid.uuid4().hex}.tmp"
    Path(temporary).write_text(
        json.dumps(settings, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, QWEN_SETTINGS_FILE)
    CONNECTOR_CACHE.update({"time": 0.0, "value": []})
    return {"ok": True, "connector": connector}


def start_connector_auth(connector, client_id="", client_secret=""):
    oauth = configure_connector_oauth(connector, client_id, client_secret)
    if connector in {"gmail", "google-drive", "google-calendar"} and not oauth.get(
        "clientId"
    ):
        raise RuntimeError(
            "Google Workspace connectors require an OAuth client ID and secret. "
            "Create one in Google Cloud, then enter it in Workbench Options."
        )
    log_dir = os.path.join(CONFIG_DIR, "connector-auth")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{connector}.log")
    log = open(log_path, "a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            ["node", QWEN_MCP_BRIDGE, "auth", connector],
            cwd=os.path.expanduser("~"),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    return {"connector": connector, "pid": process.pid, "log": log_path}


def direct_connector_task(connector, prompt, cwd, timeout, emit, control):
    tools_payload = run_qwen_mcp_bridge(
        ["tools", connector],
        cwd=cwd,
        timeout=min(timeout, 90),
    )
    discovered = tools_payload.get("tools") or []
    if not discovered:
        return f"ERROR: {connector} exposed no MCP tools."
    tools = [
        {
            "type": "function",
            "function": {
                "name": clean_text(tool.get("name")),
                "description": clean_text(tool.get("description")),
                "parameters": tool.get("inputSchema")
                or {"type": "object", "properties": {}},
            },
        }
        for tool in discovered
        if clean_text(tool.get("name"))
    ]
    messages = [
        {
            "role": "system",
            "content": (
                f"You execute tasks through the {connector} MCP connector. "
                "Use the provided MCP tools directly. Do not use shell commands, "
                "do not enter plan mode, and do not claim a tool is unavailable "
                "when it is listed. Use read-only tools unless the user's request "
                "explicitly requires a write. After tool results, return a concise "
                "factual answer."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    total_input = 0
    total_output = 0
    for step in range(8):
        if control.cancel.is_set():
            raise RuntimeError("Turn stopped.")
        payload = json.dumps(
            {
                "model": "qwen3-coder:30b",
                "messages": messages,
                "tools": tools,
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{OLLAMA_BASE}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        total_input += int(result.get("prompt_eval_count") or 0)
        total_output += int(result.get("eval_count") or 0)
        message = result.get("message") or {}
        content = clean_text(message.get("content"))
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            emit_usage(
                emit,
                {
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "total_tokens": total_input + total_output,
                },
            )
            return content or f"{connector} completed without a text response."
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            function = call.get("function") or {}
            tool_name = clean_text(function.get("name"))
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            emit(
                {
                    "type": "work",
                    "kind": "tool",
                    "content": f"{connector} · {tool_name}",
                }
            )
            try:
                call_payload = run_qwen_mcp_bridge(
                    [
                        "call",
                        connector,
                        json.dumps({"tool": tool_name, "arguments": arguments}),
                    ],
                    cwd=cwd,
                    timeout=timeout,
                )
                tool_result = call_payload.get("text") or json.dumps(
                    call_payload.get("content") or []
                )
                if call_payload.get("is_error"):
                    tool_result = f"ERROR: {tool_result}"
            except Exception as error:
                tool_result = f"ERROR: {type(error).__name__}: {error}"
            messages.append(
                {
                    "role": "tool",
                    "tool_name": tool_name,
                    "content": bounded_text(tool_result),
                }
            )
    return f"ERROR: {connector} exceeded 8 MCP tool steps without completing."


def local_agent_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a real text file from the user's computer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or replace a real text file on the user's computer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "executable": {"type": "boolean"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Replace exact text in an existing file. Fails if the old text is absent or ambiguous.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": "List files and directories on the user's computer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 4},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "description": "Search file contents with ripgrep.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "glob": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": "Run a shell command with unrestricted local user permissions. Use for coding, tests, package tools, git, system inspection, and automation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string"},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 900,
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_path",
                "description": "Open a local file, directory, application document, or URL in the user's desktop environment.",
                "parameters": {
                    "type": "object",
                    "properties": {"target": {"type": "string"}},
                    "required": ["target"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "launch_app",
                "description": "Launch a desktop application or long-running command without blocking the agent.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "cwd": {"type": "string"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch a public HTTP or HTTPS page for research.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_connectors",
                "description": "List MCP services configured directly for local Qwen Code.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "connector_task",
                "description": "Use a directly configured MCP connector with local Qwen. This does not use Claude credits.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "connector": {
                            "type": "string",
                            "enum": [
                                "threejs",
                                "cloudflare",
                                "google-drive",
                                "supabase",
                                "vercel",
                                "gmail",
                                "google-calendar",
                                "canva",
                                "hugging-face",
                                "indeed",
                                "cocounsel-legal",
                                "robinhood",
                            ],
                        },
                        "prompt": {"type": "string"},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 10,
                            "maximum": 900,
                        },
                    },
                    "required": ["connector", "prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_agent",
                "description": "Delegate a bounded task to an installed Claude, Codex, or Gemini CLI. Use Claude for its connected MCP services, Codex for difficult coding, or Gemini only when specifically useful.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "agent": {
                            "type": "string",
                            "enum": ["claude", "codex", "gemini"],
                        },
                        "prompt": {"type": "string"},
                        "cwd": {"type": "string"},
                        "timeout_seconds": {
                            "type": "integer",
                            "minimum": 10,
                            "maximum": 900,
                        },
                    },
                    "required": ["agent", "prompt"],
                },
            },
        },
    ]


def local_agent_system_prompt(cwd, model):
    return f"""You are {model}, running as a fully operational local desktop and coding agent inside Agent Workbench.

You have direct tools for the user's real filesystem, shell, applications, browser, development tools, and installed cloud-agent CLIs.
Your current working directory is:
{cwd}

Operational rules:
- When the user requests an action, perform it with tools. Do not merely explain how.
- Never claim you cannot read, save, edit, create, launch, or inspect local resources unless the relevant tool actually returns an error.
- Prefer the current working directory for project work, but absolute paths anywhere under the user's account are allowed.
- Use open_path to open files, folders, websites, and generated applications for the user.
- Use launch_app for desktop applications or long-running processes.
- Use run_command for builds, tests, package installation, git, scripts, process control, and system automation.
- Use connector_task for Gmail, Google Drive, Calendar, Supabase, Vercel, Cloudflare, Canva, Hugging Face, Indeed, CoCounsel, Three.js, or Robinhood. Connector tasks run through local Qwen Code and do not consume Claude credits.
- Use delegate_agent only when a stronger specialized cloud agent is materially useful. Delegation may consume that provider's usage.
- Robinhood write actions require the exact phrase CONFIRM ROBINHOOD ACTION in the connector prompt. Read-only Robinhood requests do not.
- Keep destructive operations scoped and intentional. Do not broadly delete or overwrite unrelated data.
- Verify completed work with an appropriate read, command, test, or process check before reporting success.
- Report concise results, exact changed paths, launched applications, and any unresolved failure.

All tool calls and results are retained in the Work Log. Conversation replies should contain the useful outcome, not raw terminal chatter."""


def bounded_local_history(session, max_messages=24, max_characters=72_000):
    selected = []
    used = 0
    for message in reversed(prior_history_messages(session)):
        content = clean_text(message.get("content"))
        if not content:
            continue
        content = re.sub(
            r"^(?:Restored previous conversation history\.\s*)+",
            "",
            content,
            flags=re.IGNORECASE,
        )
        content = re.sub(
            r"^(?:Detected dumb terminal.*?\n+)?"
            r"(?:You should probably run aider.*?\n+)?"
            r"(?:Aider v[^\n]*\n+)?"
            r"(?:Model:[^\n]*\n+)?"
            r"(?:Git repo:[^\n]*\n+)?"
            r"(?:Repo-map:[^\n]*\n+)?",
            "",
            content,
            flags=re.IGNORECASE,
        ).strip()
        if not content:
            continue
        if used + len(content) > max_characters and selected:
            break
        selected.append({"role": message["role"], "content": content})
        used += len(content)
        if len(selected) >= max_messages:
            break
    return list(reversed(selected))


def local_tool_result(name, arguments, cwd, emit, control):
    arguments = arguments if isinstance(arguments, dict) else {}
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")

    if name == "read_file":
        path = resolved_local_path(arguments.get("path"), cwd)
        if not os.path.isfile(path):
            return f"ERROR: file not found: {path}"
        if os.path.getsize(path) > LOCAL_FILE_MAX_BYTES:
            return f"ERROR: file exceeds {LOCAL_FILE_MAX_BYTES:,} byte read limit: {path}"
        start = max(1, int(arguments.get("start_line") or 1))
        end = max(start, int(arguments.get("end_line") or start + 499))
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        excerpt = "".join(
            f"{index}: {line}"
            for index, line in enumerate(lines[start - 1 : end], start=start)
        )
        emit({"type": "work", "kind": "tool", "content": f"Read {path} lines {start}-{min(end, len(lines))}"})
        return bounded_text(excerpt)

    if name == "write_file":
        path = resolved_local_path(arguments.get("path"), cwd)
        content = str(arguments.get("content") or "")
        if len(content.encode("utf-8")) > LOCAL_FILE_MAX_BYTES:
            return f"ERROR: content exceeds {LOCAL_FILE_MAX_BYTES:,} byte write limit"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.awbench-{uuid.uuid4().hex}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
        if arguments.get("executable"):
            os.chmod(path, os.stat(path).st_mode | 0o111)
        emit({"type": "work", "kind": "fileChange", "content": f"Wrote {path} ({len(content.encode('utf-8')):,} bytes)"})
        return f"OK: wrote {path}"

    if name == "edit_file":
        path = resolved_local_path(arguments.get("path"), cwd)
        old_text = str(arguments.get("old_text") or "")
        new_text = str(arguments.get("new_text") or "")
        if not old_text:
            return "ERROR: old_text must not be empty"
        try:
            original = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            return f"ERROR: {error}"
        count = original.count(old_text)
        if count != 1:
            return f"ERROR: old_text matched {count} times; expected exactly once"
        updated = original.replace(old_text, new_text, 1)
        temporary = f"{path}.awbench-{uuid.uuid4().hex}.tmp"
        Path(temporary).write_text(updated, encoding="utf-8")
        os.replace(temporary, path)
        emit({"type": "work", "kind": "fileChange", "content": f"Edited {path}"})
        return f"OK: edited {path}"

    if name == "list_directory":
        root = resolved_local_path(arguments.get("path"), cwd)
        depth = min(4, max(1, int(arguments.get("depth") or 2)))
        if not os.path.isdir(root):
            return f"ERROR: directory not found: {root}"
        entries = []
        root_depth = Path(root).parts
        for current, directories, files in os.walk(root):
            current_depth = len(Path(current).parts) - len(root_depth)
            if current_depth >= depth:
                directories[:] = []
            directories[:] = sorted(
                directory for directory in directories if directory != ".git"
            )
            for directory in directories:
                entries.append(os.path.relpath(os.path.join(current, directory), root) + "/")
            for filename in sorted(files):
                entries.append(os.path.relpath(os.path.join(current, filename), root))
            if len(entries) >= 400:
                entries.append("[truncated]")
                break
        emit({"type": "work", "kind": "tool", "content": f"Listed {root}"})
        return "\n".join(entries) or "[empty directory]"

    if name == "search_files":
        query = clean_text(arguments.get("query"))
        root = resolved_local_path(arguments.get("path"), cwd)
        command = ["rg", "-n", "--hidden", "--glob", "!.git/**"]
        glob = clean_text(arguments.get("glob"))
        if glob:
            command += ["--glob", glob]
        command += [query, root]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=45,
            check=False,
        )
        emit({"type": "work", "kind": "command", "content": shlex.join(command)})
        return bounded_text(result.stdout or f"rg exit code {result.returncode}")

    if name == "run_command":
        command = str(arguments.get("command") or "")
        command_cwd = resolved_local_path(arguments.get("cwd"), cwd)
        timeout = min(900, max(1, int(arguments.get("timeout_seconds") or 120)))
        if not command:
            return "ERROR: command must not be empty"
        if not os.path.isdir(command_cwd):
            return f"ERROR: working directory not found: {command_cwd}"
        emit({"type": "work", "kind": "command", "content": f"$ cd {shlex.quote(command_cwd)} && {command}"})
        try:
            result = subprocess.run(
                ["bash", "-lc", command],
                cwd=command_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = result.stdout or ""
            if output:
                emit({"type": "work", "kind": "system", "content": bounded_text(output, 12_000)})
            return bounded_text(f"exit_code={result.returncode}\n{output}")
        except subprocess.TimeoutExpired as error:
            output = clean_text(error.stdout)
            return bounded_text(f"ERROR: command timed out after {timeout}s\n{output}")

    if name == "open_path":
        target = clean_text(arguments.get("target"))
        if not re.match(r"^https?://", target, flags=re.IGNORECASE):
            target = resolved_local_path(target, cwd)
            if not os.path.exists(target):
                return f"ERROR: target not found: {target}"
        subprocess.Popen(
            ["xdg-open", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        emit({"type": "work", "kind": "command", "content": f"Opened {target}"})
        return f"OK: opened {target}"

    if name == "launch_app":
        command = clean_text(arguments.get("command"))
        command_cwd = resolved_local_path(arguments.get("cwd"), cwd)
        if not command:
            return "ERROR: command must not be empty"
        if not os.path.isdir(command_cwd):
            return f"ERROR: working directory not found: {command_cwd}"
        subprocess.Popen(
            ["bash", "-lc", command],
            cwd=command_cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        emit({"type": "work", "kind": "command", "content": f"Launched from {command_cwd}: {command}"})
        return f"OK: launched {command}"

    if name == "web_fetch":
        url = clean_text(arguments.get("url"))
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return "ERROR: only HTTP and HTTPS URLs are supported"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "AgentWorkbenchNext/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(LOCAL_TOOL_MAX_OUTPUT + 1)
            charset = response.headers.get_content_charset() or "utf-8"
        emit({"type": "work", "kind": "tool", "content": f"Fetched {url}"})
        return bounded_text(body.decode(charset, errors="replace"))

    if name == "list_connectors":
        emit(
            {
                "type": "work",
                "kind": "command",
                "content": "Checked direct Qwen MCP connector status",
            }
        )
        return bounded_text(json.dumps(connector_statuses(force=True), indent=2))

    if name == "connector_task":
        connector = clean_text(arguments.get("connector")).lower()
        prompt = clean_text(arguments.get("prompt"))
        timeout = min(900, max(10, int(arguments.get("timeout_seconds") or 300)))
        if connector not in SUPPORTED_CONNECTORS:
            return f"ERROR: unsupported connector: {connector}"
        if not prompt:
            return "ERROR: connector prompt must not be empty"
        if connector == "robinhood":
            write_action = re.search(
                r"\b(buy|sell|trade|order|cancel|exercise|deposit|withdraw|transfer)\b",
                prompt,
                flags=re.IGNORECASE,
            )
            if write_action and "CONFIRM ROBINHOOD ACTION" not in prompt:
                return (
                    "ERROR: Robinhood write actions require the exact phrase "
                    "CONFIRM ROBINHOOD ACTION in the connector prompt."
                )
        emit(
            {
                "type": "work",
                "kind": "worker",
                "content": f"Local Qwen MCP · {connector}",
            }
        )
        emit(
            {
                "type": "work",
                "kind": "command",
                "content": f"Direct MCP task · {connector}",
            }
        )
        try:
            return bounded_text(
                direct_connector_task(
                    connector,
                    prompt,
                    cwd,
                    timeout,
                    emit,
                    control,
                )
            )
        except subprocess.TimeoutExpired:
            return bounded_text(
                f"ERROR: connector task timed out after {timeout}s"
            )

    if name == "delegate_agent":
        agent = clean_text(arguments.get("agent")).lower()
        prompt = clean_text(arguments.get("prompt"))
        agent_cwd = resolved_local_path(arguments.get("cwd"), cwd)
        timeout = min(900, max(10, int(arguments.get("timeout_seconds") or 300)))
        if agent == "claude":
            command = [
                "claude",
                "-p",
                prompt,
                "--output-format",
                "text",
                "--permission-mode",
                "bypassPermissions",
                "--dangerously-skip-permissions",
                "--no-session-persistence",
            ]
        elif agent == "codex":
            command = [
                "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--skip-git-repo-check",
                "-C",
                agent_cwd,
                prompt,
            ]
        elif agent == "gemini":
            command = ["gemini", "-p", prompt, "--approval-mode", "yolo", "-o", "text"]
        else:
            return f"ERROR: unsupported delegated agent: {agent}"
        if not shutil.which(command[0]):
            return f"ERROR: {command[0]} is not installed"
        emit({"type": "work", "kind": "worker", "content": f"{agent} delegated task"})
        emit({"type": "work", "kind": "command", "content": f"$ {agent} <delegated prompt>"})
        try:
            result = subprocess.run(
                command,
                cwd=agent_cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            return bounded_text(f"ERROR: {agent} timed out after {timeout}s\n{clean_text(error.stdout)}")
        return bounded_text(
            f"exit_code={result.returncode}\n{result.stdout or ''}"
        )

    return f"ERROR: unknown tool: {name}"


def stream_local_tool_agent(session, user_text, model, emit, control):
    installed = ollama_models()
    if not model or model in {"Default", "Auto"} or model not in installed:
        model = installed[0] if installed else (model or "qwen3-coder:30b")
    cwd = os.path.abspath(
        os.path.expanduser(clean_text(session.get("cwd")) or os.path.expanduser("~"))
    )
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
        session["cwd"] = cwd
    backend = session.setdefault("backend", {})
    backend["model"] = model
    backend["local_tool_agent"] = True
    emit({"type": "meta", "backend": "local-tools", "model": model})
    emit(
        {
            "type": "work",
            "kind": "report",
            "content": (
                f"Local tool agent active · unrestricted user access · cwd {cwd}"
            ),
        }
    )

    messages = [
        {"role": "system", "content": local_agent_system_prompt(cwd, model)},
        *bounded_local_history(session),
        {"role": "user", "content": user_text},
    ]
    tools = local_agent_tools()
    total_input = 0
    total_output = 0
    last_prompt_tokens = 0
    repeated_calls = {}

    for step in range(LOCAL_AGENT_MAX_STEPS):
        if control.cancel.is_set():
            raise RuntimeError("Turn stopped.")
        payload = json.dumps(
            {
                "model": model,
                "messages": messages,
                "tools": tools,
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{OLLAMA_BASE}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        emit(
            {
                "type": "work",
                "kind": "reasoning",
                "content": (
                    "Planning the next action…"
                    if step == 0
                    else f"Evaluating tool results · step {step + 1}"
                ),
            }
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
        total_input += int(result.get("prompt_eval_count") or 0)
        total_output += int(result.get("eval_count") or 0)
        last_prompt_tokens = int(result.get("prompt_eval_count") or last_prompt_tokens)
        message = result.get("message") or {}
        content = clean_text(message.get("content"))
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            if not content:
                content = "The local model completed without returning a response."
            emit({"type": "delta", "text": content})
            usage = {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_input + total_output,
                "model_context_window": 262_144,
            }
            emit_usage(emit, usage)
            update_context(
                session,
                {
                    "input_tokens": last_prompt_tokens,
                    "model_context_window": 262_144,
                },
                model,
            )
            return content, model

        if content:
            emit({"type": "work", "kind": "reasoning", "content": content})
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            function = call.get("function") or {}
            name = clean_text(function.get("name"))
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            signature = json.dumps(
                {"name": name, "arguments": arguments},
                sort_keys=True,
                default=str,
            )
            repeated_calls[signature] = repeated_calls.get(signature, 0) + 1
            if repeated_calls[signature] > 2:
                tool_result = (
                    "ERROR: identical tool call repeated. Inspect the prior result "
                    "and choose a different action."
                )
            else:
                try:
                    tool_result = local_tool_result(
                        name,
                        arguments,
                        cwd,
                        emit,
                        control,
                    )
                except Exception as error:
                    tool_result = f"ERROR: {type(error).__name__}: {error}"
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": bounded_text(tool_result),
                }
            )

    raise RuntimeError(
        f"Local agent exceeded {LOCAL_AGENT_MAX_STEPS} tool steps without completing."
    )


def stream_claude(session, user_text, model, emit, control):
    backend = session.setdefault("backend", {})
    session_id = backend.get("session_id")
    first_turn = not session_id
    if first_turn:
        session_id = str(uuid.uuid4())
    command = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]
    if orchestration_mode() == "smart":
        command += [
            "--append-system-prompt",
            CLAUDE_SMART_ORCHESTRATION_PROMPT,
            "--agents",
            CLAUDE_SMART_AGENTS_JSON,
        ]
    model = clean_text(model or backend.get("model"))
    effort = clean_text(backend.get("effort"))
    if model and model != "Default":
        command += ["--model", model]
    if effort and effort != "Default":
        command += ["--effort", effort]
    if first_turn:
        command += ["--session-id", session_id]
        title = clean_text(session.get("title"))
        if title and not title.startswith("New "):
            command += ["--name", title]
    else:
        command += ["--resume", session_id]
    command.append(user_text)
    emit({"type": "meta", "backend": "claude", "model": model or "default"})
    logged_command = list(command[:-1])
    if "--append-system-prompt" in logged_command:
        index = logged_command.index("--append-system-prompt") + 1
        logged_command[index] = "<smart orchestration policy>"
    if "--agents" in logged_command:
        index = logged_command.index("--agents") + 1
        logged_command[index] = "<smart agent profiles>"
    emit(
        {
            "type": "work",
            "kind": "command",
            "content": "$ " + shlex.join(logged_command + ["<prompt>"]),
        }
    )
    process = run_process(command, session.get("cwd") or os.path.expanduser("~"), control)
    full = []
    for raw in process.stdout:
        if control.cancel.is_set():
            break
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            emit({"type": "work", "kind": "system", "content": line})
            continue
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            new_id = event.get("session_id")
            if new_id:
                backend["session_id"] = new_id
        elif event_type == "assistant":
            message = event.get("message") or {}
            update_context(session, message.get("usage") or {}, message.get("model") or model)
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    piece = block.get("text", "")
                    if piece:
                        full.append(piece)
                        emit({"type": "delta", "text": piece})
                elif block.get("type") == "tool_use":
                    tool_name = clean_text(block.get("name")) or "Claude tool"
                    emit(
                        {
                            "type": "work",
                            "kind": "tool",
                            "content": tool_name,
                        }
                    )
                    if tool_name in {"Agent", "Task"}:
                        tool_input = block.get("input") or {}
                        worker_name = clean_text(
                            tool_input.get("subagent_type")
                            or tool_input.get("agent")
                            or tool_input.get("description")
                            or "Claude sub-agent"
                        )
                        worker_model = clean_text(tool_input.get("model"))
                        emit(
                            {
                                "type": "work",
                                "kind": "worker",
                                "content": (
                                    f"{worker_name} · {worker_model}"
                                    if worker_model
                                    else worker_name
                                ),
                            }
                        )
        elif event_type == "result":
            result = clean_text(event.get("result"))
            if result and not full:
                full.append(result)
                emit({"type": "delta", "text": result})
            emit_usage(emit, event.get("usage") or {})
            if event.get("subtype") not in {None, "success", "completed"}:
                raise RuntimeError(result or clean_text(event.get("error")) or "Claude turn failed.")
            break
        else:
            emit({"type": "work", "kind": "system", "content": line})
    process.wait()
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")
    if process.returncode not in (0, None) and not full:
        error = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"Claude exited {process.returncode}: {error[:600]}")
    backend["session_id"] = backend.get("session_id") or session_id
    return "".join(full), model or "default"


def stream_gemini(session, user_text, model, emit, control):
    backend = session.setdefault("backend", {})
    session_id = backend.get("session_id")
    first_turn = not session_id
    if first_turn:
        session_id = str(uuid.uuid4())
    command = [
        "gemini",
        "-p",
        user_text,
        "--output-format",
        "stream-json",
        "--yolo",
        "--skip-trust",
    ]
    model = clean_text(model or backend.get("model"))
    if model and model not in {"Default", "Auto"}:
        command += ["--model", model]
    if first_turn:
        command += ["--session-id", session_id]
    else:
        command += ["--resume", session_id]
    emit({"type": "meta", "backend": "gemini", "model": model or "Auto"})
    emit({"type": "work", "kind": "command", "content": "$ gemini -p <prompt> --output-format stream-json --yolo --skip-trust"})
    process = run_process(command, session.get("cwd") or os.path.expanduser("~"), control)
    visible_chunks = []
    final_text = ""
    for raw in process.stdout:
        if control.cancel.is_set():
            break
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            emit({"type": "work", "kind": "system", "content": line})
            continue
        event_type = event.get("type")
        if event_type == "init":
            backend["session_id"] = event.get("session_id") or session_id
        elif event_type == "message":
            if event.get("role") == "assistant":
                thought, visible = split_gemini_visible_text(clean_text(event.get("content")))
                if thought:
                    emit({"type": "work", "kind": "reasoning", "content": thought})
                if visible:
                    visible_chunks.append(visible)
        elif event_type == "result":
            thought, visible = split_gemini_visible_text(clean_text(event.get("result")))
            if thought:
                emit({"type": "work", "kind": "reasoning", "content": thought})
            final_text = visible or clean_text(" ".join(visible_chunks))
            if final_text:
                emit({"type": "delta", "text": final_text})
            emit_usage(emit, event.get("stats") or {})
            if event.get("status") not in {None, "success", "completed"}:
                raise RuntimeError(final_text or "Gemini turn failed.")
            break
        else:
            emit({"type": "work", "kind": "system", "content": line})
    process.wait()
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")
    if process.returncode not in (0, None) and not final_text:
        error = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"Gemini exited {process.returncode}: {error[:600]}")
    backend["session_id"] = backend.get("session_id") or session_id
    return final_text, model or "Auto"


def custom_client_config_for_session(session):
    agent_key = clean_text(session.get("agent")).lower()
    config = load_json(CONFIG_FILE, {}) or {}
    for client in config.get("custom_clients") or []:
        if not isinstance(client, dict):
            continue
        if clean_text(client.get("key")).lower() == agent_key:
            resolved = dict(client)
            model = custom_client_ollama_model(resolved)
            if custom_client_is_aider(resolved) and model:
                resolved.setdefault("backend", "ollama-tools")
                resolved.setdefault("model", model)
                resolved.setdefault("access", "full-user")
            return resolved
    return None


def looks_like_git_repo(path):
    if not path:
        return False
    root = Path(os.path.abspath(os.path.expanduser(clean_text(path))))
    if not root.is_dir():
        return False
    git_dir = root / ".git"
    return git_dir.is_dir() or git_dir.is_file()


def custom_client_is_aider(client):
    command = clean_text((client or {}).get("command"))
    try:
        return any(Path(token).name == "aider" for token in shlex.split(command))
    except ValueError:
        return False


def custom_client_ollama_model(client):
    command = clean_text((client or {}).get("command"))
    match = re.search(r"--model\s+(?:ollama_chat|ollama)/([^\s\"']+)", command)
    if match:
        return clean_text(match.group(1))
    label = clean_text((client or {}).get("model") or (client or {}).get("label"))
    return label if label in ollama_models() else ""


def custom_session_slug(session):
    title = re.sub(
        r"[^a-z0-9]+",
        "-",
        clean_text(session.get("title")).lower(),
    ).strip("-")
    short_id = clean_text(session.get("id"))[:8] or uuid.uuid4().hex[:8]
    return f"{title[:48] or 'custom-project'}-{short_id}"


def ensure_aider_workspace(session):
    home = os.path.abspath(os.path.expanduser("~"))
    current = os.path.abspath(
        os.path.expanduser(clean_text(session.get("cwd")) or home)
    )
    if current != home and os.path.isdir(current):
        return current

    workspace = os.path.join(CUSTOM_PROJECTS_DIR, custom_session_slug(session))
    os.makedirs(workspace, exist_ok=True)
    if not looks_like_git_repo(workspace):
        subprocess.run(
            ["git", "init", "-q", workspace],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    session["cwd"] = workspace
    backend = session.setdefault("backend", {})
    backend["workspace_auto_created"] = True
    return workspace


def preferred_custom_cli_cwd(session):
    session_cwd = clean_text(session.get("cwd")) or os.path.expanduser("~")
    if looks_like_git_repo(session_cwd) and not os.path.abspath(os.path.expanduser(session_cwd)) == os.path.abspath(os.path.expanduser("~")):
        return os.path.abspath(os.path.expanduser(session_cwd))

    config = load_json(CONFIG_FILE, {}) or {}
    candidates = []
    for value in [
        config.get("cwd"),
        *(config.get("recent_projects") or []),
    ]:
        value = clean_text(value)
        if value:
            candidates.append(value)
    for workspace in config.get("workspace_tabs") or []:
        if isinstance(workspace, dict):
            value = clean_text(workspace.get("cwd"))
            if value:
                candidates.append(value)
    for value in candidates:
        absolute = os.path.abspath(os.path.expanduser(value))
        if looks_like_git_repo(absolute):
            return absolute

    return os.path.abspath(os.path.expanduser(session_cwd))


def add_aider_session_options(command, cwd):
    additions = []
    options = {
        "--no-pretty": None,
        "--no-fancy-input": None,
        "--restore-chat-history": None,
        "--chat-history-file": os.path.join(cwd, ".aider.chat.history.md"),
        "--input-history-file": os.path.join(cwd, ".aider.input.history"),
    }
    for option, value in options.items():
        if option in command:
            continue
        additions.append(option)
        if value:
            additions.append(value)
    for path in aider_editable_files(cwd):
        additions.extend(["--file", path])
    insert_at = command.index("--message") if "--message" in command else len(command)
    command[insert_at:insert_at] = additions


def aider_editable_files(cwd):
    command = [
        "git",
        "-C",
        cwd,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    candidates = []
    for relative in result.stdout.splitlines():
        relative = relative.strip()
        if not relative or relative.startswith("."):
            continue
        path = os.path.abspath(os.path.join(cwd, relative))
        if not path.startswith(os.path.abspath(cwd) + os.sep):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if os.path.isfile(path) and size <= 250_000:
            candidates.append((size, path))
    candidates.sort(key=lambda item: item[0])
    return [path for _size, path in candidates[:24]]


def aider_operational_prompt(user_text, cwd):
    return f"""You are operating through Aider with direct read/write access to the project at:
{cwd}

Files supplied by Aider are real local files. You may also create new files in this project.
Never claim that you cannot access or save project files. When the user asks you to build,
save, edit, fix, or create something, perform the file changes through Aider and then report
the exact paths changed. Do not tell the user to copy or save code manually.

User request:
{user_text}"""


def token_number(value, suffix):
    number = float(value)
    multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix.lower(), 1)
    return int(number * multiplier)


def clean_aider_output(lines, emit):
    visible = []
    startup = []
    input_tokens = 0
    output_tokens = 0
    token_pattern = re.compile(
        r"Tokens:\s*([\d.]+)\s*([kKmM]?)\s+sent,\s*"
        r"([\d.]+)\s*([kKmM]?)\s+received\.\s*"
    )
    startup_patterns = (
        "Detected dumb terminal",
        "Warning: Input is not a terminal",
        "You should probably run aider",
        "Aider v",
        "Model:",
        "Git repo:",
        "Repo-map:",
    )
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            if visible and visible[-1] != "":
                visible.append("")
            continue
        if set(line) <= {"─", "-", " "}:
            continue
        match = token_pattern.search(line)
        if match:
            input_tokens = token_number(match.group(1), match.group(2))
            output_tokens = token_number(match.group(3), match.group(4))
            line = token_pattern.sub("", line).strip()
            if not line:
                continue
        if line.startswith(startup_patterns):
            startup.append(line)
            continue
        visible.append(line)
    if startup:
        emit(
            {
                "type": "work",
                "kind": "system",
                "content": "\n".join(startup),
            }
        )
    if input_tokens or output_tokens:
        emit_usage(
            emit,
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )
    return "\n".join(visible).strip()


def interpolate_custom_command(command, prompt, model, cwd, title):
    result = []
    for token in shlex.split(command):
        token = token.replace("{prompt}", prompt)
        token = token.replace("{model}", model)
        token = token.replace("{cwd}", cwd)
        token = token.replace("{title}", title)
        result.append(token)
    return result


def stream_custom_cli(session, user_text, model, emit, control):
    client = custom_client_config_for_session(session)
    if not client:
        raise RuntimeError("custom client configuration not found")
    command_template = clean_text(client.get("command"))
    configured_backend = clean_text(client.get("backend")).lower()
    is_native_local = configured_backend in {
        "local-tools",
        "ollama-tools",
        "native-ollama",
    }
    if not command_template and not is_native_local:
        raise RuntimeError("custom client command is missing")
    is_aider = custom_client_is_aider(client)
    backend = session.setdefault("backend", {})
    model = clean_text(model or backend.get("model"))
    if not model or model in {"Default", "Auto"}:
        model = clean_text(client.get("model") or client.get("label") or "Default")
    native_ollama_model = custom_client_ollama_model(client)
    if is_native_local or (is_aider and native_ollama_model):
        return stream_local_tool_agent(
            session,
            user_text,
            native_ollama_model or model,
            emit,
            control,
        )
    cwd = ensure_aider_workspace(session) if is_aider else preferred_custom_cli_cwd(session)
    title = clean_text(session.get("title"))
    execution_prompt = aider_operational_prompt(user_text, cwd) if is_aider else user_text
    command = interpolate_custom_command(
        command_template,
        execution_prompt,
        model,
        cwd,
        title,
    )
    if "{prompt}" not in command_template:
        command.append(execution_prompt)
    if is_aider:
        add_aider_session_options(command, cwd)
    emit(
        {
            "type": "meta",
            "backend": "custom",
            "model": model or clean_text(client.get("label")) or "custom",
        }
    )
    emit(
        {
            "type": "work",
            "kind": "command",
            "content": "$ " + shlex.join(command),
        }
    )
    process = run_process(command, cwd, control)
    output_lines = []
    stderr_lines = []

    def read_stderr():
        if not process.stderr:
            return
        for raw in process.stderr:
            if control.cancel.is_set():
                break
            line = clean_text(raw)
            if line:
                stderr_lines.append(line)
                emit({"type": "work", "kind": "system", "content": line})

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    for raw in process.stdout:
        if control.cancel.is_set():
            break
        output_lines.append(raw.rstrip("\n"))

    process.wait()
    stderr_thread.join(timeout=2)
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")
    full_text = (
        clean_aider_output(output_lines, emit)
        if is_aider
        else "\n".join(output_lines).strip()
    )
    if full_text:
        emit({"type": "delta", "text": full_text})
    if process.returncode not in (0, None) and not full_text:
        error = "\n".join(stderr_lines)
        if not error and process.stderr:
            error = process.stderr.read()[:600]
        raise RuntimeError(f"Custom CLI exited {process.returncode}: {clean_text(error)[:600]}")
    backend["custom_client_key"] = clean_text(session.get("agent"))
    backend["model"] = model
    return full_text, model or "Default"


def codex_item_text(item):
    if not isinstance(item, dict):
        return ""
    text = clean_text(item.get("text") or item.get("content"))
    if text:
        return text
    parts = []
    for part in item.get("content") or []:
        if isinstance(part, dict) and part.get("type") in {"text", "output_text"}:
            parts.append(clean_text(part.get("text")))
    return "\n".join(part for part in parts if part)


def stream_codex(session, user_text, model, emit, control):
    backend = session.setdefault("backend", {})
    thread_id = backend.get("thread_id") or backend.get("session_id")
    base = ["codex", "exec"]
    if thread_id:
        base += ["resume"]
    command = base + [
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--skip-git-repo-check",
    ]
    model = clean_text(model or backend.get("model"))
    effort = clean_text(backend.get("effort"))
    if model and model != "Default":
        command += ["--model", model]
    if effort and effort != "Default":
        command += ["-c", f'model_reasoning_effort="{effort}"']
    if thread_id:
        command += [thread_id, user_text]
    else:
        command += ["-C", session.get("cwd") or os.path.expanduser("~"), user_text]
    emit({"type": "meta", "backend": "codex", "model": model or "default"})
    emit({"type": "work", "kind": "command", "content": "$ codex exec <prompt>"})
    process = run_process(command, session.get("cwd") or os.path.expanduser("~"), control)
    full = []
    for raw in process.stdout:
        if control.cancel.is_set():
            break
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            emit({"type": "work", "kind": "system", "content": line})
            continue
        event_type = clean_text(event.get("type"))
        item = event.get("item") or {}
        if event_type in {"thread.started", "thread/started", "thread.created"}:
            new_id = event.get("thread_id") or (event.get("thread") or {}).get("id")
            if new_id:
                backend["thread_id"] = new_id
        elif event_type in {"item.completed", "item/completed"}:
            item_type = clean_text(item.get("type"))
            if item_type in {"agent_message", "agentMessage"}:
                text = codex_item_text(item)
                if text:
                    full.append(text)
                    emit({"type": "delta", "text": text})
            elif item_type in {"command_execution", "commandExecution"}:
                command_text = clean_text(item.get("command"))
                output = clean_text(item.get("aggregated_output") or item.get("aggregatedOutput"))
                if command_text:
                    emit({"type": "work", "kind": "commandExecution", "content": "$ " + command_text})
                if output:
                    emit({"type": "work", "kind": "system", "content": output})
            elif item_type in {"file_change", "fileChange"}:
                emit({"type": "work", "kind": "fileChange", "content": json.dumps(item.get("changes") or item)})
            elif item_type == "reasoning":
                text = codex_item_text(item) or clean_text(" ".join(item.get("summary") or []))
                if text:
                    emit({"type": "work", "kind": "reasoning", "content": text})
        elif event_type in {"turn.completed", "turn/completed"}:
            usage = event.get("usage") or (event.get("turn") or {}).get("usage") or {}
            emit_usage(emit, usage)
            update_context(session, usage, model)
            turn = event.get("turn") or {}
            if clean_text(turn.get("status")) in {"failed", "error"}:
                raise RuntimeError(clean_text((turn.get("error") or {}).get("message")) or "Codex turn failed.")
        elif event_type == "error":
            raise RuntimeError(clean_text(event.get("message") or event.get("error")) or "Codex error.")
        elif event_type not in {"turn.started", "item.started"}:
            emit({"type": "work", "kind": "system", "content": line})
    process.wait()
    if control.cancel.is_set():
        raise RuntimeError("Turn stopped.")
    if process.returncode not in (0, None) and not full:
        error = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"Codex exited {process.returncode}: {error[:600]}")
    return "\n\n".join(full), model or "default"


def run_turn(session, user_text, requested_model, emit, control):
    backend = pick_backend(session)
    model = requested_model or (session.get("backend") or {}).get("model")
    if backend == "custom":
        return stream_custom_cli(session, user_text, model, emit, control)
    if backend == "ollama":
        return stream_ollama(session, user_text, model, emit, control)
    if backend == "claude":
        return stream_claude(session, user_text, model, emit, control)
    if backend == "codex":
        return stream_codex(session, user_text, model, emit, control)
    if backend == "gemini":
        return stream_gemini(session, user_text, model, emit, control)
    raise RuntimeError(f"Unsupported backend: {backend}")


def format_reset_time(timestamp):
    try:
        moment = datetime.fromtimestamp(float(timestamp)).astimezone()
        return moment.strftime("%b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except (TypeError, ValueError, OSError):
        return ""


def codex_usage_limits():
    root = Path(os.path.expanduser("~/.codex/sessions"))
    try:
        candidates = sorted(
            root.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:16]
    except OSError:
        candidates = []
    for path in candidates:
        latest = None
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    rate_limits = (event.get("payload") or {}).get("rate_limits")
                    if isinstance(rate_limits, dict):
                        latest = rate_limits
        except OSError:
            continue
        if not latest:
            continue
        windows = []
        for key in ("primary", "secondary"):
            value = latest.get(key) or {}
            if not isinstance(value, dict) or value.get("used_percent") is None:
                continue
            minutes = int(value.get("window_minutes") or 0)
            windows.append(
                {
                    "label": "5h" if minutes and minutes <= 360 else "week",
                    "used_percent": round(float(value.get("used_percent") or 0)),
                    "reset": format_reset_time(value.get("resets_at")),
                }
            )
        if windows:
            return {"provider": "Codex", "windows": windows}
    return {"provider": "Codex", "windows": [], "error": "No rate-limit telemetry yet."}


def claude_usage_limits():
    try:
        result = subprocess.run(
            [
                "claude",
                "-p",
                "/usage",
                "--output-format",
                "json",
                "--permission-mode",
                "default",
                "--tools",
                "",
                "--no-session-persistence",
            ],
            cwd=os.path.expanduser("~"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"provider": "Claude", "windows": [], "error": str(error)}
    try:
        payload = json.loads(result.stdout or "{}")
        text = clean_text(payload.get("result"))
    except ValueError:
        text = clean_text(result.stdout)
    windows = []
    for label, pattern in (
        ("5h", r"Current session:\s*(\d+)% used\s*·\s*resets\s*(.+)"),
        ("week", r"Current week \(all models\):\s*(\d+)% used\s*·\s*resets\s*(.+)"),
        ("sonnet", r"Current week \(Sonnet only\):\s*(\d+)% used\s*·\s*resets\s*(.+)"),
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            windows.append(
                {
                    "label": label,
                    "used_percent": int(match.group(1)),
                    "reset": clean_text(match.group(2)),
                }
            )
    if windows:
        return {"provider": "Claude", "windows": windows}
    return {"provider": "Claude", "windows": [], "error": text or "Claude usage unavailable."}


def usage_limits(agent):
    normalized = clean_text(agent).lower()
    key = "claude" if "claude" in normalized else "codex" if "codex" in normalized else normalized
    cached = LIMIT_CACHE.get(key)
    if cached and time.monotonic() - cached["time"] < LIMIT_CACHE_SECONDS:
        return cached["value"]
    if key == "claude":
        value = claude_usage_limits()
    elif key == "codex":
        value = codex_usage_limits()
    else:
        value = {
            "provider": normalized.capitalize() or "Client",
            "windows": [],
            "error": "This client does not expose account-limit telemetry.",
        }
    LIMIT_CACHE[key] = {"time": time.monotonic(), "value": value}
    return value


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json(
                200,
                {
                    "ok": True,
                    "ollama_models": ollama_models(),
                    "local_agent": {
                        "backend": "ollama-tools",
                        "access": "full-user",
                        "tools": [
                            tool["function"]["name"] for tool in local_agent_tools()
                        ],
                    },
                },
            )
            return
        if parsed.path == "/limits":
            agent = urllib.parse.parse_qs(parsed.query).get("agent", [""])[0]
            self._json(200, usage_limits(agent))
            return
        if parsed.path == "/connectors":
            try:
                force = (
                    urllib.parse.parse_qs(parsed.query).get("refresh", ["0"])[0]
                    == "1"
                )
                self._json(
                    200,
                    {"ok": True, "connectors": connector_statuses(force=force)},
                )
            except Exception as error:
                self._json(500, {"ok": False, "error": clean_text(error)})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            request = {}

        if parsed.path == "/stop":
            session_id = clean_text(request.get("session_id"))
            with ACTIVE_LOCK:
                control = ACTIVE_TURNS.get(session_id)
            if control:
                control.stop()
            self._json(200, {"ok": bool(control)})
            return

        if parsed.path == "/handoff/stage":
            session_id = clean_text(request.get("session_id"))
            try:
                session = load_session(session_id)
                artifact = stage_handoff(session, force=True)
                save_session(session)
                self._json(200, {"ok": True, "artifact": artifact})
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/handoff/deploy":
            try:
                session = deploy_handoff(
                    clean_text(request.get("session_id")),
                    clean_text(request.get("agent")) or "claude",
                    clean_text(request.get("model")) or "Default",
                    clean_text(request.get("effort")) or "Default",
                )
                self._json(200, {"ok": True, "session": session})
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/handoff/auto":
            try:
                session = maybe_auto_deploy_handoff(
                    clean_text(request.get("session_id"))
                )
                self._json(200, {"ok": True, "session": session})
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/history/sync":
            try:
                self._json(200, {"ok": True, **sync_native_history()})
            except Exception as error:
                self._json(500, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/history/load":
            try:
                session = load_native_transcript(clean_text(request.get("session_id")))
                self._json(
                    200,
                    {
                        "ok": True,
                        "session_id": session.get("id"),
                        "turn_count": len(session.get("turns") or []),
                    },
                )
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/connectors/auth":
            try:
                value = start_connector_auth(
                    clean_text(request.get("connector")).lower(),
                    clean_text(request.get("client_id")),
                    clean_text(request.get("client_secret")),
                )
                self._json(202, {"ok": True, **value})
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path == "/connectors/token":
            try:
                value = set_connector_token(
                    clean_text(request.get("connector")).lower(),
                    request.get("token") or "",
                )
                self._json(200, value)
            except Exception as error:
                self._json(400, {"ok": False, "error": clean_text(error)})
            return

        if parsed.path != "/send":
            self._json(404, {"error": "not found"})
            return

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        control = TurnControl()

        def emit(event):
            try:
                self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                control.stop()

        session_id = clean_text(request.get("session_id"))
        visible_prompt = clean_text(request.get("message"))
        attachments = [
            os.path.abspath(os.path.expanduser(clean_text(path)))
            for path in request.get("attachments") or []
            if clean_text(path)
        ]
        attachments = [path for path in attachments if os.path.isfile(path)]
        if not session_id:
            emit({"type": "error", "message": "missing session_id"})
            return
        if not visible_prompt and not attachments:
            emit({"type": "error", "message": "empty message"})
            return

        try:
            session = load_session(session_id)
        except Exception as error:
            emit({"type": "error", "message": f"session not found: {error}"})
            return

        with ACTIVE_LOCK:
            if session_id in ACTIVE_TURNS:
                emit({"type": "error", "message": "this session already has an active turn"})
                return
            ACTIVE_TURNS[session_id] = control

        if not visible_prompt:
            names = ", ".join(os.path.basename(path) for path in attachments)
            visible_prompt = (
                "Please inspect and respond to the attached "
                f"file{'s' if len(attachments) != 1 else ''}: {names}"
            )

        inherited_context = clean_text(session.get("handoff_context"))
        context_parts = [authoritative_time_context()]
        if inherited_context and not session.get("turns"):
            context_parts.append(inherited_context)
        context_parts.append(visible_prompt)
        backend_prompt = prompt_with_attachments("\n\n".join(context_parts), attachments)
        created_at = now_iso()
        turn = {
            "id": str(uuid.uuid4()),
            "created_at": created_at,
            "status": "running",
            "prompt": visible_prompt,
            "items": [
                {
                    "type": "userMessage",
                    "content": [{"type": "text", "text": visible_prompt}],
                }
            ],
        }
        if attachments:
            turn["items"].append(
                {
                    "type": "fileChange",
                    "content": "Attached files:\n" + "\n".join(attachments),
                }
            )
        session.setdefault("turns", []).append(turn)
        session["updated_at"] = created_at
        save_session(session)

        assistant_item = {"type": "agentMessage", "text": ""}
        last_checkpoint = 0.0

        def durable_emit(event):
            nonlocal last_checkpoint
            event_type = event.get("type")
            if event_type == "delta":
                if assistant_item not in turn["items"]:
                    turn["items"].append(assistant_item)
                assistant_item["text"] += str(event.get("text") or "")
                if time.monotonic() - last_checkpoint >= 1.0:
                    session["updated_at"] = now_iso()
                    save_session(session)
                    last_checkpoint = time.monotonic()
            elif event_type == "work":
                content = clean_text(event.get("content"))
                if content:
                    kind = clean_text(event.get("kind")) or "system"
                    item_type = {
                        "command": "commandExecution",
                        "reasoning": "reasoning",
                        "fileChange": "fileChange",
                        "report": "report",
                        "worker": "report",
                    }.get(kind, "system")
                    turn["items"].append(
                        {
                            "type": item_type,
                            "tag": kind,
                            "content": content,
                        }
                    )
                    session["updated_at"] = now_iso()
                    save_session(session)
            emit(event)

        try:
            full_text, _model = run_turn(
                session,
                backend_prompt,
                request.get("model"),
                durable_emit,
                control,
            )
            if full_text and not assistant_item["text"]:
                assistant_item["text"] = full_text
                turn["items"].append(assistant_item)
            turn["status"] = "completed"
        except Exception as error:
            message = clean_text(error) or "Backend error"
            turn["status"] = "cancelled" if control.cancel.is_set() else "error"
            turn["items"].append({"type": "note", "text": "ERROR: " + message})
            emit({"type": "error", "message": message})
        finally:
            turn["completed_at"] = now_iso()
            session["updated_at"] = turn["completed_at"]
            try:
                stage_handoff(session)
            except Exception:
                pass
            save_session(session)
            with ACTIVE_LOCK:
                ACTIVE_TURNS.pop(session_id, None)
        emit({"type": "done", "turn_id": turn["id"], "status": turn["status"]})


def main():
    print(f"awbench_server → http://{HOST}:{PORT}  (ollama={OLLAMA_BASE})")
    print(f"  sessions: {SESS_DIR}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
