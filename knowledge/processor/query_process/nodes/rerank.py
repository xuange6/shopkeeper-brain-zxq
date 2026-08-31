"""
嵌入检索、HyDE 检索、KG 检索、MCP 检索之后：
   ---- rrf：多路检索融合打分排序，利用 RRF 公式做 sorted 降序排序
   ---- rerank：重新进行精排打分，前面多路召回已经完成，这里只管精排
"""

from __future__ import annotations

from typing import Any, Dict, List

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.state import QueryGraphState
from knowledge.utils.bge_rerank_util import get_reranker_model


class RerankNode(BaseNode):
    name = "rerank_node"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        # 1. 获取 query
        user_query = state.get("rewritten_query", "") or state.get("original_query", "")

        # 2. 合并多源文档
        merged_multi_docs = self._deduplicate_docs(self._merge_multi_source_docs(state))

        # 3. Rerank 精排（精排打分）
        reranked_docs = self._rerank_merged_docs(user_query, merged_multi_docs)

        # 4. 动态 Top_K 截取（断崖检测）
        cutoff_docs = self._cliff_cutoff(reranked_docs)

        state["reranked_docs"] = cutoff_docs
        return state

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
            current_score = ranked_docs[index].get("score")
            next_score = ranked_docs[index + 1].get("score")

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
