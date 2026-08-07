# Running OpenOPC with Local LLM Models

OpenOPC natively supports self-hosted local LLMs (Ollama, vLLM, LM Studio, LocalAI, Llama.cpp, TGI) without requiring remote API keys.

---

## 🚀 Supported Local Providers & Formats

| Local Provider | Default Port | Model Prefix Format | Example Model Name |
|---|---|---|---|
| **Ollama** | `http://localhost:11434` | `ollama/<model>` | `ollama/llama3.3`, `ollama/qwen2.5-coder` |
| **vLLM** | `http://localhost:8000/v1` | `vllm/<model>` or `openai/<model>` | `vllm/meta-llama-3.1-8b-instruct` |
| **LM Studio** | `http://localhost:1234/v1` | `lmstudio/<model>` or `openai/<model>` | `lmstudio/deepseek-r1-distill-qwen-14b` |
| **LocalAI** | `http://localhost:8080/v1` | `localai/<model>` | `localai/starcoder2-15b` |
| **Llama.cpp** | `http://localhost:8080/v1` | `llama-cpp/<model>` | `llama-cpp/mistral-7b-instruct` |

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
   ```

3. **Launch OpenOPC Session**:
   ```bash
   opc chat "Build a REST API in Python"
   ```

---

## 🖥️ Running with vLLM / LM Studio / LocalAI

OpenAI-compatible local servers (like vLLM or LM Studio) can be used by setting `api_base`:

```yaml
llm:
  default_model: "vllm/meta-llama-3.1-8b-instruct"
  api_base: "http://localhost:8000/v1"
```

Or via environment variables:

```bash
export OLLAMA_HOST="http://localhost:11434"
# or
export LOCAL_LLM_API_BASE="http://localhost:8000/v1"
```

---

## 🔧 Explicit `is_local` Flag

If your local server uses a non-standard port or hostname, mark `is_local: true` in `llm_config.yaml`:

```yaml
llm:
  default_model: "my-custom-local-model"
  api_base: "http://192.168.1.150:9000/v1"
  is_local: true
```

This instructs OpenOPC's LLM provider layer to skip remote credential validation and execute native AI agents using your local workstation.
