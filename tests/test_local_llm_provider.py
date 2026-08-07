from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from opc.core.config import LLMConfig
from opc.llm.provider import LLMProvider, _is_local_endpoint


class TestLocalLLMProvider(unittest.IsolatedAsyncioTestCase):
    def test_is_local_endpoint_helper(self) -> None:
        """Test local endpoint detection across model names, URLs, and env vars."""
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

        # Environment variables
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://192.168.1.100:11434"}, clear=True):
            self.assertTrue(_is_local_endpoint())

    def test_has_credentials_for_ollama(self) -> None:
        """Ollama model without API key evaluates to has_credentials() == True."""
        provider = LLMProvider(LLMConfig(default_model="ollama/llama3.3", api_key=""))
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(provider.has_credentials())
            self.assertEqual(provider._api_base, "http://localhost:11434")

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
            default_model="custom-model",
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


if __name__ == "__main__":
    unittest.main()
