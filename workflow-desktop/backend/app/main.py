from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.graph.workflow import run_workflow
from app.knowledge import find_project_by_query, get_project, list_projects, reload_knowledge
from app.models import init_db
from app.runtime import (
    cleanup_runs,
    create_run,
    delete_run,
    get_run,
    has_active_run,
    hub,
    screenshot_dir,
)
from app.vectorstore import close_vectorstore, init_vectorstore, is_ready, search_projects

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    vector_ok = init_vectorstore()
    reload_knowledge()
    if not vector_ok:
        logger.warning("向量搜索服务未就绪，语义检索将不可用")
    yield
    close_vectorstore()


app = FastAPI(title="Workflow Desktop", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRunBody(BaseModel):
    project_id: str
    kind: str
    extras: dict = Field(default_factory=dict)


class SearchBody(BaseModel):
    query: str
    kind: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


@app.get("/api/health")
def health():
    return {"ok": True, "vector_search": is_ready()}


@app.get("/api/projects")
def api_projects():
    return {"projects": list_projects()}


@app.post("/api/projects/search")
def api_search_projects(body: SearchBody):
    if not is_ready():
        raise HTTPException(503, "向量搜索服务未就绪，请检查嵌入模型配置")
    if body.kind is not None and body.kind not in {"test", "release"}:
        raise HTTPException(400, "kind 必须是 test、release 或空")
    try:
        results = search_projects(query=body.query, kind=body.kind, limit=body.limit)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc))
    # 用 SQLite 补全每条命中的完整项目配置
    enriched = []
    for r in results:
        project = get_project(r["project_id"])
        if project is None:
            continue
        enriched.append({**r, "config": project["config"]})
    return {"results": enriched}


@app.post("/api/projects/resolve")
def api_resolve_project(body: SearchBody):
    """按自然语言 query 解析出单个最匹配的项目，供工作流目标检索使用。

    kind 默认 test；无足够相似命中时返回 404，调用方应回退到精确选择。
    """
    if not is_ready():
        raise HTTPException(503, "向量搜索服务未就绪，请检查嵌入模型配置")
    if body.kind is None:
        body = body.model_copy(update={"kind": "test"})
    if body.kind not in {"test", "release"}:
        raise HTTPException(400, "kind 必须是 test 或 release")
    project = find_project_by_query(query=body.query, kind=body.kind)
    if project is None:
        raise HTTPException(404, "未找到足够相似的项目，请改用精确选择")
    return {"project": project, "kind": body.kind}


@app.post("/api/knowledge/reload")
def api_reload():
    count = reload_knowledge()
    return {"ok": True, "count": count, "projects": list_projects()}


@app.post("/api/runs")
async def api_start_run(body: StartRunBody):
    if body.kind not in {"test", "release"}:
        raise HTTPException(400, "kind 必须是 test 或 release")
    project = get_project(body.project_id)
    if project is None:
        raise HTTPException(404, "项目不存在，请检查知识库 YAML")
    active = has_active_run()
    if active is not None:
        raise HTTPException(409, f"已有任务在执行: {active.id} ({active.status})")
    extras = dict(body.extras or {})
    extras.setdefault("ticket", extras.get("ticket", ""))
    row = create_run(body.project_id, body.kind, extras)
    hub.register(row.id)
    asyncio.create_task(
        run_workflow(
            {
                "run_id": row.id,
                "project_id": body.project_id,
                "kind": body.kind,
                "extras": extras,
                "config": project["config"],
            }
        )
    )
    return {"id": row.id, "status": row.status, "kind": row.kind}


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    data = get_run(run_id)
    if data is None:
        raise HTTPException(404, "运行记录不存在")
    return data


@app.delete("/api/runs/{run_id}")
async def api_delete_run(run_id: str):
    try:
        deleted = delete_run(run_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    if not deleted:
        raise HTTPException(404, "运行记录不存在")
    return {"ok": True}


@app.post("/api/runs/cleanup")
async def api_cleanup(older_than_hours: int = 24, limit: int = 100):
    count = cleanup_runs(older_than_hours=older_than_hours, limit=limit)
    return {"ok": True, "deleted": count}


@app.post("/api/runs/{run_id}/confirm")
async def api_confirm(run_id: str):
    session = hub.get(run_id)
    if session is None:
        raise HTTPException(404, "当前没有可确认的运行")
    session.confirm_event.set()
    return {"ok": True}


@app.post("/api/runs/{run_id}/abort")
async def api_abort(run_id: str):
    session = hub.get(run_id)
    if session is None:
        raise HTTPException(404, "当前没有可中止的运行")
    session.abort_event.set()
    session.confirm_event.set()
    return {"ok": True}


@app.get("/api/runs/{run_id}/files/{filename}")
def api_screenshot(run_id: str, filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "非法文件名")
    path = screenshot_dir(run_id) / filename
    if not path.exists():
        raise HTTPException(404, "截图不存在")
    return FileResponse(path, media_type="image/png")


@app.websocket("/api/runs/{run_id}/events")
async def ws_events(websocket: WebSocket, run_id: str):
    await websocket.accept()
    data = get_run(run_id)
    if data is None:
        await websocket.close(code=1008)
        return
    for event in data["events"]:
        await websocket.send_text(json.dumps(event, ensure_ascii=False))
    session = hub.get(run_id)
    if session is None:
        return
    queue: asyncio.Queue = asyncio.Queue()
    session.subscribers.append(queue)
    try:
        while True:
            event = await queue.get()
            await websocket.send_text(json.dumps(event, ensure_ascii=False))
            if event.get("type") == "status" and event.get("payload", {}).get("status") in {
                "succeeded",
                "failed",
                "aborted",
            }:
                break
    except WebSocketDisconnect:
        pass
    finally:
        if session and queue in session.subscribers:
            session.subscribers.remove(queue)
