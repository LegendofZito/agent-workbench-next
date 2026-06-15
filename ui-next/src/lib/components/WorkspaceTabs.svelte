<script lang="ts">
  import { Plus, X } from "@lucide/svelte";
  import AgentMark from "./AgentMark.svelte";
  import type { WorkspaceTab } from "$lib/types";

  let {
    tabs,
    activeId,
    onselect,
    onclose,
    onnew,
  }: {
    tabs: WorkspaceTab[];
    activeId: string;
    onselect: (id: string) => void;
    onclose: (id: string) => void;
    onnew: () => void;
  } = $props();
</script>

<nav class="workspace-tabs" aria-label="Open workspaces">
  <div class="window-drag-region" data-tauri-drag-region></div>
  <div class="tab-strip">
    {#each tabs as tab (tab.id)}
      <div
        class:active={tab.id === activeId}
        class="workspace-tab"
        title={`${tab.label}\n${tab.cwd}`}
      >
        <button class="tab-select" onclick={() => onselect(tab.id)}>
          <AgentMark
            agent={tab.agent}
            agentKey={tab.agentKey}
            model={tab.model}
            size={20}
          />
          <span class="tab-title">{tab.label}</span>
        </button>
        <button
          class="tab-close"
          aria-label={`Close ${tab.label}`}
          disabled={tabs.length === 1}
          onclick={() => onclose(tab.id)}
        >
          <X size={14} strokeWidth={1.8} />
        </button>
      </div>
    {/each}
    <button class="new-tab" aria-label="New workspace" onclick={onnew}>
      <Plus size={17} />
    </button>
  </div>
</nav>
