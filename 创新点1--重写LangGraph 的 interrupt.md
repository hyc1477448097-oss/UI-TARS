# 创新点 1：重写 LangGraph 的 interrupt——基于 asyncio.Event 的协作式中止机制

## 1. 背景

本项目（UI-TARS Workflow Desktop）使用 LangGraph 构建了一个浏览器自动化工作流，包含 7 个节点：`prepare → git_steps → open_page → observe → decide → wait_confirm/act → finalize`。工作流可能运行数分钟，用户需要随时能够**彻底中止**正在执行的任务。

LangGraph 官方提供了 `interrupt` 机制用于人机协作（human-in-the-loop），但本项目没有采用它，而是基于 `asyncio.Event` 自行实现了一套协作式中止机制。

---

## 2. 本项目的实现方式

### 2.1 核心组件

#### RunSession —— 内存中的运行会话

```python
# app/runtime.py

class RunSession:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.abort_event = asyncio.Event()      # 中止信号
        self.confirm_event = asyncio.Event()    # 人工确认信号
        self.subscribers: list[asyncio.Queue] = []  # WebSocket 订阅者
        self.pending_confirm: dict | None = None
```

`RunSession` 是一个纯内存对象，与数据库无关，生命周期等于一次工作流的运行期。

#### Aborted 异常 —— 中止的载体

```python
class Aborted(RuntimeError):
    pass
```

当检测到中止信号时，抛出此异常。它不是 bug，而是一个**受控的退出路径**。

#### Hub —— 会话注册中心

```python
class Hub:
    def __init__(self):
        self.sessions: dict[str, RunSession] = {}

    def get(self, run_id: str) -> RunSession | None:
        return self.sessions.get(run_id)

    def register(self, run_id: str) -> RunSession:
        session = RunSession(run_id)
        self.sessions[run_id] = session
        return session

    def drop(self, run_id: str) -> None:
        self.sessions.pop(run_id, None)

hub = Hub()
```

全局单例，管理所有活跃的 `RunSession`。

### 2.2 中止的触发

```python
# app/main.py

@app.post("/api/runs/{run_id}/abort")
async def api_abort(run_id: str):
    session = hub.get(run_id)
    if session is None:
        raise HTTPException(404, "当前没有可中止的运行")
    session.abort_event.set()       # 触发中止信号
    session.confirm_event.set()     # 同时解除可能正在等待的确认
    return {"ok": True}
```

用户在前端点击"中止"按钮 → 前端调用 `POST /api/runs/{run_id}/abort` → 后端调用 `abort_event.set()`。

同时设置 `confirm_event.set()` 是一个关键细节：如果工作流正停在 `wait_confirm` 节点等待人工确认，仅设置 `abort_event` 不够，因为 `asyncio.wait` 在等 `confirm_event`，必须同时唤醒它，否则 `wait_confirm` 会一直阻塞，无法走到下一个 `check_abort()`。

### 2.3 中止的检测 —— 每个节点开头的轮询

```python
# 每个节点方法的前两行都是一样的：

async def prepare(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()          # ← 这里检测
    ...

async def observe(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()          # ← 这里检测
    ...
```

`check_abort()` 的实现极其简单：

```python
def check_abort(self) -> None:
    if self.abort_event.is_set():
        raise Aborted("用户已中止")
```

**这是一个同步方法**，不需要 `await`，开销几乎为零——只是读一个内存中的布尔值。

### 2.4 中止的捕获与善后

```python
# app/graph/workflow.py

async def run_workflow(initial: WorkflowState) -> None:
    run_id = initial["run_id"]
    session = _session(run_id)
    try:
        await workflow_app.ainvoke(initial)
    except Aborted:
        # 断开浏览器
        chrome = getattr(session, "chrome", None)
        if chrome:
            await chrome.disconnect()
            session.chrome = None
        # 写入数据库
        update_run(run_id, status="aborted", error="用户已中止")
        # 通知前端
        await session.emit("status", "已中止", step="done", payload={"status": "aborted"})
    except Exception as exc:
        # ... 其他异常处理
    finally:
        hub.drop(run_id)   # 清理会话
```

`Aborted` 异常向上冒泡到 `run_workflow`，在这里被统一捕获，执行：断开浏览器连接 → 数据库状态标记为 `aborted` → 通过 WebSocket 通知前端 → 从 Hub 中移除会话。

### 2.5 人工确认的混合机制

本项目还实现了 `wait_confirm`，用于在 AI 决策涉及危险操作（如部署、发布）时暂停等待人工确认：

```python
async def wait_confirm(self, timeout: float) -> None:
    self.confirm_event.clear()
    done, pending = await asyncio.wait(
        [
            asyncio.create_task(self.confirm_event.wait()),   # 等待确认
            asyncio.create_task(self.abort_event.wait()),     # 等待中止
        ],
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if not done:
        raise TimeoutError("等待人工确认超时")
    self.check_abort()   # 如果是中止唤醒的，这里会抛 Aborted
```

`asyncio.wait` 同时监听两个事件：谁先触发就响应谁。如果是 `confirm_event` 触发，继续执行；如果是 `abort_event` 触发，`check_abort()` 抛出 `Aborted`。

### 2.6 完整数据流

```
用户点"中止"
    │
    ▼
POST /api/runs/{id}/abort
    │
    ▼
session.abort_event.set() + session.confirm_event.set()
    │
    ├── 如果工作流在 wait_confirm 中等待 → confirm_event 唤醒 → check_abort() → Aborted
    │
    └── 如果工作流在正常节点中执行 → 下一个节点开头 check_abort() → Aborted
                                         │
                                         ▼
                                    Aborted 异常冒泡
                                         │
                                         ▼
                              run_workflow() 捕获 → 断开浏览器 → 写数据库 → 通知前端 → 清理会话
```

---

## 3. 为什么这样做——与 LangGraph interrupt 的对比

### 3.1 LangGraph interrupt 的工作方式

```python
from langgraph.types import interrupt

def my_node(state):
    # 工作流暂停，状态被持久化，等人工输入后恢复
    user_decision = interrupt("是否继续？")
    if user_decision == "proceed":
        return {"approved": True}
    return {"approved": False}
```

恢复时需要调用：

```python
# 暂停后，用 None 或人工回复来恢复执行
for chunk in graph.stream(None, config):
    print(chunk)

# 或者传入人工回复
for chunk in graph.stream({"approved": True}, config):
    print(chunk)
```

### 3.2 核心差异

| 维度 | 本项目的 asyncio.Event 机制 | LangGraph interrupt |
|------|---------------------------|-------------------|
| **设计意图** | 用户随时**彻底中止**工作流 | 工作流**暂停等待人工输入**，收到后继续 |
| **结果** | 抛异常终止，不可恢复 | 暂停 + 持久化状态，可恢复执行 |
| **持久化** | 不持久化中断点，中止即结束 | LangGraph 自动持久化 checkpoint |
| **恢复方式** | 无需恢复（已经终止） | 调用 `graph.stream(None, config)` 恢复 |
| **检测方式** | 每个节点开头主动轮询 `is_set()` | 节点内调用 `interrupt()`，框架接管 |
| **响应延迟** | 最多延迟一个节点的执行时间 | 即时暂停（interrupt 点立即生效） |
| **代码侵入性** | 每个节点需手动加 `check_abort()` | 只在需要暂停的地方调用 `interrupt()` |
| **实时性** | 需要等当前节点执行完 | interrupt 点立刻暂停 |
| **浏览器资源** | 统一在异常捕获中断开 | 需要另外处理资源清理 |

### 3.3 为什么本项目不用 interrupt

1. **语义不匹配**：`interrupt` 的本质是"暂停等回复再继续"，而本项目的核心需求是"随时彻底取消"。用 `interrupt` 实现中止需要额外逻辑判断恢复时的意图是"继续"还是"终止"，徒增复杂度。

2. **恢复逻辑过重**：`interrupt` 触发后，LangGraph 会持久化 checkpoint，恢复时需要重新调用 `graph.stream(None, config)`。对于"中止"场景，这套保存-恢复机制完全不需要。

3. **即时性不足**：`interrupt` 只在调用点生效。如果用户想在两个 `interrupt` 点之间中止，仍然需要等执行到下一个 `interrupt` 才能暂停。本项目的 `check_abort()` 分布在每个节点开头，粒度更细。

4. **资源清理更直接**：中止时需要断开浏览器连接。本项目的 `try/except Aborted` 在顶层统一处理，比 `interrupt` 的恢复机制更适合做资源清理。

5. **wait_confirm 的混合需求**：本项目需要在同一个机制下同时处理"人工确认"和"用户中止"两种场景，`asyncio.wait` 同时监听两个 Event 是最自然的方式。如果用 `interrupt` 实现确认，再用 `asyncio.Event` 实现中止，两套机制混用反而更复杂。

---

## 4. 改进点

### 4.1 当前方案的不足

#### 问题 1：中止延迟不可控

`check_abort()` 只在节点开头调用，如果某个节点执行时间很长（比如 `decide` 调用 LLM 推理可能耗时 30 秒以上），用户点击中止后，最长需要等这个节点跑完才能停下。

```
用户点中止（t=0）
    │
    ▼
┌──────────────────────────────┐
│  decide 节点（调用 LLM，30s） │  ← check_abort() 在开头已过了，现在无法中断
│                              │
│  t=0: check_abort() ✗        │
│  t=5: 用户点中止              │
│  t=30: 节点执行完毕            │
│  t=30: 下一个节点 check_abort ✓│  ← 这里才能真正停止
└──────────────────────────────┘
```

**改进方案 A：在长操作中插入检查点**

```python
async def decide(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()

    # ... 构建指令
    instruction = build_instruction(...)

    session.check_abort()   # ← 在调用 LLM 之前再检查一次

    parsed, raw = await infer_action(llm, png, instruction, width, height)

    session.check_abort()   # ← LLM 返回后也检查一次

    # ... 后续处理
```

**改进方案 B：使用 asyncio 取消替代轮询**

不再依赖 `check_abort()` 轮询，而是用 `asyncio.Task.cancel()` 主动取消工作流任务：

```python
@app.post("/api/runs/{run_id}/abort")
async def api_abort(run_id: str):
    session = hub.get(run_id)
    session.abort_event.set()
    # 同时取消工作流协程
    if session.workflow_task:
        session.workflow_task.cancel()
    return {"ok": True)
```

```python
async def run_workflow(initial: WorkflowState) -> None:
    run_id = initial["run_id"]
    session = _session(run_id)
    session.workflow_task = asyncio.current_task()  # 记录当前任务
    try:
        await workflow_app.ainvoke(initial)
    except asyncio.CancelledError:
        # 处理中止
        ...
```

这样中止是即时的——`task.cancel()` 会在下一个 `await` 点抛出 `CancelledError`，不需要等节点执行完。但需要注意 LangGraph 内部对 `CancelledError` 的处理，可能需要将其转换为自定义异常。

**改进方案 C：结合 interrupt 做细粒度暂停**

对于 LLM 调用等不可中断的异步操作，可以先用 `interrupt` 暂停图的执行，等操作完成后再决定继续还是中止。这样至少可以在 `observe → decide → act` 循环的每个回合之间提供暂停机会。

---

#### 问题 2：check_abort() 散落在每个节点，容易遗漏

每个节点开头都需要手动写两行代码，新增节点时如果忘记写，该节点就不响应中止。

**改进方案：用装饰器统一注入**

```python
def abortable(func):
    """自动为节点注入中止检查"""
    async def wrapper(state: WorkflowState) -> dict:
        session = _session(state["run_id"])
        session.check_abort()
        return await func(state)
    return wrapper

@abortable
async def observe(state: WorkflowState) -> dict:
    # 不再需要手动写 check_abort()
    chrome = _chrome(state["run_id"])
    png = await chrome.screenshot_png()
    ...
```

或者更进一步，在 `build_graph` 阶段自动包装所有节点：

```python
def build_graph():
    graph = StateGraph(WorkflowState)
    for name, func in [
        ("prepare", prepare),
        ("git_steps", git_steps),
        ("observe", observe),
        ("decide", decide),
        ("act", act),
    ]:
        graph.add_node(name, abortable(func))   # 自动注入 check_abort
    ...
```

这样无论新增多少节点，都不会遗漏中止检查。

---

#### 问题 3：wait_confirm 与 check_abort 的双重机制可以统一

当前 `wait_confirm` 用 `asyncio.wait` 同时监听 `abort_event` 和 `confirm_event`，而其他节点用 `check_abort()` 轮询。两种检测中止的方式共存，增加了理解成本。

**改进方案：让 check_abort 支持异步等待**

```python
class RunSession:
    async def check_abort_or_wait(self, timeout: float | None = None) -> None:
        """检查中止信号，或等待指定时间。返回表示可以继续，抛异常表示应中止。"""
        if self.abort_event.is_set():
            raise Aborted("用户已中止")
        if timeout is not None:
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(self.abort_event.wait()),
                    asyncio.create_task(asyncio.sleep(timeout)),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            self.check_abort()
```

这样 `wait_confirm` 也可以用统一的接口，减少认知负担。

---

#### 问题 4：缺少暂停/恢复能力

当前只有"中止"（不可逆），没有"暂停/恢复"。在实际使用中，用户可能想暂时停下来看看当前状态，再决定是否继续。

**改进方案：基于 LangGraph interrupt 实现暂停/恢复，与 asyncio.Event 实现中止并存**

```python
from langgraph.types import interrupt

async def decide(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()          # asyncio.Event —— 彻底中止

    # ... LLM 推理 ...

    if state.get("needs_confirm"):
        decision = interrupt("请确认是否执行该操作")  # LangGraph interrupt —— 暂停等回复

        if decision == "abort":
            raise Aborted("用户在确认时选择中止")
        # decision == "proceed" → 继续

    return {...}
```

两层机制各司其职：
- `check_abort()`：即时中止，不可恢复
- `interrupt`：暂停等回复，可恢复

---

#### 问题 5：进程重启后中止信号丢失

`asyncio.Event` 是纯内存状态，如果后端进程崩溃或重启，所有 `RunSession` 丢失，正在运行的工作流无法被感知，也无法被中止。

**改进方案：在数据库中同步记录中止意图**

```python
@app.post("/api/runs/{run_id}/abort")
async def api_abort(run_id: str):
    session = hub.get(run_id)
    if session:
        session.abort_event.set()
        session.confirm_event.set()
    # 无论 session 是否存在，都在数据库中标记
    update_run(run_id, status="aborting")
    return {"ok": True}
```

工作流启动时也检查数据库中的状态：

```python
async def run_workflow(initial: WorkflowState) -> None:
    run_id = initial["run_id"]
    session = _session(run_id)
    try:
        await workflow_app.ainvoke(initial)
    except Aborted:
        ...
```

如果进程重启后发现 `status="aborting"` 的记录，可以跳过恢复，直接标记为 `aborted`。

---

### 4.2 改进优先级

| 优先级 | 改进点 | 原因 |
|--------|--------|------|
| 🔴 高 | 装饰器自动注入 check_abort | 防止遗漏，零成本 |
| 🔴 高 | 长操作中插入检查点 | 解决 LLM 调用期间无法中止的问题 |
| 🟡 中 | 数据库同步记录中止意图 | 处理进程崩溃场景 |
| 🟡 中 | 统一 check_abort 和 wait_confirm 的接口 | 降低认知成本 |
| 🟢 低 | 暂停/恢复能力 | 需求不明确，可等用户反馈 |

---

## 5. 总结

本项目使用 `asyncio.Event` + 每节点轮询的方式实现了工作流中止机制，核心优势是：

1. **简单直接**：一个内存布尔值 + 一个异常，没有持久化开销
2. **语义清晰**：`abort_event.set()` = 中止，`Aborted` = 中止异常，没有歧义
3. **与 WebSocket 实时通信天然契合**：前端 API 调用 → 后端 set Event → 节点感知，全链路内存操作，无 IO 延迟
4. **混合场景支持**：`asyncio.wait` 同时监听中止和确认，一个机制解决两个问题

主要不足是：中止延迟取决于节点粒度，且每个节点需手动注入检查代码。通过装饰器自动注入、长操作中插入检查点、结合 `asyncio.Task.cancel()` 等手段可以有效改进。
