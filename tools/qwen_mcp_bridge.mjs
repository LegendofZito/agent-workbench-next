#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const SETTINGS_PATH = path.join(os.homedir(), ".qwen", "settings.json");
const PUBLIC_CONNECTORS = new Set(["threejs"]);

function qwenRoot() {
  const candidates = [
    process.env.QWEN_CODE_ROOT,
    path.join(
      os.homedir(),
      ".npm-global",
      "lib",
      "node_modules",
      "@qwen-code",
      "qwen-code",
    ),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, "chunks", "src-GLLQ3R5W.js"))) {
      return candidate;
    }
  }
  const npmRoot = execFileSync("npm", ["root", "-g"], {
    encoding: "utf8",
  }).trim();
  return path.join(npmRoot, "@qwen-code", "qwen-code");
}

function loadSettings() {
  return JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf8"));
}

function connectorConfig(settings, name) {
  const config = settings.mcpServers?.[name];
  if (!config) throw new Error(`Unknown connector: ${name}`);
  return config;
}

function createClient(McpClient, name, config) {
  return new McpClient(
    name,
    config,
    { registerTool() {} },
    { registerPrompt() {} },
    { getDirectories: () => [process.cwd()] },
    false,
  );
}

function contentText(result) {
  return (result?.content ?? [])
    .map((part) => {
      if (typeof part?.text === "string") return part.text;
      if (part?.resource) return JSON.stringify(part.resource);
      return JSON.stringify(part);
    })
    .filter(Boolean)
    .join("\n");
}

async function connectorStatus(core, settings, name, config) {
  const credentials = await new core.MCPOAuthTokenStorage().getCredentials(name);
  const client = createClient(core.McpClient, name, config);
  let reachable = false;
  let toolCount = 0;
  let error = "";
  try {
    await client.connect();
    reachable = true;
    const tools = await client.client.listTools();
    toolCount = tools.tools?.length ?? 0;
  } catch (caught) {
    error = String(caught);
  } finally {
    await client.disconnect().catch(() => {});
  }
  const requiresAuth = !PUBLIC_CONNECTORS.has(name);
  return {
    name,
    url: config.httpUrl ?? config.url ?? "",
    reachable,
    authenticated: !requiresAuth || Boolean(credentials?.token?.accessToken),
    requires_auth: requiresAuth,
    oauth_configured: Boolean(config.oauth?.clientId),
    tool_count: toolCount,
    error,
  };
}

const corePath = path.join(qwenRoot(), "chunks", "src-GLLQ3R5W.js");
const core = await import(pathToFileURL(corePath).href);
const [mode, name, argument] = process.argv.slice(2);
const settings = loadSettings();

if (mode === "list") {
  const entries = await Promise.all(
    Object.entries(settings.mcpServers ?? {}).map(([connectorName, config]) =>
      connectorStatus(core, settings, connectorName, config),
    ),
  );
  console.log(JSON.stringify({ connectors: entries }));
} else if (mode === "tools") {
  const config = connectorConfig(settings, name);
  const client = createClient(core.McpClient, name, config);
  try {
    await client.connect();
    const tools = await client.client.listTools();
    console.log(
      JSON.stringify({
        connector: name,
        tools: (tools.tools ?? []).map((tool) => ({
          name: tool.name,
          description: tool.description ?? "",
          inputSchema: tool.inputSchema ?? {
            type: "object",
            properties: {},
          },
          annotations: tool.annotations ?? {},
        })),
      }),
    );
  } finally {
    await client.disconnect().catch(() => {});
  }
} else if (mode === "call") {
  const config = connectorConfig(settings, name);
  const request = JSON.parse(argument ?? "{}");
  const client = createClient(core.McpClient, name, config);
  try {
    await client.connect();
    const result = await client.client.callTool({
      name: request.tool,
      arguments: request.arguments ?? {},
    });
    console.log(
      JSON.stringify({
        connector: name,
        tool: request.tool,
        is_error: Boolean(result?.isError),
        content: result?.content ?? [],
        text: contentText(result),
      }),
    );
  } finally {
    await client.disconnect().catch(() => {});
  }
} else if (mode === "auth") {
  const config = connectorConfig(settings, name);
  const serverUrl = config.httpUrl ?? config.url;
  const discovered = await core.OAuthUtils.discoverOAuthConfig(serverUrl);
  if (!discovered) {
    throw new Error(`OAuth discovery failed for ${name}`);
  }
  const oauth = { ...discovered, ...(config.oauth ?? {}), enabled: true };
  if (!oauth.clientId && !oauth.registrationUrl) {
    throw new Error(
      `${name} requires an OAuth client ID and secret in Qwen settings before login.`,
    );
  }
  const provider = new core.MCPOAuthProvider(
    new core.MCPOAuthTokenStorage(),
  );
  await provider.authenticate(name, oauth, serverUrl);
  console.log(JSON.stringify({ ok: true, connector: name }));
} else {
  throw new Error("Usage: qwen_mcp_bridge.mjs list|tools|call|auth [connector] [json]");
}
