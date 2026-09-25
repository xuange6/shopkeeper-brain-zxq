from __future__ import annotations

import concurrent.futures
from threading import Lock
import time
import unittest
from unittest.mock import patch

from knowledge.processor.query_process.config import QueryConfig
from knowledge.processor.query_process.nodes.answer_output import AnswerOutputNode
from knowledge.processor.query_process.nodes.rerank import RerankNode
from knowledge.processor.query_process.nodes.rrf import RrfNode
from knowledge.service.query_service import QueryService


class RetrievalTests(unittest.TestCase):
    def test_milvus_output_fields_respect_old_and_new_schema(self) -> None:
        from knowledge.utils.milvus_utils import supported_output_fields

        class Client:
            def __init__(self, fields):
                self.fields = fields

            def describe_collection(self, collection_name):
                return {"fields": [{"name": field} for field in self.fields]}

        requested = ["chunk_id", "content", "title", "stable_id", "page_numbers"]
        old = Client(["chunk_id", "content", "title"])
        new = Client(requested)
        self.assertEqual(
            supported_output_fields(old, "old", requested),
            ["chunk_id", "content", "title"],
        )
        self.assertEqual(supported_output_fields(new, "new", requested), requested)
        with self.assertRaisesRegex(ValueError, "required retrieval fields"):
            supported_output_fields(Client(["title"]), "broken", requested)

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

    def test_shared_embedding_model_is_not_encoded_concurrently(self) -> None:
        from knowledge.utils import embedding_utils

        guard = Lock()
        active = 0
        maximum_active = 0

        class DenseVector:
            def tolist(self):
                return [0.1, 0.2]

        class FakeModel:
            def encode_queries(self, texts):
                nonlocal active, maximum_active
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(0.02)
                with guard:
                    active -= 1
                return {"dense": [DenseVector() for _ in texts], "sparse": object()}

        with (
            patch.object(embedding_utils, "get_bge_m3_model", return_value=FakeModel()),
            patch.object(embedding_utils, "_extract_sparse_vectors", return_value=[{1: 0.5}]),
            concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor,
        ):
            outputs = list(executor.map(
                lambda _index: embedding_utils.generate_hybrid_embeddings(["manual query"]),
                range(3),
            ))

        self.assertEqual(maximum_active, 1)
        self.assertEqual(len(outputs), 3)

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

    def test_grounding_requires_calibrated_evidence_decision(self) -> None:
        node = AnswerOutputNode(config=QueryConfig(refusal_min_score=0.3))

        self.assertEqual(node._get_refusal_reason({"reranked_docs": []}), "empty_context")
        self.assertEqual(
            node._get_refusal_reason(
                {"reranked_docs": [{"content": "weak", "score": 0.1}]}
            ),
            "missing_evidence_decision",
        )
        self.assertEqual(
            node._get_refusal_reason(
                {
                    "reranked_docs": [{"content": "strong", "score": -4.0}],
                    "evidence_decision": {
                        "should_answer": True,
                        "reason": "sufficient_evidence",
                    },
                }
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

    def test_empty_verified_sources_do_not_fall_back_to_rerank_candidates(self) -> None:
        state = {
            "sources": [],
            "reranked_docs": [
                {"chunk_id": "uncited", "content": "related but not claim-bound"}
            ],
        }
        self.assertEqual(QueryService._select_public_sources(state), [])

    def test_history_gracefully_degrades_when_mongodb_is_unavailable(self) -> None:
        service = QueryService()
        with patch(
            "knowledge.utils.mongo_history_utils.get_recent_messages",
            side_effect=RuntimeError("mongo offline"),
        ):
            self.assertEqual(service.get_history("session"), [])


if __name__ == "__main__":
    unittest.main()
