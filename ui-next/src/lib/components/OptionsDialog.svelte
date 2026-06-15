<script lang="ts">
  import { PlugZap, RefreshCw, Settings2, X } from "@lucide/svelte";
  import {
    getConnectorStatuses,
    setConnectorToken,
    startConnectorAuth,
    type ConnectorStatus,
  } from "$lib/api";
  import type { AgentOption } from "$lib/types";

  let {
    open,
    agents,
    claudeOrchestration,
    handoffPreferences,
    busy = false,
    onclose,
    onsave,
  }: {
    open: boolean;
    agents: AgentOption[];
    claudeOrchestration: Record<string, unknown>;
    handoffPreferences: Record<string, unknown>;
    busy?: boolean;
    onclose: () => void;
    onsave: (
      claude: Record<string, unknown>,
      handoff: Record<string, unknown>,
    ) => void;
  } = $props();

  let orchestrationMode = $state("smart");
  let handoffMode = $state("current");
  let fixedAgent = $state("claude");
  let sequence = $state(["claude", "codex", "gemini", ""]);
  let autoDeploy = $state(false);
  let preferCheap = $state(true);
  let connectors = $state<ConnectorStatus[]>([]);
  let connectorLoading = $state(false);
  let connectorMessage = $state("");
  let authenticatingConnector = $state("");
  let savingTokenConnector = $state("");
  let tokenInputs = $state<Record<string, string>>({});
  let googleClientId = $state("");
  let googleClientSecret = $state("");
  let lastOpen = false;

  $effect(() => {
    if (open && !lastOpen) {
      orchestrationMode = String(claudeOrchestration["mode"] ?? "smart");
      handoffMode = String(handoffPreferences["mode"] ?? "current");
      fixedAgent = String(handoffPreferences["fixed_agent"] ?? "claude");
      const saved = Array.isArray(handoffPreferences["sequence"])
        ? handoffPreferences["sequence"].map(String).slice(0, 4)
        : [];
      sequence = [...saved, "", "", "", ""].slice(0, 4);
      autoDeploy = Boolean(handoffPreferences["auto_deploy"]);
      preferCheap = handoffPreferences["prefer_cheap_models"] !== false;
      void loadConnectors(false);
    }
    lastOpen = open;
  });

  async function loadConnectors(refresh: boolean) {
    connectorLoading = true;
    connectorMessage = "";
    try {
      connectors = await getConnectorStatuses(refresh);
    } catch (error) {
      connectorMessage = String(error);
    } finally {
      connectorLoading = false;
    }
  }

  function connectorLabel(name: string) {
    const labels: Record<string, string> = {
      threejs: "Three.js",
      "google-drive": "Google Drive",
      "google-calendar": "Google Calendar",
      gmail: "Gmail",
      "hugging-face": "Hugging Face",
      "cocounsel-legal": "CoCounsel Legal",
    };
    return labels[name] ?? name.replaceAll("-", " ");
  }

  function isGoogleWorkspace(name: string) {
    return ["gmail", "google-drive", "google-calendar"].includes(name);
  }

  async function connect(connector: ConnectorStatus) {
    if (authenticatingConnector) return;
    authenticatingConnector = connector.name;
    connectorMessage = "";
    try {
      await startConnectorAuth({
        connector: connector.name,
        clientId: isGoogleWorkspace(connector.name) ? googleClientId : "",
        clientSecret: isGoogleWorkspace(connector.name)
          ? googleClientSecret
          : "",
      });
      connectorMessage = `${connectorLabel(connector.name)} login opened in your browser. Complete it, then refresh status.`;
    } catch (error) {
      connectorMessage = String(error);
    } finally {
      authenticatingConnector = "";
    }
  }

  async function saveToken(connector: ConnectorStatus) {
    if (savingTokenConnector) return;
    const token = tokenInputs[connector.name] ?? "";
    savingTokenConnector = connector.name;
    connectorMessage = "";
    try {
      await setConnectorToken(connector.name, token);
      tokenInputs = { ...tokenInputs, [connector.name]: "" };
      await loadConnectors(true);
    } catch (error) {
      connectorMessage = String(error);
    } finally {
      savingTokenConnector = "";
    }
  }

  function updateSequence(index: number, value: string) {
    sequence = sequence.map((entry, entryIndex) =>
      entryIndex === index ? value : entry,
    );
  }

  function save() {
    onsave(
      { mode: orchestrationMode },
      {
        mode: handoffMode,
        fixed_agent: fixedAgent,
        sequence: [...new Set(sequence.filter(Boolean))],
        auto_deploy: autoDeploy,
        prefer_cheap_models: preferCheap,
      },
    );
  }
</script>

{#if open}
  <div
    class="modal-backdrop"
    role="presentation"
    onclick={onclose}
    onkeydown={(event) => { if (event.key === "Escape") onclose(); }}
  >
    <dialog
      open
      class="options-dialog"
      aria-labelledby="options-title"
      onclick={(event) => event.stopPropagation()}
      onkeydown={(event) => event.stopPropagation()}
    >
      <header>
        <div>
          <h2 id="options-title"><Settings2 size={19} /> Workbench options</h2>
          <p>Controls behavior; client credentials remain in their native CLIs.</p>
        </div>
        <button class="icon-button subtle" onclick={onclose} disabled={busy} aria-label="Close options">
          <X size={17} />
        </button>
      </header>

      <section class="options-section">
        <h3>Claude orchestration</h3>
        <label class="option-row">
          <span>
            <strong>Routing policy</strong>
            <small>Smart delegates bounded research to Haiku and implementation to Sonnet.</small>
          </span>
          <select bind:value={orchestrationMode}>
            <option value="smart">Smart</option>
            <option value="direct">Direct driver only</option>
          </select>
        </label>
      </section>

      <section class="options-section">
        <h3>Handoff behavior</h3>
        <label class="option-row">
          <span>
            <strong>Default target</strong>
            <small>Choose the current client, one fixed client, or a repeating sequence.</small>
          </span>
          <select bind:value={handoffMode}>
            <option value="current">Current client</option>
            <option value="fixed">Fixed client</option>
            <option value="sequence">Client sequence</option>
          </select>
        </label>

        {#if handoffMode === "fixed"}
          <label class="option-row">
            <span><strong>Fixed client</strong></span>
            <select bind:value={fixedAgent}>
              {#each agents as agent}
                <option value={agent.key}>{agent.label}</option>
              {/each}
            </select>
          </label>
        {:else if handoffMode === "sequence"}
          <div class="sequence-grid">
            {#each sequence as value, index}
              <label>
                <span>{index + 1}</span>
                <select value={value} onchange={(event) => updateSequence(index, event.currentTarget.value)}>
                  <option value="">Unused</option>
                  {#each agents as agent}
                    <option value={agent.key}>{agent.label}</option>
                  {/each}
                </select>
              </label>
            {/each}
          </div>
        {/if}

        <label class="check-row">
          <input type="checkbox" bind:checked={preferCheap} />
          <span>
            <strong>Prefer cheaper continuation models</strong>
            <small>Uses Sonnet, Codex Mini, or Gemini Flash when available.</small>
          </span>
        </label>
        <label class="check-row">
          <input type="checkbox" bind:checked={autoDeploy} />
          <span>
            <strong>Automatically deploy staged handoffs</strong>
            <small>After an eligible turn finishes with no queued prompts, starts the configured continuation.</small>
          </span>
        </label>
      </section>

      <section class="options-section">
        <div class="connector-heading">
          <div>
            <h3><PlugZap size={15} /> Direct connectors</h3>
            <small>Local Qwen calls these MCP services directly. Claude credits are not used.</small>
          </div>
          <button
            class="icon-button subtle"
            onclick={() => loadConnectors(true)}
            disabled={connectorLoading || Boolean(authenticatingConnector)}
            aria-label="Refresh connector status"
          >
            <RefreshCw size={15} class={connectorLoading ? "spin" : ""} />
          </button>
        </div>

        {#if connectors.some((connector) => isGoogleWorkspace(connector.name) && !connector.authenticated)}
          <div class="oauth-fields">
            <label>
              <span>Google Workspace OAuth client ID</span>
              <input
                bind:value={googleClientId}
                placeholder="Required for Gmail, Drive, and Calendar"
                autocomplete="off"
              />
            </label>
            <label>
              <span>Google Workspace OAuth client secret</span>
              <input
                type="password"
                bind:value={googleClientSecret}
                placeholder="Stored locally in ~/.qwen/settings.json"
                autocomplete="new-password"
              />
            </label>
          </div>
        {/if}

        <div class="connector-list" aria-live="polite">
          {#if connectorLoading && connectors.length === 0}
            <p class="connector-empty">Checking direct MCP services…</p>
          {:else}
            {#each connectors as connector}
              <div class="connector-row">
                <span class:connected={connector.authenticated} class:unavailable={!connector.reachable}>
                  {connector.authenticated ? "Connected" : connector.reachable ? "Login required" : "Unavailable"}
                </span>
                <div>
                  <strong>{connectorLabel(connector.name)}</strong>
                  <small>
                    {connector.toolCount} tools
                    {connector.error ? ` · ${connector.error}` : ""}
                  </small>
                </div>
                {#if connector.requiresAuth}
                  <button
                    class="header-button"
                    onclick={() => connect(connector)}
                    disabled={Boolean(authenticatingConnector) || Boolean(savingTokenConnector)}
                  >
                    {authenticatingConnector === connector.name
                      ? "Opening…"
                      : connector.authenticated
                        ? "Reconnect"
                        : "Connect"}
                  </button>
                {/if}
              </div>
              {#if connector.requiresAuth && !isGoogleWorkspace(connector.name)}
                <div class="oauth-fields token-row">
                  <label>
                    <span>{connectorLabel(connector.name)} API token</span>
                    <input
                      type="password"
                      placeholder="Paste API token…"
                      autocomplete="new-password"
                      value={tokenInputs[connector.name] ?? ""}
                      oninput={(e) => {
                        tokenInputs = { ...tokenInputs, [connector.name]: (e.currentTarget as HTMLInputElement).value };
                      }}
                    />
                  </label>
                  <button
                    class="header-button"
                    onclick={() => saveToken(connector)}
                    disabled={Boolean(savingTokenConnector) || Boolean(authenticatingConnector) || !(tokenInputs[connector.name] ?? "").trim()}
                  >
                    {savingTokenConnector === connector.name ? "Saving…" : "Save token"}
                  </button>
                </div>
              {/if}
            {/each}
          {/if}
        </div>
        {#if connectorMessage}
          <p class="connector-message">{connectorMessage}</p>
        {/if}
      </section>

      <footer>
        <button class="header-button" onclick={onclose} disabled={busy}>Cancel</button>
        <button class="header-button primary" onclick={save} disabled={busy}>
          {busy ? "Saving…" : "Save options"}
        </button>
      </footer>
    </dialog>
  </div>
{/if}
