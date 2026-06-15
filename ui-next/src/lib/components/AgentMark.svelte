<script lang="ts">
  import claudeCodeIcon from "@lobehub/icons-static-svg/icons/claudecode-color.svg?url";
  import codexIcon from "@lobehub/icons-static-svg/icons/codex-color.svg?url";
  import geminiCliIcon from "@lobehub/icons-static-svg/icons/gemini-color.svg?url";
  import ollamaIcon from "@lobehub/icons-static-svg/icons/ollama.svg?url";
  import openAiIcon from "@lobehub/icons-static-svg/icons/openai.svg?url";
  import qwenIcon from "@lobehub/icons-static-svg/icons/qwen-color.svg?url";
  import type { AgentId } from "$lib/types";

  let {
    agent,
    agentKey = "",
    model = "",
    size = 26,
  }: {
    agent: AgentId;
    agentKey?: string;
    model?: string;
    size?: number;
  } = $props();

  const identity = $derived(`${agent} ${agentKey} ${model}`.toLowerCase());
  const mark = $derived.by(() => {
    if (identity.includes("qwen")) {
      return { src: qwenIcon, label: "Qwen", kind: "qwen" };
    }
    if (identity.includes("ollama")) {
      return { src: ollamaIcon, label: "Ollama", kind: "ollama" };
    }
    if (agent === "claude" || identity.includes("claude")) {
      return { src: claudeCodeIcon, label: "Claude Code", kind: "claude" };
    }
    if (agent === "codex" || identity.includes("codex")) {
      return { src: codexIcon, label: "Codex", kind: "codex" };
    }
    if (agent === "gemini" || identity.includes("gemini")) {
      return { src: geminiCliIcon, label: "Gemini CLI", kind: "gemini" };
    }
    if (identity.includes("gpt") || identity.includes("openai")) {
      return { src: openAiIcon, label: "OpenAI", kind: "openai" };
    }
    return { src: ollamaIcon, label: model || agentKey || "Local model", kind: "local" };
  });
</script>

<span
  class="agent-mark {mark.kind}"
  style:width={`${size}px`}
  style:height={`${size}px`}
  aria-label={mark.label}
  title={mark.label}
>
  <img src={mark.src} alt="" draggable="false" />
</span>
