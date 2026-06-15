export type AgentId = "claude" | "codex" | "gemini" | "local";

export type ContextUsage = {
  used: number;
  limit: number;
  percent: number;
  compactCount: number;
  model: string;
};

export type Session = {
  id: string;
  title: string;
  agent: AgentId;
  agentKey: string;
  model: string;
  effort: string;
  project: string;
  cwd: string;
  updatedAt: string;
  updatedAtIso?: string;
  context?: ContextUsage;
  active?: boolean;
};

export type Message = {
  id: string;
  role: "user" | "assistant";
  agent?: AgentId;
  content: string;
  time: string;
};

export type WorkspaceTab = {
  id: string;
  label: string;
  agent: AgentId;
  agentKey: string;
  cwd: string;
  model: string;
  effort: string;
  sessionId: string;
  draft: string;
  pendingAttachments: string[];
  queuedPrompts: Array<{
    prompt: string;
    attachments: string[];
  }>;
  active?: boolean;
};

export type AgentOption = {
  key: string;
  label: string;
  agent: AgentId;
  models: string[];
  efforts: string[];
  connected: boolean;
  local?: boolean;
};

export type Artifact = {
  name: string;
  path: string;
  size: number;
  modified: number;
  isImage: boolean;
  isText: boolean;
};

export type UsageWindow = {
  label: string;
  usedPercent: number;
  reset: string;
};

export type UsageLimits = {
  provider: string;
  windows: UsageWindow[];
  error?: string;
};
