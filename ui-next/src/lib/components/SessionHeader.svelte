<script lang="ts">
  import { Clock3, GitBranch, Hand, RefreshCw, Settings2 } from "@lucide/svelte";
  import { onMount } from "svelte";
  import AgentMark from "./AgentMark.svelte";
  import type { Session, UsageLimits } from "$lib/types";

  let {
    session,
    limits,
    workers = [],
    queueCount = 0,
    limitsLoading = false,
    refreshing = false,
    onlimits,
    onrefresh,
    onhandoff,
    onoptions,
    onspawnworker,
  }: {
    session: Session;
    limits?: UsageLimits;
    workers?: Array<{ id: string; label: string }>;
    queueCount?: number;
    limitsLoading?: boolean;
    refreshing?: boolean;
    onlimits: () => void;
    onrefresh: () => void;
    onhandoff: () => void;
    onoptions: () => void;
    onspawnworker?: () => void;
  } = $props();

  let now = $state(new Date());
  let limitsOpen = $state(false);

  const clockText = $derived(
    new Intl.DateTimeFormat(undefined, {
      weekday: "short",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(now),
  );
  const contextPercent = $derived(
    Math.max(0, Math.min(100, session.context?.percent ?? 0)),
  );
  const contextLabel = $derived(
    session.context ? `${Math.round(contextPercent)}%` : "—",
  );
  const contextTitle = $derived(
    session.context
      ? `${session.context.used.toLocaleString()} of ${session.context.limit.toLocaleString()} tokens${session.context.compactCount ? ` · compacted ${session.context.compactCount}×` : ""}`
      : "Context data unavailable",
  );
  const limitsLabel = $derived(
    limits?.windows.length
      ? limits.windows.map((window) => `${Math.round(window.usedPercent)}% ${window.label}`).join(" · ")
      : "—",
  );
  const handoffAvailable = $derived(
    contextPercent >= 70 || (session.context?.compactCount ?? 0) > 0,
  );
  const contextColorClass = $derived.by(() => {
    if (contextPercent >= 80) return "context-red";
    if (contextPercent >= 60) return "context-orange";
    if (contextPercent >= 40) return "context-amber";
    return "context-green";
  });

  let limitsWrap: HTMLElement;

  function toggleLimits() {
    limitsOpen = !limitsOpen;
    if (limitsOpen) onlimits();
  }

  onMount(() => {
    const timer = window.setInterval(() => (now = new Date()), 1000);
    function onDocClick(event: MouseEvent) {
      if (limitsOpen && limitsWrap && !limitsWrap.contains(event.target as Node)) {
        limitsOpen = false;
      }
    }
    document.addEventListener("click", onDocClick, { capture: true });
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("click", onDocClick, { capture: true });
    };
  });
</script>

<header class="session-header">
  <div class="session-heading">
    <AgentMark
      agent={session.agent}
      agentKey={session.agentKey}
      model={session.model}
      size={34}
    />
    <div>
      <div class="eyebrow">{session.agent} · {session.model}</div>
      <h1>{session.title}</h1>
    </div>
  </div>

  <div class="header-actions">
    <div class="system-clock" title={now.toString()}>
      <Clock3 size={14} />
      {clockText}
    </div>
    <div class="metric context {contextColorClass}" title={contextTitle}>
      <span>Context</span>
      <strong>{contextLabel}</strong>
      <div class="metric-track"><i style:width={`${contextPercent}%`}></i></div>
    </div>
    <div class="limits-wrap" bind:this={limitsWrap}>
      <button class="metric limits" onclick={toggleLimits}>
        <span>Limits:</span>
        <strong>{limitsLoading ? "Loading…" : limitsLabel}</strong>
      </button>
      {#if limitsOpen}
        <div class="limits-popover">
          <strong>{limits?.provider ?? "Usage limits"}</strong>
          {#if limitsLoading}
            <p>Refreshing limit data…</p>
          {:else if limits?.windows.length}
            {#each limits.windows as window}
              <div class="limit-row">
                <span>{window.label}</span>
                <b>{Math.round(window.usedPercent)}% used</b>
                <small>{window.reset ? `Resets ${window.reset}` : "Reset time unavailable"}</small>
              </div>
            {/each}
          {:else}
            <p>{limits?.error ?? "No account-limit telemetry available."}</p>
          {/if}
        </div>
      {/if}
    </div>
    {#if workers.length > 0}
      <div
        class="worker-badge"
        title={workers.map((w) => w.label).join("\n")}
        aria-label={`${workers.length} active sub-agent${workers.length === 1 ? "" : "s"}`}
      >
        <GitBranch size={13} />
        {workers.length} sub-agent{workers.length === 1 ? "" : "s"}
      </div>
    {/if}
    {#if queueCount > 0}
      <div class="queue-badge" title="{queueCount} prompt{queueCount === 1 ? '' : 's'} queued">
        {queueCount} queued
      </div>
    {/if}
    {#if onspawnworker}
      <button
        class="header-button spawn"
        title="Delegate a bounded task to a sub-agent"
        onclick={onspawnworker}
      >
        <GitBranch size={15} />
        Delegate
      </button>
    {/if}
    <button
      class="header-button handoff"
      disabled={!handoffAvailable}
      title={handoffAvailable ? "Deploy the staged continuation packet" : "Available at 70% context"}
      onclick={onhandoff}
    >
      <Hand size={16} />
      Hand off
    </button>
    <button class="icon-button" aria-label="Refresh session" onclick={onrefresh} disabled={refreshing}>
      <span class:spinning={refreshing}><RefreshCw size={17} /></span>
    </button>
    <button class="icon-button" aria-label="Workbench options" onclick={onoptions}>
      <Settings2 size={17} />
    </button>
  </div>
</header>
