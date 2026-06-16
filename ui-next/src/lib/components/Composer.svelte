<script lang="ts">
  import {
    ArrowUp,
    File,
    Paperclip,
    Square,
    Trash2,
    Users,
    X,
  } from "@lucide/svelte";

  let {
    workspaceId,
    draft = "",
    attachments = [],
    queue = [],
    workers = [],
    busy = false,
    elapsed = 0,
    liveTokens = 0,
    placeholder = "Message",
    statusLabel = "",
    note = "",
    onsend,
    onstop,
    onattach,
    onremoveattachment,
    onremovequeue,
    onpasteimage,
    ondraft,
    onqueue,
  }: {
    workspaceId: string;
    draft?: string;
    attachments?: string[];
    queue?: Array<{ prompt: string; attachments: string[] }>;
    workers?: string[];
    busy?: boolean;
    elapsed?: number;
    liveTokens?: number;
    placeholder?: string;
    statusLabel?: string;
    note?: string;
    onsend: (text: string) => void;
    onstop: () => void;
    onattach: () => void;
    onremoveattachment: (path: string) => void;
    onremovequeue: (index: number) => void;
    onpasteimage: () => void;
    ondraft: (text: string) => void;
    onqueue?: (text: string) => void;
  } = $props();

  let prompt = $state("");
  let queueOpen = $state(false);
  let workersOpen = $state(false);
  let lastWorkspaceId = "";

  function submit() {
    const text = prompt.trim();
    if (!text && attachments.length === 0) return;
    onsend(text);
    prompt = "";
    ondraft("");
  }

  function queueDraft() {
    const text = prompt.trim();
    if (!text) return;
    onqueue?.(text);
    prompt = "";
    ondraft("");
  }

  function onInput(event: Event) {
    prompt = (event.currentTarget as HTMLTextAreaElement).value;
    ondraft(prompt);
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function onPaste(event: ClipboardEvent) {
    if (
      Array.from(event.clipboardData?.types ?? []).some((type) =>
        type.startsWith("image/"),
      )
    ) {
      event.preventDefault();
      onpasteimage();
    }
  }

  function basename(path: string) {
    return path.split("/").filter(Boolean).at(-1) ?? path;
  }

  function elapsedLabel(seconds: number) {
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
  }

  $effect(() => {
    if (workspaceId !== lastWorkspaceId) {
      lastWorkspaceId = workspaceId;
      prompt = draft;
      queueOpen = false;
      workersOpen = false;
    }
  });
</script>

<section class="composer-wrap">
  {#if note}
    <div class="preview-warning">{note}</div>
  {/if}
  {#if queueOpen && queue.length}
    <div class="queue-panel">
      <div class="queue-heading">
        <strong>Queued prompts</strong>
        <button class="icon-button subtle" onclick={() => (queueOpen = false)} aria-label="Close queue">
          <X size={15} />
        </button>
      </div>
      {#each queue as queued, index}
        <div class="queue-row">
          <span>{queued.prompt || queued.attachments.map(basename).join(", ")}</span>
          <button class="icon-button subtle" onclick={() => onremovequeue(index)} aria-label="Remove queued prompt">
            <Trash2 size={14} />
          </button>
        </div>
      {/each}
    </div>
  {/if}
  {#if workersOpen && workers.length}
    <div class="queue-panel worker-panel">
      <div class="queue-heading">
        <strong>Active sub-agents</strong>
        <button class="icon-button subtle" onclick={() => (workersOpen = false)} aria-label="Close sub-agent list">
          <X size={15} />
        </button>
      </div>
      {#each workers as worker}
        <div class="queue-row"><span>{worker}</span></div>
      {/each}
    </div>
  {/if}
  <div class="composer">
    {#if attachments.length}
      <div class="attachment-chips">
        {#each attachments as path}
          <span class="attachment-chip">
            <File size={13} />
            {basename(path)}
            <button onclick={() => onremoveattachment(path)} aria-label={`Remove ${basename(path)}`}>
              <X size={12} />
            </button>
          </span>
        {/each}
      </div>
    {/if}
    <textarea
      value={prompt}
      oninput={onInput}
      onkeydown={onKeydown}
      onpaste={onPaste}
      {placeholder}
    ></textarea>
    <div class="composer-toolbar">
      <div class="composer-tools">
        <button class="icon-button subtle" aria-label="Attach file" onclick={onattach}>
          <Paperclip size={18} />
        </button>
        <span class="draft-state">Draft saves automatically</span>
      </div>
      <div class="composer-actions">
        {#if busy}
          <button class="stop-button" onclick={onstop} aria-label="Stop active turn">
            <Square size={14} fill="currentColor" />
            Stop
          </button>
        {/if}
        {#if onqueue && !busy}
          <button
            class="queue-button"
            onclick={queueDraft}
            disabled={!prompt.trim()}
            aria-label="Add to queue"
            title="Add to queue — runs after current turn finishes"
          >
            Queue
          </button>
        {/if}
        <button
          class="send-button"
          onclick={submit}
          disabled={!prompt.trim() && attachments.length === 0}
          aria-label={busy ? "Add to queue" : "Send"}
          title={busy ? "Add this prompt to the queue" : "Send"}
        >
          <ArrowUp size={19} />
        </button>
      </div>
    </div>
  </div>
  <div class="activity-bar">
    <button
      class="footer-control"
      disabled={workers.length === 0}
      onclick={() => (workersOpen = !workersOpen)}
    >
      <Users size={13} />
      Sub-agents {workers.length}
    </button>
    <span class:working={busy} class="status-pill">
      <i></i>
      <span class="working-label">{busy ? "Working" : "Ready"}</span>
      {#if busy}<span class="working-dots" aria-hidden="true"></span>{/if}
    </span>
    <span>{statusLabel}</span>
    {#if busy}
      <span>{elapsedLabel(elapsed)} · ~{liveTokens.toLocaleString()} output tokens</span>
    {:else if liveTokens > 0}
      <span>Last response · {liveTokens.toLocaleString()} output tokens</span>
    {/if}
    <span class="activity-spacer"></span>
    {#if queue.length}
      <button class="footer-control queue" onclick={() => (queueOpen = !queueOpen)}>
        Queue {queue.length}
      </button>
    {/if}
  </div>
</section>
