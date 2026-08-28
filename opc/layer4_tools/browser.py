"""Native Playwright-backed browser tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from opc.core.config import OPCConfig, get_opc_home
from opc.layer4_tools.output_budget import clip_text
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_FORBIDDEN,
    COMPANY_EFFECT_NO_LOCAL_FS,
    ToolDefinition,
)

try:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover - exercised via install-hint tests
    PlaywrightError = RuntimeError
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None


_INSTALL_HINT = (
    "Browser tools require the optional Playwright dependency. "
    "Install it with `pip install -e .[browser]` and then run "
    "`python -m playwright install chromium`."
)
_EMBEDDED_BROWSER_ARGS = ("--disable-dev-shm-usage", "--no-sandbox")


@dataclass(frozen=True)
class BrowserLaunchConfig:
    mode: str = "embedded"
    headless: bool = True
    chrome_channel: str = "chrome"
    chrome_executable_path: str = ""
    user_data_dir: str = ""
    args: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> "BrowserLaunchConfig":
        config_dir = get_opc_home() / "config"
        if not config_dir.is_dir():
            return cls()
        config = OPCConfig.load(config_dir)
        browser = getattr(config.system, "browser", None)
        if browser is None:
            return cls()
        return cls(
            mode=str(browser.mode or "embedded").strip().lower(),
            headless=bool(browser.headless),
            chrome_channel=str(browser.chrome_channel or "").strip(),
            chrome_executable_path=str(browser.chrome_executable_path or "").strip(),
            user_data_dir=str(browser.user_data_dir or "").strip(),
            args=tuple(str(arg).strip() for arg in (browser.args or []) if str(arg).strip()),
        )


class BrowserRuntime:
    """Single-browser runtime shared by native browser tools."""

    def __init__(self, config_loader: Callable[[], BrowserLaunchConfig] | None = None) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self._config_loader = config_loader or BrowserLaunchConfig.load
        self._launch_config: BrowserLaunchConfig | None = None

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        async with self._lock:
            page = await self._ensure_page()
            await page.goto(url, wait_until=wait_until, timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=6_000)

    async def snapshot(self, filename: str | None = None, max_chars: int = 12_000) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            snapshot = await self._build_snapshot(page, max_chars=max_chars)
            if filename:
                path = self._resolve_output_path(filename, suffix=".md")
                path.write_text(self._snapshot_to_markdown(snapshot), encoding="utf-8")
                snapshot["saved_to"] = str(path)
            return snapshot

    async def click(self, selector: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            await locator.click(timeout=10_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=4_000)

    async def type(
        self,
        selector: str,
        text: str,
        press_enter: bool = False,
        clear_existing: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            if clear_existing:
                await locator.fill(text, timeout=10_000)
            else:
                await locator.click(timeout=10_000)
                await locator.type(text, timeout=10_000)
            if press_enter:
                await locator.press("Enter")
            return await self._build_snapshot(page, max_chars=4_000)

    async def wait_for(
        self,
        selector: str | None = None,
        timeout_seconds: float = 10.0,
        state: str = "visible",
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            timeout_ms = max(100, int(timeout_seconds * 1000))
            if selector:
                await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
            else:
                await page.wait_for_load_state(state if state in {"load", "domcontentloaded", "networkidle"} else "networkidle", timeout=timeout_ms)
            return await self._build_snapshot(page, max_chars=4_000)

    async def scroll(
        self,
        amount: int = 800,
        direction: str = "down",
        to_bottom: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            if to_bottom:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            else:
                delta = abs(int(amount or 0))
                if direction.strip().lower() == "up":
                    delta = -delta
                await page.evaluate(f"window.scrollBy(0, {delta})")
            try:
                await page.wait_for_load_state("networkidle", timeout=3_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=4_000)

    async def select_option(
        self,
        selector: str,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            locator = await self._resolve_locator(page, selector)
            option: str | dict[str, Any]
            if index is not None:
                option = {"index": int(index)}
            elif label is not None:
                option = {"label": label}
            elif value is not None:
                option = value
            else:
                raise RuntimeError("Provide at least one of `value`, `label`, or `index`.")
            await locator.select_option(option, timeout=10_000)
            return await self._build_snapshot(page, max_chars=4_000)

    async def navigate_back(self) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            previous = await page.go_back(wait_until="domcontentloaded", timeout=15_000)
            if previous is None:
                raise RuntimeError("No previous page in browser history.")
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            return await self._build_snapshot(page, max_chars=6_000)

    async def evaluate(self, expression: str, selector: str | None = None) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            if selector:
                locator = await self._resolve_locator(page, selector)
                result = await locator.evaluate(expression)
            else:
                result = await page.evaluate(expression)
            return {
                "url": page.url,
                "title": await page.title(),
                "result": result,
            }

    async def take_screenshot(
        self,
        filename: str | None = None,
        full_page: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            page = await self._require_page()
            path = self._resolve_output_path(filename, suffix=".png")
            await page.screenshot(path=str(path), full_page=full_page)
            return {
                "saved_to": str(path),
                "url": page.url,
                "title": await page.title(),
            }

    async def close(self) -> dict[str, Any]:
        async with self._lock:
            await self._reset()
            return {"closed": True}

    async def _ensure_page(self) -> Any:
        self._ensure_dependency()
        launch_config = self._config_loader()
        if self._page is not None:
            if self._launch_config == launch_config:
                return self._page
            await self._reset()
        try:
            self._playwright = await async_playwright().start()
            self._browser, self._context, self._page = await self._launch_browser(launch_config)
            self._launch_config = launch_config
            return self._page
        except Exception as exc:
            await self._reset()
            raise RuntimeError(self._format_launch_error(exc, launch_config)) from exc

    async def _launch_browser(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        mode = (launch_config.mode or "embedded").strip().lower()
        if mode == "chrome":
            return await self._launch_local_chrome(launch_config)
        if mode == "auto":
            chrome_error: Exception | None = None
            try:
                return await self._launch_local_chrome(launch_config)
            except Exception as exc:
                chrome_error = exc
            try:
                return await self._launch_embedded_browser(launch_config)
            except Exception as embedded_exc:
                raise RuntimeError(
                    f"Auto mode could not launch local Chrome ({chrome_error}) "
                    f"or embedded Chromium ({embedded_exc})."
                ) from embedded_exc
        return await self._launch_embedded_browser(launch_config)

    async def _launch_embedded_browser(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        browser = await self._playwright.chromium.launch(
            headless=launch_config.headless,
            args=self._embedded_launch_args(launch_config),
        )
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        return browser, context, page

    async def _launch_local_chrome(self, launch_config: BrowserLaunchConfig) -> tuple[Any, Any, Any]:
        launch_kwargs: dict[str, Any] = {"headless": launch_config.headless}
        if launch_config.args:
            launch_kwargs["args"] = list(launch_config.args)
        executable_path = self._normalize_executable_path(launch_config.chrome_executable_path)
        user_data_dir = self._normalize_user_data_dir(launch_config.user_data_dir)
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        else:
            launch_kwargs["channel"] = launch_config.chrome_channel or "chrome"
        if user_data_dir:
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                ignore_https_errors=True,
                **launch_kwargs,
            )
            pages = list(getattr(context, "pages", []) or [])
            page = pages[0] if pages else await context.new_page()
            browser = getattr(context, "browser", None)
            return browser, context, page
        browser = await self._playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        return browser, context, page

    def _embedded_launch_args(self, launch_config: BrowserLaunchConfig) -> list[str]:
        args = list(_EMBEDDED_BROWSER_ARGS)
        for item in launch_config.args:
            if item not in args:
                args.append(item)
        return args

    def _normalize_executable_path(self, raw_path: str) -> str:
        raw = (raw_path or "").strip()
        if not raw:
            return ""
        return str(Path(raw).expanduser())

    def _normalize_user_data_dir(self, raw_path: str) -> str:
        raw = (raw_path or "").strip()
        if not raw:
            return ""
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = get_opc_home().parent / path
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _format_launch_error(self, exc: Exception, launch_config: BrowserLaunchConfig) -> str:
        if (launch_config.mode or "").strip().lower() == "chrome":
            return (
                f"Failed to start local Chrome browser: {exc}. "
                "Check `system.browser.chrome_executable_path`, `system.browser.user_data_dir`, "
                "or switch `system.browser.mode` back to `embedded`."
            )
        return f"Failed to start browser runtime: {exc}. {_INSTALL_HINT}"

    async def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError('No open browser page. Use `browser_navigate` first.')
        return self._page

    async def _resolve_locator(self, page: Any, selector: str) -> Any:
        raw = selector.strip()
        if not raw:
            raise RuntimeError("Selector must not be empty.")
        try:
            locator = page.locator(raw).first
            count = await locator.count()
        except PlaywrightError as exc:
            raise RuntimeError(f"Invalid selector `{raw}`: {exc}") from exc
        if count < 1:
            raise RuntimeError(f"No element matched selector `{raw}`.")
        return locator

    async def _build_snapshot(self, page: Any, max_chars: int) -> dict[str, Any]:
        payload = await page.evaluate(
            """
            () => {
              const normalize = (value) => (value || "").replace(/\\s+/g, " ").trim();
              const clip = (value, limit) => {
                const text = normalize(value);
                return text.length > limit ? text.slice(0, limit) + "..." : text;
              };
              const makeSelector = (el) => {
                const tag = (el.tagName || "div").toLowerCase();
                if (el.id) return `#${CSS.escape(el.id)}`;
                const name = el.getAttribute("name");
                if (name) return `${tag}[name="${name.replace(/"/g, '\\"')}"]`;
                const aria = el.getAttribute("aria-label");
                if (aria) return `${tag}[aria-label="${aria.replace(/"/g, '\\"')}"]`;
                const placeholder = el.getAttribute("placeholder");
                if (placeholder) return `${tag}[placeholder="${placeholder.replace(/"/g, '\\"')}"]`;
                const text = clip(el.innerText || el.textContent, 60);
                if (text && (tag === "button" || tag === "a")) {
                  return `${tag}:has-text("${text.replace(/"/g, '\\"')}")`;
                }
                return tag;
              };

              const interactive = Array.from(
                document.querySelectorAll('a, button, input, textarea, select, [role="button"]')
              )
                .filter((el) => {
                  const style = window.getComputedStyle(el);
                  return style && style.display !== "none" && style.visibility !== "hidden";
                })
                .slice(0, 40)
                .map((el) => ({
                  tag: (el.tagName || "").toLowerCase(),
                  text: clip(el.innerText || el.textContent, 120),
                  type: el.getAttribute("type") || "",
                  role: el.getAttribute("role") || "",
                  placeholder: el.getAttribute("placeholder") || "",
                  selector: makeSelector(el),
                }));

              const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
                .slice(0, 12)
                .map((el) => clip(el.innerText || el.textContent, 160))
                .filter(Boolean);

              return {
                title: document.title || "",
                url: location.href,
                text: clip(document.body ? document.body.innerText : "", 50000),
                headings,
                interactive,
              };
            }
            """
        )
        text = str(payload.get("text", "") or "")
        clip = clip_text(text, limit=max_chars, marker="browser snapshot text truncated") if max_chars > 0 else None
        if clip is not None:
            text = clip.text
        return {
            "title": str(payload.get("title", "") or ""),
            "url": str(payload.get("url", "") or page.url),
            "text": text,
            "text_truncated": bool(clip.truncated) if clip is not None else False,
            "text_omitted_chars": int(clip.omitted_chars) if clip is not None else 0,
            "headings": list(payload.get("headings", []) or []),
            "interactive_elements": list(payload.get("interactive", []) or []),
        }

    def _snapshot_to_markdown(self, snapshot: dict[str, Any]) -> str:
        parts = [
            f"# {snapshot.get('title') or 'Browser Snapshot'}",
            "",
            f"- URL: {snapshot.get('url', '')}",
            "",
            "## Page Text",
            "",
            str(snapshot.get("text", "") or ""),
        ]
        interactive = snapshot.get("interactive_elements", []) or []
        if interactive:
            parts.extend(["", "## Interactive Elements", ""])
            for item in interactive:
                parts.append(
                    f"- `{item.get('selector', '')}` "
                    f"[{item.get('tag', '')}] {item.get('text', '')}"
                )
        return "\n".join(parts).strip() + "\n"

    def _resolve_output_path(self, filename: str | None, *, suffix: str) -> Path:
        if filename:
            path = Path(filename)
            if not path.suffix:
                path = path.with_suffix(suffix)
            if not path.is_absolute():
                path = Path.cwd() / path
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = get_opc_home() / "artifacts" / "browser" / f"browser-{stamp}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _ensure_dependency(self) -> None:
        if async_playwright is None:
            raise RuntimeError(_INSTALL_HINT)

    async def _reset(self) -> None:
        page, context, browser, playwright = self._page, self._context, self._browser, self._playwright
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._launch_config = None
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                pass


_browser_runtime = BrowserRuntime()


async def browser_navigate(url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
    return await _browser_runtime.navigate(url=url, wait_until=wait_until)


async def browser_snapshot(filename: str | None = None, max_chars: int = 12_000) -> dict[str, Any]:
    return await _browser_runtime.snapshot(filename=filename, max_chars=max_chars)


async def browser_click(selector: str) -> dict[str, Any]:
    return await _browser_runtime.click(selector=selector)


async def browser_type(
    selector: str,
    text: str,
    press_enter: bool = False,
    clear_existing: bool = True,
) -> dict[str, Any]:
    return await _browser_runtime.type(
        selector=selector,
        text=text,
        press_enter=press_enter,
        clear_existing=clear_existing,
    )


async def browser_take_screenshot(
    filename: str | None = None,
    full_page: bool = True,
) -> dict[str, Any]:
    return await _browser_runtime.take_screenshot(filename=filename, full_page=full_page)


async def browser_wait_for(
    selector: str | None = None,
    timeout_seconds: float = 10.0,
    state: str = "visible",
) -> dict[str, Any]:
    return await _browser_runtime.wait_for(selector=selector, timeout_seconds=timeout_seconds, state=state)


async def browser_scroll(
    amount: int = 800,
    direction: str = "down",
    to_bottom: bool = False,
) -> dict[str, Any]:
    return await _browser_runtime.scroll(amount=amount, direction=direction, to_bottom=to_bottom)


async def browser_select_option(
    selector: str,
    value: str | None = None,
    label: str | None = None,
    index: int | None = None,
) -> dict[str, Any]:
    return await _browser_runtime.select_option(selector=selector, value=value, label=label, index=index)


async def browser_navigate_back() -> dict[str, Any]:
    return await _browser_runtime.navigate_back()


async def browser_close() -> dict[str, Any]:
    return await _browser_runtime.close()


def create_browser_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="browser_navigate",
            description="Open a page in the configured local browser and return a text snapshot.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to open"},
                    "wait_until": {
                        "type": "string",
                        "description": "Playwright wait condition",
                        "default": "domcontentloaded",
                    },
                },
                "required": ["url"],
            },
            func=browser_navigate,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_snapshot",
            description="Return the current page title, text, and interactive elements. Optionally save the snapshot to a markdown file.",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Optional markdown file path to save the snapshot",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum page text characters to return",
                        "default": 12000,
                    },
                },
            },
            func=browser_snapshot,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["network_access", "workspace_write"],
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element on the current page using a Playwright selector.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Playwright selector for the target element"},
                },
                "required": ["selector"],
            },
            func=browser_click,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_type",
            description="Type or fill text into a page element using a Playwright selector.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Playwright selector for the target element"},
                    "text": {"type": "string", "description": "Text to enter"},
                    "press_enter": {
                        "type": "boolean",
                        "description": "Press Enter after typing",
                        "default": False,
                    },
                    "clear_existing": {
                        "type": "boolean",
                        "description": "Replace existing content instead of appending",
                        "default": True,
                    },
                },
                "required": ["selector", "text"],
            },
            func=browser_type,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_take_screenshot",
            description="Save a screenshot of the current page.",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Optional screenshot path"},
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page",
                        "default": True,
                    },
                },
            },
            func=browser_take_screenshot,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            permission_effects=["network_access", "workspace_write"],
        ),
        ToolDefinition(
            name="browser_wait_for",
            description="Wait for a selector or page load state before continuing.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "Optional selector to wait for. If omitted, waits for page load state.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Maximum wait time in seconds",
                        "default": 10.0,
                    },
                    "state": {
                        "type": "string",
                        "description": "Selector state (attached/visible/hidden/detached) or load state (load/domcontentloaded/networkidle)",
                        "default": "visible",
                    },
                },
            },
            func=browser_wait_for,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_scroll",
            description="Scroll the current page up or down, or jump to the bottom.",
            parameters={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "Scroll distance in pixels",
                        "default": 800,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Scroll direction: down or up",
                        "default": "down",
                    },
                    "to_bottom": {
                        "type": "boolean",
                        "description": "Jump directly to the bottom of the page",
                        "default": False,
                    },
                },
            },
            func=browser_scroll,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_select_option",
            description="Choose an option in a select element by value, label, or index.",
            parameters={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Selector for the <select> element"},
                    "value": {"type": "string", "description": "Option value to choose"},
                    "label": {"type": "string", "description": "Visible label to choose"},
                    "index": {"type": "integer", "description": "Zero-based option index"},
                },
                "required": ["selector"],
            },
            func=browser_select_option,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access", "external_side_effect"],
        ),
        ToolDefinition(
            name="browser_navigate_back",
            description="Go back to the previous page in browser history.",
            parameters={"type": "object", "properties": {}},
            func=browser_navigate_back,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["network_access"],
        ),
        ToolDefinition(
            name="browser_close",
            description="Close the active local browser session and clear in-memory page state.",
            parameters={"type": "object", "properties": {}},
            func=browser_close,
            category="browser",
            company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            permission_effects=["runtime_internal"],
        ),
    ]
