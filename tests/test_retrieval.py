from __future__ import annotations

import concurrent.futures
import time
import unittest
from unittest.mock import patch

from knowledge.processor.query_process.config import QueryConfig
from knowledge.processor.query_process.nodes.answer_output import AnswerOutputNode
from knowledge.processor.query_process.nodes.rerank import RerankNode
from knowledge.processor.query_process.nodes.rrf import RrfNode
from knowledge.service.query_service import QueryService


class RetrievalTests(unittest.TestCase):
    def test_embedding_model_is_initialized_once_under_concurrency(self) -> None:
        from knowledge.utils import embedding_utils

        sentinel = object()

        def slow_constructor(**_kwargs):
            time.sleep(0.05)
            return sentinel

        with (
            patch.object(embedding_utils, "_bge_m3_model", None),
            patch.object(
                embedding_utils,
                "BGEM3EmbeddingFunction",
                side_effect=slow_constructor,
            ) as constructor,
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor,
        ):
            models = list(executor.map(lambda _index: embedding_utils.get_bge_m3_model(), range(2)))

        self.assertEqual(constructor.call_count, 1)
        self.assertTrue(all(model is sentinel for model in models))

    def test_rrf_merges_duplicate_chunks_and_records_sources(self) -> None:
        results = RrfNode._rrf_merge(
            search_resources={
                "direct": (
                    [
                        {"chunk_id": "a", "content": "A"},
                        {"chunk_id": "b", "content": "B"},
                    ],
                    1.0,
                ),
                "hyde": (
                    [
                        {"chunk_id": "b", "content": "B"},
                        {"chunk_id": "c", "content": "C"},
                    ],
                    1.0,
                ),
            },
            smoothing_factor=60,
            top_n=20,
        )

        self.assertEqual(results[0]["chunk_id"], "b")
        self.assertEqual(set(results[0]["rrf_sources"]), {"direct", "hyde"})
        self.assertEqual(len(results), 3)

    def test_reranker_gracefully_falls_back_to_rrf_order(self) -> None:
        docs = [
            {"chunk_id": "1", "content": "first"},
            {"chunk_id": "2", "content": "second"},
        ]
        node = RerankNode(config=QueryConfig())

        with patch(
            "knowledge.processor.query_process.nodes.rerank.get_reranker_model",
            return_value=None,
        ):
            result = node._rerank_merged_docs("question", docs)

        self.assertEqual([item["chunk_id"] for item in result], ["1", "2"])
        self.assertTrue(all(item["score"] is None for item in result))

    def test_grounding_refuses_empty_or_low_score_context(self) -> None:
        node = AnswerOutputNode(config=QueryConfig(refusal_min_score=0.3))

        self.assertEqual(node._get_refusal_reason({"reranked_docs": []}), "empty_context")
        reason = node._get_refusal_reason(
            {"reranked_docs": [{"content": "weak", "score": 0.1}]}
        )
        self.assertTrue(reason.startswith("top_score_below_"))
        self.assertEqual(
            node._get_refusal_reason(
                {"reranked_docs": [{"content": "strong", "score": 0.8}]}
            ),
            "",
        )

    def test_sources_are_structured_and_content_is_truncated(self) -> None:
        sources = QueryService._build_sources(
            [
                {
                    "source": "local",
                    "chunk_id": 42,
                    "file_title": "manual",
                    "title": "voltage",
                    "score": "0.91234",
                    "content": "x" * 400,
                }
            ]
        )

        self.assertEqual(sources[0]["index"], 1)
        self.assertEqual(sources[0]["chunk_id"], "42")
        self.assertEqual(sources[0]["score"], 0.9123)
        self.assertLessEqual(len(sources[0]["preview"]), 281)

    def test_history_gracefully_degrades_when_mongodb_is_unavailable(self) -> None:
        service = QueryService()
        with patch(
            "knowledge.utils.mongo_history_utils.get_recent_messages",
            side_effect=RuntimeError("mongo offline"),
        ):
            self.assertEqual(service.get_history("session"), [])


if __name__ == "__main__":
    unittest.main()
