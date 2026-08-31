from __future__ import annotations

import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.operators.chrome import ChromeOperator
from app.operators.git import GitError, GitOperator
from app.operators.uitars import build_instruction, create_llm, infer_action
from app.runtime import Aborted, hub, screenshot_dir, update_run


class WorkflowState(TypedDict, total=False):
    run_id: str
    project_id: str
    kind: str
    extras: dict
    config: dict
    instruction: str
    step_count: int
    history: list[str]
    last_action: dict
    last_raw: str
    needs_confirm: bool
    confirm_summary: str
    screenshot_name: str
    status: str
    error: str
    close_tab: bool


TEST_DANGER = ("部署", "启动", "deploy", "start")
RELEASE_DANGER = ("发布", "提交", "上线", "publish", "release", "submit", "确认发布")


def _session(run_id: str):
    session = hub.get(run_id)
    if session is None:
        raise RuntimeError("找不到运行会话")
    return session


def _chrome(run_id: str) -> ChromeOperator:
    session = _session(run_id)
    chrome = getattr(session, "chrome", None)
    if chrome is None:
        raise RuntimeError("浏览器尚未连接")
    return chrome


def _apply_placeholders(text: str, extras: dict) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        return str(extras.get(key, match.group(0)))

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", repl, text)


def _goal_and_url(state: WorkflowState) -> tuple[str, str, str, bool]:
    config = state["config"]
    extras = state.get("extras") or {}
    kind = state["kind"]
    if kind == "test":
        test = config.get("test") or {}
        goal = _apply_placeholders(test.get("uitars_goal") or "", extras)
        url = test.get("deploy_url") or ""
        hint = test.get("success_hint") or ""
        close_tab = bool(test.get("close_tab"))
        extra = f"服务名: {test.get('service_name', '')}"
        return f"{goal}\n{extra}", url, hint, close_tab
    release = config.get("release") or {}
    form = release.get("form") or {}
    rendered = {k: _apply_placeholders(str(v), extras) for k, v in form.items()}
    form_text = "；".join(f"{k}={v}" for k, v in rendered.items())
    goal = _apply_placeholders(release.get("uitars_goal") or "", extras)
    extra = (
        f"服务名: {release.get('service_name', '')}；"
        f"环境: {release.get('env', '')}；表单: {form_text}"
    )
    url = release.get("url") or ""
    hint = release.get("success_hint") or ""
    close_tab = bool(release.get("close_tab"))
    return f"{goal}\n{extra}", url, hint, close_tab


def _is_dangerous(kind: str, action: dict, raw: str) -> bool:
    action_type = (action.get("action_type") or "").lower()
    if action_type in ("wait", "finished", "scroll", "hotkey"):
        return False
    blob = " ".join(
        [
            action.get("thought") or "",
            raw or "",
            action_type,
            str(action.get("action_inputs") or {}),
        ]
    ).lower()
    keywords = TEST_DANGER if kind == "test" else RELEASE_DANGER
    return any(k.lower() in blob for k in keywords)


async def prepare(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    update_run(state["run_id"], status="running")
    await session.emit("status", "开始执行", step="prepare", payload={"status": "running"})
    instruction, url, hint, close_tab = _goal_and_url(state)
    if not url:
        raise RuntimeError("知识库未配置目标 URL")
    session.success_hint = hint
    session.target_url = url
    return {
        "instruction": instruction,
        "step_count": 0,
        "history": [],
        "status": "running",
        "close_tab": close_tab,
        "error": "",
    }


def route_after_prepare(state: WorkflowState) -> Literal["git_steps", "open_page"]:
    config = state["config"]
    kind = state["kind"]
    if kind == "test" and not (config.get("test") or {}).get("skip_git"):
        return "git_steps"
    if kind == "release" and (config.get("release") or {}).get("enable_git"):
        return "git_steps"
    return "open_page"


async def git_steps(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    config = state["config"]
    await session.emit("log", "开始 Git 操作", step="git")
    repo = config.get("repo_path") or ""
    test = config.get("test") or {}
    release = config.get("release") or {}
    git = GitOperator(repo)
    try:
        if state["kind"] == "test":
            logs = git.prepare_test(
                test.get("branch") or "release/test",
                test.get("merge_from") or "current",
            )
        else:
            logs = git.prepare_test(
                release.get("branch") or test.get("branch") or "release/test",
                release.get("merge_from") or "current",
            )
    except GitError as exc:
        raise RuntimeError(str(exc)) from exc
    for line in logs:
        await session.emit("log", line, step="git")
    return {}


async def open_page(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    url = getattr(session, "target_url", "")
    chrome = ChromeOperator(settings.chrome_cdp_url)
    session.chrome = chrome
    await session.emit("log", f"连接 Chrome 并打开 {url}", step="browser")
    await chrome.connect()
    final_url = await chrome.open_tab(url)
    await session.emit("log", f"已打开 {final_url}", step="browser")
    return {}


async def observe(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    chrome = _chrome(state["run_id"])
    png = await chrome.screenshot_png()
    step = int(state.get("step_count") or 0) + 1
    name = f"step-{step:02d}.png"
    path = screenshot_dir(state["run_id"]) / name
    path.write_bytes(png)
    await session.emit(
        "screenshot",
        f"第 {step} 步截图",
        step="uitars",
        payload={"file": name, "step": step},
    )
    return {"screenshot_name": name, "step_count": step}


async def decide(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    if state.get("step_count", 0) > settings.max_uitars_steps:
        raise RuntimeError(f"超过最大步数 {settings.max_uitars_steps}，已停止")
    chrome = _chrome(state["run_id"])
    path = screenshot_dir(state["run_id"]) / state["screenshot_name"]
    png = path.read_bytes()
    width, height = await chrome.viewport()
    llm = getattr(session, "llm", None)
    if llm is None:
        llm = create_llm()
        session.llm = llm
    instruction = build_instruction(state["instruction"], state.get("history") or [])
    parsed, raw = await infer_action(llm, png, instruction, width, height)
    action = parsed[0]
    thought = action.get("thought") or ""
    await session.emit(
        "log",
        f"Thought: {thought}\nAction: {action.get('action_type')}",
        step="uitars",
        payload={"raw": raw, "action": action},
    )
    needs = _is_dangerous(state["kind"], action, raw)
    summary = _confirm_summary(state, action, thought)
    return {
        "last_action": action,
        "last_raw": raw,
        "needs_confirm": needs,
        "confirm_summary": summary,
    }


def route_after_decide(
    state: WorkflowState,
) -> Literal["wait_confirm", "act", "finalize"]:
    action = state.get("last_action") or {}
    if (action.get("action_type") or "").lower() == "finished":
        return "finalize"
    if state.get("needs_confirm"):
        return "wait_confirm"
    return "act"


async def wait_confirm(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    action = state.get("last_action") or {}
    summary = state.get("confirm_summary") or "请确认是否执行该操作"
    session.pending_confirm = {"summary": summary, "action": action}
    update_run(state["run_id"], status="waiting_confirm")
    await session.emit(
        "confirm_required",
        summary,
        step="confirm",
        payload={"summary": summary, "action": action},
    )
    await session.emit(
        "status",
        "等待确认",
        step="confirm",
        payload={"status": "waiting_confirm"},
    )
    try:
        await session.wait_confirm(settings.confirm_timeout_seconds)
    except Aborted:
        session.pending_confirm = None
        raise
    session.pending_confirm = None
    update_run(state["run_id"], status="running")
    await session.emit(
        "status",
        "已确认，继续执行",
        step="confirm",
        payload={"status": "running"},
    )
    return {"needs_confirm": False}


async def act(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    session.check_abort()
    chrome = _chrome(state["run_id"])
    action = state.get("last_action") or {}
    detail = await chrome.execute(action)
    history = list(state.get("history") or [])
    history.append(
        f"{state.get('step_count')}. {action.get('action_type')} | {action.get('thought') or ''} | {detail}"
    )
    await session.emit("log", f"已执行: {detail}", step="act")
    hint = getattr(session, "success_hint", "") or ""
    if hint:
        text = await chrome.page_text()
        if hint in text:
            await session.emit("log", f"页面已出现成功标志: {hint}", step="verify")
            return {
                "history": history,
                "status": "succeeded",
                "last_action": {**action, "action_type": "finished"},
            }
    return {"history": history}


def route_after_act(state: WorkflowState) -> Literal["observe", "finalize"]:
    action = state.get("last_action") or {}
    if (action.get("action_type") or "").lower() == "finished":
        return "finalize"
    if state.get("status") == "succeeded":
        return "finalize"
    if int(state.get("step_count") or 0) >= settings.max_uitars_steps:
        return "finalize"
    return "observe"


async def finalize(state: WorkflowState) -> dict:
    session = _session(state["run_id"])
    chrome = getattr(session, "chrome", None)
    status = state.get("status") or "succeeded"
    if int(state.get("step_count") or 0) >= settings.max_uitars_steps and status != "succeeded":
        status = "failed"
        error = f"达到最大步数 {settings.max_uitars_steps}"
    else:
        error = state.get("error") or ""
        if (state.get("last_action") or {}).get("action_type") == "finished":
            status = "succeeded"
    if chrome and state.get("close_tab"):
        await chrome.close_tab()
    if chrome:
        await chrome.disconnect()
        session.chrome = None
    update_run(state["run_id"], status=status, error=error)
    await session.emit("status", f"流程结束: {status}", step="done", payload={"status": status})
    return {"status": status, "error": error}


def _confirm_summary(state: WorkflowState, action: dict, thought: str) -> str:
    kind = "测试部署/启动" if state["kind"] == "test" else "上线发布/提交"
    config = state["config"]
    extras = state.get("extras") or {}
    if state["kind"] == "test":
        test = config.get("test") or {}
        meta = f"服务 {test.get('service_name')}，分支 {test.get('branch')}"
    else:
        rel = config.get("release") or {}
        form = {
            k: _apply_placeholders(str(v), extras) for k, v in (rel.get("form") or {}).items()
        }
        meta = f"服务 {rel.get('service_name')}，环境 {rel.get('env')}，表单 {form}"
    return (
        f"即将执行{kind}相关点击（{action.get('action_type')}）。{meta}。"
        f"模型意图: {thought or '（无）'}"
    )


def build_graph():
    graph = StateGraph(WorkflowState)
    graph.add_node("prepare", prepare)
    graph.add_node("git_steps", git_steps)
    graph.add_node("open_page", open_page)
    graph.add_node("observe", observe)
    graph.add_node("decide", decide)
    graph.add_node("wait_confirm", wait_confirm)
    graph.add_node("act", act)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", route_after_prepare)
    graph.add_edge("git_steps", "open_page")
    graph.add_edge("open_page", "observe")
    graph.add_edge("observe", "decide")
    graph.add_conditional_edges("decide", route_after_decide)
    graph.add_edge("wait_confirm", "act")
    graph.add_conditional_edges("act", route_after_act)
    graph.add_edge("finalize", END)
    return graph.compile()


workflow_app = build_graph()


async def run_workflow(initial: WorkflowState) -> None:
    run_id = initial["run_id"]
    session = _session(run_id)
    try:
        await workflow_app.ainvoke(initial)
    except Aborted:
        chrome = getattr(session, "chrome", None)
        if chrome:
            await chrome.disconnect()
            session.chrome = None
        update_run(run_id, status="aborted", error="用户已中止")
        await session.emit("status", "已中止", step="done", payload={"status": "aborted"})
    except Exception as exc:
        chrome = getattr(session, "chrome", None)
        if chrome:
            try:
                await chrome.disconnect()
            except Exception:
                pass
            session.chrome = None
        update_run(run_id, status="failed", error=str(exc))
        raw = getattr(exc, "raw", "")
        await session.emit(
            "error",
            str(exc),
            step="error",
            payload={"raw": raw, "status": "failed"},
        )
        await session.emit("status", "失败", step="done", payload={"status": "failed"})
    finally:
        hub.drop(run_id)
