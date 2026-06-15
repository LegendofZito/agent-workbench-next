<script lang="ts">
  import {
    ExternalLink,
    File,
    FilePlus2,
    Paperclip,
    Pencil,
    Trash2,
  } from "@lucide/svelte";
  import { convertFileSrc } from "@tauri-apps/api/core";
  import { openPath } from "@tauri-apps/plugin-opener";
  import type { Artifact } from "$lib/types";

  let {
    artifacts,
    loading = false,
    onadd,
    onattach,
    onrename,
    ondelete,
  }: {
    artifacts: Artifact[];
    loading?: boolean;
    onadd: () => void;
    onattach: (path: string) => void;
    onrename: (artifact: Artifact) => void;
    ondelete: (artifact: Artifact) => void;
  } = $props();

  function sizeLabel(size: number) {
    if (size >= 1_000_000) return `${(size / 1_000_000).toFixed(1)} MB`;
    if (size >= 1_000) return `${Math.round(size / 1_000)} KB`;
    return `${size} B`;
  }

  function openArtifact(path: string) {
    void openPath(path);
  }
</script>

<section class="artifacts-view">
  <header class="artifacts-toolbar">
    <div>
      <h2>Artifacts</h2>
      <p>Files stored with this session.</p>
    </div>
    <button class="header-button" onclick={onadd}>
      <FilePlus2 size={16} />
      Add files
    </button>
  </header>

  {#if loading}
    <p class="work-log-empty">Loading artifacts…</p>
  {:else if artifacts.length === 0}
    <div class="empty-view">
      <div class="empty-icon"><File size={24} /></div>
      <h2>No artifacts yet</h2>
      <p>Use Add files or the paperclip in the composer.</p>
    </div>
  {:else}
    <div class="artifact-grid">
      {#each artifacts as artifact (artifact.path)}
        <article class="artifact-card">
          <div class="artifact-preview">
            {#if artifact.isImage}
              <img src={convertFileSrc(artifact.path)} alt={artifact.name} />
            {:else}
              <File size={24} />
            {/if}
          </div>
          <div class="artifact-copy">
            <strong>{artifact.name}</strong>
            <span>{sizeLabel(artifact.size)}</span>
          </div>
          <div class="artifact-actions">
            <button class="icon-button subtle" onclick={() => onattach(artifact.path)} aria-label={`Attach ${artifact.name}`}>
              <Paperclip size={15} />
            </button>
            <button class="icon-button subtle" onclick={() => openArtifact(artifact.path)} aria-label={`Open ${artifact.name}`}>
              <ExternalLink size={15} />
            </button>
            <button class="icon-button subtle" onclick={() => onrename(artifact)} aria-label={`Rename ${artifact.name}`}>
              <Pencil size={15} />
            </button>
            <button class="icon-button subtle danger" onclick={() => ondelete(artifact)} aria-label={`Delete ${artifact.name}`}>
              <Trash2 size={15} />
            </button>
          </div>
        </article>
      {/each}
    </div>
  {/if}
</section>
