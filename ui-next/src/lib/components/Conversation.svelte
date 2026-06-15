<script lang="ts">
  import { ArrowDown } from "@lucide/svelte";
  import { tick } from "svelte";
  import MessageCard from "./MessageCard.svelte";
  import type { Message } from "$lib/types";

  let {
    messages,
    sessionId,
    agentKey,
    model,
  }: {
    messages: Message[];
    sessionId: string;
    agentKey: string;
    model: string;
  } = $props();

  let viewport: HTMLElement;
  let visibleCount = $state(80);
  let followingLatest = $state(true);
  let hasNewOutput = $state(false);

  const startIndex = $derived(Math.max(0, messages.length - visibleCount));
  const visibleMessages = $derived(messages.slice(startIndex));
  const hiddenCount = $derived(startIndex);

  function isAtBottom() {
    if (!viewport) return true;
    return viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 48;
  }

  function handleScroll() {
    followingLatest = isAtBottom();
    if (followingLatest) hasNewOutput = false;
  }

  function scrollToLatest() {
    followingLatest = true;
    hasNewOutput = false;
    void tick().then(() => {
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    });
  }

  function showOlder() {
    const before = viewport?.scrollHeight ?? 0;
    visibleCount += 80;
    void tick().then(() => {
      if (viewport) viewport.scrollTop += viewport.scrollHeight - before;
    });
  }

  $effect(() => {
    sessionId;
    visibleCount = 80;
    followingLatest = true;
    hasNewOutput = false;
    scrollToLatest();
  });

  $effect(() => {
    messages.length;
    messages.at(-1)?.content.length;
    if (followingLatest) {
      scrollToLatest();
    } else {
      hasNewOutput = true;
    }
  });
</script>

<section class="conversation" bind:this={viewport} onscroll={handleScroll}>
  <div class="conversation-inner">
    {#if hiddenCount > 0}
      <button class="load-older" onclick={showOlder}>
        Load {Math.min(80, hiddenCount)} earlier messages
      </button>
    {/if}
    {#if visibleMessages.length === 0}
      <div class="conversation-empty">
        <strong>No messages yet</strong>
        <span>Send the first prompt to start this session.</span>
      </div>
    {/if}
    {#each visibleMessages as message (message.id)}
      <MessageCard {message} {agentKey} {model} />
    {/each}
  </div>
  <button class:new={hasNewOutput} class="jump-bottom" aria-label="Jump to latest" onclick={scrollToLatest}>
    <ArrowDown size={18} />
    {#if hasNewOutput}<span>New</span>{/if}
  </button>
</section>
