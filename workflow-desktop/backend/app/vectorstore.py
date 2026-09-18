from __future__ import annotations

import logging
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)
from sentence_transformers import SentenceTransformer

from app.config import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    EMBEDDING_QUERY_PREFIX,
    QDRANT_DIR,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "project_vectors"
_NAMESPACE_UUID = uuid.UUID("a3b2c1d0-e5f6-7a8b-9c0d-1e2f3a4b5c6d")

_client: QdrantClient | None = None
_model: SentenceTransformer | None = None
_ready: bool = False


def _point_id(project_id: str, kind: str) -> str:
    """由 (project_id, kind) 确定性派生的 UUID v5，保证 upsert 幂等。"""
    return str(uuid.uuid5(_NAMESPACE_UUID, f"{project_id}::{kind}"))


def _build_text(project: dict, kind: str) -> str:
    """为 (project, kind) 对构造结构化中文文本，给 embedding 模型明确语义信号。"""
    name = project.get("name", "")
    section = project.get(kind) or {}
    service_name = section.get("service_name", "")
    uitars_goal = section.get("uitars_goal", "")

    if kind == "test":
        deploy_url = section.get("deploy_url", "")
        parts = [
            f"项目名: {name}",
            f"服务名: {service_name}",
            "类型: 测试部署",
            f"目标: {uitars_goal}",
        ]
        if deploy_url:
            parts.append(f"部署地址: {deploy_url}")
        return "\n".join(parts)

    # kind == "release"
    url = section.get("url", "")
    env = section.get("env", "")
    form = section.get("form") or {}
    form_keys = ", ".join(form.keys()) if form else ""
    parts = [
        f"项目名: {name}",
        f"服务名: {service_name}",
        "类型: 上线发布",
        f"目标: {uitars_goal}",
    ]
    if env:
        parts.append(f"环境: {env}")
    if form_keys:
        parts.append(f"表单字段: {form_keys}")
    if url:
        parts.append(f"发布地址: {url}")
    return "\n".join(parts)


def init_vectorstore() -> bool:
    """启动时调用：创建 Qdrant 集合并加载 embedding 模型。

    返回 True 表示向量搜索可用；失败时优雅降级，返回 False。
    """
    global _client, _model, _ready
    try:
        QDRANT_DIR.mkdir(parents=True, exist_ok=True)
        _client = QdrantClient(path=str(QDRANT_DIR))
        existing = {c.name for c in _client.get_collections().collections}
        if COLLECTION_NAME not in existing:
            _client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIM,
                    distance=Distance.COSINE,
                ),
            )
            # 本地模式下 payload 索引无效（过滤走内存扫描），数据量小无需建索引；
            # 若后续切换到 Qdrant server 模式可再为 project_id/kind 建 keyword 索引
            logger.info("已创建 Qdrant 集合 '%s'", COLLECTION_NAME)
        logger.info("正在加载 embedding 模型 '%s'...", EMBEDDING_MODEL)
        _model = SentenceTransformer(EMBEDDING_MODEL)
        _ready = True
        logger.info("向量存储就绪 (dim=%d)", EMBEDDING_DIM)
        return True
    except Exception:
        logger.exception("向量存储初始化失败，语义搜索将不可用")
        _ready = False
        return False


def close_vectorstore() -> None:
    """关闭 Qdrant 客户端并释放本地模式文件锁。"""
    global _client, _ready
    if _client is not None:
        _client.close()
        _client = None
    _ready = False


def is_ready() -> bool:
    return _ready


def _embed(texts: list[str]) -> list[list[float]]:
    if _model is None:
        raise RuntimeError("embedding 模型未加载")
    embeddings = _model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [e.tolist() for e in embeddings]


def upsert_project_vectors(projects: list[dict]) -> int:
    """为每个 (project, kind) 对生成向量并写入 Qdrant。

    在 reload_knowledge() 完成 SQLite upsert 后调用。Qdrant point id 由
    (project_id, kind) 确定性派生，重复写入为覆盖而非新增。
    """
    if not _ready or _client is None:
        return 0
    texts: list[str] = []
    meta: list[dict[str, Any]] = []
    for proj in projects:
        for kind in ("test", "release"):
            section = proj.get(kind)
            if not section:
                continue
            text = _build_text(proj, kind)
            texts.append(text)
            meta.append(
                {
                    "project_id": proj["id"],
                    "kind": kind,
                    "name": proj.get("name", ""),
                    "service_name": section.get("service_name", ""),
                    "embedded_text": text,
                }
            )
    if not texts:
        return 0
    vectors = _embed(texts)
    points = [
        PointStruct(id=_point_id(m["project_id"], m["kind"]), vector=vec, payload=m)
        for vec, m in zip(vectors, meta)
    ]
    _client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)


def search_projects(
    query: str,
    kind: str | None = None,
    limit: int = 5,
    score_threshold: float = 0.3,
) -> list[dict]:
    """按自然语言 query 做向量相似度检索。

    kind 可选 "test"/"release" 用于过滤；返回每条命中含
    project_id/kind/name/service_name/score。
    """
    if not _ready or _client is None:
        raise RuntimeError("向量存储未初始化")
    query_vec = _embed([EMBEDDING_QUERY_PREFIX + query])[0]
    qdrant_filter: Filter | None = None
    if kind:
        qdrant_filter = Filter(
            must=[FieldCondition(key="kind", match=MatchValue(value=kind))]
        )
    response = _client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vec,
        query_filter=qdrant_filter,
        limit=limit,
        score_threshold=score_threshold,
    )
    return [
        {
            "project_id": hit.payload["project_id"],
            "kind": hit.payload["kind"],
            "name": hit.payload["name"],
            "service_name": hit.payload.get("service_name", ""),
            "score": hit.score,
        }
        for hit in response.points
    ]
