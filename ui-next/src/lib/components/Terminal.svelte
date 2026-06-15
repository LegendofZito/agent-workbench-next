<script lang="ts">
  import { onMount } from "svelte";
  import { listen } from "@tauri-apps/api/event";
  import { FitAddon } from "@xterm/addon-fit";
  import { Terminal as XTerminal } from "@xterm/xterm";
  import "@xterm/xterm/css/xterm.css";
  import {
    resizeTerminal,
    startTerminal,
    writeTerminal,
  } from "$lib/api";

  let {
    workspaceId,
    cwd,
  }: {
    workspaceId: string;
    cwd: string;
  } = $props();

  let host: HTMLDivElement;
  let error = $state("");

  onMount(() => {
    const terminal = new XTerminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: 'ui-monospace, "SFMono-Regular", Consolas, monospace',
      fontSize: 12,
      lineHeight: 1.25,
      scrollback: 10_000,
      theme: {
        background: "#090d14",
        foreground: "#dbe4f0",
        cursor: "#8ea2ff",
        selectionBackground: "#314269",
        black: "#111827",
        red: "#f87171",
        green: "#4ade80",
        yellow: "#fbbf24",
        blue: "#60a5fa",
        magenta: "#c084fc",
        cyan: "#67e8f9",
        white: "#f8fafc",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(host);
    fit.fit();
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const input = terminal.onData((data) => {
      void writeTerminal(workspaceId, data).catch((reason) => {
        error = String(reason);
      });
    });
    const resize = new ResizeObserver(() => {
      fit.fit();
      void resizeTerminal(workspaceId, terminal.cols, terminal.rows).catch(
        () => undefined,
      );
    });
    resize.observe(host);

    void (async () => {
      unlisten = await listen<{ workspace_id: string; data: string }>(
        "terminal-output",
        (event) => {
          if (!disposed && event.payload.workspace_id === workspaceId) {
            terminal.write(event.payload.data);
          }
        },
      );
      try {
        const backlog = await startTerminal(
          workspaceId,
          cwd,
          terminal.cols,
          terminal.rows,
        );
        if (!disposed && backlog) terminal.write(backlog);
        terminal.focus();
      } catch (reason) {
        error = String(reason);
      }
    })();

    return () => {
      disposed = true;
      unlisten?.();
      input.dispose();
      resize.disconnect();
      terminal.dispose();
    };
  });
</script>

<section class="terminal-view">
  {#if error}<div class="terminal-error">{error}</div>{/if}
  <div class="terminal-host" bind:this={host}></div>
</section>
