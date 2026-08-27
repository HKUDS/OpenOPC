from __future__ import annotations

import unittest
from pathlib import Path
import uuid
from unittest.mock import patch

from opc.core.models import Task
from opc.layer4_tools.file_ops import apply_patch, file_read, glob, grep
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry
from opc.layer4_tools.web_search import web_fetch, web_search


class NativeToolStackTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_search_preserves_nested_snippet_and_decodes_redirect(self) -> None:
        class FakeResponse:
            text = """
                <div class="result">
                  <a rel="nofollow" class="result__a"
                     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.test%2Freport%3Fa%3D1%26b%3D2&amp;rut=abc">
                    <b>NVIDIA</b> &amp; AMD results
                  </a>
                  <a class="result__snippet" href="//duckduckgo.com/l/">
                    Revenue <b>grew</b> 262% &amp; the outlook <span>remains strong</span>.
                  </a>
                </div>
            """

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, *args, **kwargs) -> FakeResponse:
                return FakeResponse()

        with patch("opc.layer4_tools.web_search.httpx.AsyncClient", FakeClient):
            result = await web_search("semiconductor results")

        self.assertEqual(result["query"], "semiconductor results")
        self.assertEqual(
            result["results"],
            [
                {
                    "title": "NVIDIA & AMD results",
                    "url": "https://example.test/report?a=1&b=2",
                    "snippet": "Revenue grew 262% & the outlook remains strong.",
                }
            ],
        )

    async def test_web_search_retains_safe_redirect_when_uddg_target_is_invalid(self) -> None:
        class FakeResponse:
            text = """
                <a href="//duckduckgo.com/l/?uddg=not-a-http-url&amp;rut=xyz" class="result__a">
                  Example&#39;s result
                </a>
                <div class="result__snippet">First <b>highlight</b>, then the rest.</div>
            """

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, *args, **kwargs) -> FakeResponse:
                return FakeResponse()

        with patch("opc.layer4_tools.web_search.httpx.AsyncClient", FakeClient):
            result = await web_search("fallback")

        self.assertEqual(result["results"][0]["title"], "Example's result")
        self.assertEqual(result["results"][0]["snippet"], "First highlight, then the rest.")
        self.assertEqual(
            result["results"][0]["url"],
            "https://duckduckgo.com/l/?uddg=not-a-http-url&rut=xyz",
        )

    async def test_glob_and_grep_return_matches(self) -> None:
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('hello')\nVALUE = 42\n", encoding="utf-8")
            (root / "src" / "util.py").write_text("VALUE = 41\n", encoding="utf-8")

            glob_result = await glob("*.py", path=str(root / "src"))
            grep_result = await grep("VALUE", path=str(root / "src"), file_glob="*.py")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(glob_result["success"])
        self.assertEqual(sorted(glob_result["entries"]), ["app.py", "util.py"])
        self.assertTrue(grep_result["success"])
        self.assertEqual(grep_result["count"], 2)

    async def test_apply_patch_updates_file_and_returns_diff_preview(self) -> None:
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            target = root / "demo.txt"
            target.write_text("alpha\nbeta\n", encoding="utf-8")

            result = await apply_patch(
                "\n".join(
                    [
                        "*** Begin Patch",
                        f"*** Update File: {target}",
                        "@@",
                        " alpha",
                        "-beta",
                        "+gamma",
                        "*** End Patch",
                    ]
                )
            )

            updated = target.read_text(encoding="utf-8")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(result["success"])
        self.assertEqual(updated, "alpha\ngamma\n")
        self.assertIn("gamma", result["changed_files"][0]["diff_preview"])

    async def test_grep_truncates_oversized_single_line_matches(self) -> None:
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "src").mkdir()
            long_line = "prefix " + ("A" * 2000) + " suffix\n"
            (root / "src" / "big.txt").write_text(long_line, encoding="utf-8")

            grep_result = await grep("prefix", path=str(root / "src"), file_glob="*.txt")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(grep_result["success"])
        self.assertEqual(grep_result["count"], 1)
        self.assertIn("truncated", grep_result["matches"][0])
        self.assertLess(len(grep_result["matches"][0]), 700)

    async def test_file_read_returns_recoverable_pagination_metadata(self) -> None:
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            target = root / "large.txt"
            target.write_text("".join(f"line-{i:04d} " + ("x" * 140) + "\n" for i in range(180)), encoding="utf-8")

            first = await file_read(str(target))
            self.assertTrue(first["success"])
            self.assertTrue(first["truncated"])
            self.assertIsNotNone(first["next_offset"])

            second = await file_read(str(target), offset=int(first["next_offset"]), limit=5)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(second["success"])
        self.assertFalse(second["truncated"])
        self.assertGreater(second["returned_start_line"], first["returned_start_line"])
        self.assertIn("line-", second["content"])

    async def test_grep_supports_head_limit_and_offset(self) -> None:
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            (root / "src").mkdir()
            for idx in range(4):
                (root / "src" / f"f{idx}.txt").write_text(f"VALUE {idx}\n", encoding="utf-8")

            first = await grep("VALUE", path=str(root / "src"), file_glob="*.txt", head_limit=1)
            second = await grep("VALUE", path=str(root / "src"), file_glob="*.txt", head_limit=1, offset=1)
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(first["success"])
        self.assertEqual(first["count"], 1)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], 1)
        self.assertTrue(second["success"])
        self.assertEqual(second["applied_offset"], 1)
        self.assertEqual(second["count"], 1)

    async def test_registry_persists_large_non_self_bounded_result(self) -> None:
        registry = ToolRegistry()

        async def large_tool() -> dict[str, str]:
            return {"content": "A" * 50000}

        registry.register(
            ToolDefinition(
                name="large_tool",
                description="Large output",
                parameters={"type": "object", "properties": {}},
                func=large_tool,
            )
        )
        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            task = Task(title="large", description="large", session_id="sess-large", metadata={"workspace_root": str(root)})
            result = await registry.execute("large_tool", {}, task=task, skip_approval=True)
            full_path = Path(result["result"]["full_output_path"])
            persisted = full_path.read_text(encoding="utf-8")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(result["truncated"])
        self.assertTrue(full_path.exists() or "A" * 100 in persisted)
        self.assertIn("A" * 100, persisted)

    async def test_registry_keeps_self_bounded_result_recovery_metadata_inline(self) -> None:
        registry = ToolRegistry()

        async def self_bounded_tool() -> dict[str, object]:
            return {
                "content": "B" * 50000,
                "truncated": True,
                "next_offset": 123,
                "omitted_chars": 10,
            }

        registry.register(
            ToolDefinition(
                name="self_bounded_tool",
                description="Self bounded output",
                parameters={"type": "object", "properties": {}},
                func=self_bounded_tool,
                self_bounded_output=True,
                max_result_chars=1000,
            )
        )

        result = await registry.execute("self_bounded_tool", {}, skip_approval=True)

        self.assertTrue(result["truncated"])
        self.assertTrue(result["result"]["truncated"])
        self.assertEqual(result["result"]["next_offset"], 123)
        self.assertNotIn("full_output_path", result["result"])

    async def test_web_fetch_returns_marked_preview_and_full_path(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://example.test/page"
            headers = {"content-type": "text/html"}
            text = "<html><body><h1>Hello</h1><script>ignore()</script><p>" + ("alpha " * 200) + "</p></body></html>"

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, *args, **kwargs) -> FakeResponse:
                return FakeResponse()

        root = Path.cwd() / ".tmp-test" / f"native-tool-stack-{uuid.uuid4().hex}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            task = Task(title="web", description="web", session_id="sess-web", metadata={"workspace_root": str(root)})
            with patch("opc.layer4_tools.web_search.httpx.AsyncClient", FakeClient):
                result = await web_fetch("https://example.test/page", max_length=80, task=task)
            full_path = Path(result["full_content_path"])
            persisted = full_path.read_text(encoding="utf-8")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertIn("[web_fetch truncated:", result["content"])
        self.assertNotIn("ignore()", result["content"])
        self.assertIn("alpha", persisted)

    async def test_web_fetch_small_response_is_not_truncated(self) -> None:
        class FakeResponse:
            status_code = 200
            url = "https://example.test/small"
            headers = {"content-type": "text/plain"}
            text = "short response"

            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self) -> "FakeClient":
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def get(self, *args, **kwargs) -> FakeResponse:
                return FakeResponse()

        with patch("opc.layer4_tools.web_search.httpx.AsyncClient", FakeClient):
            result = await web_fetch("https://example.test/small", max_length=80)

        self.assertTrue(result["success"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["content"], "short response")
        self.assertEqual(result["full_content_path"], "")


if __name__ == "__main__":
    unittest.main()
