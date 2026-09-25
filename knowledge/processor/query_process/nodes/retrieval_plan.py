"""Versioned retrieval planning and channel routing."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.state import QueryGraphState
from knowledge.security.access_control import AccessContext


_KG_TERMS = re.compile(r"(?:关系|关联|组成|部件|属于|依赖|供应商|上下游|知识图谱)")
_SAFETY_TERMS = re.compile(r"(?:安全|警告|危险|烫伤|高温|触电|冷却|锁定|卡纸)")
_TABLE_TERMS = re.compile(r"(?:表格|参数|规格|边界|上限|下限|重量|尺寸|范围)")
_IMAGE_TERMS = re.compile(r"(?:图片|示意图|位置|外观|面板|指示灯|接线)")


class RetrievalPlanNode(BaseNode):
    """Choose channels before any costly retrieval work is started."""

    name = "retrieval_plan"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        principal = AccessContext.from_state(state.get("access_context"))
        state["access_context"] = principal.to_state()
        state["access_control"] = {
            **principal.audit_summary(),
            "enforcement": "pre_retrieval",
        }
        query = str(
            state.get("rewritten_query")
            or state.get("policy_query")
            or state.get("original_query")
            or ""
        )
        intent = str((state.get("policy_decision") or {}).get("intent") or "business_qa")
        has_product = bool(state.get("item_names"))

        if intent == "freshness":
            hyde_enabled = False
            kg_enabled = False
            web_mode = "required"
        elif intent == "prompt_injection":
            hyde_enabled = self.config.enable_hyde
            kg_enabled = False
            web_mode = "disabled"
        else:
            hyde_enabled = self.config.enable_hyde
            kg_enabled = self.config.enable_knowledge_graph and bool(_KG_TERMS.search(query))
            # Product manuals and safety instructions are local-authority first.
            # Web is only a fallback when local retrieval is actually empty.
            web_mode = "fallback" if self.config.enable_web else "disabled"

        plan: Dict[str, Any] = {
            "version": self.config.retrieval_policy_version,
            "intent": intent,
            "query_features": {
                "has_product_constraint": has_product,
                "safety": bool(_SAFETY_TERMS.search(query)),
                "table": bool(_TABLE_TERMS.search(query)),
                "image": bool(_IMAGE_TERMS.search(query)),
                "freshness": intent == "freshness",
            },
            "channels": {
                "direct": {
                    "enabled": True,
                    "limit": self.config.embedding_search_limit,
                    "timeout_ms": self.config.direct_timeout_ms,
                },
                "hyde": {
                    "enabled": hyde_enabled,
                    "limit": self.config.hyde_search_limit,
                    "timeout_ms": self.config.hyde_timeout_ms,
                },
                "kg": {
                    "enabled": kg_enabled,
                    "limit": self.config.kg_max_total_chunks,
                    "timeout_ms": self.config.kg_timeout_ms,
                },
                "web": {
                    "enabled": web_mode != "disabled",
                    "mode": web_mode,
                    "limit": self.config.web_search_limit,
                    "timeout_ms": self.config.web_timeout_ms,
                    "official_required": bool(
                        intent == "freshness"
                        and self.config.web_freshness_require_official
                    ),
                    "official_domains": self._official_domains(),
                    "official_query_expansion": bool(
                        self.config.web_official_query_expansion
                    ),
                },
            },
            "fusion": {
                "type": "weighted_rrf",
                "k": self.config.rrf_k,
                "weights": {
                    "direct": self.config.rrf_direct_weight,
                    "hyde": self.config.rrf_hyde_weight,
                    "kg": self.config.rrf_kg_weight,
                },
            },
        }
        plan["fingerprint"] = hashlib.sha256(
            json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        state["retrieval_plan"] = plan
        return state

    def _official_domains(self) -> list[str]:
        return list(dict.fromkeys(
            value.strip().lower()
            for value in self.config.web_official_domains.split(",")
            if value.strip()
        ))


def channel_enabled(state: QueryGraphState, channel: str, default: bool = True) -> bool:
    plan = state.get("retrieval_plan") or {}
    channels = plan.get("channels") if isinstance(plan, dict) else {}
    settings = channels.get(channel) if isinstance(channels, dict) else None
    if not isinstance(settings, dict):
        return default
    return bool(settings.get("enabled", False))


_node_instance = RetrievalPlanNode()


def node_retrieval_plan(state: QueryGraphState) -> QueryGraphState:
    return _node_instance(state)
