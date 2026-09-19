"""HyDE 向量检索节点。"""

from __future__ import annotations

import json
from typing import List, Optional

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.prompt import HYDE_PROMPT_TEMPLATE
from knowledge.processor.query_process.state import QueryGraphState


class SearchEmbeddingHydeNode(BaseNode):
    """第二路检索：先生成假设答案，再用增强文本做 Milvus 混合检索。"""

    name = "search_embedding_hyde"

    SEARCH_TOP_K = 10
    RERANK_TOP_K = 5
    RANKER_WEIGHTS = (0.5, 0.5)
    OUTPUT_FIELDS = [
        "chunk_id",
        "content",
        "item_name",
        "title",
        "parent_title",
        "file_title",
        "part",
    ]

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query = state.get("rewritten_query") or state.get("original_query") or ""
        item_names = state.get("item_names")

        if not query:
            return {
                "hyde_embedding_chunks": [],
                "hyde_doc": "",
                "retrieval_status": {
                    self.name: {"status": "skipped", "reason": "empty query"}
                },
            }

        try:
            self.log_step("step_1", "生成假设性文档")
            hyde_doc = self._generate_hyde_doc(query, state.get("task_id", ""))

            self.log_step("step_2", "执行混合搜索")
            chunks = self._search(query, hyde_doc, item_names)

            self.log_step("step_3", f"搜索完成，返回 {len(chunks)} 条结果")
            return {"hyde_embedding_chunks": chunks, "hyde_doc": hyde_doc}
        except Exception as exc:
            self.logger.error("HyDE 搜索失败: %s", exc)
            return {
                "hyde_embedding_chunks": [],
                "hyde_doc": "",
                "retrieval_status": {
                    self.name: {"status": "error", "reason": str(exc)[:500]}
                },
            }

    def _generate_hyde_doc(self, query: str, trace_id: str = "") -> str:
        """使用 LLM 根据用户查询生成假设性答案文档。"""
        from knowledge.utils.llm_utils import get_llm_client

        llm = get_llm_client(trace_id=trace_id)
        prompt = HYDE_PROMPT_TEMPLATE.format(query=query)
        response = llm.invoke(prompt)
        return str(response.content).strip()

    def _search(
        self,
        query: str,
        hyde_doc: str,
        item_names: Optional[List[str]] = None,
    ) -> List:
        """将查询与假设文档拼接后执行混合检索。"""
        from knowledge.utils.embedding_utils import generate_hybrid_embeddings
        from knowledge.utils.milvus_utils import (
            build_hybrid_search_requests,
            execute_hybrid_search,
            get_milvus_client,
        )

        combined_text = f"{query} {hyde_doc}".strip()
        embeddings = generate_hybrid_embeddings([combined_text])
        filter_expr = self._build_filter_expr(item_names)
        self.logger.debug("过滤表达式: %s", filter_expr)

        reqs = build_hybrid_search_requests(
            dense_vector=embeddings["dense"][0],
            sparse_vector=embeddings["sparse"][0],
            dense_search_params={"metric_type": "IP"},
            sparse_search_params={"metric_type": "IP"},
            filter_expr=filter_expr,
            top_k=self.config.hyde_search_limit,
        )

        res = execute_hybrid_search(
            client=get_milvus_client(),
            collection_name=self.config.chunks_collection or "chunks_test",
            search_requests=reqs,
            ranker_weights=self.RANKER_WEIGHTS,
            normalize_score=True,
            top_k=self.config.hyde_search_limit,
            output_fields=self.OUTPUT_FIELDS,
        )

        return res[0] if res else []

    @staticmethod
    def _build_filter_expr(item_names: Optional[List[str]]) -> Optional[str]:
        """将商品名列表转成 Milvus 标量过滤表达式。"""
        if not item_names:
            return None

        quoted = ", ".join(json.dumps(name, ensure_ascii=False) for name in item_names)
        return f"item_name in [{quoted}]"


_node_instance = SearchEmbeddingHydeNode()


def node_search_embedding_hyde(state: QueryGraphState) -> QueryGraphState:
    """兼容函数式调用入口。"""
    return _node_instance(state)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging()

    test_state = {
        "session_id": "test_001",
        "rewritten_query": "如何使用万用表测量电压？",
        "original_query": "如何使用万用表测量电压？",
        "item_names": ["RS-12数字万用表"],
        "hyde_embedding_chunks": [],
    }

    result = node_search_embedding_hyde(test_state)
    hyde_doc = result.get("hyde_doc", "")
    chunks = result.get("hyde_embedding_chunks", [])
    print(f"假设文档: {hyde_doc[:200]}")
    print(f"检索到 {len(chunks)} 条结果")
    for index, chunk in enumerate(chunks, 1):
        entity = chunk.get("entity", chunk) if isinstance(chunk, dict) else {}
        print(f"[{index}] {entity.get('item_name', '?')} | {entity.get('chunk_id', 'N/A')}")
