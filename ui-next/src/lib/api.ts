import { invoke } from "@tauri-apps/api/core";
import type {
  AgentId,
  Artifact,
  Message,
  Session,
  UsageLimits,
  WorkspaceTab,
} from "./types";

export function normalizeAgent(raw: string): AgentId {
  const value = (raw ?? "").toLowerCase();
  if (value.includes("codex") || value.includes("gpt")) return "codex";
  if (value.includes("gemini")) return "gemini";
  if (
    value.includes("claude") ||
    value.includes("opus") ||
    value.includes("sonnet") ||
    value.includes("haiku") ||
    value.includes("fable")
  ) {
    return "claude";
  }
  return "local";
}

function relativeTime(iso: string): string {
  if (!iso) return "";
  const timestamp = new Date(iso).getTime();
  if (!Number.isFinite(timestamp)) return "";
  const diff = Math.max(0, Date.now() - timestamp);
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 2) return "Now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "Yesterday" : `${days}d`;
}

function formatTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
  });
}

function projectLabel(cwd: string): string {
  if (!cwd) return "~";
  return `~${cwd.replace(/^\/home\/[^/]+/, "") || "/"}`;
}

export async function listSessions(): Promise<Session[]> {
  const raw = await invoke<unknown[]>("list_sessions");
  return raw
    .filter(
      (session): session is Record<string, unknown> =>
        session !== null && typeof session === "object" && !Array.isArray(session),
    )
    .map((session) => {
      const cwd = String(session["cwd"] ?? "");
      const backend = session["backend"] as Record<string, unknown> | null;
      const context = session["context"] as Record<string, unknown> | null;
      const updatedAtIso = String(
        session["updated_at"] ?? session["created_at"] ?? "",
      );
      const agentKey = String(session["agent"] ?? "");
      return {
        id: String(session["id"] ?? ""),
        title: String(session["title"] ?? "Untitled").slice(0, 120),
        agent: normalizeAgent(agentKey),
        agentKey,
        model: String(backend?.["model"] ?? agentKey ?? "unknown"),
        effort: String(backend?.["effort"] ?? "Default"),
        project: projectLabel(cwd),
        cwd,
        updatedAt: relativeTime(updatedAtIso),
        updatedAtIso,
        context:
          context && Number(context["limit"] ?? 0) > 0
            ? {
                used: Number(context["used"] ?? 0),
                limit: Number(context["limit"] ?? 0),
                percent: Number(context["percent"] ?? 0),
                compactCount: Number(context["compact_count"] ?? 0),
                model: String(context["model"] ?? ""),
              }
            : undefined,
      };
    })
    .filter((session) => session.id !== "");
}

type RawMessage = {
  id?: string;
  role?: string;
  agent?: string;
  content?: string;
  time?: string;
};

export async function getSessionMessages(id: string): Promise<Message[]> {
  const raw = await invoke<RawMessage[]>("get_session_messages", { id });
  return raw.map((message, index) => ({
    id: message.id ?? `${id}-message-${index}`,
    role: message.role === "assistant" ? "assistant" : "user",
    agent:
      message.role === "assistant"
        ? normalizeAgent(message.agent ?? "")
        : undefined,
    content: message.content ?? "",
    time: formatTime(message.time ?? ""),
  }));
}

export type WorkLogEntry = {
  id: string;
  type: string;
  content: string;
  time: string;
};

type RawWorkLogEntry = Omit<WorkLogEntry, "time"> & { time?: string };

export async function getSessionWorkLog(id: string): Promise<WorkLogEntry[]> {
  const raw = await invoke<RawWorkLogEntry[]>("get_session_work_log", { id });
  return raw.map((entry) => ({
    ...entry,
    time: formatTime(entry.time ?? ""),
  }));
}

export type AppState = {
  activeAgent: string;
  activeWorkspaceIndex: number;
  agentSettings: Record<string, { model?: string; effort?: string }>;
  claudeOrchestration: { mode?: string };
  customClients: Array<{
    key?: string;
    label?: string;
    command?: string;
    model?: string;
  }>;
  cwd: string;
  handoffPreferences: Record<string, unknown>;
  recentProjects: string[];
  workspaceTabs: Array<{
    title?: string;
    agent?: string;
    cwd?: string;
    model?: string;
    effort?: string;
    session_id?: string;
    input_text?: string;
    attachment_paths?: string[];
    queued_prompts?: Array<{
      prompt?: string;
      attachments?: string[];
    }>;
  }>;
};

export async function getAppState(): Promise<AppState> {
  const raw = await invoke<Record<string, unknown>>("get_app_state");
  return {
    activeAgent: String(raw["active_agent"] ?? "claude"),
    activeWorkspaceIndex: Number(raw["active_workspace_index"] ?? 0),
    agentSettings:
      (raw["agent_settings"] as AppState["agentSettings"]) ?? {},
    claudeOrchestration:
      (raw["claude_orchestration"] as AppState["claudeOrchestration"]) ?? {},
    customClients:
      (raw["custom_clients"] as AppState["customClients"]) ?? [],
    cwd: String(raw["cwd"] ?? ""),
    handoffPreferences:
      (raw["handoff_preferences"] as Record<string, unknown>) ?? {},
    recentProjects: Array.isArray(raw["recent_projects"])
      ? raw["recent_projects"].map(String)
      : [],
    workspaceTabs: Array.isArray(raw["workspace_tabs"])
      ? (raw["workspace_tabs"] as AppState["workspaceTabs"])
      : [],
  };
}

export async function saveWorkspaceState(
  tabs: WorkspaceTab[],
  activeIndex: number,
): Promise<void> {
  const serialized = tabs.map((tab) => ({
    title: tab.label,
    agent: tab.agentKey,
    cwd: tab.cwd,
    model: tab.model,
    effort: tab.effort,
    session_id: tab.sessionId,
    input_text: tab.draft,
    attachment_paths: tab.pendingAttachments,
    queued_prompts: tab.queuedPrompts,
  }));
  await invoke("save_workspace_state", {
    tabs: serialized,
    activeIndex,
  });
}

export async function saveAppPreferences(
  claudeOrchestration: Record<string, unknown>,
  handoffPreferences: Record<string, unknown>,
): Promise<void> {
  await invoke("save_app_preferences", {
    claudeOrchestration,
    handoffPreferences,
  });
}

export async function createSession(input: {
  agent: string;
  model: string;
  effort: string;
  cwd: string;
  title: string;
}): Promise<Session> {
  const raw = await invoke<Record<string, unknown>>("create_session", input);
  return sessionFromRecord(raw, input);
}

function sessionFromRecord(
  raw: Record<string, unknown>,
  fallback: {
    agent: string;
    model: string;
    effort: string;
    cwd: string;
  },
): Session {
  const backend = raw["backend"] as Record<string, unknown> | null;
  const cwd = String(raw["cwd"] ?? fallback.cwd);
  const agentKey = String(raw["agent"] ?? fallback.agent);
  return {
    id: String(raw["id"]),
    title: String(raw["title"] ?? "Untitled"),
    agent: normalizeAgent(agentKey),
    agentKey,
    model: String(backend?.["model"] ?? fallback.model),
    effort: String(backend?.["effort"] ?? fallback.effort),
    project: projectLabel(cwd),
    cwd,
    updatedAt: "Now",
    updatedAtIso: String(raw["updated_at"] ?? ""),
  };
}

export async function renameSession(id: string, title: string): Promise<void> {
  await invoke("rename_session", { id, title });
}

export async function deleteSession(id: string): Promise<void> {
  await invoke("delete_session", { id });
}

type RawArtifact = {
  name?: string;
  path?: string;
  size?: number;
  modified?: number;
  is_image?: boolean;
  is_text?: boolean;
};

function normalizeArtifact(raw: RawArtifact): Artifact {
  return {
    name: raw.name ?? "artifact",
    path: raw.path ?? "",
    size: Number(raw.size ?? 0),
    modified: Number(raw.modified ?? 0),
    isImage: Boolean(raw.is_image),
    isText: Boolean(raw.is_text),
  };
}

export async function listArtifacts(sessionId: string): Promise<Artifact[]> {
  const raw = await invoke<RawArtifact[]>("list_artifacts", { sessionId });
  return raw.map(normalizeArtifact);
}

export async function storeArtifacts(
  sessionId: string,
  paths: string[],
): Promise<Artifact[]> {
  const raw = await invoke<RawArtifact[]>("store_artifacts", {
    sessionId,
    paths,
  });
  return raw.map(normalizeArtifact);
}

export async function renameArtifact(
  sessionId: string,
  path: string,
  newName: string,
): Promise<Artifact> {
  const raw = await invoke<RawArtifact>("rename_artifact", {
    sessionId,
    path,
    newName,
  });
  return normalizeArtifact(raw);
}

export async function deleteArtifact(
  sessionId: string,
  path: string,
): Promise<void> {
  await invoke("delete_artifact", { sessionId, path });
}

export async function pasteClipboardImage(sessionId: string): Promise<Artifact> {
  const raw = await invoke<RawArtifact>("paste_clipboard_image", { sessionId });
  return normalizeArtifact(raw);
}

export async function startTerminal(
  workspaceId: string,
  cwd: string,
  cols: number,
  rows: number,
): Promise<string> {
  return invoke<string>("start_terminal", { workspaceId, cwd, cols, rows });
}

export async function writeTerminal(workspaceId: string, data: string) {
  await invoke("terminal_write", { workspaceId, data });
}

export async function resizeTerminal(
  workspaceId: string,
  cols: number,
  rows: number,
) {
  await invoke("resize_terminal", { workspaceId, cols, rows });
}

export async function stopTerminal(workspaceId: string) {
  await invoke("stop_terminal", { workspaceId });
}

const SEND_SERVER = "http://127.0.0.1:8765";

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number,
) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
}

export type ServerInfo = {
  ok: boolean;
  ollamaModels: string[];
};

export type ConnectorStatus = {
  name: string;
  url: string;
  reachable: boolean;
  authenticated: boolean;
  requiresAuth: boolean;
  oauthConfigured: boolean;
  toolCount: number;
  error?: string;
};

export async function getConnectorStatuses(
  refresh = false,
): Promise<ConnectorStatus[]> {
  const response = await fetchWithTimeout(
    `${SEND_SERVER}/connectors${refresh ? "?refresh=1" : ""}`,
    {},
    70_000,
  );
  const raw = (await response.json()) as {
    ok?: boolean;
    connectors?: Array<Record<string, unknown>>;
    error?: string;
  };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not load connectors");
  }
  return (raw.connectors ?? []).map((connector) => ({
    name: String(connector["name"] ?? ""),
    url: String(connector["url"] ?? ""),
    reachable: Boolean(connector["reachable"]),
    authenticated: Boolean(connector["authenticated"]),
    requiresAuth: Boolean(connector["requires_auth"]),
    oauthConfigured: Boolean(connector["oauth_configured"]),
    toolCount: Number(connector["tool_count"] ?? 0),
    error: connector["error"] ? String(connector["error"]) : undefined,
  }));
}

export async function startConnectorAuth(input: {
  connector: string;
  clientId?: string;
  clientSecret?: string;
}): Promise<void> {
  const response = await fetch(`${SEND_SERVER}/connectors/auth`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      connector: input.connector,
      client_id: input.clientId ?? "",
      client_secret: input.clientSecret ?? "",
    }),
  });
  const raw = (await response.json()) as { ok?: boolean; error?: string };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not start connector login");
  }
}

export async function setConnectorToken(
  connector: string,
  token: string,
): Promise<void> {
  const response = await fetch(`${SEND_SERVER}/connectors/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connector, token }),
  });
  const raw = (await response.json()) as { ok?: boolean; error?: string };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not save connector token");
  }
}

export async function stageHandoff(sessionId: string): Promise<string> {
  const response = await fetch(`${SEND_SERVER}/handoff/stage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  const raw = (await response.json()) as {
    ok?: boolean;
    artifact?: string;
    error?: string;
  };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not stage handoff");
  }
  return raw.artifact ?? "";
}

export async function deployHandoff(input: {
  sessionId: string;
  agent: string;
  model: string;
  effort: string;
  cwd: string;
}): Promise<Session> {
  const response = await fetch(`${SEND_SERVER}/handoff/deploy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: input.sessionId,
      agent: input.agent,
      model: input.model,
      effort: input.effort,
    }),
  });
  const raw = (await response.json()) as {
    ok?: boolean;
    session?: Record<string, unknown>;
    error?: string;
  };
  if (!response.ok || !raw.ok || !raw.session) {
    throw new Error(raw.error || "Could not deploy handoff");
  }
  return sessionFromRecord(raw.session, {
    agent: input.agent,
    model: input.model,
    effort: input.effort,
    cwd: input.cwd,
  });
}

export async function maybeAutoDeployHandoff(
  sessionId: string,
): Promise<Session | undefined> {
  const response = await fetch(`${SEND_SERVER}/handoff/auto`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  const raw = (await response.json()) as {
    ok?: boolean;
    session?: Record<string, unknown> | null;
    error?: string;
  };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not evaluate automatic handoff");
  }
  if (!raw.session) return undefined;
  const backend = raw.session["backend"] as Record<string, unknown> | undefined;
  return sessionFromRecord(raw.session, {
    agent: String(raw.session["agent"] ?? "claude"),
    model: String(backend?.["model"] ?? "Default"),
    effort: String(backend?.["effort"] ?? "Default"),
    cwd: String(raw.session["cwd"] ?? ""),
  });
}

export async function spawnWorker(input: {
  source_id: string;
  prompt: string;
  agent?: string;
  model?: string;
  effort?: string;
}): Promise<Session> {
  const response = await fetch(`${SEND_SERVER}/worker/spawn`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  const raw = (await response.json()) as {
    ok?: boolean;
    session?: Record<string, unknown>;
    error?: string;
  };
  if (!response.ok || !raw.ok || !raw.session) {
    throw new Error(raw.error || "Could not spawn worker session");
  }
  const sess = raw.session;
  const backend = sess["backend"] as Record<string, unknown> | undefined;
  return sessionFromRecord(sess, {
    agent: String(sess["agent"] ?? input.agent ?? "claude"),
    model: String(backend?.["model"] ?? input.model ?? "Default"),
    effort: String(backend?.["effort"] ?? input.effort ?? "Default"),
    cwd: String(sess["cwd"] ?? ""),
  });
}

export async function serverInfo(): Promise<ServerInfo> {
  try {
    const response = await fetchWithTimeout(`${SEND_SERVER}/health`, {}, 2_000);
    if (!response.ok) return { ok: false, ollamaModels: [] };
    const raw = (await response.json()) as {
      ok?: boolean;
      ollama_models?: string[];
    };
    return {
      ok: Boolean(raw.ok),
      ollamaModels: Array.isArray(raw.ollama_models)
        ? raw.ollama_models.map(String)
        : [],
    };
  } catch {
    return { ok: false, ollamaModels: [] };
  }
}

export async function syncNativeHistory(): Promise<void> {
  const response = await fetchWithTimeout(
    `${SEND_SERVER}/history/sync`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    },
    30_000,
  );
  const raw = (await response.json()) as { ok?: boolean; error?: string };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not synchronize native history");
  }
}

export async function loadNativeTranscript(sessionId: string): Promise<void> {
  const response = await fetchWithTimeout(
    `${SEND_SERVER}/history/load`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    },
    60_000,
  );
  const raw = (await response.json()) as { ok?: boolean; error?: string };
  if (!response.ok || !raw.ok) {
    throw new Error(raw.error || "Could not load native transcript");
  }
}

export async function getUsageLimits(agent: string): Promise<UsageLimits> {
  try {
    const response = await fetchWithTimeout(
      `${SEND_SERVER}/limits?agent=${encodeURIComponent(agent)}`,
      {},
      25_000,
    );
    const raw = (await response.json()) as {
      provider?: string;
      windows?: Array<{
        label?: string;
        used_percent?: number;
        reset?: string;
      }>;
      error?: string;
    };
    return {
      provider: raw.provider ?? agent,
      windows: Array.isArray(raw.windows)
        ? raw.windows.map((window) => ({
            label: window.label ?? "",
            usedPercent: Number(window.used_percent ?? 0),
            reset: window.reset ?? "",
          }))
        : [],
      error: raw.error,
    };
  } catch (error) {
    return {
      provider: agent,
      windows: [],
      error: error instanceof Error ? error.message : "Limits unavailable",
    };
  }
}

export type SendResult = {
  ok: boolean;
  error?: string;
  tokenUsage?: {
    input: number;
    output: number;
    total: number;
  };
};

export async function stopMessage(sessionId: string): Promise<void> {
  await fetch(`${SEND_SERVER}/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => undefined);
}

export async function sendMessage(
  sessionId: string,
  message: string,
  model: string | undefined,
  attachments: string[],
  onDelta: (text: string) => void,
  onMeta?: (backend: string, model: string) => void,
  onWork?: (type: string, content: string, id?: string) => void,
  signal?: AbortSignal,
): Promise<SendResult> {
  let response: Response;
  try {
    response = await fetch(`${SEND_SERVER}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: sessionId,
        message,
        model,
        attachments,
      }),
      signal,
    });
  } catch (error) {
    if (signal?.aborted) return { ok: false, error: "Turn stopped." };
    return {
      ok: false,
      error: "Send server unreachable. Restart Agent Workbench Next.",
    };
  }
  if (!response.ok || !response.body) {
    return { ok: false, error: `Send server returned ${response.status}` };
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let error: string | undefined;
  let tokenUsage: SendResult["tokenUsage"];

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      const type = event["type"];
      if (type === "delta") {
        onDelta(String(event["text"] ?? ""));
      } else if (type === "meta") {
        onMeta?.(
          String(event["backend"] ?? ""),
          String(event["model"] ?? ""),
        );
      } else if (type === "work") {
        onWork?.(
          String(event["kind"] ?? "system"),
          String(event["content"] ?? ""),
          event["id"] !== undefined ? String(event["id"]) : undefined,
        );
      } else if (type === "usage") {
        tokenUsage = {
          input: Number(event["input_tokens"] ?? 0),
          output: Number(event["output_tokens"] ?? 0),
          total: Number(event["total_tokens"] ?? 0),
        };
      } else if (type === "error") {
        error = String(event["message"] ?? "unknown error");
      }
    }
  }

  return { ok: !error, error, tokenUsage };
}
