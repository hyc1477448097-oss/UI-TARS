from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
KNOWLEDGE_DIR = ROOT_DIR / "knowledge"
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "workflow.db"
RUNS_DIR = DATA_DIR / "runs"
QDRANT_DIR = DATA_DIR / "qdrant"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512
# bge-small-zh-v1.5 检索时为 query 加的前缀，文档侧不加
EMBEDDING_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_model: str = "doubao-1.5-thinking-vision-pro-250428"
    chrome_cdp_url: str = "http://127.0.0.1:9222"
    max_uitars_steps: int = 25
    confirm_timeout_seconds: int = 1800


settings = Settings()
