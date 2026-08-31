from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.graph.workflow import run_workflow
from app.knowledge import get_project, list_projects, reload_knowledge
from app.models import init_db
from app.runtime import (
    create_run,
    get_run,
    has_active_run,
    hub,
    screenshot_dir,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    reload_knowledge()
    yield


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


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/projects")
def api_projects():
    return {"projects": list_projects()}


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
