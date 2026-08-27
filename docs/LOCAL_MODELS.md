# Running OpenOPC with Local LLM Models

OpenOPC natively supports self-hosted local LLMs (Ollama, vLLM, LM Studio, LocalAI, Llama.cpp, TGI) without requiring remote API keys.

---

## 🚀 Supported Local Providers & Formats

| Local Provider | Default Port | Model Prefix Format | Example Model Identifier |
|---|---|---|---|
| **Ollama** | `http://localhost:11434` | `ollama/<model>` | `ollama/llama3.3`, `ollama/qwen2.5-coder` |
| **vLLM** | `http://localhost:8000/v1` | `vllm/<model>` or `openai/<model>` | `vllm/meta-llama-3.1-8b-instruct` |
| **LM Studio** | `http://localhost:1234/v1` | `openai/<model>` | `openai/deepseek-r1-distill-qwen-14b` |
| **LocalAI / Llama.cpp** | `http://localhost:8080/v1` | `openai/<model>` | `openai/starcoder2-15b` |
| **Custom Local OpenAI Server** | `http://<host>:<port>/v1` | `openai/<model>` | `openai/custom-model` |

> ⚠️ **Important Requirement**: Native OpenOPC agents require a local model that supports **tool calling / function calling** (such as `ollama/llama3.3` or `ollama/qwen2.5-coder`) to execute multi-step work items cleanly.

---

## ⚡ Quick Start: Running with Ollama

1. **Start Ollama** locally and pull your model:
   ```bash
   ollama run llama3.3
   ```

2. **Configure OpenOPC `llm_config.yaml`**:
   Edit `~/.opc/config/llm_config.yaml`:
   ```yaml
   llm:
     default_model: "ollama/llama3.3"
     api_base: "http://localhost:11434"
     is_local: true
   ```

3. **Launch OpenOPC Session**:
   ```bash
   opc chat "Build a REST API in Python"
   ```

---

## 🖥️ Running with LM Studio / LocalAI / OpenAI-Compatible Servers

For OpenAI-compatible local servers (like LM Studio or LocalAI), use `openai/<model>` together with `api_base`:

```yaml
llm:
  default_model: "openai/deepseek-r1-distill-qwen-14b"
  api_base: "http://localhost:1234/v1"
  is_local: true
```

---

## 🌐 Office UI Settings Modal Integration

Settings can be changed dynamically from the browser using the **LLM & Local Model Settings Modal (`🤖` button)** in the top header of Office UI:

1. Click the **`🤖` button** in the header.
2. Select your provider (Ollama, vLLM, LM Studio, LocalAI, OpenAI, Anthropic).
3. Enter model identifier and API base URL.
4. Click **Save & Apply Settings**. Changes are saved atomically to `.opc/config/llm_config.yaml` and reinitialize the runtime LLMProvider instantly.
