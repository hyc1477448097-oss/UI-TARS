# Plan: 为 workflow-desktop 添加向量数据库（Qdrant + BGE-small-zh）

## Context

当前 `projects.yaml` 中的项目配置仅通过 SQLite 关系型数据库存储和精确 ID 查询。用户希望通过自然语言语义搜索匹配项目配置，并在工作流执行时检索最匹配的目标描述。需要引入向量数据库和本地部署的 embedding 模型来实现。

**选型结果：**
- 向量数据库：Qdrant 本地模式（嵌入式，无需 Docker）
- Embedding 模型：BAAI/bge-small-zh-v1.5（512 维，512 上下文，中文优化，~100MB）

---

## 实现步骤

### Step 1: 添加 Python 依赖

**文件：** `backend/requirements.txt`

添加：
```
qdrant-client>=1.12.0
sentence-transformers>=3.3.0
```

> `qdrant-client` 内含嵌入式本地模式，无需独立服务。`sentence-transformers` 会自动拉取 `torch` 和 `transformers`。

### Step 2: 添加配置常量

**文件：** `backend/app/config.py`

在路径常量区添加：
```python
QDRANT_DIR = DATA_DIR / "qdrant"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512
```

### Step 3: 创建 `vectorstore.py` 模块

**新文件：** `backend/app/vectorstore.py`

核心职责：
- Qdrant 客户端和 embedding 模型的生命周期管理
- 文本构建：为每个 (project, kind) 对构造结构化中文文本用于 embedding
- 向量写入：`upsert_project_vectors(projects)` — 批量编码并写入 Qdrant
- 语义搜索：`search_projects(query, kind?, limit, score_threshold)` — 向量相似度检索
- 初始化/关闭：`init_vectorstore()` / `close_vectorstore()`

**嵌入文本格式（结构化拼接，给模型明确语义信号）：**

对于 kind=test：
```
项目名: {name}
服务名: {test.service_name}
类型: 测试部署
目标: {test.uitars_goal}
部署地址: {test.deploy_url}
```

对于 kind=release：
```
项目名: {name}
服务名: {release.service_name}
类型: 上线发布
目标: {release.uitars_goal}
环境: {release.env}
表单字段: {comma-joined keys of release.form}
发布地址: {release.url}
```

**Qdrant 集合设计：**
- 集合名：`project_vectors`
- 向量：512 维，cosine 距离
- Payload：`project_id`（keyword 索引）、`kind`（keyword 索引）、`name`、`service_name`、`embedded_text`
- Point ID：UUID v5 派生自 `{project_id}::{kind}`，确保 upsert 幂等

**关键函数签名：**
```python
def init_vectorstore() -> bool          # 启动时调用，创建集合+加载模型
def close_vectorstore() -> None         # 关闭时释放 Qdrant 客户端
def is_ready() -> bool                  # 查询向量搜索是否可用
def upsert_project_vectors(projects: list[dict]) -> int
def search_projects(query: str, kind: str | None = None, limit: int = 5, score_threshold: float = 0.3) -> list[dict]
```

### Step 4: 修改 `knowledge.py` — reload_knowledge 同时写入 Qdrant

**文件：** `backend/app/knowledge.py`

在 `reload_knowledge()` 中，SQLite upsert 之后追加 Qdrant 向量写入。`get_project()` / `list_projects()` 不变，仍然是 SQLite 精确查询。

### Step 5: 修改 `main.py` — 启动初始化 + 搜索 API

**文件：** `backend/app/main.py`

- lifespan 中初始化和关闭向量库（`init_vectorstore()` 在 `reload_knowledge()` 之前；`close_vectorstore()` 在 yield 之后）
- 新增 `POST /api/projects/search` 语义搜索端点，用 SQLite 补全完整项目配置
- 增强 `GET /api/health` 返回 `vector_search` 状态

### Step 6: 前端添加语义搜索 UI

**文件：** `frontend/src/App.vue`

在项目选择器上方添加搜索输入框，输入后调用 `POST /api/projects/search`，展示匹配结果列表，点击结果可选择对应项目。

---

## 文件变更总结

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/requirements.txt` | 修改 | 添加 qdrant-client、sentence-transformers |
| `backend/app/config.py` | 修改 | 添加 QDRANT_DIR、EMBEDDING_MODEL、EMBEDDING_DIM |
| `backend/app/vectorstore.py` | 新建 | 向量库核心模块（客户端、模型、upsert、search） |
| `backend/app/knowledge.py` | 修改 | reload_knowledge 中追加 Qdrant 向量写入 |
| `backend/app/main.py` | 修改 | lifespan 初始化/关闭向量库 + 搜索 API 端点 |
| `frontend/src/App.vue` | 修改 | 添加语义搜索输入框和结果展示 |

---

## 注意事项

- **首次启动模型下载**：bge-small-zh-v1.5 ~100MB，需从 HuggingFace 下载。网络受限时可预先运行 `python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"` 缓存模型。若下载失败，`init_vectorstore()` 捕获异常，向量搜索优雅降级。
- **启动延迟**：模型加载约 2-5 秒，增加服务启动时间。
- **Qdrant 本地模式文件锁**：仅支持单进程访问，当前 uvicorn 单 worker 无冲突。
- **SQLite 保留**：Qdrant 是补充索引，不替代 SQLite。关系查询、FK 约束、Run 数据仍在 SQLite。

## 验证方法

1. 安装依赖后启动后端，确认日志输出 "Vector store ready (dim=512)"
2. 调用 `POST /api/knowledge/reload` 确认 YAML 数据同时写入 SQLite 和 Qdrant
3. 调用 `POST /api/projects/search` body: `{"query": "部署前端服务"}` 验证返回语义匹配结果
4. 调用 `GET /api/health` 确认 `vector_search: true`
5. 前端输入搜索词，验证搜索结果展示和项目选择功能
