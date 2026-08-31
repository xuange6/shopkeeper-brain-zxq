"""
导入流程配置管理模块

集中管理所有配置项，支持环境变量覆盖
"""

from dataclasses import dataclass, field
from typing import Set, Optional
from pathlib import Path
import os
from dotenv import load_dotenv

# 明确加载 knowledge/.env，避免从项目根目录启动时读不到环境变量
load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

# 为了消灭这些重复的 __init__ 和 self，
# Python 官方推出了 @dataclass 这个偷懒神器。它的目的就是让你直接把变量写在外面（类层级）：
# 使其属于每个对象，而不是大的类
@dataclass
class ImportConfig:
    """导入流程配置"""

    # ==================== 文档处理配置 ====================
    max_content_length: int = field(
        default_factory=lambda: _env_int("CHUNK_MAX_LENGTH", 1200)
    )
    min_content_length: int = field(
        default_factory=lambda: _env_int("CHUNK_MIN_LENGTH", 300)
    )
    overlap_sentences: int = field(
        default_factory=lambda: _env_int("CHUNK_OVERLAP_SENTENCES", 1)
    )
    item_name_chunk_k: int = 3      # 商品名识别时使用的切片数量
    img_context_length: int = 200

    image_extensions: Set[str] = field(
        default_factory=lambda: {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
    )

    # ==================== LLM 配置 ====================
    openai_api_base: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_BASE", "")
    )
    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "")
    )
    vl_model: str = field(
        default_factory=lambda: os.getenv("VL_MODEL", "")
    )
    item_model: str = field(
        default_factory=lambda: os.getenv("ITEM_MODEL", "")
    )
    default_model: str = field(
        default_factory=lambda: os.getenv("MODEL", "")
    )

    # ==================== Milvus 配置 ====================
    milvus_url: str = field(
        default_factory=lambda: os.getenv("MILVUS_URL", "")
    )
    chunks_collection: str = field(
        default_factory=lambda: os.getenv("CHUNKS_COLLECTION", "")
    )
    item_name_collection: str = field(
        default_factory=lambda: os.getenv("ITEM_NAME_COLLECTION", "")
    )
    entity_name_collection: str = field(
        default_factory=lambda: os.getenv("ENTITY_NAME_COLLECTION", "")
    )

    # ==================== Neo4j 配置 ====================
    neo4j_uri: str = field(
        default_factory=lambda: os.getenv("NEO4J_URI", "")
    )
    neo4j_username: str = field(
        default_factory=lambda: os.getenv("NEO4J_USERNAME", "")
    )
    neo4j_password: str = field(
        default_factory=lambda: os.getenv("NEO4J_PASSWORD", "")
    )
    neo4j_database: str = field(
        default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j")
    )

    # ==================== MinIO 配置 ====================
    minio_endpoint: str = field(
        default_factory=lambda: os.getenv("MINIO_ENDPOINT", "")
    )
    minio_access_key: str = field(
        default_factory=lambda: os.getenv("MINIO_ACCESS_KEY", "")
    )
    minio_secret_key: str = field(
        default_factory=lambda: os.getenv("MINIO_SECRET_KEY", "")
    )
    minio_bucket: str = field(
        default_factory=lambda: os.getenv("MINIO_BUCKET_NAME", "")
    )
    minio_secure: bool = field(
        default_factory=lambda: os.getenv("MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}
    )

    # ==================== 向量配置 ====================
    embedding_dim: int = field(
        default_factory=lambda: _env_int("EMBEDDING_DIM", 1024)
    )
    embedding_batch_size: int = 5

    # ==================== 速率限制 ====================
    requests_per_minute: int = 60  # 图片总结 API 速率限制

    @classmethod
    def from_env(cls) -> "ImportConfig":
        """从环境变量加载配置"""
        return cls()

    def get_minio_base_url(self):
        """获取 MinIO 基础 URL"""
        base_protocol = "https://" if self.minio_secure else "http://"
        endpoint = self.minio_endpoint.strip().rstrip("/")
        if endpoint.startswith(("http://", "https://")):
            return endpoint
        return base_protocol + endpoint



# ==================== 全局单例 ====================
_config: Optional[ImportConfig] = None


def get_config() -> ImportConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = ImportConfig.from_env()
    return _config\r\n