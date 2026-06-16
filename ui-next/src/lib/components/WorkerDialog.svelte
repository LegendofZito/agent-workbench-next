<script lang="ts">
  import { GitBranch, X } from "@lucide/svelte";
  import type { AgentOption } from "$lib/types";

  let {
    open,
    agents,
    sourceAgentKey = "claude",
    sourceSessionId = "",
    busy = false,
    onclose,
    onspawn,
  }: {
    open: boolean;
    agents: AgentOption[];
    sourceAgentKey?: string;
    sourceSessionId?: string;
    busy?: boolean;
    onclose: () => void;
    onspawn: (params: {
      prompt: string;
      agent: string;
      model: string;
      effort: string;
    }) => void;
  } = $props();

  const CHEAP_MODEL: Record<string, string> = {
    claude: "sonnet",
    codex: "gpt-5.4-mini",
    gemini: "gemini-3-flash-preview",
  };

  let prompt = $state("");
  let selectedAgent = $state("");
  let selectedModel = $state("");

  const agentOption = $derived(
    agents.find((a) => a.key === selectedAgent) ?? agents[0],
  );

  const defaultModel = $derived.by(() => {
    if (!agentOption) return "Default";
    const cheap = CHEAP_MODEL[agentOption.key];
    if (cheap && agentOption.models.includes(cheap)) return cheap;
    return agentOption.models[0] ?? "Default";
  });

  // Reset when dialog opens or agent changes.
  $effect(() => {
    if (open) {
      prompt = "";
      selectedAgent = sourceAgentKey;
    }
  });

  $effect(() => {
    selectedModel = defaultModel;
  });

  function submit() {
    const text = prompt.trim();
    if (!text || busy) return;
    onspawn({
      prompt: text,
      agent: selectedAgent,
      model: selectedModel,
      effort: agentOption?.efforts[0] ?? "Default",
    });
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === "Escape") onclose();
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="modal-backdrop"
    role="presentation"
    onclick={onclose}
    onkeydown={onKeydown}
  >
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="worker-dialog"
      role="dialog"
      tabindex="-1"
      aria-label="Spawn a delegated worker session"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.stopPropagation()}
    >
      <header>
        <div>
          <h2><GitBranch size={16} /> Delegate to sub-agent</h2>
          <p>A new session will be opened alongside this one.</p>
        </div>
        <button class="icon-button" aria-label="Close" onclick={onclose}>
          <X size={17} />
        </button>
      </header>

      <div class="worker-dialog-fields">
        <label>
          <span>Task prompt</span>
          <textarea
            bind:value={prompt}
            placeholder="Describe the bounded task for the sub-agent…"
            rows={5}
            disabled={busy}
          ></textarea>
        </label>

        <div class="worker-dialog-row">
          <label>
            <span>Agent</span>
            <select bind:value={selectedAgent} disabled={busy}>
              {#each agents as option}
                <option value={option.key}>{option.label}</option>
              {/each}
            </select>
          </label>

          <label>
            <span>Model (defaults to cheap)</span>
            <select bind:value={selectedModel} disabled={busy}>
              {#each agentOption?.models ?? [] as model}
                <option value={model}>{model || "Default"}</option>
              {/each}
            </select>
          </label>
        </div>
      </div>

      <footer>
        <button class="header-button" onclick={onclose} disabled={busy}>Cancel</button>
        <button
          class="header-button deploy"
          onclick={submit}
          disabled={!prompt.trim() || busy}
        >
          <GitBranch size={15} />
          {busy ? "Spawning…" : "Delegate"}
        </button>
      </footer>
    </div>
  </div>
{/if}
