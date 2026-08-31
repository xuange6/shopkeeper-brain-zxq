"""Item name confirmation node."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
import json
import logging
import os
import re

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.config import QueryConfig, get_config
from knowledge.processor.query_process.prompt import ITEM_NAME_EXTRACT_TEMPLATE
from knowledge.processor.query_process.state import QueryGraphState, create_default_state


HIGH_CONFIDENCE = 0.63
MID_CONFIDENCE = 0.60
SCORE_GAP_THRESHOLD = 0.15
MAX_OPTIONS = 3


class ItemExtractor:
    """Call LLM to extract item names and rewrite the query."""

    _SYSTEM_PROMPT = "你是一个专业的客服助手，擅长理解用户意图和提取关键信息。"

    def __init__(self, config: QueryConfig | None = None):
        self.config = config or get_config()
        self.logger = logging.getLogger("query.item_extractor")

    def extract(self, query: str, history: List[Dict] | None = None) -> Dict[str, Any]:
        result = {"item_names": [], "rewritten_query": query}
        if not query:
            return result

        model = self.config.item_model or os.getenv("ITEM_MODEL", "")
        if not model:
            self.logger.warning("ITEM_MODEL is not configured; skip item extraction")
            return result

        history_text = self._format_history(history or [])
        prompt = ITEM_NAME_EXTRACT_TEMPLATE.format(
            history_text=history_text or "无历史记录",
            query=query,
        )

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from knowledge.utils.llm_utils import get_llm_client

            llm_client = get_llm_client(model, json_mode=True)
            response = llm_client.invoke(
                [
                    SystemMessage(content=self._SYSTEM_PROMPT),
                    HumanMessage(content=prompt),
                ]
            )
            parsed = self._parse_llm_json(response.content)
            result["item_names"] = parsed["item_names"]
            result["rewritten_query"] = parsed["rewritten_query"] or query
        except Exception as exc:
            self.logger.error("LLM item extraction failed: %s", exc, exc_info=True)

        return result

    @staticmethod
    def _format_history(history: List[Dict]) -> str:
        return "\n".join(
            f"{msg.get('role', 'unknown')}: {msg.get('text', '')}"
            for msg in history
            if msg.get("text")
        )

    @staticmethod
    def _parse_llm_json(llm_response: str) -> Dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", (llm_response or "").strip())
        content = re.sub(r"\s*```$", "", cleaned)

        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("LLM JSON root must be an object")

        raw_items = parsed.get("item_names")
        item_names = (
            [str(name).strip() for name in raw_items if str(name).strip()]
            if isinstance(raw_items, list)
            else []
        )

        raw_query = parsed.get("rewritten_query")
        rewritten_query = str(raw_query).strip() if isinstance(raw_query, str) else ""

        return {"item_names": item_names, "rewritten_query": rewritten_query}


class ItemAligner:
    """Align extracted item names with canonical item names in storage."""

    def __init__(self, config: QueryConfig | None = None):
        self.config = config or get_config()
        self.logger = logging.getLogger("query.item_aligner")

    def match_align(self, item_names: List[str]) -> Tuple[List[str], List[str]]:
        search_results = self._vector_search(item_names)
        confirmed, options = self._align_by_score(search_results)

        if len(confirmed) > 1:
            confirmed = self._filter_by_score_gap(confirmed, search_results)

        return confirmed, options

    def _vector_search(self, item_names: List[str]) -> List[Dict[str, Any]]:
        """Vector search placeholder matching the markdown's expected shape."""
        return [
            {
                "extracted_name": item_name,
                "matches": [{"item_name": item_name, "score": 1.0}],
            }
            for item_name in item_names
            if item_name
        ]

    @staticmethod
    def _align_by_score(search_results: List[Dict]) -> Tuple[List[str], List[str]]:
        confirmed: List[str] = []
        options: List[str] = []

        for res in search_results:
            extracted = (res.get("extracted_name") or "").strip()
            matches = sorted(
                res.get("matches") or [],
                key=lambda match: match.get("score", 0),
                reverse=True,
            )
            if not matches:
                continue

            high = [
                match
                for match in matches
                if match.get("score", 0) > HIGH_CONFIDENCE
            ]

            if high:
                exact = next(
                    (
                        match
                        for match in high
                        if (match.get("item_name") or "").strip() == extracted
                    ),
                    None,
                )
                if exact:
                    picked = exact["item_name"]
                    if picked not in confirmed:
                        confirmed.append(picked)
                elif len(high) == 1:
                    picked = high[0]["item_name"]
                    if picked not in confirmed:
                        confirmed.append(picked)
                else:
                    for match in high[:MAX_OPTIONS]:
                        name = match["item_name"]
                        if name not in confirmed and name not in options:
                            options.append(name)
            else:
                mid = [
                    match
                    for match in matches
                    if match.get("score", 0) >= MID_CONFIDENCE
                    and match.get("item_name") not in confirmed
                    and match.get("item_name") not in options
                ]
                if mid:
                    options.extend(match["item_name"] for match in mid[:MAX_OPTIONS])

        return confirmed, options[:MAX_OPTIONS]

    @staticmethod
    def _filter_by_score_gap(
        confirmed: List[str],
        search_results: List[Dict],
    ) -> List[str]:
        score_map: Dict[str, float] = {}
        for res in search_results:
            for match in res.get("matches") or []:
                name = (match.get("item_name") or "").strip()
                score = match.get("score", 0)
                if name in confirmed:
                    score_map[name] = max(score_map.get(name, 0), score)

        if len(score_map) < 2:
            return confirmed

        sorted_items = sorted(score_map.items(), key=lambda item: item[1], reverse=True)
        top1_score = sorted_items[0][1]
        kept = [
            name
            for name, score in sorted_items
            if top1_score - score <= SCORE_GAP_THRESHOLD
        ]
        return kept


class HistoryService:
    """Read and write chat history.

    MongoDB errors are swallowed here so history persistence never breaks the
    query confirmation flow.
    """

    def __init__(self):
        self.logger = logging.getLogger("query.history")
        self.reference_terms = (
            "这个",
            "那个",
            "它",
            "这款",
            "那款",
            "该产品",
            "这个产品",
            "那个产品",
            "这台",
            "那台",
            "刚才",
            "上面",
            "前面",
            "第一个",
            "第二个",
        )

    def fetch(self, session_id: str, limit: int = 10) -> List[Dict]:
        try:
            from knowledge.utils.mongo_history_utils import get_recent_messages

            return get_recent_messages(session_id, limit=limit)
        except Exception as exc:
            self.logger.warning("fetch history failed: %s", exc)
            return []

    def save_user_message(
        self,
        session_id: str,
        query: str,
        item_names: List[str] | None = None,
    ) -> str:
        return self._save_message(session_id, "user", query, item_names=item_names or [])

    def update_user_message(
        self,
        message_id: str,
        session_id: str,
        query: str,
        rewritten_query: str,
        item_names: List[str],
    ) -> str:
        return self._save_message(
            session_id=session_id,
            role="user",
            text=query,
            rewritten_query=rewritten_query,
            item_names=item_names,
            message_id=message_id,
        )

    def save_assistant_message(
        self,
        session_id: str,
        answer: str,
        item_names: List[str] | None = None,
    ) -> str:
        return self._save_message(
            session_id=session_id,
            role="assistant",
            text=answer,
            item_names=item_names or [],
        )

    def backfill_item_names(
        self,
        history: List[Dict],
        item_names: List[str],
        max_messages: int = 3,
    ) -> None:
        ids_to_update: List[str] = []

        for message in reversed(history):
            if len(ids_to_update) >= max_messages:
                break
            if message.get("role") != "user":
                continue
            if message.get("item_names"):
                break

            text = str(message.get("text") or "")
            if not self._looks_like_reference(text):
                continue
            if message.get("_id"):
                ids_to_update.append(str(message["_id"]))

        if not ids_to_update:
            return

        for message in history:
            if str(message.get("_id")) in ids_to_update:
                message["item_names"] = item_names

        try:
            from knowledge.utils.mongo_history_utils import update_message_item_names

            update_message_item_names(ids_to_update, item_names)
        except Exception as exc:
            self.logger.warning("backfill history item names failed: %s", exc)

    def _looks_like_reference(self, text: str) -> bool:
        return any(term in text for term in self.reference_terms)

    def _save_message(
        self,
        session_id: str,
        role: str,
        text: str,
        rewritten_query: str = "",
        item_names: List[str] | None = None,
        message_id: str = "",
    ) -> str:
        try:
            from knowledge.utils.mongo_history_utils import save_chat_message

            return save_chat_message(
                session_id=session_id,
                role=role,
                text=text,
                rewritten_query=rewritten_query,
                item_names=item_names or [],
                message_id=message_id,
            )
        except Exception as exc:
            self.logger.warning("save history message failed: %s", exc)
            return message_id or ""


class ItemNameConfirmNode(BaseNode):
    """Confirm product names before the query workflow enters retrieval."""

    name = "item_name_confirm"

    def __init__(self, config: QueryConfig | None = None):
        super().__init__(config=config)
        self.extractor = ItemExtractor(self.config)
        self.aligner = ItemAligner(self.config)
        self.history_service = HistoryService()

    def process(self, state: QueryGraphState) -> QueryGraphState:
        session_id = state.get("session_id", "")
        query = state.get("original_query", "")

        history = self.history_service.fetch(session_id)
        message_id = self.history_service.save_user_message(
            session_id=session_id,
            query=query,
            item_names=state.get("item_names", []),
        )
        if message_id:
            state["message_id"] = message_id

        extracted = self.extractor.extract(query, history)
        item_names = extracted.get("item_names", [])
        rewritten_query = extracted.get("rewritten_query", query)

        if item_names:
            confirmed, options = self.aligner.match_align(item_names)
        else:
            confirmed, options = [], []

        self._decide(state, confirmed, options, rewritten_query, history)
        self._write_history(state, session_id, query, rewritten_query, message_id)
        state["history"] = self.history_service.fetch(session_id)
        return state

    def _decide(
        self,
        state: QueryGraphState,
        confirmed: List[str],
        options: List[str],
        rewritten_query: str,
        history: List[Dict],
    ) -> None:
        if confirmed:
            self.history_service.backfill_item_names(history, confirmed)
            state["item_names"] = confirmed
            state["rewritten_query"] = rewritten_query
            state["answer"] = ""
        elif options:
            state["answer"] = (
                f"我不确定您指的是哪款产品。您是在询问以下产品吗：{'、'.join(options)}？"
            )
        else:
            state["answer"] = (
                "抱歉，我无法识别您询问的具体产品名称，请提供更准确的产品名称或型号。"
            )

        state["history"] = history

    def _write_history(
        self,
        state: QueryGraphState,
        session_id: str,
        query: str,
        rewritten_query: str,
        message_id: str,
    ) -> None:
        item_names = state.get("item_names") or []
        if message_id:
            self.history_service.update_user_message(
                message_id=message_id,
                session_id=session_id,
                query=query,
                rewritten_query=rewritten_query,
                item_names=item_names,
            )

        answer = (state.get("answer") or "").strip()
        if answer:
            self.history_service.save_assistant_message(
                session_id=session_id,
                answer=answer,
                item_names=item_names,
            )


_node_instance = ItemNameConfirmNode()


def node_item_name_confirm(state: QueryGraphState) -> QueryGraphState:
    return _node_instance(state)


if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    test_state = create_default_state(
        session_id="test_123",
        original_query="你们店里那款苏伯尔RS-12数字万用表怎么测电压？",
        item_names=[],
        rewritten_query="",
        answer="",
        history=[],
    )
    result = node_item_name_confirm(test_state)
    print(json.dumps(result, ensure_ascii=False, indent=2))
