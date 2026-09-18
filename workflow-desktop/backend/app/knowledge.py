import json
import logging
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.config import KNOWLEDGE_DIR
from app.models import Project, SessionLocal

logger = logging.getLogger(__name__)


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
        count = upsert_projects(session, projects)
    # 同步写入 Qdrant 向量索引；向量库未就绪时跳过
    from app.vectorstore import is_ready, upsert_project_vectors

    if is_ready():
        vector_count = upsert_project_vectors(projects)
        logger.info("已写入 %d 条向量到 Qdrant", vector_count)
    else:
        logger.warning("向量库未就绪，跳过向量同步")
    return count


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


def find_project_by_query(query: str, kind: str = "test") -> dict | None:
    """按自然语言 query 语义匹配最相关的项目。

    供工作流目标检索使用；向量库未就绪或无足够相似命中时返回 None，
    调用方应回退到精确 project_id 查询。
    """
    from app.vectorstore import is_ready, search_projects

    if not is_ready():
        return None
    results = search_projects(query=query, kind=kind, limit=1, score_threshold=0.5)
    if not results:
        return None
    return get_project(results[0]["project_id"])
