from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import litellm
from opc.core.config import LLMConfig, OPCConfig
from opc.llm.provider import LLMProvider, _is_local_endpoint, _normalize_litellm_model


class TestLocalLLMProvider(unittest.IsolatedAsyncioTestCase):
    def test_is_local_endpoint_helper(self) -> None:
        """Test local endpoint detection across model names and host URLs."""
        # Local model prefixes
        self.assertTrue(_is_local_endpoint(model="ollama/llama3.3"))
        self.assertTrue(_is_local_endpoint(model="ollama_chat/qwen2.5-coder"))
        self.assertTrue(_is_local_endpoint(model="vllm/meta-llama-3.1-8b"))
        self.assertTrue(_is_local_endpoint(model="localai/starcoder"))
        self.assertTrue(_is_local_endpoint(model="lmstudio/deepseek-r1"))
        self.assertTrue(_is_local_endpoint(model="llama-cpp/mistral-7b"))

        # Remote model names
        self.assertFalse(_is_local_endpoint(model="openai/gpt-4o"))
        self.assertFalse(_is_local_endpoint(model="anthropic/claude-sonnet-4"))

        # Local API bases
        self.assertTrue(_is_local_endpoint(api_base="http://localhost:11434"))
        self.assertTrue(_is_local_endpoint(api_base="http://127.0.0.1:8000/v1"))
        self.assertTrue(_is_local_endpoint(api_base="http://0.0.0.0:8080/v1"))
        self.assertTrue(_is_local_endpoint(api_base="http://my-gpu-node.local:11434"))

        # Remote API bases
        self.assertFalse(_is_local_endpoint(api_base="https://api.openai.com/v1"))
        self.assertFalse(_is_local_endpoint(api_base="https://openrouter.ai/api/v1"))

    def test_litellm_provider_resolution_for_advertised_models(self) -> None:
        """Verify LiteLLM==1.82.1 real provider resolution (no network calls) for all supported formats."""
        test_models = [
            ("ollama/llama3.3", "ollama"),
            ("ollama_chat/qwen2.5-coder", "ollama_chat"),
            ("vllm/meta-llama-3.1-8b-instruct", "vllm"),
            ("openai/deepseek-r1-distill-qwen-14b", "openai"),
            ("openai/starcoder2-15b", "openai"),
            ("openai/gpt-4o", "openai"),
            ("anthropic/claude-sonnet-4-20250514", "anthropic"),
            ("openrouter/auto", "openrouter"),
        ]

        for raw_model, expected_provider in test_models:
            normalized = _normalize_litellm_model(raw_model, api_base="http://localhost:1234/v1")
            _, resolved_provider, _, _ = litellm.get_llm_provider(normalized)
            self.assertEqual(
                resolved_provider,
                expected_provider,
                f"LiteLLM failed to resolve provider for model: {raw_model} (normalized: {normalized})",
            )

    def test_local_env_var_does_not_misroute_cloud_models(self) -> None:
        """OLLAMA_HOST or LOCAL_LLM_API_BASE in os.environ must NOT misroute cloud model requests."""
        env = {
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "LOCAL_LLM_API_BASE": "http://127.0.0.1:8000",
        }
        with patch.dict(os.environ, env, clear=True):
            provider = LLMProvider(LLMConfig(default_model="anthropic/claude-sonnet-4-20250514", api_key=""))
            # Cloud model should NOT inherit local api_base
            self.assertIsNone(provider.resolve_api_base("anthropic/claude-sonnet-4-20250514"))
            self.assertFalse(provider.has_credentials("anthropic/claude-sonnet-4-20250514"))

    def test_routed_models_resolution_both_directions(self) -> None:
        """Test per-model API base resolution for mixed routing configurations."""
        # Case A: Local default + Cloud routed model
        config_a = LLMConfig(
            default_model="ollama/llama3.3",
            routing={"planning": "anthropic/claude-sonnet-4-20250514"},
            api_key="",
        )
        provider_a = LLMProvider(config_a)
        self.assertEqual(provider_a.resolve_api_base(config_a.default_model), "http://localhost:11434")
        self.assertIsNone(provider_a.resolve_api_base("anthropic/claude-sonnet-4-20250514"))

        # Case B: Cloud default + Local routed model
        config_b = LLMConfig(
            default_model="openai/gpt-4o",
            routing={"code": "ollama/qwen2.5-coder"},
            api_key="sk-test-key",
        )
        provider_b = LLMProvider(config_b)
        self.assertIsNone(provider_b.resolve_api_base(config_b.default_model))
        self.assertEqual(provider_b.resolve_api_base("ollama/qwen2.5-coder"), "http://localhost:11434")

    def test_has_credentials_for_ollama(self) -> None:
        """Ollama model without API key evaluates to has_credentials() == True."""
        provider = LLMProvider(LLMConfig(default_model="ollama/llama3.3", api_key=""))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider.has_credentials())
            self.assertEqual(provider.resolve_api_base(), "http://localhost:11434")

    def test_has_credentials_for_vllm_local_base(self) -> None:
        """vLLM with local api_base evaluates to has_credentials() == True."""
        provider = LLMProvider(LLMConfig(
            default_model="vllm/llama3",
            api_base="http://127.0.0.1:8000/v1",
            api_key="",
        ))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider.has_credentials())

    def test_has_credentials_for_explicit_is_local(self) -> None:
        """Explicit is_local flag in LLMConfig evaluates to has_credentials() == True."""
        provider = LLMProvider(LLMConfig(
            default_model="openai/custom-model",
            is_local=True,
            api_key="",
        ))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider.has_credentials())

    def test_capabilities_reporting_for_local_model(self) -> None:
        """Test model capabilities reporting for local endpoints."""
        provider = LLMProvider(LLMConfig(
            default_model="ollama/qwen2.5-coder",
            api_base="http://localhost:11434",
        ))
        capabilities = provider.get_capabilities()
        self.assertEqual(capabilities.model, "ollama/qwen2.5-coder")
        self.assertEqual(capabilities.provider_family, "ollama")
        self.assertTrue(capabilities.metadata.get("is_local"))

    @patch("opc.llm.provider.litellm.acompletion")
    async def test_chat_injects_dummy_api_key_for_local_openai_endpoint(
        self, mock_acompletion: AsyncMock
    ) -> None:
        """Chat injects 'local' API key when targeting keyless local endpoint."""
        mock_response = AsyncMock()
        mock_response.choices = [
            AsyncMock(
                message=AsyncMock(content="Local response", tool_calls=None),
                finish_reason="stop",
            )
        ]
        mock_response.usage = None
        mock_acompletion.return_value = mock_response

        provider = LLMProvider(LLMConfig(
            default_model="vllm/qwen",
            api_base="http://localhost:8000/v1",
            api_key="",
        ))

        res = await provider.chat(messages=[{"role": "user", "content": "Hello"}])
        self.assertEqual(res["content"], "Local response")

        # Verify call_kwargs received api_key="local"
        kwargs = mock_acompletion.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "local")
        self.assertEqual(kwargs["api_base"], "http://localhost:8000/v1")

    def test_backend_config_persistence_and_provider_reinitialization(self) -> None:
        """Test saving LLM config updates .opc/config/llm_config.yaml and reinitializes LLMProvider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            opc_home = Path(tmpdir)
            config = OPCConfig.load(opc_home)

            # Initial state: default model configured
            self.assertTrue(bool(config.llm.default_model))

            # Update to local Ollama model
            config.llm.default_model = "ollama/llama3.3"
            config.llm.api_base = "http://localhost:11434"
            config.llm.provider = "ollama"
            config.llm.is_local = True
            config.save(opc_home)

            # Reload and reinitialize LLMProvider
            reloaded_config = OPCConfig.load(opc_home)
            self.assertEqual(reloaded_config.llm.default_model, "ollama/llama3.3")
            self.assertTrue(reloaded_config.llm.is_local)

            provider = LLMProvider(reloaded_config.llm, opc_home=opc_home)
            self.assertEqual(provider.resolve_api_base("ollama/llama3.3"), "http://localhost:11434")
            self.assertTrue(provider.has_credentials())


if __name__ == "__main__":
    unittest.main()
