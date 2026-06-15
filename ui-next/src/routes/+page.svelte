<script lang="ts">
  import { onMount } from "svelte";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { confirm as confirmDialog, open } from "@tauri-apps/plugin-dialog";
  import { Archive, FileBox, MessageSquare, TerminalSquare } from "@lucide/svelte";
  import Artifacts from "$lib/components/Artifacts.svelte";
  import Composer from "$lib/components/Composer.svelte";
  import Conversation from "$lib/components/Conversation.svelte";
  import HandoffDialog from "$lib/components/HandoffDialog.svelte";
  import OptionsDialog from "$lib/components/OptionsDialog.svelte";
  import SessionHeader from "$lib/components/SessionHeader.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import Terminal from "$lib/components/Terminal.svelte";
  import WorkLog from "$lib/components/WorkLog.svelte";
  import WorkspaceTabs from "$lib/components/WorkspaceTabs.svelte";
  import {
    createSession,
    deleteArtifact,
    deleteSession,
    deployHandoff,
    getAppState,
    getSessionMessages,
    getSessionWorkLog,
    getUsageLimits,
    loadNativeTranscript,
    listArtifacts,
    listSessions,
    maybeAutoDeployHandoff,
    normalizeAgent,
    pasteClipboardImage,
    renameArtifact,
    renameSession,
    saveWorkspaceState,
    saveAppPreferences,
    sendMessage,
    serverInfo,
    stageHandoff,
    stopTerminal,
    stopMessage,
    storeArtifacts,
    syncNativeHistory,
  } from "$lib/api";
  import type { AppState, WorkLogEntry } from "$lib/api";
  import type {
    AgentOption,
    Artifact,
    Message,
    Session,
    UsageLimits,
    WorkspaceTab,
  } from "$lib/types";

  type SessionView = {
    messages: Message[];
    workLog: WorkLogEntry[];
    artifacts: Artifact[];
  };

  const EMPTY_VIEW: SessionView = { messages: [], workLog: [], artifacts: [] };
  const MODEL_OPTIONS: Record<string, string[]> = {
    claude: ["Default", "fable", "opus", "sonnet", "haiku"],
    codex: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
    gemini: [
      "Auto",
      "gemini-3-pro-preview",
      "gemini-3-flash-preview",
      "gemini-2.5-pro",
      "gemini-2.5-flash",
    ],
  };
  const EFFORT_OPTIONS: Record<string, string[]> = {
    claude: ["low", "medium", "high", "xhigh", "max"],
    codex: ["low", "medium", "high", "xhigh"],
    gemini: ["Default"],
    ollama: ["Default"],
  };
  const AGENT_LABELS: Record<string, string> = {
    claude: "Claude Code",
    codex: "Codex",
    gemini: "Gemini CLI",
    ollama: "Ollama",
    local: "Local model",
  };

  let sessions = $state<Session[]>([]);
  let tabs = $state<WorkspaceTab[]>([]);
  let activeWorkspaceId = $state("");
  let activeView = $state("conversation");
  let loaded = $state(false);
  let appState = $state<AppState | null>(null);
  let agentOptions = $state<AgentOption[]>([]);
  let serverConnected = $state(false);
  let sidebarCollapsed = $state(false);
  let sessionViews = $state<Record<string, SessionView>>({});
  let busyBySession = $state<Record<string, boolean>>({});
  let startedAtBySession = $state<Record<string, number>>({});
  let tokensBySession = $state<Record<string, number>>({});
  let workersBySession = $state<Record<string, string[]>>({});
  let limitsByAgent = $state<Record<string, UsageLimits>>({});
  let limitsLoading = $state(false);
  let refreshing = $state(false);
  let handoffOpen = $state(false);
  let handoffBusy = $state(false);
  let optionsOpen = $state(false);
  let optionsBusy = $state(false);
  let clientPickerOpen = $state(false);
  let attachPicking = $state(false);
  let notice = $state("");
  let clockTick = $state(Date.now());
  let sessionLoadSequence = 0;
  let persistTimer: number | undefined;
  const controllers = new Map<string, AbortController>();
  const stagedSessions = new Set<string>();

  const activeWorkspace = $derived(
    tabs.find((tab) => tab.id === activeWorkspaceId) ?? tabs[0],
  );
  const selectedSessionId = $derived(activeWorkspace?.sessionId ?? "");
  const selectedSession = $derived(
    sessions.find((session) => session.id === selectedSessionId),
  );
  const displaySession = $derived(
    selectedSession ??
      ({
        id: selectedSessionId || activeWorkspace?.id || "new",
        title: activeWorkspace?.label || "New session",
        agent: activeWorkspace?.agent ?? "claude",
        agentKey: activeWorkspace?.agentKey ?? "claude",
        model: activeWorkspace?.model ?? "Default",
        effort: activeWorkspace?.effort ?? "Default",
        project: activeWorkspace?.cwd ?? "~",
        cwd: activeWorkspace?.cwd ?? "",
        updatedAt: "",
      } satisfies Session),
  );
  const currentView = $derived(
    selectedSessionId ? sessionViews[selectedSessionId] ?? EMPTY_VIEW : EMPTY_VIEW,
  );
  const busy = $derived(Boolean(selectedSessionId && busyBySession[selectedSessionId]));
  const elapsed = $derived(
    busy && startedAtBySession[selectedSessionId]
      ? Math.max(0, Math.floor((clockTick - startedAtBySession[selectedSessionId]) / 1000))
      : 0,
  );
  const liveTokens = $derived(tokensBySession[selectedSessionId] ?? 0);
  const activeWorkers = $derived(workersBySession[selectedSessionId] ?? []);
  const activeStatusLabel = $derived.by(() => {
    const label =
      optionFor(activeWorkspace?.agentKey ?? "")?.label ??
      activeWorkspace?.agentKey ??
      "Agent";
    const model = activeWorkspace?.model ?? "";
    if (activeWorkspace?.agent === "local") {
      return `${model || label} · Full system`;
    }
    return label === model || !model ? label : `${label} · ${model}`;
  });
  const currentLimits = $derived(limitsByAgent[activeWorkspace?.agentKey ?? ""]);
  const configuredHandoffAgent = $derived.by(() => {
    const source = activeWorkspace?.agentKey ?? "claude";
    const preferences = appState?.handoffPreferences ?? {};
    const mode = String(preferences["mode"] ?? "current");
    const sequence = Array.isArray(preferences["sequence"])
      ? preferences["sequence"].map(String).filter(Boolean)
      : [];
    if (mode === "fixed") {
      return String(preferences["fixed_agent"] ?? source) || source;
    }
    if (mode === "sequence" && sequence.length) {
      const index = sequence.indexOf(source);
      return index >= 0 ? sequence[(index + 1) % sequence.length] : sequence[0];
    }
    return source;
  });

  const views = [
    { id: "conversation", label: "Conversation", icon: MessageSquare },
    { id: "work-log", label: "Work log", icon: Archive },
    { id: "artifacts", label: "Artifacts", icon: FileBox },
    { id: "terminal", label: "Terminal", icon: TerminalSquare },
  ];

  function dedupe(values: string[]) {
    return [...new Set(values.filter(Boolean))];
  }

  function optionFor(key: string) {
    return agentOptions.find((option) => option.key === key);
  }

  function buildAgentOptions(state: AppState, ollamaModels: string[]): AgentOption[] {
    const native = ["claude", "codex", "gemini"].map((key) => {
      const configured = state.agentSettings[key];
      return {
        key,
        label: AGENT_LABELS[key],
        agent: normalizeAgent(key),
        models: dedupe([
          configured?.model ?? "",
          ...(MODEL_OPTIONS[key] ?? ["Default"]),
        ]),
        efforts: dedupe([
          configured?.effort ?? "",
          ...(EFFORT_OPTIONS[key] ?? ["Default"]),
        ]),
        connected: true,
      } satisfies AgentOption;
    });
    const local: AgentOption[] = ollamaModels.length
      ? [
          {
            key: "ollama",
            label: "Ollama",
            agent: "local",
            models: ollamaModels,
            efforts: ["Default"],
            connected: true,
            local: true,
          },
        ]
      : [];
    const custom = state.customClients
      // Hide custom clients that merely duplicate an Ollama model — the unified
      // "Ollama" agent already exposes every local model via its model dropdown.
      .filter((client) => !ollamaModels.includes(client.label ?? ""))
      .map((client) => ({
      key: client.key || `custom-${client.label || "client"}`,
      label: client.label || "Custom CLI",
      agent: "local" as const,
      models: dedupe([
        client.model && client.model !== "Default" ? client.model : "",
        client.label ?? "Custom CLI",
      ]),
      efforts: ["Default"],
      connected: true,
      local: true,
    }));
    return [...native, ...local, ...custom];
  }

  function workspaceFromState(
    raw: AppState["workspaceTabs"][number],
    index: number,
    state: AppState,
  ): WorkspaceTab {
    const agentKey = raw.agent || state.activeAgent || "claude";
    const option = optionFor(agentKey);
    const settings = state.agentSettings[agentKey] ?? {};
    const session = sessions.find((item) => item.id === raw.session_id);
    const savedModel = raw.model || settings.model || "";
    const model =
      agentKey.startsWith("custom-") &&
      (!savedModel || savedModel === "Default" || !option?.models.includes(savedModel))
        ? option?.models[0] || "Custom CLI"
        : savedModel || option?.models[0] || "Default";
    return {
      id: `workspace-${index}-${raw.session_id || crypto.randomUUID()}`,
      label:
        session?.title ||
        (raw.title && !/^Tab \d+$/i.test(raw.title) ? raw.title : "") ||
        `New ${option?.label ?? AGENT_LABELS[agentKey] ?? "session"}`,
      agent: option?.agent ?? normalizeAgent(agentKey),
      agentKey,
      cwd: raw.cwd || state.cwd || "",
      model,
      effort: raw.effort || settings.effort || option?.efforts[0] || "Default",
      sessionId: raw.session_id || "",
      draft: raw.input_text || "",
      pendingAttachments: Array.isArray(raw.attachment_paths)
        ? raw.attachment_paths.map(String)
        : [],
      queuedPrompts: Array.isArray(raw.queued_prompts)
        ? raw.queued_prompts.map((queued) => ({
            prompt: queued.prompt || "",
            attachments: Array.isArray(queued.attachments)
              ? queued.attachments.map(String)
              : [],
          }))
        : [],
    };
  }

  function schedulePersist() {
    if (persistTimer) window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(() => {
      const activeIndex = Math.max(
        0,
        tabs.findIndex((tab) => tab.id === activeWorkspaceId),
      );
      void saveWorkspaceState(tabs, activeIndex).catch((error) => {
        notice = `Could not save workspace state: ${String(error)}`;
      });
    }, 250);
  }

  function updateWorkspace(id: string, patch: Partial<WorkspaceTab>) {
    tabs = tabs.map((tab) => (tab.id === id ? { ...tab, ...patch } : tab));
    schedulePersist();
  }

  function updateCurrentWorkspace(patch: Partial<WorkspaceTab>) {
    if (activeWorkspace) updateWorkspace(activeWorkspace.id, patch);
  }

  function updateSessionView(sessionId: string, patch: Partial<SessionView>) {
    sessionViews = {
      ...sessionViews,
      [sessionId]: {
        ...(sessionViews[sessionId] ?? EMPTY_VIEW),
        ...patch,
      },
    };
  }

  async function loadSessionView(sessionId: string, force = false) {
    if (!sessionId) return;
    const sequence = ++sessionLoadSequence;
    if (!force && sessionViews[sessionId]) return;
    try {
      if (serverConnected) {
        await loadNativeTranscript(sessionId);
      }
      const [messages, workLog, artifacts] = await Promise.all([
        getSessionMessages(sessionId),
        getSessionWorkLog(sessionId),
        listArtifacts(sessionId),
      ]);
      if (sequence !== sessionLoadSequence && sessionId === selectedSessionId) return;
      updateSessionView(sessionId, { messages, workLog, artifacts });
    } catch (error) {
      notice = `Could not load session: ${String(error)}`;
    }
  }

  async function refreshSessionsAndView() {
    if (refreshing) return;
    refreshing = true;
    try {
      await refreshServerConnection();
      if (serverConnected) await syncNativeHistory();
      const sessionList = await listSessions();
      sessions = sessionList;
      if (selectedSessionId) await loadSessionView(selectedSessionId, true);
      notice = "Session synchronized.";
    } catch (error) {
      notice = `Refresh failed: ${String(error)}`;
    } finally {
      refreshing = false;
    }
  }

  async function refreshServerConnection() {
    const wasConnected = serverConnected;
    const info = await serverInfo();
    serverConnected = info.ok;
    if (appState) {
      agentOptions = buildAgentOptions(appState, info.ollamaModels);
    }
    if (info.ok && !wasConnected) {
      limitsByAgent = {};
      void loadLimits();
    }
  }

  function selectSession(id: string) {
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    updateCurrentWorkspace({
      sessionId: session.id,
      label: session.title,
      agent: session.agent,
      agentKey: session.agentKey,
      cwd: session.cwd,
      model: session.model,
      effort: session.effort,
    });
  }

  async function renameSelectedSession(session: Session) {
    const title = window.prompt("Rename session", session.title)?.trim();
    if (!title || title === session.title) return;
    try {
      await renameSession(session.id, title);
      sessions = sessions.map((item) =>
        item.id === session.id ? { ...item, title } : item,
      );
      tabs = tabs.map((tab) =>
        tab.sessionId === session.id ? { ...tab, label: title } : tab,
      );
      schedulePersist();
    } catch (error) {
      notice = `Rename failed: ${String(error)}`;
    }
  }

  async function deleteSelectedSession(session: Session) {
    if (busyBySession[session.id]) {
      notice = "Stop the active turn before deleting this session.";
      return;
    }
    if (
      !window.confirm(
        `Delete "${session.title}" from Agent Workbench and its native client history?`,
      )
    ) {
      return;
    }
    try {
      await deleteSession(session.id);
      sessions = sessions.filter((item) => item.id !== session.id);
      const nextViews = { ...sessionViews };
      delete nextViews[session.id];
      sessionViews = nextViews;
      tabs = tabs.map((tab) =>
        tab.sessionId === session.id
          ? {
              ...tab,
              sessionId: "",
              label: `New ${optionFor(tab.agentKey)?.label ?? "session"}`,
              draft: "",
              pendingAttachments: [],
              queuedPrompts: [],
            }
          : tab,
      );
      schedulePersist();
      notice = `Deleted ${session.title}.`;
    } catch (error) {
      notice = `Delete failed: ${String(error)}`;
    }
  }

  async function ensureSession(): Promise<Session | undefined> {
    if (selectedSession) return selectedSession;
    if (!activeWorkspace) return undefined;
    try {
      const session = await createSession({
        agent: activeWorkspace.agentKey,
        model: activeWorkspace.model,
        effort: activeWorkspace.effort,
        cwd: activeWorkspace.cwd,
        title: activeWorkspace.label.startsWith("New ") ? "" : activeWorkspace.label,
      });
      sessions = [session, ...sessions];
      updateCurrentWorkspace({
        sessionId: session.id,
        label: session.title,
        agent: session.agent,
        agentKey: session.agentKey,
      });
      updateSessionView(session.id, EMPTY_VIEW);
      return session;
    } catch (error) {
      notice = `Could not create session: ${String(error)}`;
      return undefined;
    }
  }

  async function createNewSession() {
    if (!activeWorkspace) return;
    try {
      const session = await createSession({
        agent: activeWorkspace.agentKey,
        model: activeWorkspace.model,
        effort: activeWorkspace.effort,
        cwd: activeWorkspace.cwd,
        title: "",
      });
      sessions = [session, ...sessions];
      updateCurrentWorkspace({
        sessionId: session.id,
        label: session.title,
        agent: session.agent,
        agentKey: session.agentKey,
        draft: "",
        pendingAttachments: [],
        queuedPrompts: [],
      });
      updateSessionView(session.id, EMPTY_VIEW);
    } catch (error) {
      notice = `Could not create session: ${String(error)}`;
    }
  }

  async function addWorkspace(agentKey?: string) {
    const source = activeWorkspace;
    const explicit = Boolean(agentKey);
    const option = optionFor(agentKey ?? source?.agentKey ?? "claude") ?? agentOptions[0];
    const workspace: WorkspaceTab = {
      id: crypto.randomUUID(),
      label: `New ${option?.label ?? "session"}`,
      agent: option?.agent ?? "claude",
      agentKey: option?.key ?? "claude",
      cwd: source?.cwd || appState?.cwd || "",
      model: (explicit ? option?.models[0] : source?.model) || option?.models[0] || "Default",
      effort: (explicit ? option?.efforts[0] : source?.effort) || option?.efforts[0] || "Default",
      sessionId: "",
      draft: "",
      pendingAttachments: [],
      queuedPrompts: [],
    };
    tabs = [...tabs, workspace];
    activeWorkspaceId = workspace.id;
    schedulePersist();
    try {
      const session = await createSession({
        agent: workspace.agentKey,
        model: workspace.model,
        effort: workspace.effort,
        cwd: workspace.cwd,
        title: "",
      });
      sessions = [session, ...sessions];
      updateWorkspace(workspace.id, {
        sessionId: session.id,
        label: session.title,
        agent: session.agent,
        agentKey: session.agentKey,
      });
      updateSessionView(session.id, EMPTY_VIEW);
    } catch (error) {
      notice = `Could not create session: ${String(error)}`;
    }
  }

  function closeWorkspace(id: string) {
    if (tabs.length <= 1) return;
    const index = tabs.findIndex((tab) => tab.id === id);
    const nextTabs = tabs.filter((tab) => tab.id !== id);
    void stopTerminal(id).catch(() => undefined);
    tabs = nextTabs;
    if (activeWorkspaceId === id) {
      activeWorkspaceId = nextTabs[Math.min(index, nextTabs.length - 1)].id;
    }
    schedulePersist();
  }

  function selectWorkspace(id: string) {
    activeWorkspaceId = id;
    schedulePersist();
  }

  function changeAgent(key: string) {
    const option = optionFor(key);
    if (!option) return;
    const settings = appState?.agentSettings[key];
    const configuredModel =
      settings?.model && settings.model !== "Default"
        ? settings.model
        : option.models[0] || "Default";
    updateCurrentWorkspace({
      agentKey: key,
      agent: option.agent,
      model: configuredModel,
      effort: settings?.effort || option.efforts[0] || "Default",
      sessionId: "",
      label: `New ${option.label} session`,
      pendingAttachments: [],
      queuedPrompts: [],
    });
  }

  async function chooseProject() {
    const selected = await open({
      directory: true,
      multiple: false,
      defaultPath: activeWorkspace?.cwd || appState?.cwd,
      title: "Choose project folder",
    });
    if (typeof selected === "string") updateCurrentWorkspace({ cwd: selected });
  }

  async function chooseAttachments() {
    if (attachPicking) return; // one file dialog at a time — ignore button spam
    attachPicking = true;
    try {
      const session = await ensureSession();
      if (!session) return;
      const selected = await open({
        multiple: true,
        directory: false,
        title: "Attach files",
      });
      const paths =
        typeof selected === "string" ? [selected] : Array.isArray(selected) ? selected : [];
      if (!paths.length) return;
      const stored = await storeArtifacts(session.id, paths);
      const storedPaths = stored.map((artifact) => artifact.path);
      updateCurrentWorkspace({
        pendingAttachments: dedupe([
          ...(activeWorkspace?.pendingAttachments ?? []),
          ...storedPaths,
        ]),
      });
      updateSessionView(session.id, {
        artifacts: dedupeArtifacts([
          ...stored,
          ...(sessionViews[session.id]?.artifacts ?? []),
        ]),
      });
    } catch (error) {
      notice = `Could not attach files: ${String(error)}`;
    } finally {
      attachPicking = false;
    }
  }

  function dedupeArtifacts(artifacts: Artifact[]) {
    return [...new Map(artifacts.map((artifact) => [artifact.path, artifact])).values()];
  }

  function attachArtifact(path: string) {
    updateCurrentWorkspace({
      pendingAttachments: dedupe([
        ...(activeWorkspace?.pendingAttachments ?? []),
        path,
      ]),
    });
    activeView = "conversation";
  }

  function removeAttachment(path: string) {
    updateCurrentWorkspace({
      pendingAttachments: (activeWorkspace?.pendingAttachments ?? []).filter(
        (item) => item !== path,
      ),
    });
  }

  async function pasteImageAttachment() {
    const session = await ensureSession();
    if (!session) return;
    try {
      const artifact = await pasteClipboardImage(session.id);
      updateCurrentWorkspace({
        pendingAttachments: dedupe([
          ...(activeWorkspace?.pendingAttachments ?? []),
          artifact.path,
        ]),
      });
      updateSessionView(session.id, {
        artifacts: dedupeArtifacts([
          artifact,
          ...(sessionViews[session.id]?.artifacts ?? []),
        ]),
      });
      notice = `Attached ${artifact.name} from the clipboard.`;
    } catch (error) {
      notice = `Clipboard image paste failed: ${String(error)}`;
    }
  }

  async function renameStoredArtifact(artifact: Artifact) {
    if (!selectedSessionId) return;
    const newName = window.prompt("Rename artifact", artifact.name)?.trim();
    if (!newName || newName === artifact.name) return;
    try {
      const renamed = await renameArtifact(
        selectedSessionId,
        artifact.path,
        newName,
      );
      updateSessionView(selectedSessionId, {
        artifacts: currentView.artifacts.map((item) =>
          item.path === artifact.path ? renamed : item,
        ),
      });
      updateCurrentWorkspace({
        pendingAttachments: (activeWorkspace?.pendingAttachments ?? []).map((path) =>
          path === artifact.path ? renamed.path : path,
        ),
      });
    } catch (error) {
      notice = `Artifact rename failed: ${String(error)}`;
    }
  }

  async function deleteStoredArtifact(artifact: Artifact) {
    if (
      !selectedSessionId ||
      !window.confirm(`Delete artifact "${artifact.name}" permanently?`)
    ) {
      return;
    }
    try {
      await deleteArtifact(selectedSessionId, artifact.path);
      updateSessionView(selectedSessionId, {
        artifacts: currentView.artifacts.filter(
          (item) => item.path !== artifact.path,
        ),
      });
      removeAttachment(artifact.path);
    } catch (error) {
      notice = `Artifact delete failed: ${String(error)}`;
    }
  }

  function setBusy(sessionId: string, value: boolean) {
    busyBySession = { ...busyBySession, [sessionId]: value };
  }

  async function runTurn(
    session: Session,
    prompt: string,
    attachments: string[],
  ) {
    const sessionId = session.id;
    const stamp = new Date().toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
    });
    const userId = `live-user-${crypto.randomUUID()}`;
    const replyId = `live-assistant-${crypto.randomUUID()}`;
    const existing = sessionViews[sessionId] ?? EMPTY_VIEW;
    updateSessionView(sessionId, {
      messages: [
        ...existing.messages,
        { id: userId, role: "user", content: prompt, time: stamp },
        {
          id: replyId,
          role: "assistant",
          agent: session.agent,
          content: "",
          time: stamp,
        },
      ],
    });
    setBusy(sessionId, true);
    startedAtBySession = { ...startedAtBySession, [sessionId]: Date.now() };
    tokensBySession = { ...tokensBySession, [sessionId]: 0 };
    workersBySession = { ...workersBySession, [sessionId]: [] };
    const controller = new AbortController();
    controllers.set(sessionId, controller);
    let streamedCharacters = 0;

    const result = await sendMessage(
      sessionId,
      prompt,
      session.model,
      attachments,
      (delta) => {
        streamedCharacters += delta.length;
        tokensBySession = {
          ...tokensBySession,
          [sessionId]: Math.max(
            tokensBySession[sessionId] ?? 0,
            Math.ceil(streamedCharacters / 4),
          ),
        };
        const view = sessionViews[sessionId] ?? EMPTY_VIEW;
        updateSessionView(sessionId, {
          messages: view.messages.map((message) =>
            message.id === replyId
              ? { ...message, content: message.content + delta }
              : message,
          ),
        });
      },
      (_backend, model) => {
        sessions = sessions.map((item) =>
          item.id === sessionId ? { ...item, model } : item,
        );
      },
      (type, content) => {
        if (type === "worker") {
          workersBySession = {
            ...workersBySession,
            [sessionId]: dedupe([
              ...(workersBySession[sessionId] ?? []),
              content,
            ]),
          };
        }
        const view = sessionViews[sessionId] ?? EMPTY_VIEW;
        // Normalize "worker" to "report" to match how the server persists it,
        // so live work-log entries look the same as replayed ones.
        const logType = type === "worker" ? "report" : type;
        updateSessionView(sessionId, {
          workLog: [
            ...view.workLog,
            {
              id: `live-work-${crypto.randomUUID()}`,
              type: logType,
              content,
              time: stamp,
            },
          ],
        });
      },
      controller.signal,
    );

    if (result.tokenUsage?.output) {
      tokensBySession = {
        ...tokensBySession,
        [sessionId]: result.tokenUsage.output,
      };
    }
    if (!result.ok) {
      const view = sessionViews[sessionId] ?? EMPTY_VIEW;
      updateSessionView(sessionId, {
        messages: view.messages.map((message) =>
          message.id === replyId
            ? {
                ...message,
                content:
                  message.content +
                  `\n\n_⚠ ${result.error || "The turn failed."}_`,
              }
            : message,
        ),
      });
    }

    controllers.delete(sessionId);
    setBusy(sessionId, false);
    workersBySession = { ...workersBySession, [sessionId]: [] };
    sessions = await listSessions().catch(() => sessions);
    await loadSessionView(sessionId, true);

    const workspace = tabs.find((tab) => tab.sessionId === sessionId);
    const queued = workspace?.queuedPrompts[0];
    if (workspace && queued) {
      updateWorkspace(workspace.id, {
        queuedPrompts: workspace.queuedPrompts.slice(1),
      });
      const refreshedSession = sessions.find((item) => item.id === sessionId) ?? session;
      await runTurn(refreshedSession, queued.prompt, queued.attachments);
      return;
    }
    if (result.ok) {
      try {
        const continuation = await maybeAutoDeployHandoff(sessionId);
        if (continuation) await openContinuationSession(continuation);
      } catch (error) {
        notice = `Automatic handoff failed: ${String(error)}`;
      }
    }
  }

  async function handleSend(text: string) {
    const session = await ensureSession();
    if (!session || !activeWorkspace) return;
    const attachments = [...activeWorkspace.pendingAttachments];
    updateCurrentWorkspace({ draft: "", pendingAttachments: [] });
    if (busyBySession[session.id]) {
      updateCurrentWorkspace({
        queuedPrompts: [
          ...activeWorkspace.queuedPrompts,
          { prompt: text, attachments },
        ],
      });
      return;
    }
    await runTurn(session, text, attachments);
  }

  async function stopActiveTurn() {
    if (!selectedSessionId) return;
    controllers.get(selectedSessionId)?.abort();
    await stopMessage(selectedSessionId);
  }

  function removeQueuedPrompt(index: number) {
    updateCurrentWorkspace({
      queuedPrompts: (activeWorkspace?.queuedPrompts ?? []).filter(
        (_item, itemIndex) => itemIndex !== index,
      ),
    });
  }

  async function loadLimits() {
    const agentKey = activeWorkspace?.agentKey;
    if (!agentKey || limitsLoading) return;
    limitsLoading = true;
    limitsByAgent = {
      ...limitsByAgent,
      [agentKey]: await getUsageLimits(agentKey),
    };
    limitsLoading = false;
  }

  async function openContinuationSession(continuation: Session) {
    sessions = [
      continuation,
      ...sessions.filter((session) => session.id !== continuation.id),
    ];
    const workspace: WorkspaceTab = {
      id: crypto.randomUUID(),
      label: continuation.title,
        agent: continuation.agent,
        agentKey: continuation.agentKey,
        cwd: continuation.cwd,
        model: continuation.model,
        effort: continuation.effort,
        sessionId: continuation.id,
        draft: "",
      pendingAttachments: [],
      queuedPrompts: [],
    };
    tabs = [...tabs, workspace];
    activeWorkspaceId = workspace.id;
    activeView = "conversation";
    handoffOpen = false;
    schedulePersist();
    updateSessionView(continuation.id, EMPTY_VIEW);
    await runTurn(
      continuation,
      "Continue from the automatic handoff. Confirm the inherited state briefly, then continue the latest unresolved objective without asking me to repeat it.",
      [],
    );
  }

  async function deploySelectedHandoff(
    agentKey: string,
    model: string,
    effort: string,
  ) {
    if (!selectedSession || handoffBusy) return;
    handoffBusy = true;
    try {
      const continuation = await deployHandoff({
        sessionId: selectedSession.id,
        agent: agentKey,
        model,
        effort,
        cwd: selectedSession.cwd,
      });
      await openContinuationSession(continuation);
    } catch (error) {
      notice = `Handoff failed: ${String(error)}`;
    } finally {
      handoffBusy = false;
    }
  }

  async function saveOptions(
    claudeOrchestration: Record<string, unknown>,
    handoffPreferences: Record<string, unknown>,
  ) {
    if (optionsBusy) return;
    optionsBusy = true;
    try {
      await saveAppPreferences(claudeOrchestration, handoffPreferences);
      if (appState) {
        appState = {
          ...appState,
          claudeOrchestration,
          handoffPreferences,
        };
      }
      optionsOpen = false;
      notice = "Workbench options saved.";
    } catch (error) {
      notice = `Could not save options: ${String(error)}`;
    } finally {
      optionsBusy = false;
    }
  }

  $effect(() => {
    const id = selectedSessionId;
    if (id) void loadSessionView(id);
  });

  $effect(() => {
    const session = selectedSession;
    const percent = session?.context?.percent ?? 0;
    const compactCount = session?.context?.compactCount ?? 0;
    if (
      session &&
      (percent >= 70 || compactCount > 0) &&
      !stagedSessions.has(session.id)
    ) {
      stagedSessions.add(session.id);
      void stageHandoff(session.id)
        .then(() => loadSessionView(session.id, true))
        .catch((error) => {
          stagedSessions.delete(session.id);
          notice = `Could not stage handoff: ${String(error)}`;
        });
    }
  });

  $effect(() => {
    const agentKey = activeWorkspace?.agentKey;
    if (agentKey && !limitsByAgent[agentKey] && !limitsLoading) {
      void loadLimits();
    }
  });

  onMount(() => {
    const ticker = window.setInterval(() => (clockTick = Date.now()), 1000);
    const healthTicker = window.setInterval(() => {
      void refreshServerConnection();
    }, 5_000);
    let unlistenClose: (() => void) | undefined;
    let closing = false;
    void getCurrentWindow()
      .onCloseRequested(async (event) => {
        if (closing) return;
        event.preventDefault();
        const activeSessionIds = Object.entries(busyBySession)
          .filter(([, active]) => active)
          .map(([sessionId]) => sessionId);
        if (
          activeSessionIds.length &&
          !(await confirmDialog(
            "Agent turns are still running. Close the window and stop them?",
            { title: "Close Agent Workbench Next", kind: "warning" },
          ))
        ) {
          return;
        }
        closing = true;
        if (persistTimer) window.clearTimeout(persistTimer);
        const activeIndex = Math.max(
          0,
          tabs.findIndex((tab) => tab.id === activeWorkspaceId),
        );
        await saveWorkspaceState(tabs, activeIndex).catch(() => undefined);
        await Promise.all(
          activeSessionIds.map(async (sessionId) => {
            controllers.get(sessionId)?.abort();
            await stopMessage(sessionId);
          }),
        );
        await Promise.all(tabs.map((tab) => stopTerminal(tab.id).catch(() => undefined)));
        await getCurrentWindow().destroy();
      })
      .then((unlisten) => {
        unlistenClose = unlisten;
      });
    void (async () => {
      try {
        const [state, info] = await Promise.all([
          getAppState(),
          serverInfo(),
        ]);
        appState = state;
        serverConnected = info.ok;
        agentOptions = buildAgentOptions(state, info.ollamaModels);
        if (info.ok) await syncNativeHistory();
        const sessionList = await listSessions();
        sessions = sessionList;
        const restored = state.workspaceTabs.map((raw, index) =>
          workspaceFromState(raw, index, state),
        );
        if (restored.length) {
          tabs = restored;
          activeWorkspaceId =
            restored[
              Math.max(0, Math.min(state.activeWorkspaceIndex, restored.length - 1))
            ].id;
        } else {
          const newest = sessionList[0];
          const agentKey = newest?.agentKey || state.activeAgent || "claude";
          const option = optionFor(agentKey) ?? agentOptions[0];
          tabs = [
            {
              id: crypto.randomUUID(),
              label: newest?.title || `New ${option?.label ?? "session"}`,
              agent: newest?.agent || option?.agent || "claude",
              agentKey,
              cwd: newest?.cwd || state.cwd || "",
              model: newest?.model || state.agentSettings[agentKey]?.model || option?.models[0] || "Default",
              effort: newest?.effort || state.agentSettings[agentKey]?.effort || option?.efforts[0] || "Default",
              sessionId: newest?.id || "",
              draft: "",
              pendingAttachments: [],
              queuedPrompts: [],
            },
          ];
          activeWorkspaceId = tabs[0].id;
        }
        loaded = true;
      } catch (error) {
        notice = `Agent Workbench Next could not initialize: ${String(error)}`;
        loaded = true;
      }
    })();
    return () => {
      window.clearInterval(ticker);
      window.clearInterval(healthTicker);
      if (persistTimer) window.clearTimeout(persistTimer);
      unlistenClose?.();
    };
  });
</script>

<main class="app-shell">
  {#if tabs.length}
    <WorkspaceTabs
      {tabs}
      activeId={activeWorkspaceId}
      onselect={selectWorkspace}
      onclose={closeWorkspace}
      onnew={() => (clientPickerOpen = true)}
    />
  {:else}
    <div class="workspace-tabs"></div>
  {/if}

  <div class:sidebar-collapsed={sidebarCollapsed} class="app-body">
    {#if loaded && activeWorkspace}
      <Sidebar
        {sessions}
        selectedId={selectedSessionId}
        workspace={activeWorkspace}
        agents={agentOptions}
        connected={serverConnected}
        collapsed={sidebarCollapsed}
        onselect={selectSession}
        onnewsession={createNewSession}
        onagentchange={changeAgent}
        onmodelchange={(model) => updateCurrentWorkspace({ model })}
        oneffortchange={(effort) => updateCurrentWorkspace({ effort })}
        onproject={chooseProject}
        oncollapse={() => (sidebarCollapsed = !sidebarCollapsed)}
        onrename={renameSelectedSession}
        ondelete={deleteSelectedSession}
      />

      <section class="workspace">
        <SessionHeader
          session={displaySession}
          limits={currentLimits}
          {limitsLoading}
          {refreshing}
          onlimits={loadLimits}
          onrefresh={refreshSessionsAndView}
          onhandoff={() => (handoffOpen = true)}
          onoptions={() => (optionsOpen = true)}
        />

        <nav class="view-tabs" aria-label="Session views">
          {#each views as view}
            <button
              class:active={activeView === view.id}
              onclick={() => (activeView = view.id)}
            >
              <view.icon size={16} />
              {view.label}
            </button>
          {/each}
        </nav>

        {#if notice}
          <button class="notice-bar" onclick={() => (notice = "")}>{notice}</button>
        {/if}

        {#if activeView === "conversation"}
          <Conversation
            messages={currentView.messages}
            sessionId={selectedSessionId}
            agentKey={activeWorkspace.agentKey}
            model={activeWorkspace.model}
          />
        {:else if activeView === "work-log"}
          <WorkLog entries={currentView.workLog} />
        {:else if activeView === "artifacts"}
          <Artifacts
            artifacts={currentView.artifacts}
            onadd={chooseAttachments}
            onattach={attachArtifact}
            onrename={renameStoredArtifact}
            ondelete={deleteStoredArtifact}
          />
        {:else}
          <Terminal workspaceId={activeWorkspace.id} cwd={activeWorkspace.cwd} />
        {/if}

        <Composer
          workspaceId={activeWorkspace.id}
          draft={activeWorkspace.draft}
          attachments={activeWorkspace.pendingAttachments}
          queue={activeWorkspace.queuedPrompts}
          workers={activeWorkers}
          {busy}
          {elapsed}
          {liveTokens}
          placeholder={`Message ${AGENT_LABELS[activeWorkspace.agentKey] ?? optionFor(activeWorkspace.agentKey)?.label ?? "agent"}`}
          statusLabel={activeStatusLabel}
          note={serverConnected ? "" : "Send server is offline. Refresh after restarting Agent Workbench Next."}
          onsend={handleSend}
          onstop={stopActiveTurn}
          onattach={chooseAttachments}
          onremoveattachment={removeAttachment}
          onremovequeue={removeQueuedPrompt}
          onpasteimage={pasteImageAttachment}
          ondraft={(draft) => updateCurrentWorkspace({ draft })}
        />
      </section>
    {:else if !loaded}
      <div class="loading-sessions">Loading Agent Workbench Next…</div>
    {:else}
      <div class="loading-sessions">No workspace could be restored.</div>
    {/if}
  </div>

  <HandoffDialog
    open={handoffOpen}
    agents={agentOptions}
    sourceAgent={configuredHandoffAgent}
    busy={handoffBusy}
    onclose={() => (handoffOpen = false)}
    ondeploy={deploySelectedHandoff}
  />
  <OptionsDialog
    open={optionsOpen}
    agents={agentOptions}
    claudeOrchestration={appState?.claudeOrchestration ?? {}}
    handoffPreferences={appState?.handoffPreferences ?? {}}
    busy={optionsBusy}
    onclose={() => (optionsOpen = false)}
    onsave={saveOptions}
  />

  {#if clientPickerOpen}
    <div
      class="modal-backdrop"
      role="presentation"
      onclick={() => (clientPickerOpen = false)}
      onkeydown={(event) => {
        if (event.key === "Escape") clientPickerOpen = false;
      }}
    >
      <div
        class="client-picker"
        role="dialog"
        tabindex="-1"
        aria-label="Choose a client for the new session"
        onclick={(event) => event.stopPropagation()}
        onkeydown={(event) => event.stopPropagation()}
      >
        <h3>New session — choose a client</h3>
        <div class="client-picker-list">
          {#each agentOptions as option}
            <button
              class="client-picker-item"
              onclick={() => {
                clientPickerOpen = false;
                addWorkspace(option.key);
              }}
            >
              <span class="client-picker-name">{option.label}</span>
              {#if option.local}
                <span class="client-picker-badge">Local</span>
              {:else if !option.connected}
                <span class="client-picker-badge muted">Not signed in</span>
              {/if}
            </button>
          {/each}
        </div>
      </div>
    </div>
  {/if}
</main>
