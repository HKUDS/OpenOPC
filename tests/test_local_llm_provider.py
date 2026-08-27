from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import yaml

from opc.core.config import LLMConfig, OPCConfig
from opc.llm.provider import LLMProvider, _is_local_endpoint, _normalize_litellm_model
from opc.plugins.office_ui.llm_config_service import (
    get_llm_config_service,
    update_llm_config_service,
)


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

    @patch("opc.llm.provider.litellm.acompletion")
    async def test_acompletion_kwargs_mixed_routing_isolation(
        self, mock_acompletion: AsyncMock
    ) -> None:
        """Verify LiteLLM acompletion kwargs isolate cloud routed calls from local default endpoint."""
        mock_response = AsyncMock()
        mock_response.choices = [
            AsyncMock(
                message=AsyncMock(content="Planning result", tool_calls=None),
                finish_reason="stop",
            )
        ]
        mock_response.usage = None
        mock_acompletion.return_value = mock_response

        # Config: Ollama default with local base + Anthropic routed planning model
        config = LLMConfig(
            default_model="ollama/llama3.3",
            api_base="http://localhost:11434",
            is_local=True,
            routing={"planning": "anthropic/claude-sonnet-4-20250514"},
            api_key="",
        )
        provider = LLMProvider(config)

        env = {"ANTHROPIC_API_KEY": "sk-ant-test-key-123"}
        with patch.dict(os.environ, env):
            await provider.chat(task_type="planning", messages=[{"role": "user", "content": "Plan project"}])

        kwargs = mock_acompletion.call_args.kwargs
        self.assertEqual(kwargs["model"], "anthropic/claude-sonnet-4-20250514")
        self.assertNotIn("api_base", kwargs)
        self.assertEqual(kwargs["api_key"], "sk-ant-test-key-123")

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

    def test_canonical_config_dir_file_persistence(self) -> None:
        """Test update_llm_config_service updates canonical opc_home/config/llm_config.yaml."""
        with tempfile.TemporaryDirectory() as tmpdir:
            opc_home = Path(tmpdir)
            config_dir = opc_home / "config"
            config_dir.mkdir(parents=True)

            # Create existing config
            initial_config = OPCConfig()
            initial_config.save(config_dir)

            payload = {
                "default_model": "ollama/llama3.3",
                "api_base": "http://localhost:11434",
                "provider": "ollama",
                "is_local": True,
                "context_window": 128000,
            }

            res = update_llm_config_service(opc_home, payload)
            self.assertTrue(res["ok"])

            # Verify ONLY opc_home/config/llm_config.yaml was updated
            llm_yaml = config_dir / "llm_config.yaml"
            self.assertTrue(llm_yaml.exists())

            with open(llm_yaml, encoding="utf-8") as f:
                data = yaml.safe_load(f)

            self.assertEqual(data["llm"]["default_model"], "ollama/llama3.3")
            self.assertEqual(data["llm"]["api_base"], "http://localhost:11434")
            self.assertTrue(data["llm"]["is_local"])

            # Verify root opc_home has no leaked yaml files
            self.assertFalse((opc_home / "llm_config.yaml").exists())

    def test_explicit_key_clearing_semantics(self) -> None:
        """Test API key clearing semantics: '' clears key, '***' preserves key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            opc_home = Path(tmpdir)
            config_dir = opc_home / "config"
            config_dir.mkdir(parents=True)

            # 1. Set initial key
            update_llm_config_service(opc_home, {
                "default_model": "openai/gpt-4o",
                "api_key": "sk-secret-key-123",
            })
            info1 = get_llm_config_service(opc_home)
            self.assertTrue(info1["has_api_key"])

            # 2. Update with '***' -> key remains preserved
            update_llm_config_service(opc_home, {
                "default_model": "openai/gpt-4o",
                "api_key": "***",
            })
            info2 = get_llm_config_service(opc_home)
            self.assertTrue(info2["has_api_key"])

            # 3. Update with '' -> key is explicitly cleared
            update_llm_config_service(opc_home, {
                "default_model": "ollama/llama3.3",
                "api_key": "",
            })
            info3 = get_llm_config_service(opc_home)
            self.assertFalse(info3["has_api_key"])

    def test_engine_reconfigure_llm_hot_apply(self) -> None:
        """Test OPCEngine.reconfigure_llm updates LLMProvider across all sub-components."""
        mock_engine = MagicMock()
        mock_engine.opc_home = Path("/tmp/fake_opc_home")
        mock_engine.config = OPCConfig()

        mock_history = MagicMock()
        mock_comms = MagicMock()
        mock_approval = MagicMock()
        mock_secretary = MagicMock()
        mock_executor = MagicMock()
        mock_router = MagicMock()

        mock_engine.history_compactor = mock_history
        mock_engine.communication = mock_comms
        mock_engine.approval_engine = mock_approval
        mock_engine.secretary = mock_secretary
        mock_engine.company_executor = mock_executor
        mock_engine.task_router = mock_router

        # Bind real reconfigure_llm method
        from opc.engine import OPCEngine
        mock_engine.reconfigure_llm = OPCEngine.reconfigure_llm.__get__(mock_engine, OPCEngine)

        new_cfg = LLMConfig(default_model="ollama/qwen2.5-coder", api_base="http://localhost:11434")
        new_provider = mock_engine.reconfigure_llm(new_cfg)

        self.assertEqual(mock_engine.llm, new_provider)
        self.assertEqual(mock_history.llm, new_provider)
        self.assertEqual(mock_comms.llm, new_provider)
        self.assertEqual(mock_approval.llm, new_provider)
        self.assertEqual(mock_secretary.llm, new_provider)
        self.assertEqual(mock_executor.llm, new_provider)
        self.assertEqual(mock_router.llm, new_provider)


if __name__ == "__main__":
    unittest.main()
