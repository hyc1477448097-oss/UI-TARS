from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from app.config import RUNS_DIR
from app.models import Run, RunEvent, SessionLocal

ACTIVE_STATUSES = {"pending", "running", "waiting_confirm"}


class Aborted(RuntimeError):
    pass


class RunSession:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.abort_event = asyncio.Event()
        self.confirm_event = asyncio.Event()
        self.subscribers: list[asyncio.Queue] = []
        self.pending_confirm: dict[str, Any] | None = None

    def check_abort(self) -> None:
        if self.abort_event.is_set():
            raise Aborted("用户已中止")

    async def emit(
        self,
        event_type: str,
        message: str = "",
        step: str = "",
        payload: dict | None = None,
    ) -> dict:
        event = {
            "type": event_type,
            "step": step,
            "message": message,
            "payload": payload or {},
            "run_id": self.run_id,
        }
        with SessionLocal() as session:
            session.add(
                RunEvent(
                    run_id=self.run_id,
                    type=event_type,
                    step=step,
                    message=message,
                    payload_json=json.dumps(payload or {}, ensure_ascii=False),
                )
            )
            session.commit()
        for queue in list(self.subscribers):
            await queue.put(event)
        return event

    async def wait_confirm(self, timeout: float) -> None:
        self.confirm_event.clear()
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(self.confirm_event.wait()),
                asyncio.create_task(self.abort_event.wait()),
            ],
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if not done:
            raise TimeoutError("等待人工确认超时")
        self.check_abort()


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


def has_active_run() -> Run | None:
    with SessionLocal() as session:
        return (
            session.query(Run)
            .filter(Run.status.in_(ACTIVE_STATUSES))
            .order_by(Run.created_at.desc())
            .first()
        )


def create_run(project_id: str, kind: str, extras: dict) -> Run:
    run_id = uuid.uuid4().hex[:12]
    RUNS_DIR.joinpath(run_id).mkdir(parents=True, exist_ok=True)
    with SessionLocal() as session:
        row = Run(
            id=run_id,
            project_id=project_id,
            kind=kind,
            status="pending",
            extras_json=json.dumps(extras or {}, ensure_ascii=False),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def update_run(run_id: str, **fields: Any) -> None:
    with SessionLocal() as session:
        row = session.get(Run, run_id)
        if row is None:
            return
        for key, value in fields.items():
            setattr(row, key, value)
        if fields.get("status") in {"succeeded", "failed", "aborted"}:
            row.finished_at = datetime.utcnow()
        session.commit()


def get_run(run_id: str) -> dict | None:
    with SessionLocal() as session:
        row = session.get(Run, run_id)
        if row is None:
            return None
        events = (
            session.query(RunEvent)
            .filter(RunEvent.run_id == run_id)
            .order_by(RunEvent.id.asc())
            .all()
        )
        return {
            "id": row.id,
            "project_id": row.project_id,
            "kind": row.kind,
            "status": row.status,
            "error": row.error or "",
            "extras": json.loads(row.extras_json or "{}"),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "events": [
                {
                    "type": ev.type,
                    "step": ev.step,
                    "message": ev.message,
                    "payload": json.loads(ev.payload_json or "{}"),
                }
                for ev in events
            ],
        }


def screenshot_dir(run_id: str):
    path = RUNS_DIR / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path
