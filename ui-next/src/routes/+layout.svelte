<script lang="ts">
  import "@fontsource-variable/inter";
  import "../app.css";
  import { onMount } from "svelte";

  let { children } = $props();

  // Ctrl+scroll / Ctrl+(=,-,0) zoom — applied via CSS `zoom` on the root so it
  // scales everything (including fixed overlays/modals) and persists.
  const ZOOM_KEY = "awbench-zoom";
  const MIN = 0.5;
  const MAX = 3;
  const STEP = 0.1;

  function clampZoom(z: number): number {
    return Math.min(MAX, Math.max(MIN, Math.round(z * 100) / 100));
  }

  function currentZoom(): number {
    const v = parseFloat((document.documentElement.style as any).zoom || "1");
    return Number.isFinite(v) && v > 0 ? v : 1;
  }

  function applyZoom(z: number): number {
    const zoom = clampZoom(z);
    (document.documentElement.style as any).zoom = String(zoom);
    try {
      localStorage.setItem(ZOOM_KEY, String(zoom));
    } catch {}
    return zoom;
  }

  onMount(() => {
    let saved = 1;
    try {
      saved = parseFloat(localStorage.getItem(ZOOM_KEY) || "1") || 1;
    } catch {}
    applyZoom(saved);

    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      applyZoom(currentZoom() + (e.deltaY < 0 ? STEP : -STEP));
    };
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "=" || e.key === "+") {
        e.preventDefault();
        applyZoom(currentZoom() + STEP);
      } else if (e.key === "-" || e.key === "_") {
        e.preventDefault();
        applyZoom(currentZoom() - STEP);
      } else if (e.key === "0") {
        e.preventDefault();
        applyZoom(1);
      }
    };

    // wheel must be non-passive so preventDefault can suppress native zoom/scroll
    window.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("keydown", onKey);
    };
  });
</script>

{@render children()}
