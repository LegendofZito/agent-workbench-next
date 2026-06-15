<script lang="ts">
  import {
    ChevronDown,
    FolderOpen,
    PanelLeftClose,
    Pencil,
    Plus,
    Search,
    Trash2,
  } from "@lucide/svelte";
  import AgentMark from "./AgentMark.svelte";
  import type { AgentOption, Session, WorkspaceTab } from "$lib/types";

  let {
    sessions,
    selectedId,
    workspace,
    agents,
    connected,
    collapsed = false,
    onselect,
    onnewsession,
    onagentchange,
    onmodelchange,
    oneffortchange,
    onproject,
    oncollapse,
    onrename,
    ondelete,
  }: {
    sessions: Session[];
    selectedId: string;
    workspace: WorkspaceTab;
    agents: AgentOption[];
    connected: boolean;
    collapsed?: boolean;
    onselect: (id: string) => void;
    onnewsession: () => void;
    onagentchange: (key: string) => void;
    onmodelchange: (model: string) => void;
    oneffortchange: (effort: string) => void;
    onproject: () => void;
    oncollapse: () => void;
    onrename: (session: Session) => void;
    ondelete: (session: Session) => void;
  } = $props();

  let search = $state("");

  const activeAgent = $derived(
    agents.find((option) => option.key === workspace.agentKey) ?? agents[0],
  );
  const visibleSessions = $derived(
    sessions.filter((session) => {
      const sameAgent =
        workspace.agent === "local"
          ? session.agent === "local"
          : session.agent === workspace.agent;
      const matches = `${session.title} ${session.project}`
        .toLowerCase()
        .includes(search.toLowerCase());
      return sameAgent && matches;
    }),
  );
</script>

<aside class:collapsed class="sidebar">
  <header class="brand">
    <div class="brand-symbol">AW</div>
    {#if !collapsed}
      <div>
        <strong>Agent Workbench</strong>
        <span>Unified agent workspace</span>
      </div>
    {/if}
    <button class="icon-button subtle" aria-label="Collapse sidebar" onclick={oncollapse}>
      <PanelLeftClose size={18} />
    </button>
  </header>

  {#if !collapsed}
    <button class="new-session" onclick={onnewsession}>
      <Plus size={17} />
      New session
    </button>

    <section class="control-stack">
      <label>
        <span>Agent</span>
        <div class="select-shell">
          <AgentMark
            agent={workspace.agent}
            agentKey={workspace.agentKey}
            model={workspace.model}
            size={22}
          />
          <select
            value={workspace.agentKey}
            onchange={(event) => onagentchange(event.currentTarget.value)}
          >
            {#each agents as option}
              <option value={option.key}>
                {option.label}{option.local ? " · Local" : ""}
              </option>
            {/each}
          </select>
          <ChevronDown size={15} />
        </div>
      </label>

      <div class="split-fields">
        <label>
          <span>Model</span>
          <div class="select-shell compact">
            <select
              value={workspace.model}
              onchange={(event) => onmodelchange(event.currentTarget.value)}
            >
              {#each activeAgent?.models ?? [workspace.model] as model}
                <option value={model}>{model}</option>
              {/each}
            </select>
            <ChevronDown size={14} />
          </div>
        </label>
        <label>
          <span>Effort</span>
          <div class="select-shell compact">
            <select
              value={workspace.effort}
              disabled={(activeAgent?.efforts.length ?? 0) <= 1}
              onchange={(event) => oneffortchange(event.currentTarget.value)}
            >
              {#each activeAgent?.efforts ?? ["Default"] as effort}
                <option value={effort}>{effort}</option>
              {/each}
            </select>
            <ChevronDown size={14} />
          </div>
        </label>
      </div>

      <label>
        <span>Project</span>
        <button class="project-field" onclick={onproject}>
          <FolderOpen size={16} />
          <span>{workspace.cwd || "Choose project folder…"}</span>
        </button>
      </label>
    </section>

    <div class="section-title">
      <span>Sessions</span>
      <span>{visibleSessions.length}</span>
    </div>

    <label class="search-field">
      <Search size={16} />
      <input bind:value={search} placeholder="Search sessions" />
    </label>

    <div class="session-list">
      {#each visibleSessions as session (session.id)}
        <div class="session-row-shell">
          <button
            class="session-row"
            class:active={session.id === selectedId}
            onclick={() => onselect(session.id)}
          >
            <AgentMark
              agent={session.agent}
              agentKey={session.agentKey}
              model={session.model}
              size={24}
            />
            <span class="session-copy">
              <strong>{session.title}</strong>
              <small>{session.project}</small>
            </span>
            <span class="session-time">{session.updatedAt}</span>
          </button>
          <div class="session-actions">
            <button onclick={() => onrename(session)} aria-label={`Rename ${session.title}`}>
              <Pencil size={13} />
            </button>
            <button class="danger" onclick={() => ondelete(session)} aria-label={`Delete ${session.title}`}>
              <Trash2 size={13} />
            </button>
          </div>
        </div>
      {:else}
        <p class="sidebar-empty">No matching sessions.</p>
      {/each}
    </div>

    <footer class="sidebar-footer">
      <span class:offline={!connected} class="connection-dot"></span>
      {connected ? "Native clients connected" : "Send server offline"}
    </footer>
  {/if}
</aside>
