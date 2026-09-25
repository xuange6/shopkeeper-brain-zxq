"""向量检索节点。"""

from __future__ import annotations

from typing import List, Optional

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.state import QueryGraphState
from knowledge.processor.query_process.nodes.retrieval_plan import channel_enabled
from knowledge.security.access_control import (
    AccessContext,
    build_milvus_access_filter,
    build_milvus_item_filter,
    combine_milvus_filters,
)


class SearchEmbeddingNode(BaseNode):
    """第一路检索：用用户问题直接做 Milvus 混合向量检索。"""

    name = "search_embedding"

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
        "stable_id",
        "document_id",
        "revision_id",
        "source_uri",
        "section_id",
        "block_ids",
        "title_path",
        "page_numbers",
        "citation",
        "parser_name",
        "parser_version",
        "ir_schema_version",
        "has_table",
        "has_image",
    ]

    def process(self, state: QueryGraphState) -> QueryGraphState:
        if not channel_enabled(state, "direct"):
            return {
                "embedding_chunks": [],
                "retrieval_status": {
                    self.name: {"status": "skipped", "reason": "disabled by retrieval plan"}
                },
            }
        query = state.get("rewritten_query") or state.get("original_query") or ""
        item_names = state.get("item_names")
        collection_name = self.config.chunks_collection or "chunks_test"

        if not query:
            return {
                "embedding_chunks": [],
                "retrieval_status": {
                    self.name: {"status": "skipped", "reason": "empty query"}
                },
            }

        try:
            from knowledge.utils.embedding_utils import generate_hybrid_embeddings
            from knowledge.utils.milvus_utils import (
                build_hybrid_search_requests,
                execute_hybrid_search,
                get_milvus_client,
                supported_output_fields,
            )

            self.log_step("step_1", f"查询向量化: {query}")
            embeddings = generate_hybrid_embeddings([query])

            filter_expr = self._build_filter_expr(item_names, state.get("access_context"))
            self.logger.debug("过滤表达式: %s", filter_expr)

            reqs = build_hybrid_search_requests(
                dense_vector=embeddings["dense"][0],
                sparse_vector=embeddings["sparse"][0],
                dense_search_params={"metric_type": "IP"},
                sparse_search_params={"metric_type": "IP"},
                filter_expr=filter_expr,
                top_k=self.config.embedding_search_limit,
            )

            self.log_step("step_2", "执行混合搜索")
            client = get_milvus_client()
            res = execute_hybrid_search(
                client=client,
                collection_name=collection_name,
                search_requests=reqs,
                ranker_weights=self.RANKER_WEIGHTS,
                normalize_score=True,
                top_k=self.config.embedding_search_limit,
                output_fields=supported_output_fields(client, collection_name, self.OUTPUT_FIELDS),
            )

            chunks = res[0] if res else []
            self.log_step("step_3", f"搜索完成，返回 {len(chunks)} 条结果")
            return {"embedding_chunks": chunks}
        except Exception as exc:
            # 多路召回中的单一路故障不应拖垮整个问答流程。
            self.logger.warning("直接向量检索降级为空结果: %s", exc, exc_info=True)
            return {
                "embedding_chunks": [],
                "retrieval_status": {
                    self.name: {"status": "error", "reason": str(exc)[:500]}
                },
            }

    @staticmethod
    def _build_filter_expr(
        item_names: Optional[List[str]],
        access_context: object = None,
    ) -> Optional[str]:
        """Combine product scope and mandatory tenant/ACL pre-filter."""

        return combine_milvus_filters(
            build_milvus_item_filter(item_names),
            build_milvus_access_filter(AccessContext.from_state(access_context)),
        )


_node_instance = SearchEmbeddingNode()


def node_search_embedding(state: QueryGraphState) -> QueryGraphState:
    """兼容函数式调用入口。"""
    return _node_instance(state)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging()

    test_state = {
        "session_id": "test_001",
        "rewritten_query": "如何使用万用表测量电压？",
        "item_names": ["RS-12数字万用表"],
        "embedding_chunks": [],
    }

    result = node_search_embedding(test_state)
    chunks = result.get("embedding_chunks", [])
    print(f"检索到 {len(chunks)} 条结果")
    for index, chunk in enumerate(chunks, 1):
        entity = chunk.get("entity", chunk) if isinstance(chunk, dict) else {}
        print(f"[{index}] {entity.get('item_name', '?')} | {entity.get('chunk_id', 'N/A')}")
