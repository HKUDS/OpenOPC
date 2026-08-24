from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opc.layer4_tools import browser as browser_tools


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str):
        self.page = page
        self.selector = selector
        self.first = self

    async def count(self) -> int:
        return 1 if self.selector in self.page.selectors else 0

    async def click(self, timeout: int = 0) -> None:
        self.page.clicked.append((self.selector, timeout))

    async def fill(self, text: str, timeout: int = 0) -> None:
        self.page.filled.append((self.selector, text, timeout))

    async def type(self, text: str, timeout: int = 0) -> None:
        self.page.typed.append((self.selector, text, timeout))

    async def press(self, key: str) -> None:
        self.page.pressed.append((self.selector, key))

    async def select_option(self, option, timeout: int = 0) -> None:
        self.page.selected.append((self.selector, option, timeout))

    async def evaluate(self, expression: str):
        self.page.locator_evaluations.append((self.selector, expression))
        return {"selector": self.selector, "expression": expression}


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.history = ["about:blank"]
        self.clicked: list[tuple[str, int]] = []
        self.filled: list[tuple[str, str, int]] = []
        self.typed: list[tuple[str, str, int]] = []
        self.pressed: list[tuple[str, str]] = []
        self.selected: list[tuple[str, object, int]] = []
        self.waited_for_selectors: list[tuple[str, str, int]] = []
        self.waited_for_states: list[tuple[str, int]] = []
        self.scrolls: list[str] = []
        self.page_evaluations: list[str] = []
        self.locator_evaluations: list[tuple[str, str]] = []
        self.selectors = {
            "input[name=\"q\"]",
            "button:has-text(\"Search\")",
            "select[name=\"market\"]",
        }

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 0) -> None:
        self.url = url
        self.history.append(url)

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.waited_for_states.append((state, timeout))
        return None

    async def wait_for_selector(self, selector: str, state: str = "visible", timeout: int = 0) -> None:
        if selector not in self.selectors:
            raise RuntimeError("missing selector")
        self.waited_for_selectors.append((selector, state, timeout))

    async def title(self) -> str:
        return "Fake Title"

    async def evaluate(self, script: str):
        self.page_evaluations.append(script)
        if "window.scrollTo" in script or "window.scrollBy" in script:
            self.scrolls.append(script)
            return None
        if "document.querySelectorAll" in script:
            return {
                "title": "Fake Title",
                "url": self.url,
                "text": "Main page text " * 50,
                "headings": ["Heading 1", "Heading 2"],
                "interactive": [
                    {"tag": "input", "text": "", "type": "text", "role": "", "placeholder": "Search", "selector": 'input[name="q"]'},
                    {"tag": "button", "text": "Search", "type": "", "role": "", "placeholder": "", "selector": 'button:has-text("Search")'},
                ],
            }
        return {
            "expression": script,
            "url": self.url,
        }

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def go_back(self, wait_until: str = "domcontentloaded", timeout: int = 0):
        if len(self.history) < 2:
            return None
        self.history.pop()
        self.url = self.history[-1]
        return {"url": self.url}

    async def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).write_bytes(b"fake-image")

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, page: _FakePage, browser: "_FakeBrowser" | None = None, *, include_existing_page: bool = False):
        self._page = page
        self.browser = browser
        self.pages = [page] if include_existing_page else []

    async def new_page(self) -> _FakePage:
        if self._page not in self.pages:
            self.pages.append(self._page)
        return self._page

    async def close(self) -> None:
        return None


class _FakeBrowser:
    def __init__(self, page: _FakePage):
        self._page = page

    async def new_context(self, ignore_https_errors: bool = True) -> _FakeContext:
        return _FakeContext(self._page, browser=self)

    async def close(self) -> None:
        return None


class _FakePlaywrightInstance:
    def __init__(self, page: _FakePage, *, fail_local_chrome: bool = False):
        self.chromium = _FakeChromium(page, fail_local_chrome=fail_local_chrome)

    async def stop(self) -> None:
        return None


class _FakeChromium:
    def __init__(self, page: _FakePage, *, fail_local_chrome: bool = False):
        self._page = page
        self._fail_local_chrome = fail_local_chrome
        self.launches: list[dict[str, object]] = []

    async def launch(self, headless: bool = True, args: list[str] | None = None, **kwargs) -> _FakeBrowser:
        launch_kwargs: dict[str, object] = {"headless": headless, "args": list(args or [])}
        launch_kwargs.update(kwargs)
        self.launches.append(launch_kwargs)
        if self._fail_local_chrome and (launch_kwargs.get("channel") == "chrome" or launch_kwargs.get("executable_path")):
            raise RuntimeError("local chrome unavailable")
        return _FakeBrowser(self._page)

    async def launch_persistent_context(self, user_data_dir: str, headless: bool = True, args: list[str] | None = None, **kwargs) -> _FakeContext:
        launch_kwargs: dict[str, object] = {
            "persistent": True,
            "user_data_dir": user_data_dir,
            "headless": headless,
            "args": list(args or []),
        }
        launch_kwargs.update(kwargs)
        self.launches.append(launch_kwargs)
        if self._fail_local_chrome and (launch_kwargs.get("channel") == "chrome" or launch_kwargs.get("executable_path")):
            raise RuntimeError("local chrome unavailable")
        browser = _FakeBrowser(self._page)
        return _FakeContext(self._page, browser=browser, include_existing_page=True)


class _FakePlaywrightFactory:
    def __init__(self) -> None:
        self.starts = 0
        self.pages: list[_FakePage] = []
        self.instances: list[_FakePlaywrightInstance] = []
        self.fail_local_chrome = False

    def __call__(self) -> "_FakePlaywrightFactory":
        return self

    async def start(self) -> _FakePlaywrightInstance:
        self.starts += 1
        page = _FakePage()
        self.pages.append(page)
        instance = _FakePlaywrightInstance(page, fail_local_chrome=self.fail_local_chrome)
        self.instances.append(instance)
        return instance


class BrowserToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Browser behavior tests use an explicit trusted default instead of
        # consulting the developer checkout's project-local configuration.
        patcher = patch.object(
            browser_tools.BrowserLaunchConfig,
            "load",
            return_value=browser_tools.BrowserLaunchConfig(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_browser_navigate_returns_snapshot_payload(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            result = await runtime.navigate("https://example.com")

        self.assertEqual(result["title"], "Fake Title")
        self.assertEqual(result["url"], "https://example.com")
        self.assertIn("Main page text", result["text"])
        self.assertGreaterEqual(len(result["interactive_elements"]), 1)

    async def test_browser_snapshot_can_save_markdown_file(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "snapshot.md"
                result = await runtime.snapshot(filename=str(path), max_chars=200)
                self.assertEqual(result["saved_to"], str(path))
                self.assertTrue(path.exists())
                self.assertIn("Fake Title", path.read_text(encoding="utf-8"))

    async def test_browser_take_screenshot_writes_file(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "page.png"
                result = await runtime.take_screenshot(filename=str(path))
                self.assertEqual(result["saved_to"], str(path))
                self.assertTrue(path.exists())
                self.assertEqual(path.read_bytes(), b"fake-image")

    async def test_browser_close_allows_runtime_rebuild(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            await runtime.close()
            await runtime.navigate("https://example.org")

        self.assertEqual(factory.starts, 2)

    async def test_browser_navigate_can_launch_local_chrome_from_config(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime(
            config_loader=lambda: browser_tools.BrowserLaunchConfig(
                mode="chrome",
                headless=False,
                chrome_channel="chrome",
                chrome_executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
                args=("--start-maximized",),
            )
        )
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")

        launch_kwargs = factory.instances[-1].chromium.launches[-1]
        self.assertFalse(bool(launch_kwargs["headless"]))
        self.assertEqual(
            launch_kwargs["executable_path"],
            str(Path("C:/Program Files/Google/Chrome/Application/chrome.exe").expanduser()),
        )
        self.assertEqual(launch_kwargs["args"], ["--start-maximized"])

    async def test_browser_auto_mode_falls_back_to_embedded_browser(self) -> None:
        factory = _FakePlaywrightFactory()
        factory.fail_local_chrome = True
        runtime = browser_tools.BrowserRuntime(
            config_loader=lambda: browser_tools.BrowserLaunchConfig(
                mode="auto",
                headless=False,
                chrome_channel="chrome",
            )
        )
        with patch.object(browser_tools, "async_playwright", factory):
            result = await runtime.navigate("https://example.com")

        launches = factory.instances[-1].chromium.launches
        self.assertEqual(result["url"], "https://example.com")
        self.assertEqual(launches[0]["channel"], "chrome")
        self.assertIn("--disable-dev-shm-usage", launches[1]["args"])
        self.assertIn("--no-sandbox", launches[1]["args"])

    async def test_browser_navigate_can_reuse_persistent_profile(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime(
            config_loader=lambda: browser_tools.BrowserLaunchConfig(
                mode="chrome",
                headless=False,
                chrome_channel="chrome",
                user_data_dir=".opc/browser-profile",
            )
        )
        with patch.object(browser_tools, "async_playwright", factory), patch.object(browser_tools, "get_opc_home", return_value=Path("D:/Project/work_HKU/OpenOPC/.opc")):
            await runtime.navigate("https://example.com")

        launch_kwargs = factory.instances[-1].chromium.launches[-1]
        self.assertTrue(bool(launch_kwargs["persistent"]))
        self.assertEqual(
            launch_kwargs["user_data_dir"],
            str(Path("D:/Project/work_HKU/OpenOPC/.opc/browser-profile")),
        )
        self.assertEqual(launch_kwargs["channel"], "chrome")

    async def test_browser_type_and_click_use_selector(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            await runtime.type('input[name="q"]', "hello", press_enter=True)
            await runtime.click('button:has-text("Search")')

        page = factory.pages[-1]
        self.assertEqual(page.filled[0][0], 'input[name="q"]')
        self.assertEqual(page.filled[0][1], "hello")
        self.assertEqual(page.pressed[0], ('input[name="q"]', "Enter"))
        self.assertEqual(page.clicked[0][0], 'button:has-text("Search")')

    async def test_browser_wait_for_selector_and_load_state(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            await runtime.wait_for(selector='input[name="q"]', timeout_seconds=2.5)
            await runtime.wait_for(timeout_seconds=1.0, state="networkidle")

        page = factory.pages[-1]
        self.assertEqual(page.waited_for_selectors[0], ('input[name="q"]', "visible", 2500))
        self.assertEqual(page.waited_for_states[-1], ("networkidle", 1000))

    async def test_browser_scroll_select_back_and_evaluate(self) -> None:
        factory = _FakePlaywrightFactory()
        runtime = browser_tools.BrowserRuntime()
        with patch.object(browser_tools, "async_playwright", factory):
            await runtime.navigate("https://example.com")
            await runtime.scroll(amount=600)
            await runtime.scroll(direction="up", amount=200)
            await runtime.navigate("https://example.org")
            back_result = await runtime.navigate_back()
            await runtime.select_option('select[name="market"]', label="NASDAQ")
            eval_page = await runtime.evaluate("() => document.title")
            eval_locator = await runtime.evaluate("(el) => el.tagName", selector='input[name="q"]')

        page = factory.pages[-1]
        self.assertIn("window.scrollBy(0, 600)", page.scrolls[0])
        self.assertIn("window.scrollBy(0, -200)", page.scrolls[1])
        self.assertEqual(back_result["url"], "https://example.com")
        self.assertEqual(page.selected[0][0], 'select[name="market"]')
        self.assertEqual(page.selected[0][1], {"label": "NASDAQ"})
        self.assertEqual(eval_page["result"]["expression"], "() => document.title")
        self.assertEqual(eval_locator["result"]["selector"], 'input[name="q"]')

    def test_create_browser_tools_exposes_expected_names(self) -> None:
        tools = browser_tools.create_browser_tools()
        self.assertEqual(
            {tool.name for tool in tools},
            {
                "browser_navigate",
                "browser_navigate_back",
                "browser_snapshot",
                "browser_click",
                "browser_type",
                "browser_wait_for",
                "browser_scroll",
                "browser_select_option",
                "browser_take_screenshot",
                "browser_close",
            },
        )


if __name__ == "__main__":
    unittest.main()
