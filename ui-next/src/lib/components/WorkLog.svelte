<script lang="ts">
  import { ArrowDown } from "@lucide/svelte";
  import type { WorkLogEntry } from "$lib/api";

  let { entries }: { entries: WorkLogEntry[] } = $props();

  let viewport: HTMLDivElement;
  let expanded = $state<Set<string>>(new Set());

  function toggle(id: string) {
    const next = new Set(expanded);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    expanded = next;
  }

  const TRUNCATE_AT = 300;

  function typeLabel(type: string): string {
    switch (type) {
      case 'system': return 'SYS';
      case 'commandExecution': return 'CMD';
      case 'fileChange': return 'FILE';
      case 'reasoning': return 'THINK';
      case 'report': return 'RPT';
      default: return type.toUpperCase().slice(0, 6);
    }
  }

  function scrollToBottom() {
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }
</script>

<div class="work-log" bind:this={viewport}>
  {#if entries.length === 0}
    <p class="work-log-empty">No work log entries for this session.</p>
  {:else}
    {#each entries as entry (entry.id)}
      {@const isLong = entry.content.length > TRUNCATE_AT}
      {@const isExpanded = expanded.has(entry.id)}
      <div class="work-log-entry">
        <span class="entry-type" data-type={entry.type}>{typeLabel(entry.type)}</span>
        <div class="entry-body">
          <div class="entry-content">
            {isLong && !isExpanded
              ? entry.content.slice(0, TRUNCATE_AT) + '…'
              : entry.content}
          </div>
          {#if isLong}
            <button class="entry-expand" onclick={() => toggle(entry.id)}>
              {isExpanded ? 'Show less' : 'Show more'}
            </button>
          {/if}
        </div>
        <span class="entry-time">{entry.time}</span>
      </div>
    {/each}
  {/if}
  {#if entries.length}
    <button class="work-log-jump" onclick={scrollToBottom} aria-label="Jump to newest work log entry">
      <ArrowDown size={17} />
    </button>
  {/if}
</div>
