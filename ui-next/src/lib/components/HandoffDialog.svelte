<script lang="ts">
  import { ArrowRight, X } from "@lucide/svelte";
  import AgentMark from "./AgentMark.svelte";
  import type { AgentOption } from "$lib/types";

  let {
    open,
    agents,
    sourceAgent,
    busy = false,
    onclose,
    ondeploy,
  }: {
    open: boolean;
    agents: AgentOption[];
    sourceAgent: string;
    busy?: boolean;
    onclose: () => void;
    ondeploy: (agent: string, model: string, effort: string) => void;
  } = $props();

  let agentKey = $state("");
  let model = $state("");
  let effort = $state("");
  let wasOpen = false;

  const activeAgent = $derived(
    agents.find((option) => option.key === agentKey) ?? agents[0],
  );

  function resetForAgent(key: string) {
    agentKey = key;
    const option = agents.find((item) => item.key === key) ?? agents[0];
    model = option?.models[0] ?? "Default";
    effort = option?.efforts[0] ?? "Default";
  }

  $effect(() => {
    if (open && !wasOpen) resetForAgent(sourceAgent || agents[0]?.key || "claude");
    wasOpen = open;
  });

  $effect(() => {
    activeAgent;
    if (activeAgent && !activeAgent.models.includes(model)) {
      model = activeAgent.models[0] ?? "Default";
    }
    if (activeAgent && !activeAgent.efforts.includes(effort)) {
      effort = activeAgent.efforts[0] ?? "Default";
    }
  });
</script>

{#if open}
  <div
    class="modal-backdrop"
    role="presentation"
    onclick={busy ? undefined : onclose}
    onkeydown={(event) => { if (!busy && event.key === "Escape") onclose(); }}
  >
    <dialog
      open
      class="handoff-dialog"
      aria-labelledby="handoff-title"
      onclick={(event) => event.stopPropagation()}
      onkeydown={(event) => event.stopPropagation()}
    >
      <header>
        <div>
          <h2 id="handoff-title">Deploy handoff</h2>
          <p>The staged `HANDOFF.md` becomes authoritative context for the new session.</p>
        </div>
        <button class="icon-button subtle" onclick={onclose} disabled={busy} aria-label="Close handoff dialog">
          <X size={17} />
        </button>
      </header>

      <div class="handoff-fields">
        <label>
          <span>Target client</span>
          <div class="select-shell">
            <AgentMark
              agent={activeAgent?.agent ?? "claude"}
              agentKey={activeAgent?.key ?? "claude"}
              model={model}
              size={22}
            />
            <select value={agentKey} onchange={(event) => resetForAgent(event.currentTarget.value)}>
              {#each agents as option}
                <option value={option.key}>{option.label}</option>
              {/each}
            </select>
          </div>
        </label>
        <label>
          <span>Model</span>
          <div class="select-shell">
            <select bind:value={model}>
              {#each activeAgent?.models ?? ["Default"] as option}
                <option value={option}>{option}</option>
              {/each}
            </select>
          </div>
        </label>
        <label>
          <span>Effort</span>
          <div class="select-shell">
            <select bind:value={effort} disabled={(activeAgent?.efforts.length ?? 0) <= 1}>
              {#each activeAgent?.efforts ?? ["Default"] as option}
                <option value={option}>{option}</option>
              {/each}
            </select>
          </div>
        </label>
      </div>

      <footer>
        <button class="header-button" onclick={onclose} disabled={busy}>Cancel</button>
        <button
          class="header-button deploy"
          disabled={busy}
          onclick={() => ondeploy(agentKey, model, effort)}
        >
          {busy ? "Creating continuation…" : "Create and continue"}
          <ArrowRight size={16} />
        </button>
      </footer>
    </dialog>
  </div>
{/if}
