"""State definitions for the query workflow."""

from typing import Annotated, Any, Dict, List, TypedDict
import copy


def merge_node_timings(
    left: Dict[str, Any] | None,
    right: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """合并并行节点写入的耗时信息。

    查询流程里向量检索、HyDE、知识图谱、联网检索是并行执行的。
    它们都会写 node_timings，如果不提供 reducer，LangGraph 会报
    INVALID_CONCURRENT_GRAPH_UPDATE。
    """

    merged: Dict[str, Any] = {}
    if isinstance(left, dict):
        merged.update(left)
    if isinstance(right, dict):
        merged.update(right)
    return merged


def merge_dict_fields(
    left: Dict[str, Any] | None,
    right: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Merge diagnostic dictionaries written by parallel retrieval nodes."""

    merged: Dict[str, Any] = {}
    if isinstance(left, dict):
        merged.update(left)
    if isinstance(right, dict):
        merged.update(right)
    return merged


class QueryGraphState(TypedDict, total=False):
    """Runtime state passed between query workflow nodes."""

    # Conversation identity
    task_id: str
    session_id: str
    message_id: str

    # Input and conversation context
    original_query: str
    policy_query: str
    rewritten_query: str
    history: List
    item_names: List[str]
    access_context: Dict[str, Any]
    access_control: Dict[str, Any]
    web_search_diagnostics: Dict[str, Any]

    # Retrieval results
    embedding_chunks: List
    hyde_embedding_chunks: List
    hyde_doc: str
    kg_chunks: List
    kg_triples: List
    kg_seed_nodes: List
    kg_triples_raw: List
    kg_entities: List[str]
    kg_aligned_entities: List[str]
    kg_alignments: List
    web_search_docs: List

    # Fusion and answer generation
    rrf_chunks: List
    reranked_docs: List
    prompt: str
    answer: str
    image_urls: List[str]
    sources: List[Dict[str, Any]]
    claims: List[Dict[str, Any]]

    # Control and diagnostics
    answer_behavior: str
    policy_decision: Dict[str, Any]
    context_security: Dict[str, Any]
    output_security: Dict[str, Any]
    retrieval_plan: Dict[str, Any]
    evidence_decision: Dict[str, Any]
    constraint_completion: Dict[str, Any]
    citation_verification: Dict[str, Any]
    retrieval_trace_events: List[Dict[str, Any]]
    is_stream: bool
    node_timings: Annotated[Dict[str, Any], merge_node_timings]
    retrieval_status: Annotated[Dict[str, Any], merge_dict_fields]


DEFAULT_STATE: QueryGraphState = {
    "task_id": "",
    "session_id": "",
    "message_id": "",
    "original_query": "",
    "policy_query": "",
    "rewritten_query": "",
    "history": [],
    "item_names": [],
    "access_context": {},
    "access_control": {},
    "web_search_diagnostics": {},
    "embedding_chunks": [],
    "hyde_embedding_chunks": [],
    "hyde_doc": "",
    "kg_chunks": [],
    "kg_triples": [],
    "kg_seed_nodes": [],
    "kg_triples_raw": [],
    "kg_entities": [],
    "kg_aligned_entities": [],
    "kg_alignments": [],
    "web_search_docs": [],
    "rrf_chunks": [],
    "reranked_docs": [],
    "prompt": "",
    "answer": "",
    "image_urls": [],
    "sources": [],
    "claims": [],
    "answer_behavior": "",
    "policy_decision": {},
    "context_security": {},
    "output_security": {},
    "retrieval_plan": {},
    "evidence_decision": {},
    "constraint_completion": {},
    "citation_verification": {},
    "retrieval_trace_events": [],
    "is_stream": False,
    "node_timings": {},
    "retrieval_status": {},
}


def create_default_state(**overrides) -> QueryGraphState:
    state = copy.deepcopy(DEFAULT_STATE)
    state.update(overrides)
    return state


def get_default_state() -> QueryGraphState:
    return copy.deepcopy(DEFAULT_STATE)
