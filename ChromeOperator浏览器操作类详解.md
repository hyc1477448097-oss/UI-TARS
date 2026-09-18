# ChromeOperator 浏览器操作类详解

## 1. 概述

`ChromeOperator` 是本项目（UI-TARS Workflow Desktop）的**浏览器操作封装层**，位于 `backend/app/operators/chrome.py`。它通过 Playwright 连接用户本地已启动的 Chrome，把工作流节点的操作意图翻译成浏览器动作。

核心职责：**适配 AI 动作格式与 Playwright 原生 API 之间的差异**，让工作流图节点只写业务逻辑，不被底层细节淹没。

---

## 2. 类定义

```python
class ChromeOperator:
    def __init__(self, cdp_url: str):
        self.cdp_url = cdp_url
        self._pw: Playwright | None = None       # Playwright 实例
        self._browser: Browser | None = None     # 浏览器连接
        self._page: Page | None = None          # 当前页面
        self._opened_page = False                # 标记是否是自己开的标签页
```

状态字段设计的关键点：
- `_opened_page`：区分"我自己打开的标签页"和"用户已存在的标签页"，决定 `close_tab()` 时是否要关它（`disconnect()` 永远不关用户的 Chrome，只断开 Playwright 连接）。
- 所有字段以 `_` 开头，私有；外部通过 `page` 属性访问当前页面，未连接时会抛 `ChromeError`。

---

## 3. 方法清单

### 3.1 连接与标签页管理

| 方法 | 功能 |
|------|------|
| `connect()` | 先探测 CDP 是否可用，再用 Playwright 通过 `connect_over_cdp` 连接 Chrome |
| `open_tab(url)` | 新开标签页并导航到 URL，等待 `domcontentloaded` + 800ms |
| `close_tab()` | 只关闭自己打开的标签页（`_opened_page=True`） |
| `disconnect()` | 断开 Playwright 连接，**不关闭**用户的 Chrome |

### 3.2 页面信息获取

| 方法 | 功能 |
|------|------|
| `screenshot_png()` | 截图返回 PNG 字节，带 `scale="css"` 兜底（旧版 Playwright 不支持该参数时降级） |
| `viewport()` | 获取视口宽高，优先用 `page.viewport_size`，为 None 时 fallback 到 `window.innerWidth/innerHeight` |
| `page_text()` | 获取 body 文本，用于检测成功标志（`success_hint`）是否出现 |

### 3.3 动作执行

`execute(action)` 是核心方法，负责把 AI 输出的动作字典翻译成 Playwright 调用。

---

## 4. 支持的动作类型

`execute()` 根据 `action_type` 分发，覆盖 UI-TARS 模型输出的所有动作：

| 动作类型 | 对应别名 | 实现细节 |
|---------|---------|---------|
| `click` / `left_single` | 鼠标单击 | `mouse.move` + `mouse.click`，左键 |
| `left_double` | 双击 | `mouse.dblclick` |
| `right_single` | 右键单击 | `mouse.click(button="right")` |
| `hover` | 悬停 | 只 `mouse.move`，不点 |
| `drag` / `select` | 拖拽/选择 | `move → down → move(steps=12) → up` 四步组合 |
| `scroll` | 滚动 | 方向 `up/down/left/right`，`up` 滚 -400，`down` 滚 +400；可带坐标先 `move` |
| `type` | 文本输入 | 先点目标位置，再 `keyboard.type(delay=20)`；末尾 `\n` 自动按 Enter |
| `hotkey` | 组合键 | 拆分按键名 → `_map_key` 转译 → `keyboard.press("Control+C")` |
| `wait` | 等待 | `wait_for_timeout(5000)` |
| `finished` | 完成 | 直接返回，不执行任何浏览器动作 |

---

## 5. 坐标转换 `_center()`

AI 模型输出的坐标是相对值（0~1），Playwright 需要像素坐标。`_center()` 负责转换：

```python
def _center(box, width, height) -> tuple[float, float]:
    # box 可能是 [x1, y1] 两元组，或 [x1, y1, x2, y2] 四元组
    nums = ast.literal_eval(box) if isinstance(box, str) else box
    if len(nums) == 2:
        x1, y1 = float(nums[0]), float(nums[1])
        x2, y2 = x1, y1
    else:
        x1, y1, x2, y2 = [float(n) for n in nums[:4]]

    # 自动识别相对坐标 vs 像素坐标
    if max(abs(x1), abs(x2), abs(y1), abs(y2)) <= 1.5:
        # 相对坐标：乘以视口宽高
        cx = (x1 + x2) / 2 * width
        cy = (y1 + y2) / 2 * height
    else:
        # 像素坐标：直接取中点
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2
    return cx, cy
```

**关键设计**：用 `1.5` 作为阈值判断是相对坐标还是像素坐标——相对坐标都在 0~1 之间，像素坐标通常远大于 1.5。取中点是因为 AI 给的 `start_box`/`end_box` 是目标区域的对角，点击要用中心点。

---

## 6. 按键名映射 `_map_key()`

AI 输出 `"ctrl"`，Playwright 要 `"Control"`；AI 输出 `"cmd"`，Playwright 要 `"Meta"`。`_map_key()` 负责这套转译：

```python
mapping = {
    "ctrl": "Control",  "control": "Control",
    "cmd": "Meta",      "command": "Meta",
    "alt": "Alt",       "shift": "Shift",
    "enter": "Enter",   "return": "Enter",
    "esc": "Escape",    "escape": "Escape",
    "tab": "Tab",       "space": " ",
    "arrowleft": "ArrowLeft",   "left": "ArrowLeft",
    "arrowright": "ArrowRight", "right": "ArrowRight",
    "arrowup": "ArrowUp",       "up": "ArrowUp",
    "arrowdown": "ArrowDown",   "down": "ArrowDown",
}
return mapping.get(key.lower(), key)   # 没映射的直接返回原值
```

组合键如 `"ctrl c"` 会被拆成 `["ctrl", "c"]`，各自映射后拼成 `"Control+C"` 传给 `keyboard.press()`。

---

## 7. 跨线程调度 `_PlaywrightScheduler`

这是工程上最巧妙的一块。

### 7.1 问题

Windows 上 uvicorn `--reload` 使用 `SelectorEventLoop`，而 Playwright 驱动浏览器需要 `ProactorEventLoop` 才能创建子进程。两者不兼容——直接在 Selector loop 上 `await async_playwright().start()` 会失败。

### 7.2 方案

单独开一个后台线程，跑一个 `ProactorEventLoop`，所有 Playwright 调用都转发到那个线程执行：

```python
class _PlaywrightScheduler:
    def _needs_sidecar(self) -> bool:
        # 只有 Windows + 非 Proactor loop 时才需要 sidecar
        if sys.platform != "win32":
            return False
        loop = asyncio.get_running_loop()
        return not isinstance(loop, asyncio.ProactorEventLoop)

    def ensure(self) -> None:
        # 启动后台线程的 ProactorEventLoop
        ...
        self._thread = threading.Thread(target=_run, name="playwright-proactor", daemon=True)

    async def run(self, coro):
        self.ensure()
        if running is self._loop:
            return await coro                       # 已经在 Proactor loop 上，直接跑
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(future)    # 转发到后台线程
```

### 7.3 装饰器自动调度

`_pw_method` 把所有 Playwright 调用自动包成跨线程调度，节点层完全无感：

```python
def _pw_method(fn):
    @wraps(fn)
    async def wrapper(self, *args, **kwargs):
        return await _scheduler.run(fn(self, *args, **kwargs))
    return wrapper

# 使用：方法上加 @_pw_method 即可
@_pw_method
async def screenshot_png(self) -> bytes:
    return await self.page.screenshot(...)
```

好处：
- 节点层调 `chrome.screenshot_png()` 就行，不用关心事件循环
- 非 Windows 环境下 `_needs_sidecar()` 返回 False，直接 `await coro`，零开销
- 装饰器统一应用，不会漏掉某个方法的跨线程处理

---

## 8. CDP 可用性探测 `_assert_cdp_available()`

连接前先用原生 `urllib` 探测 Chrome 的 CDP 端口（`/json/version`），给用户友好的错误提示：

```python
def _assert_cdp_available(cdp_url: str) -> None:
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(version_url, timeout=3) as resp:
            ...
    except urllib.error.URLError as exc:
        raise ChromeError(
            f"无法连接 Chrome CDP ({cdp_url})。请先用 "
            f"--remote-debugging-port 启动已登录的 Chrome（建议独立 "
            f'--user-data-dir）。探测 {version_url} 失败: ...'
        )
```

这样在 Playwright 连接之前就能快速失败，并告诉用户正确的启动方式，而不是抛一个晦涩的 Playwright 内部错误。

---

## 9. 异常类 `ChromeError`

```python
class ChromeError(RuntimeError):
    pass
```

统一的错误类型，配合 `_exc_text()` 提取异常文本，让错误信息可读：

```python
def _exc_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return type(exc).__name__
```

---

## 10. 在工作流中的使用位置

```
prepare
   │
   ▼
open_page ──▶ ChromeOperator(cdp_url) + connect() + open_tab(url)
   │
   ▼
observe ─────▶ screenshot_png() + viewport()         (看页面)
   │
   ▼
decide ──────▶ (调用 LLM 生成 action，不直接用 Chrome)
   │
   ▼
act ─────────▶ execute(action)                       (操作页面)
              + page_text()                          (检测成功标志)
   │
   ▼
finalize ────▶ close_tab() + disconnect()            (清理)
```

`ChromeOperator` 实例存在 `RunSession.chrome` 上，整个工作流复用同一个实例，最后在 `finalize` 或异常捕获中断开。

---

## 11. 设计模式总结

这个类体现了几个经典模式：

| 模式 | 体现 |
|------|------|
| **适配器模式（Adapter）** | 把 AI 动作格式（`{"action_type", "action_inputs"}`）适配到 Playwright 原生 API |
| **外观模式（Facade）** | 把 Playwright 的 `Playwright`/`Browser`/`Page` 三层对象简化成一个类的接口 |
| **装饰器模式（Decorator）** | `_pw_method` 装饰器自动注入跨线程调度 |
| **单例调度（Scheduler）** | `_PlaywrightScheduler` 全局单例，管理后台线程的事件循环 |
| **状态机** | 通过 `_opened_page` 等字段跟踪连接状态，决定清理行为 |

---

## 12. 为什么不直接用 Playwright 原生 API

| 场景 | 直接用 Playwright | 用 ChromeOperator |
|------|------------------|------------------|
| AI 输出 `{"action_type":"drag","action_inputs":{"start_box":[...],"end_box":[...]}}` | 自己拆成 `move+down+move+up` 四步，自己算坐标 | `await chrome.execute(action)` 一行搞定 |
| Windows + uvicorn --reload | 每个方法都要处理事件循环切换 | 装饰器自动调度，节点无感 |
| 多节点复用连接 | 每个节点重复 `connect_over_cdp` | `connect` 一次，存在 `RunSession` 上复用 |
| 结束时清理 | 散落的断开逻辑 | `disconnect()` 集中处理，不误关用户 Chrome |
| 错误提示 | Playwright 内部错误晦涩 | `_assert_cdp_available` 给出明确启动指引 |

封装层把"能操作浏览器"提升为"按 AI 动作格式、跨线程安全、生命周期可控地操作浏览器"，让上层图节点只写业务逻辑。
