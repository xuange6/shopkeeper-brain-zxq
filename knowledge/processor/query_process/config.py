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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class QueryConfig:
    """Configuration shared by query workflow nodes."""

    # Versioned stage-2 policy. All values below are included in evaluation
    # metadata so a control/candidate pair cannot silently compare different
    # routing, calibration, refusal, or evidence policies.
    retrieval_policy_version: str = field(
        default_factory=lambda: os.getenv("RAG_RETRIEVAL_POLICY_VERSION", "stage2-industrial-rag-v2")
    )

    # Security policy. Authorization remains deterministic and fail-closed;
    # normalization, bounded encoded-payload inspection, and fuzzy typo checks
    # make the first gate resilient to common obfuscation techniques.
    security_policy_version: str = field(
        default_factory=lambda: os.getenv("RAG_SECURITY_POLICY_VERSION", "intent-policy-v2")
    )
    security_max_input_chars: int = field(
        default_factory=lambda: _env_int("RAG_SECURITY_MAX_INPUT_CHARS", 4000)
    )
    security_inspect_encodings: bool = field(
        default_factory=lambda: _env_bool("RAG_SECURITY_INSPECT_ENCODINGS", True)
    )
    security_max_decoded_payloads: int = field(
        default_factory=lambda: _env_int("RAG_SECURITY_MAX_DECODED_PAYLOADS", 4)
    )
    security_fuzzy_threshold: float = field(
        default_factory=lambda: _env_float("RAG_SECURITY_FUZZY_THRESHOLD", 0.82)
    )
    security_context_guard_enabled: bool = field(
        default_factory=lambda: _env_bool("RAG_SECURITY_CONTEXT_GUARD", True)
    )
    security_output_guard_enabled: bool = field(
        default_factory=lambda: _env_bool("RAG_SECURITY_OUTPUT_GUARD", True)
    )

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
    rerank_calibration_center: float = field(
        default_factory=lambda: _env_float("RERANK_CALIBRATION_CENTER", -1.8)
    )
    rerank_calibration_scale: float = field(
        default_factory=lambda: _env_float("RERANK_CALIBRATION_SCALE", 0.9)
    )
    confidence_relevance_weight: float = field(
        default_factory=lambda: _env_float("RAG_CONFIDENCE_RELEVANCE_WEIGHT", 0.35)
    )
    confidence_margin_weight: float = field(
        default_factory=lambda: _env_float("RAG_CONFIDENCE_MARGIN_WEIGHT", 0.15)
    )
    confidence_authority_weight: float = field(
        default_factory=lambda: _env_float("RAG_CONFIDENCE_AUTHORITY_WEIGHT", 0.15)
    )
    confidence_structure_weight: float = field(
        default_factory=lambda: _env_float("RAG_CONFIDENCE_STRUCTURE_WEIGHT", 0.15)
    )
    confidence_coverage_weight: float = field(
        default_factory=lambda: _env_float("RAG_CONFIDENCE_COVERAGE_WEIGHT", 0.20)
    )
    refusal_min_confidence: float = field(
        default_factory=lambda: _env_float("RAG_REFUSAL_MIN_CONFIDENCE", 0.46)
    )
    capability_evidence_guard_enabled: bool = field(
        default_factory=lambda: _env_bool("RAG_CAPABILITY_EVIDENCE_GUARD", True)
    )
    capability_authoritative_min_coverage: float = field(
        default_factory=lambda: _env_float(
            "RAG_CAPABILITY_AUTHORITATIVE_MIN_COVERAGE", 0.5
        )
    )
    answer_max_evidence: int = field(
        default_factory=lambda: _env_int("RAG_ANSWER_MAX_EVIDENCE", 6)
    )
    citation_min_overlap: float = field(
        default_factory=lambda: _env_float("RAG_CITATION_MIN_OVERLAP", 0.08)
    )
    require_claim_citations: bool = field(
        default_factory=lambda: _env_bool("RAG_REQUIRE_CLAIM_CITATIONS", True)
    )
    structured_constraint_completion: bool = field(
        default_factory=lambda: _env_bool("RAG_STRUCTURED_CONSTRAINT_COMPLETION", True)
    )
    structured_constraint_max_claims: int = field(
        default_factory=lambda: _env_int("RAG_STRUCTURED_CONSTRAINT_MAX_CLAIMS", 4)
    )
    structured_constraint_coverage: float = field(
        default_factory=lambda: _env_float("RAG_STRUCTURED_CONSTRAINT_COVERAGE", 0.6)
    )

    # RRF
    rrf_k: int = field(default_factory=lambda: _env_int("RRF_K", 60))
    rrf_kg_weight: float = field(
        default_factory=lambda: _env_float("RRF_KG_WEIGHT", 0.7)
    )
    rrf_direct_weight: float = field(
        default_factory=lambda: _env_float("RRF_DIRECT_WEIGHT", 1.0)
    )
    rrf_hyde_weight: float = field(
        default_factory=lambda: _env_float("RRF_HYDE_WEIGHT", 0.9)
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
    enable_hyde: bool = field(
        default_factory=lambda: _env_bool("RAG_ENABLE_HYDE", True)
    )
    enable_knowledge_graph: bool = field(
        default_factory=lambda: _env_bool("RAG_ENABLE_KG", True)
    )
    enable_web: bool = field(
        default_factory=lambda: _env_bool("RAG_ENABLE_WEB", True)
    )
    web_fallback_min_local_candidates: int = field(
        default_factory=lambda: _env_int("RAG_WEB_FALLBACK_MIN_LOCAL", 1)
    )
    web_fallback_min_local_coverage: float = field(
        default_factory=lambda: _env_float("RAG_WEB_FALLBACK_MIN_LOCAL_COVERAGE", 0.25)
    )
    web_search_limit: int = field(
        default_factory=lambda: _env_int("RAG_WEB_SEARCH_LIMIT", 3)
    )
    direct_timeout_ms: int = field(
        default_factory=lambda: _env_int("RAG_DIRECT_TIMEOUT_MS", 8000)
    )
    hyde_timeout_ms: int = field(
        default_factory=lambda: _env_int("RAG_HYDE_TIMEOUT_MS", 15000)
    )
    kg_timeout_ms: int = field(
        default_factory=lambda: _env_int("RAG_KG_TIMEOUT_MS", 12000)
    )
    web_timeout_ms: int = field(
        default_factory=lambda: _env_int("RAG_WEB_TIMEOUT_MS", 12000)
    )

    # Source policy. Local manuals are authoritative for product and safety
    # questions; Web is preferred only for explicitly time-sensitive intent.
    local_authority: float = field(
        default_factory=lambda: _env_float("RAG_LOCAL_AUTHORITY", 1.0)
    )
    web_default_authority: float = field(
        default_factory=lambda: _env_float("RAG_WEB_DEFAULT_AUTHORITY", 0.45)
    )
    web_official_authority: float = field(
        default_factory=lambda: _env_float("RAG_WEB_OFFICIAL_AUTHORITY", 0.9)
    )
    web_official_domains: str = field(
        default_factory=lambda: os.getenv("RAG_WEB_OFFICIAL_DOMAINS", "")
    )
    web_freshness_require_official: bool = field(
        default_factory=lambda: _env_bool("RAG_WEB_FRESHNESS_REQUIRE_OFFICIAL", True)
    )
    web_official_query_expansion: bool = field(
        default_factory=lambda: _env_bool("RAG_WEB_OFFICIAL_QUERY_EXPANSION", True)
    )
    web_official_query_domain_limit: int = field(
        default_factory=lambda: _env_int("RAG_WEB_OFFICIAL_QUERY_DOMAIN_LIMIT", 3)
    )

    # Per-request and evaluation-batch budgets. Monetary values use the
    # currency declared by config/model_pricing.json.
    request_max_model_calls: int = field(
        default_factory=lambda: _env_int("RAG_REQUEST_MAX_MODEL_CALLS", 4)
    )
    request_max_tokens: int = field(
        default_factory=lambda: _env_int("RAG_REQUEST_MAX_TOKENS", 12000)
    )
    request_max_cost: float = field(
        default_factory=lambda: _env_float("RAG_REQUEST_MAX_COST", 0.05)
    )
    evaluation_max_batch_cost: float = field(
        default_factory=lambda: _env_float("RAG_EVALUATION_MAX_BATCH_COST", 0.5)
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
    kg_graph_version: str = field(
        default_factory=lambda: os.getenv("KG_GRAPH_VERSION", "legacy")
    )
    acl_policy_version: str = field(
        default_factory=lambda: os.getenv("RAG_ACL_POLICY_VERSION", "retrieval-acl-v1")
    )

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
