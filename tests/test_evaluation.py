from __future__ import annotations

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from knowledge.evaluation.metrics import (
    aggregate_results,
    evaluate_case,
    retrieval_metrics,
)
from knowledge.evaluation.runner import (
    REQUIRED_CATEGORIES,
    compare_with_baseline,
    load_dataset,
)
from knowledge.observability.model_usage import (
    ObservedChatModel,
    begin_model_trace,
    finish_model_trace,
)
from knowledge.processor.query_process.config import QueryConfig
from knowledge.processor.query_process.nodes.item_name_confirm import ItemNameConfirmNode
from knowledge.processor.query_process.state import create_default_state
from knowledge.utils.query_result_utils import build_retrieval_trace


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl"


class EvaluationTests(unittest.TestCase):
    def test_dataset_is_versioned_unique_and_covers_required_risks(self) -> None:
        cases = load_dataset(DATASET)
        self.assertEqual(len(cases), len({case["id"] for case in cases}))
        self.assertTrue(all(case["dataset_version"] == "shopkeeper-qa-v0.1.0" for case in cases))
        covered = {category for case in cases for category in case["category"]}
        self.assertTrue(REQUIRED_CATEGORIES.issubset(covered))
        self.assertGreaterEqual(sum(bool(case.get("ci_core")) for case in cases), 8)

    def test_retrieval_metrics_use_rank_and_relevant_sources(self) -> None:
        metrics = retrieval_metrics(
            [
                {"file_title": "manual", "title": "noise"},
                {"file_title": "manual", "title": "target"},
            ],
            [{"file_title": "manual", "title": "target"}],
            k=5,
        )
        self.assertEqual(metrics["recall@5"], 1.0)
        self.assertEqual(metrics["precision@5"], 0.5)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertGreater(metrics["ndcg@5"], 0.0)

    def test_no_answer_case_does_not_inflate_recall(self) -> None:
        metrics = retrieval_metrics([], [], k=5)

        self.assertNotIn("recall@5", metrics)
        self.assertEqual(metrics["empty_retrieval_accuracy"], 1.0)

    def test_aggregate_averages_each_metric_only_over_applicable_cases(self) -> None:
        summary = aggregate_results(
            [
                {"passed": True, "case_id": "retrieval", "metrics": {"recall@5": 0.5}},
                {"passed": True, "case_id": "no-answer", "metrics": {"empty_retrieval_accuracy": 1.0}},
            ]
        )

        self.assertEqual(summary["metrics"]["recall@5"], 0.5)
        self.assertEqual(summary["metric_case_counts"]["recall@5"], 1)

    def test_full_pipeline_recall_uses_rerank_trace_not_final_sources(self) -> None:
        case = {
            "id": "trace-recall",
            "category": ["business_qa"],
            "ci_core": True,
            "input": {"item_names": ["HAK 180"]},
            "expected": {
                "behavior": "answer",
                "relevant_sources": [{"chunk_id": "gold"}],
                "facts": [],
                "metric_thresholds": {"recall@5": 1.0},
            },
        }
        ok_status = {
            name: {"status": "ok", "reason": ""}
            for name in (
                "search_embedding",
                "search_embedding_hyde",
                "query_kg",
                "rrf",
                "rerank_node",
            )
        }
        result = evaluate_case(
            case,
            {
                "evaluation_scope": "full_pipeline",
                "response": {
                    "answer": "正常回答",
                    "sources": [{"chunk_id": "not-gold"}],
                    "diagnostics": {
                        "retrieval_trace": {
                            "stages": {
                                "direct": [{"chunk_id": "gold"}],
                                "hyde": [],
                                "knowledge_graph": [],
                                "web": [],
                                "rrf": [{"chunk_id": "gold"}],
                                "rerank": [{"chunk_id": "gold"}],
                            },
                            "status": ok_status,
                        },
                        "model_usage": {"failed_call_count": 0},
                    },
                },
            },
        )

        self.assertEqual(result["metrics"]["recall@5"], 1.0)
        self.assertEqual(result["metrics"]["pipeline_complete"], 1.0)

    def test_retrieval_trace_keeps_identity_but_not_chunk_content(self) -> None:
        trace = build_retrieval_trace(
            {
                "embedding_chunks": [
                    {
                        "entity": {
                            "chunk_id": 42,
                            "file_title": "manual",
                            "title": "target",
                            "content": "private full chunk",
                            "dense_vector": [0.1],
                        },
                        "distance": 0.9,
                    }
                ],
                "retrieval_status": {},
            }
        )

        self.assertEqual(trace["stages"]["direct"][0]["chunk_id"], "42")
        serialized = json.dumps(trace)
        self.assertNotIn("private full chunk", serialized)
        self.assertNotIn("dense_vector", serialized)

    def test_citation_and_faithfulness_are_scored_against_source(self) -> None:
        case = {
            "id": "citation",
            "category": ["business_qa"],
            "ci_core": True,
            "expected": {
                "behavior": "answer",
                "relevant_sources": [{"title": "manual"}],
                "facts": [{"any": ["5 mm"], "source_titles": ["manual"]}],
                "requires_citation": True,
                "metric_thresholds": {"citation_correctness": 1.0, "faithfulness": 1.0},
            },
        }
        result = evaluate_case(
            case,
            {
                "response": {
                    "answer": "上边距至少为 5 mm。[1]",
                    "sources": [{"title": "manual"}],
                },
                "latency_ms": 2,
            },
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["metrics"]["citation_correctness"], 1.0)
        self.assertEqual(result["metrics"]["faithfulness"], 1.0)

    def test_gate_catches_an_injected_retrieval_regression(self) -> None:
        baseline = {
            "summary": {
                "pass_rate": 1.0,
                "metrics": {"recall@5": 1.0, "safety": 1.0},
                "latency_ms": {"p95": 100},
                "model_usage": {"estimated_cost_usd": 0.01},
            }
        }
        candidate = copy.deepcopy(baseline)
        candidate["summary"]["metrics"]["recall@5"] = 0.0
        failures = compare_with_baseline(
            candidate,
            baseline,
            {
                "metric_max_drop": {"recall@5": 0.0, "safety": 0.0},
                "pass_rate_max_drop": 0.0,
                "latency_p95_max_increase_ratio": 0.25,
                "cost_max_increase_usd": 0.0,
            },
        )
        self.assertTrue(any("recall@5 regressed" in failure for failure in failures))

    def test_gate_rejects_mismatched_dataset_or_missing_cases(self) -> None:
        baseline = {
            "metadata": {"dataset_sha256": "old", "evaluation_scope": "full_pipeline"},
            "summary": {"metrics": {}, "latency_ms": {}, "model_usage": {}},
            "results": [{"case_id": "a"}],
        }
        candidate = {
            "metadata": {"dataset_sha256": "new", "evaluation_scope": "full_pipeline"},
            "summary": {"metrics": {}, "latency_ms": {}, "model_usage": {}},
            "results": [{"case_id": "b"}],
        }

        failures = compare_with_baseline(candidate, baseline, {})

        self.assertTrue(any("dataset_sha256" in failure for failure in failures))
        self.assertTrue(any("case set" in failure for failure in failures))

    def test_replay_can_verify_matching_full_pipeline_snapshot(self) -> None:
        baseline = {
            "metadata": {
                "dataset_version": "v1",
                "dataset_sha256": "same",
                "evaluation_scope": "full_pipeline",
            },
            "summary": {"metrics": {}, "latency_ms": {}, "model_usage": {}},
            "results": [{"case_id": "a"}],
        }
        candidate = copy.deepcopy(baseline)
        candidate["metadata"]["evaluation_scope"] = "recorded_output"
        candidate["metadata"]["replay_source_evaluation_scope"] = "full_pipeline"

        failures = compare_with_baseline(candidate, baseline, {})

        self.assertEqual(failures, [])

    def test_explicit_item_names_skip_non_deterministic_extraction(self) -> None:
        node = ItemNameConfirmNode(config=QueryConfig())
        node.history_service.fetch = Mock(return_value=[])
        node.history_service.save_user_message = Mock(return_value="")
        node.extractor.extract = Mock(side_effect=AssertionError("must not call LLM"))
        state = create_default_state(
            original_query="HAK 180 怎么清洁？",
            item_names=["HAK 180"],
            session_id="eval",
        )

        result = node.process(state)

        self.assertEqual(result["item_names"], ["HAK 180"])
        self.assertEqual(result["rewritten_query"], "HAK 180 怎么清洁？")
        self.assertEqual(result["answer"], "")
        node.extractor.extract.assert_not_called()

    def test_model_usage_is_aggregated_without_storing_prompt_content(self) -> None:
        class Response:
            content = "回答"
            usage_metadata = {"input_tokens": 7, "output_tokens": 2}

        client = Mock()
        client.invoke.return_value = Response()
        begin_model_trace("trace")
        observed = ObservedChatModel(client, trace_id="trace", model="test-model")
        observed.invoke("private prompt")
        summary = finish_model_trace("trace")

        self.assertEqual(summary["call_count"], 1)
        self.assertEqual(summary["total_tokens"], 9)
        self.assertEqual(summary["models"], ["test-model"])
        self.assertNotIn("prompt", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
