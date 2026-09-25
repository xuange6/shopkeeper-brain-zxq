"""
嵌入检索、HyDE 检索、KG 检索、MCP 检索之后：
   ---- rrf：多路检索融合打分排序，利用 RRF 公式做 sorted 降序排序
   ---- rerank：重新进行精排打分，前面多路召回已经完成，这里只管精排
"""

from __future__ import annotations

from typing import Any, Dict, List

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.evidence import (
    annotate_evidence,
    build_evidence_decision,
    canonical_evidence_id,
)
from knowledge.processor.query_process.security import inspect_untrusted_context
from knowledge.processor.query_process.state import QueryGraphState
from knowledge.utils.bge_rerank_util import get_reranker_model


class RerankNode(BaseNode):
    name = "rerank_node"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        # 1. 获取 query
        user_query = state.get("rewritten_query", "") or state.get("original_query", "")

        # 2. 合并多源文档
        merged_multi_docs = self._deduplicate_docs(self._merge_multi_source_docs(state))
        safe_docs: List[Dict[str, Any]] = []
        security_blocked: List[Dict[str, Any]] = []
        for doc in merged_multi_docs:
            audit = inspect_untrusted_context(doc.get("content", ""), self.config)
            if self.config.security_context_guard_enabled and audit["blocked"]:
                security_blocked.append({
                    "chunk_id": str(doc.get("chunk_id") or ""),
                    "url": str(doc.get("url") or ""),
                    "source": str(doc.get("source") or ""),
                    "matched_rules": list(audit.get("matched_rules") or []),
                })
                continue
            safe_docs.append(doc)
        state["context_security"] = {
            "version": "untrusted-context-guard-v1",
            "enabled": bool(self.config.security_context_guard_enabled),
            "retrieval_candidates": {
                "inspected_count": len(merged_multi_docs),
                "blocked_count": len(security_blocked),
                "blocked": security_blocked,
            },
            "blocked_count": len(security_blocked),
        }

        # 3. Rerank 精排（精排打分）
        reranked_docs, rerank_status = self._rerank_with_status(
            user_query, safe_docs
        )

        # 4. Keep the model's raw score for diagnostics, but calibrate it before
        # source-aware ranking or refusal. A negative raw logit is not a
        # probability and must never be compared directly with a refusal limit.
        plan = state.get("retrieval_plan") or {}
        features = plan.get("query_features") if isinstance(plan, dict) else {}
        calibrated_docs = [
            annotate_evidence(doc, user_query, features or {}, self.config)
            for doc in reranked_docs
        ]
        calibrated_docs.sort(
            key=lambda item: (
                -float(item.get("ranking_score") or 0.0),
                -float(item.get("calibrated_relevance") or 0.0),
                str(item.get("evidence_group_id") or ""),
            )
        )

        # 5. Canonical evidence grouping removes duplicate fragments created by
        # different chunking policies without recreating old duplicate chunks.
        grouped_docs, dropped = self._deduplicate_evidence_groups(calibrated_docs)

        # 6. Dynamic cutoff operates on calibrated ranking scores.
        cutoff_docs = self._cliff_cutoff(grouped_docs)[: self.config.answer_max_evidence]
        state["evidence_decision"] = build_evidence_decision(
            cutoff_docs,
            state.get("kg_triples") or [],
            user_query,
            self.config,
        )
        state["retrieval_trace_events"] = self._build_trace_events(
            safe_docs, reranked_docs, cutoff_docs, dropped, security_blocked
        )

        state["reranked_docs"] = cutoff_docs
        rerank_status = {
            **rerank_status,
            "raw_score_distribution": self._score_distribution(reranked_docs),
            "selected_count": len(cutoff_docs),
            "canonical_duplicates_removed": len(dropped),
            "security_candidates_removed": len(security_blocked),
        }
        state["retrieval_status"] = {
            **(state.get("retrieval_status") or {}),
            self.name: rerank_status,
        }
        return state

    def _rerank_with_status(
        self,
        user_query: str,
        merged_multi_docs: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        if not merged_multi_docs:
            return [], {"status": "ok", "reason": "no candidates"}

        rerank_model = get_reranker_model()
        if rerank_model is None:
            self.logger.warning("重排序模型不可用，按 RRF/Web 原顺序降级")
            return (
                [{**doc, "score": None} for doc in merged_multi_docs],
                {"status": "degraded", "reason": "reranker unavailable"},
            )

        pairs = [(user_query, doc.get("content")) for doc in merged_multi_docs]
        try:
            scores = rerank_model.compute_score(sentence_pairs=pairs)
            ranked = [
                {**doc, "score": score}
                for doc, score in zip(merged_multi_docs, scores)
            ]
            return (
                sorted(ranked, key=lambda item: item["score"], reverse=True),
                {"status": "ok", "reason": ""},
            )
        except Exception as exc:
            self.logger.error("Rerank 重排序失败：%s", exc)
            return (
                [{**doc, "score": None} for doc in merged_multi_docs],
                {"status": "error", "reason": str(exc)[:500]},
            )

    def _cliff_cutoff(
        self,
        ranked_docs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """断崖检测截断：相邻得分差距超过阈值时截断。"""
        if not ranked_docs:
            return []

        upper_bound = min(self.config.rerank_max_top_k, len(ranked_docs))
        lower_bound = min(self.config.rerank_min_top_k, upper_bound)

        cutoff_pos = upper_bound
        for index in range(lower_bound - 1, upper_bound - 1):
            current_score = ranked_docs[index].get("ranking_score")
            next_score = ranked_docs[index + 1].get("ranking_score")

            if current_score is None or next_score is None:
                continue

            abs_gap = current_score - next_score  # 绝对差距
            rel_gap = abs_gap / (abs(current_score) + 1e-6)

            if (
                abs_gap >= self.config.rerank_gap_abs
                or rel_gap >= self.config.rerank_gap_ratio
            ):
                cutoff_pos = index + 1
                self.logger.debug(
                    "Cliff detected at %d, abs_gap=%.4f, rel_gap=%.4f",
                    index + 1,
                    abs_gap,
                    rel_gap,
                )
                break

        return ranked_docs[:cutoff_pos]

    def _merge_multi_source_docs(self, state: QueryGraphState) -> List[Dict[str, Any]]:
        """
        合并本地 RRF 文档和 Web 远程文档，统一成 Rerank 输入结构。

        Args:
            state: 查询流程状态

        Returns:
            准备进入 Rerank 精排的文档列表
        """
        final_docs: List[Dict[str, Any]] = []

        # 1. 获取本地 RRF 的文档
        for rrf_doc in state.get("rrf_chunks") or []:
            # 1.1 判断当前文档对象类型
            if not isinstance(rrf_doc, dict):
                continue

            # 1.2 获取文档内容
            content = self._clean_text(rrf_doc.get("content"))
            # 1.3 判断文档内容
            if not content:
                continue

            title = self._clean_text(rrf_doc.get("title"))
            chunk_id = rrf_doc.get("chunk_id")
            # 1.4 格式化本地 RRF 的 chunk 结构
            format_rrf_doc = self._format_rrf_docs(
                content=content,
                title=title,
                chunk_id=chunk_id,
                source="local",
                metadata=rrf_doc,
            )
            final_docs.append(format_rrf_doc)

        # 2. 获取 Web 远程的文档
        for web_doc in state.get("web_search_docs") or []:
            # 2.1 判断当前文档对象类型
            if not isinstance(web_doc, dict):
                continue

            # 2.2 获取文档内容（content / snippet）
            content = self._clean_text(web_doc.get("content")) or self._clean_text(
                web_doc.get("snippet")
            )
            # 2.3 判断内容
            if not content:
                continue

            title = self._clean_text(web_doc.get("title"))
            url = self._clean_text(web_doc.get("url"))
            # 2.4 格式化 Web 的文档结构
            format_web_doc = self._format_rrf_docs(
                content=content,
                title=title,
                url=url,
                source="web",
                metadata=web_doc,
            )
            final_docs.append(format_web_doc)

        self.logger.info("收集到准备进行 Rerank 精排的文档：%d", len(final_docs))
        return final_docs

    @staticmethod
    def _format_rrf_docs(
        content: str,
        title: str = "",
        chunk_id: Any = None,
        url: str = "",
        source: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return {
            **(metadata or {}),
            "content": content,
            "title": title,
            "chunk_id": chunk_id,
            "url": url,
            "source": source,
        }

    @staticmethod
    def _deduplicate_docs(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在精排前去掉重复证据，避免同一段内容挤占上下文窗口。"""

        unique_docs: List[Dict[str, Any]] = []
        seen = set()
        for doc in docs:
            identity = (
                str(doc.get("chunk_id") or "").strip()
                or str(doc.get("url") or "").strip()
                or " ".join(str(doc.get("content") or "").lower().split())[:240]
            )
            if not identity or identity in seen:
                continue
            seen.add(identity)
            unique_docs.append(doc)
        return unique_docs

    @staticmethod
    def _deduplicate_evidence_groups(
        docs: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        selected: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for doc in docs:
            group_id = str(doc.get("evidence_group_id") or canonical_evidence_id(doc))
            doc["evidence_group_id"] = group_id
            if group_id in seen:
                dropped.append(doc)
                continue
            seen.add(group_id)
            selected.append(doc)
        return selected, dropped

    @staticmethod
    def _score_distribution(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
        scores = [
            float(doc["score"])
            for doc in docs
            if doc.get("score") is not None
        ]
        if not scores:
            return {"count": 0, "min": None, "max": None, "mean": None}
        return {
            "count": len(scores),
            "min": round(min(scores), 6),
            "max": round(max(scores), 6),
            "mean": round(sum(scores) / len(scores), 6),
        }

    @staticmethod
    def _build_trace_events(
        merged: List[Dict[str, Any]],
        raw_ranked: List[Dict[str, Any]],
        selected: List[Dict[str, Any]],
        duplicates: List[Dict[str, Any]],
        security_blocked: List[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        input_rank = {
            canonical_evidence_id(doc) + ":" + str(doc.get("chunk_id") or doc.get("url") or ""): rank
            for rank, doc in enumerate(merged, 1)
        }
        raw_rank = {
            canonical_evidence_id(doc) + ":" + str(doc.get("chunk_id") or doc.get("url") or ""): rank
            for rank, doc in enumerate(raw_ranked, 1)
        }
        selected_keys = {
            canonical_evidence_id(doc) + ":" + str(doc.get("chunk_id") or doc.get("url") or ""): rank
            for rank, doc in enumerate(selected, 1)
        }
        duplicate_keys = {
            canonical_evidence_id(doc) + ":" + str(doc.get("chunk_id") or doc.get("url") or "")
            for doc in duplicates
        }
        events = []
        for doc in raw_ranked:
            key = canonical_evidence_id(doc) + ":" + str(doc.get("chunk_id") or doc.get("url") or "")
            events.append(
                {
                    "chunk_id": str(doc.get("chunk_id") or ""),
                    "url": str(doc.get("url") or ""),
                    "evidence_group_id": canonical_evidence_id(doc),
                    "input_rank": input_rank.get(key),
                    "raw_rerank_rank": raw_rank.get(key),
                    "final_rank": selected_keys.get(key),
                    "decision": (
                        "selected" if key in selected_keys
                        else "canonical_duplicate" if key in duplicate_keys
                        else "cutoff"
                    ),
                }
            )
        for blocked in security_blocked or []:
            events.append({
                "chunk_id": str(blocked.get("chunk_id") or ""),
                "url": str(blocked.get("url") or ""),
                "evidence_group_id": "",
                "input_rank": None,
                "raw_rerank_rank": None,
                "final_rank": None,
                "decision": "security_filter",
                "filter_reason": "untrusted_instruction_in_evidence",
                "matched_rules": list(blocked.get("matched_rules") or []),
            })
        return events

    def _rerank_merged_docs(
        self,
        user_query: str,
        merged_multi_docs: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Args:
            user_query: 用户输入的查询问题
            merged_multi_docs: 不同来源合并之后的文档（all_in_docs）

        Returns:
            Rerank 精排后的文档列表
        """
        # 1. 判断合并后的多源文档是否存在
        if not merged_multi_docs:
            return []

        # 2. 获取 reranker 模型
        rerank_model = get_reranker_model()
        if rerank_model is None:
            self.logger.warning("重排序模型不可用，按 RRF/Web 原顺序降级")
            return [{**doc, "score": None} for doc in merged_multi_docs]

        # 3. 构建 Q -> D 的 pair 对：[(Q, D1[content]), (Q, D2[content]), ...]
        query_doc_content_pairs = [
            (user_query, doc.get("content")) for doc in merged_multi_docs
        ]

        try:
            # 4. 计算 rerank 分数
            rerank_scores = rerank_model.compute_score(
                sentence_pairs=query_doc_content_pairs
            )

            # 5. 映射分数和文档
            score_doc = [
                {**doc, "score": score}
                for doc, score in zip(merged_multi_docs, rerank_scores)
            ]
            # 6. 排序并返回
            sorted_score_docs = sorted(
                score_doc,
                key=lambda item: item["score"],
                reverse=True,
            )
            return sorted_score_docs
        except Exception as exc:
            self.logger.error("Rerank 重排序失败：%s", exc)
            return [{**doc, "score": None} for doc in merged_multi_docs]

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value).strip() if value is not None else ""


_node_instance = RerankNode()


def node_rerank(state: QueryGraphState) -> QueryGraphState:
    return _node_instance(state)
