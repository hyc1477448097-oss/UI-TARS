import json
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.config import KNOWLEDGE_DIR
from app.models import Project, SessionLocal


def _iter_yaml_files() -> list[Path]:
    if not KNOWLEDGE_DIR.exists():
        return []
    return sorted(KNOWLEDGE_DIR.glob("*.yaml")) + sorted(KNOWLEDGE_DIR.glob("*.yml"))


def load_projects_from_yaml() -> list[dict]:
    projects: list[dict] = []
    seen: set[str] = set()
    for path in _iter_yaml_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for item in data.get("projects") or []:
            pid = str(item.get("id") or "").strip()
            if not pid:
                raise ValueError(f"{path.name} 中存在没有 id 的项目")
            if pid in seen:
                raise ValueError(f"重复的项目 id: {pid}")
            seen.add(pid)
            if not item.get("name"):
                item["name"] = pid
            projects.append(item)
    return projects


def upsert_projects(session: Session, projects: list[dict]) -> int:
    for item in projects:
        row = session.get(Project, item["id"])
        payload = json.dumps(item, ensure_ascii=False)
        if row is None:
            session.add(Project(id=item["id"], name=item["name"], config_json=payload))
        else:
            row.name = item["name"]
            row.config_json = payload
    session.commit()
    return len(projects)


def reload_knowledge() -> int:
    projects = load_projects_from_yaml()
    with SessionLocal() as session:
        return upsert_projects(session, projects)


def list_projects() -> list[dict]:
    with SessionLocal() as session:
        rows = session.query(Project).order_by(Project.name).all()
        result = []
        for row in rows:
            config = json.loads(row.config_json)
            result.append({"id": row.id, "name": row.name, "config": config})
        return result


def get_project(project_id: str) -> dict | None:
    with SessionLocal() as session:
        row = session.get(Project, project_id)
        if row is None:
            return None
        return {"id": row.id, "name": row.name, "config": json.loads(row.config_json)}
