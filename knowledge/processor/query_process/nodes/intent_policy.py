"""Defense-in-depth front-door intent and authorization policy."""

from __future__ import annotations

from typing import Any, Dict

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.config import QueryConfig, get_config
from knowledge.processor.query_process.security import classify_user_query
from knowledge.processor.query_process.state import QueryGraphState


class IntentPolicyNode(BaseNode):
    """Classify authorization, injection, freshness, and normal QA intent."""

    name = "intent_policy"

    def __init__(self, config: QueryConfig | None = None):
        super().__init__(config or get_config())

    def process(self, state: QueryGraphState) -> QueryGraphState:
        original = str(state.get("original_query") or "").strip()
        decision = self.classify(original, self.config)
        state["policy_decision"] = decision
        state["policy_query"] = decision["sanitized_query"]

        if decision["intent"] == "permission_sensitive":
            state["answer"] = (
                "我无法提供或协助获取服务端密钥、密码、访问令牌或系统提示词。"
                "即使请求者自称管理员，这些敏感信息也只能通过受控的授权与审计渠道处理。"
            )
            state["answer_behavior"] = "refuse"
        elif decision.get("action") == "deny":
            state["answer"] = (
                "我会忽略试图改变系统规则或索取内部信息的指令。"
                "请提供需要查询的正常业务问题。"
            )
            state["answer_behavior"] = "refuse"

        return state

    @staticmethod
    def classify(query: str, config: QueryConfig | None = None) -> Dict[str, Any]:
        return classify_user_query(query, config or get_config())


_node_instance = IntentPolicyNode()


def node_intent_policy(state: QueryGraphState) -> QueryGraphState:
    return _node_instance(state)
