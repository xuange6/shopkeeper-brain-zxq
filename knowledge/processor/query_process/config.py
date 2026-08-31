"""Query workflow configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


if load_dotenv:
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class QueryConfig:
    """Configuration shared by query workflow nodes."""

    # Text processing
    max_context_chars: int = field(
        default_factory=lambda: _env_int("RAG_MAX_CONTEXT_CHARS", 12000)
    )

    # Rerank
    rerank_max_top_k: int = field(
        default_factory=lambda: _env_int("RERANK_MAX_TOP_K", 15)
    )
    rerank_min_top_k: int = field(
        default_factory=lambda: _env_int("RERANK_MIN_TOP_K", 6)
    )
    rerank_gap_ratio: float = field(
        default_factory=lambda: _env_float("RERANK_GAP_RATIO", 0.25)
    )
    rerank_gap_abs: float = field(
        default_factory=lambda: _env_float("RERANK_GAP_ABS", 0.5)
    )
    refusal_min_score: float = field(
        default_factory=lambda: _env_float("RAG_REFUSAL_MIN_SCORE", 0.3)
    )

    # RRF
    rrf_k: int = field(default_factory=lambda: _env_int("RRF_K", 60))
    rrf_kg_weight: float = field(
        default_factory=lambda: _env_float("RRF_KG_WEIGHT", 0.7)
    )
    rrf_max_results: int = field(
        default_factory=lambda: _env_int("RRF_MAX_RESULTS", 20)
    )

    # Search
    embedding_search_limit: int = field(
        default_factory=lambda: _env_int("EMBEDDING_SEARCH_LIMIT", 10)
    )
    hyde_search_limit: int = field(
        default_factory=lambda: _env_int("HYDE_SEARCH_LIMIT", 10)
    )

    # Knowledge graph
    kg_entity_align_min_score: Optional[float] = field(
        default_factory=lambda: (
            _env_float("KG_ENTITY_ALIGN_MIN_SCORE", 0.0)
            if os.getenv("KG_ENTITY_ALIGN_MIN_SCORE")
            else None
        )
    )
    kg_max_seed_candidates: int = 3
    kg_max_total_seeds: int = 30
    kg_max_triples_per_seed: int = 50
    kg_max_total_triples: int = 200
    kg_max_total_chunks: int = 200

    # LLM
    openai_api_base: str = field(default_factory=lambda: os.getenv("OPENAI_API_BASE", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    default_model: str = field(
        default_factory=lambda: os.getenv("MODEL", "") or os.getenv("LLM_DEFAULT_MODEL", "")
    )
    item_model: str = field(default_factory=lambda: os.getenv("ITEM_MODEL", ""))

    # Milvus
    milvus_url: str = field(default_factory=lambda: os.getenv("MILVUS_URL", ""))
    chunks_collection: str = field(default_factory=lambda: os.getenv("CHUNKS_COLLECTION", ""))
    item_name_collection: str = field(
        default_factory=lambda: os.getenv("ITEM_NAME_COLLECTION", "")
    )
    entity_name_collection: str = field(
        default_factory=lambda: os.getenv("ENTITY_NAME_COLLECTION", "")
    )

    # Neo4j
    neo4j_uri: str = field(default_factory=lambda: os.getenv("NEO4J_URI", ""))
    neo4j_username: str = field(default_factory=lambda: os.getenv("NEO4J_USERNAME", ""))
    neo4j_password: str = field(default_factory=lambda: os.getenv("NEO4J_PASSWORD", ""))
    neo4j_database: str = field(default_factory=lambda: os.getenv("NEO4J_DATABASE", "neo4j"))

    # MCP
    mcp_dashscope_base_url: str = field(
        default_factory=lambda: os.getenv("MCP_DASHSCOPE_BASE_URL", "")
    )

    @classmethod
    def from_env(cls) -> "QueryConfig":
        return cls()

    def validate(self, strict: bool = False) -> None:
        missing = [
            name
            for name in ("milvus_url", "chunks_collection")
            if not getattr(self, name)
        ]
        if not missing:
            return

        message = f"Missing required query config: {missing}"
        if strict:
            raise ValueError(message)
        print(f"Warning: {message}")


_config: Optional[QueryConfig] = None


def get_config() -> QueryConfig:
    global _config
    if _config is None:
        _config = QueryConfig.from_env()
    return _config


def reset_config() -> None:
    global _config
    _config = None
