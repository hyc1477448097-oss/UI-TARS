from __future__ import annotations

import ast
import asyncio
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Coroutine
from functools import wraps
from typing import Any, TypeVar

from playwright.async_api import Browser, Page, Playwright, async_playwright

T = TypeVar("T")


class ChromeError(RuntimeError):
    pass


def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__


class _PlaywrightScheduler:
    """Windows + uvicorn --reload 使用 SelectorEventLoop，无法 create_subprocess。

    Playwright 驱动必须在 ProactorEventLoop 上启动；所有 Page/Browser 调用也要在同一 loop。
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _needs_sidecar(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True
        return not isinstance(loop, asyncio.ProactorEventLoop)

    def ensure(self) -> None:
        if not self._needs_sidecar():
            return
        with self._lock:
            if self._loop is not None:
                return
            ready = threading.Event()

            def _run() -> None:
                loop = asyncio.ProactorEventLoop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(
                target=_run, name="playwright-proactor", daemon=True
            )
            self._thread.start()
            if not ready.wait(timeout=10):
                raise ChromeError("无法启动 Playwright 事件循环（Windows Proactor）")

    async def run(self, coro: Coroutine[Any, Any, T]) -> T:
        self.ensure()
        if self._loop is None:
            return await coro
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return await coro
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return await asyncio.wrap_future(future)
        except (asyncio.CancelledError, KeyboardInterrupt):
            future.cancel()
            raise


_scheduler = _PlaywrightScheduler()


def _pw_method(fn):
    @wraps(fn)
    async def wrapper(self, *args, **kwargs):
        return await _scheduler.run(fn(self, *args, **kwargs))

    return wrapper


def _assert_cdp_available(cdp_url: str) -> None:
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(version_url, timeout=3) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise ChromeError(
                    f"Chrome CDP 返回 HTTP {resp.status}（{version_url}）。"
                    "请用 --remote-debugging-port 启动已登录的 Chrome。"
                )
    except ChromeError:
        raise
    except urllib.error.URLError as exc:
        raise ChromeError(
            f"无法连接 Chrome CDP ({cdp_url})。请先用 "
            f"--remote-debugging-port 启动已登录的 Chrome（建议独立 "
            f'--user-data-dir）。探测 {version_url} 失败: {_exc_text(exc)}'
        ) from exc
    except Exception as exc:
        raise ChromeError(
            f"探测 Chrome CDP ({version_url}) 失败: {_exc_text(exc)}"
        ) from exc


class ChromeOperator:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        self._opened_page = False

    @property
    def page(self) -> Page:
        if self._page is None:
            raise ChromeError("浏览器页面尚未打开")
        return self._page

    async def connect(self) -> None:
        _assert_cdp_available(self.cdp_url)
        try:
            await _scheduler.run(self._connect_pw())
        except ChromeError:
            raise
        except Exception as exc:
            raise ChromeError(
                f"已探测到 Chrome CDP ({self.cdp_url})，但 Playwright 连接失败: "
                f"{_exc_text(exc)}"
            ) from exc

    async def _connect_pw(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)

    @_pw_method
    async def open_tab(self, url: str) -> str:
        if self._browser is None:
            await self.connect()
        assert self._browser is not None
        contexts = self._browser.contexts
        if not contexts:
            raise ChromeError("Chrome 没有可用的 BrowserContext，请确认已用调试端口启动并至少打开一个窗口")
        self._page = await contexts[0].new_page()
        self._opened_page = True
        await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(800)
        return self._page.url

    @_pw_method
    async def screenshot_png(self) -> bytes:
        try:
            return await self.page.screenshot(type="png", scale="css")
        except TypeError:
            return await self.page.screenshot(type="png")

    @_pw_method
    async def viewport(self) -> tuple[int, int]:
        size = self.page.viewport_size
        if size:
            return size["width"], size["height"]
        box = await self.page.evaluate(
            "() => ({w: window.innerWidth, h: window.innerHeight})"
        )
        return int(box["w"]), int(box["h"])

    @_pw_method
    async def page_text(self) -> str:
        try:
            return await self.page.inner_text("body")
        except Exception:
            return ""

    @_pw_method
    async def execute(self, action: dict[str, Any]) -> str:
        action_type = (action.get("action_type") or "").lower()
        inputs = action.get("action_inputs") or {}
        width, height = await self.viewport()

        if action_type in ("finished",):
            return "finished"

        if action_type in ("wait",):
            await self.page.wait_for_timeout(5000)
            return "wait 5s"

        if action_type in ("click", "left_single", "left_double", "right_single", "hover"):
            x, y = _center(inputs.get("start_box"), width, height)
            await self.page.mouse.move(x, y)
            if action_type == "hover":
                return f"hover ({x:.0f}, {y:.0f})"
            if action_type == "left_double":
                await self.page.mouse.dblclick(x, y)
                return f"dblclick ({x:.0f}, {y:.0f})"
            button = "right" if action_type == "right_single" else "left"
            await self.page.mouse.click(x, y, button=button)
            return f"{action_type} ({x:.0f}, {y:.0f})"

        if action_type in ("drag", "select"):
            x1, y1 = _center(inputs.get("start_box"), width, height)
            x2, y2 = _center(inputs.get("end_box"), width, height)
            await self.page.mouse.move(x1, y1)
            await self.page.mouse.down()
            await self.page.mouse.move(x2, y2, steps=12)
            await self.page.mouse.up()
            return f"drag ({x1:.0f},{y1:.0f}) -> ({x2:.0f},{y2:.0f})"

        if action_type == "scroll":
            direction = str(inputs.get("direction") or "down").lower()
            delta = -400 if "up" in direction else 400
            if "left" in direction:
                await self.page.mouse.wheel(-400, 0)
            elif "right" in direction:
                await self.page.mouse.wheel(400, 0)
            else:
                box = inputs.get("start_box")
                if box:
                    x, y = _center(box, width, height)
                    await self.page.mouse.move(x, y)
                await self.page.mouse.wheel(0, delta)
            return f"scroll {direction}"

        if action_type == "type":
            content = str(inputs.get("content") or "")
            box = inputs.get("start_box")
            if box:
                x, y = _center(box, width, height)
                await self.page.mouse.click(x, y)
            press_enter = content.endswith("\n") or content.endswith("\\n")
            text = content.replace("\\n", "").rstrip("\n")
            await self.page.keyboard.type(text, delay=20)
            if press_enter:
                await self.page.keyboard.press("Enter")
            return f"type {len(text)} chars"

        if action_type in ("hotkey",):
            hotkey = str(inputs.get("key") or inputs.get("hotkey") or "")
            keys = [_map_key(k) for k in hotkey.split() if k]
            if not keys:
                return "hotkey skipped"
            combo = "+".join(keys)
            await self.page.keyboard.press(combo)
            return f"hotkey {combo}"

        return f"unrecognized:{action_type}"

    @_pw_method
    async def close_tab(self) -> None:
        if self._opened_page and self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None

    @_pw_method
    async def disconnect(self) -> None:
        # 不关闭用户的 Chrome，只断开 Playwright 连接；是否关 Tab 由 close_tab 决定
        self._page = None
        self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


def _center(box: Any, width: int, height: int) -> tuple[float, float]:
    if box is None:
        raise ChromeError("动作缺少坐标 start_box/end_box")
    nums = ast.literal_eval(box) if isinstance(box, str) else box
    if len(nums) == 2:
        x1, y1 = float(nums[0]), float(nums[1])
        x2, y2 = x1, y1
    else:
        x1, y1, x2, y2 = [float(n) for n in nums[:4]]
    # doubao parser 输出 0-1 相对坐标
    if max(abs(x1), abs(x2), abs(y1), abs(y2)) <= 1.5:
        cx = (x1 + x2) / 2 * width
        cy = (y1 + y2) / 2 * height
    else:
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
    return cx, cy


def _map_key(key: str) -> str:
    mapping = {
        "ctrl": "Control",
        "control": "Control",
        "cmd": "Meta",
        "command": "Meta",
        "alt": "Alt",
        "shift": "Shift",
        "enter": "Enter",
        "return": "Enter",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "space": " ",
        "arrowleft": "ArrowLeft",
        "arrowright": "ArrowRight",
        "arrowup": "ArrowUp",
        "arrowdown": "ArrowDown",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "up": "ArrowUp",
        "down": "ArrowDown",
    }
    return mapping.get(key.lower(), key)
