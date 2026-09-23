"""答案生成节点。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.prompt import ANSWER_PROMPT
from knowledge.processor.query_process.state import QueryGraphState
from knowledge.utils.sse_util import SSEEvent, push_sse_event
from knowledge.utils.query_result_utils import (
    build_query_diagnostics,
    build_source_references,
)
from knowledge.utils.task_utils import set_task_result


class AnswerOutputNode(BaseNode):
    name = "answer_output_node"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        task_id = state.get("task_id", "")
        is_stream = bool(state.get("is_stream"))

        # 1. 前面节点已经给出答案时，直接复用，不再调用大模型。
        if state.get("answer"):
            self._push_existing_answer(state)

        # 2. 没有现成答案时，组装提示词并调用 LLM。
        else:
            refusal_reason = self._get_refusal_reason(state)
            if refusal_reason:
                self.logger.info("触发知识库拒答: %s", refusal_reason)
                state["answer"] = (
                    "当前知识库没有检索到足够可靠的依据来回答这个问题。"
                    "你可以换一种问法、补充具体产品名称，或先导入相关资料。"
                )
            else:
                prompt = self._build_prompt(state)
                state["prompt"] = prompt
                self._generate_answer(state, prompt)

        state["sources"] = build_source_references(state.get("reranked_docs") or [])
        proposed_images = self._extract_image_urls(state.get("answer", ""))
        cited_indices = {int(value) for value in re.findall(r"\[(\d+)\]", state.get("answer", ""))}
        supported_images = {
            url for source in state["sources"] if source["index"] in cited_indices
            for url in source.get("image_urls", [])
        }
        state["image_urls"] = [url for url in proposed_images if url in supported_images]
        if len(state["image_urls"]) != len(proposed_images):
            self.logger.warning("Omitted %d image references without cited source assets", len(proposed_images) - len(state["image_urls"]))

        # 3. 写入历史会话，保存用户问题和助手回答。
        self._write_history(state)

        # 4. 保存答案结果；流式 final 由 QueryService 在整个图完成后统一推送。
        answer = state.get("answer", "")
        if not is_stream:
            set_task_result(task_id, "answer", answer)

        return state

    def _get_refusal_reason(self, state: QueryGraphState) -> str:
        """在无证据或所有精排证据都过低时拒答，降低幻觉风险。"""

        docs = [doc for doc in state.get("reranked_docs") or [] if isinstance(doc, dict)]
        graph_evidence = state.get("kg_triples") or []
        if not docs and not graph_evidence:
            return "empty_context"

        numeric_scores: List[float] = []
        for doc in docs:
            try:
                if doc.get("score") is not None:
                    numeric_scores.append(float(doc["score"]))
            except (TypeError, ValueError):
                continue

        if (
            numeric_scores
            and not graph_evidence
            and max(numeric_scores) < self.config.refusal_min_score
        ):
            return f"top_score_below_{self.config.refusal_min_score}"
        return ""

    def _push_existing_answer(self, state: QueryGraphState) -> None:
        """已有答案时的兼容处理。"""

        self.logger.debug("已有答案，跳过 LLM 生成")

    def _generate_answer(self, state: QueryGraphState, prompt: str) -> None:
        self.log_step("generate", "生成答案")

        try:
            from knowledge.utils.llm_utils import get_llm_client

            llm_client = get_llm_client(trace_id=state.get("task_id", ""))
        except Exception as exc:
            self.logger.error("LLM 客户端初始化失败: %s", exc)
            state["answer"] = "抱歉，LLM 客户端初始化失败，暂时无法生成回答。"
            return

        if state.get("is_stream"):
            state["answer"] = self._stream_generate(
                llm_client,
                prompt,
                state.get("task_id", ""),
            )
        else:
            state["answer"] = self._invoke_generate(llm_client, prompt)

    def _build_prompt(self, state: QueryGraphState) -> str:
        char_budget = self.config.max_context_chars

        # 1. 获取用户问题和商品名。
        question = state.get("rewritten_query") or state.get("original_query", "")
        item_names = state.get("item_names") or []

        # 2. 格式化重排序后的文档。
        context_str, char_budget = self._format_reranked_docs(
            state.get("reranked_docs") or [],
            char_budget,
        )

        # 3. 格式化历史对话。
        history_str, char_budget = self._format_chat_history(
            state.get("history") or [],
            char_budget,
        )

        # 4. 格式化知识图谱三元组。
        graph_str, char_budget = self._format_kg_triples(
            state.get("kg_triples") or [],
            char_budget,
        )

        # 5. 填充最终提示词模板。
        return ANSWER_PROMPT.format(
            context=context_str or "无参考内容",
            history=history_str or "暂无历史对话",
            item_names=", ".join(str(name) for name in item_names)
            if item_names
            else "无指定商品",
            graph_relation_description=graph_str or "无图谱关系",
            question=question,
        )

    def _format_chat_history(
        self,
        chat_history: List[Dict[str, Any]],
        char_budget: int,
    ) -> Tuple[str, int]:
        formatted_lines: List[str] = []
        used_chars = 0
        role_label_map = {"user": "用户", "assistant": "助手"}

        for message in chat_history:
            if not isinstance(message, dict):
                continue

            role = message.get("role", "")
            text = self._clean_text(message.get("text"))
            if not text or role not in role_label_map:
                continue

            formatted_line = f"{role_label_map[role]}: {text}"
            if used_chars + len(formatted_line) > char_budget:
                break

            formatted_lines.append(formatted_line)
            used_chars += len(formatted_line) + 1

        return "\n".join(formatted_lines), char_budget - used_chars

    def _format_reranked_docs(
        self,
        reranked_docs: List[Dict[str, Any]],
        char_budget: int,
    ) -> Tuple[str, int]:
        formatted_lines: List[str] = []
        used_chars = 0

        for index, doc in enumerate(reranked_docs, 1):
            if not isinstance(doc, dict):
                continue

            content = self._clean_text(doc.get("content"))
            if not content:
                continue

            # 1. 构建元信息标签，方便模型知道每段内容的来源。
            meta_tags = [f"[{index}]"]
            for field, template in [
                ("source", "[source={}]"),
                ("chunk_id", "[chunk_id={}]"),
                ("url", "[url={}]"),
                ("title", "[title={}]"),
                ("file_title", "[file={}]"),
                ("parent_title", "[section={}]"),
            ]:
                field_value = self._clean_text(doc.get(field))
                if field_value:
                    meta_tags.append(template.format(field_value))

            # 2. 加入 rerank 相关性分数。
            relevance_score = doc.get("score")
            if relevance_score is not None:
                try:
                    meta_tags.append(f"[score={float(relevance_score):.4f}]")
                except (TypeError, ValueError):
                    meta_tags.append(f"[score={relevance_score}]")

            # 3. 拼接元信息和正文，并控制总上下文长度。
            doc_entry = " ".join(meta_tags) + "\n" + content
            if used_chars + len(doc_entry) > char_budget:
                break

            formatted_lines.append(doc_entry)
            used_chars += len(doc_entry) + 2

        return "\n\n".join(formatted_lines), char_budget - used_chars

    @staticmethod
    def _format_kg_triples(
        kg_triples: List[Any],
        char_budget: int,
    ) -> Tuple[str, int]:
        formatted_lines: List[str] = []
        used_chars = 0

        for triple in kg_triples:
            triple_text = (str(triple) if triple is not None else "").strip()
            if not triple_text:
                continue
            if used_chars + len(triple_text) > char_budget:
                break

            formatted_lines.append(triple_text)
            used_chars += len(triple_text) + 1

        return "\n".join(formatted_lines), char_budget - used_chars

    def _invoke_generate(self, llm_client: Any, prompt: str) -> str:
        self.log_step("generate", "生成答案")
        try:
            response = llm_client.invoke(prompt)
            return str(getattr(response, "content", response) or "")
        except Exception as exc:
            self.logger.error("生成回答出错: %s", exc)
            return "抱歉，生成回答时出现错误。"

    def _stream_generate(self, llm_client: Any, prompt: str, task_id: str) -> str:
        """流式生成：每拿到一个 chunk，就通过 SSE 推送 delta 事件。"""

        accumulated_answer = ""
        try:
            for chunk in llm_client.stream(prompt):
                delta_text = getattr(chunk, "content", "") or ""
                if not delta_text:
                    continue

                accumulated_answer += delta_text
                push_sse_event(task_id, SSEEvent.DELTA, {"delta": delta_text})
        except Exception as exc:
            self.logger.error("流式生成出错: %s", exc)

        return accumulated_answer

    @staticmethod
    def _extract_image_urls(answer: str) -> List[str]:
        """兼容旧提示词图片区块，同时向 API 提供结构化图片数组。"""

        if "【图片】" not in answer:
            return []
        image_block = answer.split("【图片】", 1)[1]
        urls = re.findall(r"https?://[^\s<>]+", image_block)
        return list(dict.fromkeys(url.rstrip("，。);）]") for url in urls))

    def _write_history(self, state: QueryGraphState) -> None:
        session_id = state.get("session_id", "")
        rewritten_query = state.get("rewritten_query", "") or state.get(
            "original_query",
            "",
        )
        item_names = state.get("item_names") or []

        if not session_id:
            return

        try:
            from knowledge.utils.mongo_history_utils import save_chat_message

            # 1. 写用户问题；如果前置节点已经保存过 message_id，则更新该记录。
            save_chat_message(
                session_id=session_id,
                role="user",
                text=state.get("original_query", ""),
                rewritten_query=rewritten_query,
                item_names=item_names,
                message_id=state.get("message_id", ""),
            )

            # 2. 写助手回答。
            if state.get("answer"):
                save_chat_message(
                    session_id=session_id,
                    role="assistant",
                    text=state.get("answer", ""),
                    rewritten_query=rewritten_query,
                    item_names=item_names,
                    image_urls=state.get("image_urls") or [],
                    sources=state.get("sources") or [],
                    diagnostics=build_query_diagnostics(
                        state.get("task_id", ""),
                        state,
                    ),
                )
        except Exception as exc:
            self.logger.warning("写入历史记录失败: %s", exc)

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value).strip() if value is not None else ""
