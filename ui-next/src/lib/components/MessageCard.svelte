<script lang="ts">
  import { Check, Copy } from "@lucide/svelte";
  import { marked } from "marked";
  import DOMPurify from "dompurify";
  import AgentMark from "./AgentMark.svelte";
  import type { Message } from "$lib/types";

  let {
    message,
    agentKey = "",
    model = "",
  }: {
    message: Message;
    agentKey?: string;
    model?: string;
  } = $props();
  let copied = $state(false);

  const html = $derived(
    DOMPurify.sanitize(marked.parse(message.content, { async: false }) as string),
  );

  function agentLabel(): string {
    switch (message.agent) {
      case "codex":
        return "Codex";
      case "gemini":
        return "Gemini";
      case "local":
        return "Local";
      default:
        return "Claude";
    }
  }

  async function copyMessage() {
    await navigator.clipboard.writeText(message.content);
    copied = true;
    window.setTimeout(() => (copied = false), 1_200);
  }
</script>

<article class="message {message.role}">
  {#if message.role === "assistant" && message.agent}
    <AgentMark agent={message.agent} {agentKey} {model} size={28} />
  {/if}
  <div class="message-body">
    <div class="message-meta">
      <strong>{message.role === "user" ? "You" : agentLabel()}</strong>
      <span>{message.time}</span>
    </div>
    <div class="message-card">
      <div class="message-md">{@html html}</div>
      {#if message.role === "assistant"}
        <button class="copy-button" aria-label="Copy message" onclick={copyMessage}>
          {#if copied}
            <Check size={14} />
          {:else}
            <Copy size={14} />
          {/if}
        </button>
      {/if}
    </div>
    {#if message.role === "assistant" && message.content}
      <div class="completion-note">
        <Check size={13} />
        Completed
      </div>
    {/if}
  </div>
</article>
