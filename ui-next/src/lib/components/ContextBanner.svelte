<script lang="ts">
  let {
    percent = 0,
    compactCount = 0,
  }: {
    percent?: number;
    compactCount?: number;
  } = $props();

  // Dismissed state resets when percent climbs to a new tier.
  // Tier encoding: 0=none, 1=compact-only, 2=orange(70-84%), 3=red(≥85%)
  function tier(p: number, cc: number): number {
    if (p >= 85) return 3;
    if (p >= 70) return 2;
    if (cc > 0) return 1;
    return 0;
  }

  let dismissedTier = $state(0);
  const currentTier = $derived(tier(percent, compactCount));

  $effect(() => {
    // When tier goes up, un-dismiss so the higher-urgency banner shows.
    if (currentTier > dismissedTier) {
      dismissedTier = 0;
    }
  });

  const visible = $derived(currentTier > 0 && currentTier > dismissedTier);
</script>

{#if visible}
  <div
    class="context-banner"
    class:context-banner-compact={currentTier === 1}
    class:context-banner-orange={currentTier === 2}
    class:context-banner-red={currentTier === 3}
    role="alert"
  >
    <span>
      {#if currentTier === 3}
        Context at {Math.round(percent)}% — hand off NOW to avoid quality loss
      {:else if currentTier === 2}
        Context at {Math.round(percent)}% — a handoff is recommended
      {:else}
        Context was compacted {compactCount}× — consider handing off
      {/if}
    </span>
    <button
      class="context-banner-dismiss"
      aria-label="Dismiss context warning"
      onclick={() => (dismissedTier = currentTier)}
    >
      ✕
    </button>
  </div>
{/if}
