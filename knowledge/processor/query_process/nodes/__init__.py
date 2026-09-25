"""Query workflow node exports with lazy imports.

Importing one lightweight node for tests or tooling should not eagerly import every
LLM, MCP and graph dependency. Attribute imports remain backward compatible.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AnswerOutputNode": ".answer_output",
    "IntentPolicyNode": ".intent_policy",
    "ItemNameConfirmNode": ".item_name_confirm",
    "QueryKgNode": ".query_kg",
    "RerankNode": ".rerank",
    "RetrievalPlanNode": ".retrieval_plan",
    "RrfNode": ".rrf",
    "SearchEmbeddingNode": ".search_embedding",
    "SearchEmbeddingHydeNode": ".search_embedding_hyde",
    "WebSearchMcpAgentNode": ".web_search_mcp_agent",
    "WebSearchMcpNode": ".web_search_mcp",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
