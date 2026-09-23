from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from knowledge.evaluation.metrics import (
    _source_matches,
    answer_metrics,
    aggregate_results,
    evaluate_case,
    retrieval_metrics,
)
from knowledge.evaluation.runner import (
    REQUIRED_CATEGORIES,
    compare_with_baseline,
    collect_metadata,
    load_dataset,
    load_source_contract,
    _sha256,
    _source_sha256,
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
from scripts.audit_stage1_evaluation_compat import canonical_title, normalized_case
from knowledge.evaluation.providers import ReplayProvider, contract_query_config


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl"


def comparison_report() -> dict:
    metadata = {field: "same" for field in (
        "evaluation_schema_version", "evaluator_version", "evaluator_fingerprint",
        "dataset_version", "dataset_sha256", "source_contract_sha256", "evaluation_scope",
        "provider", "prompt_sha256", "model", "item_model", "query_pipeline_sha256",
        "runtime_configuration_sha256", "pricing_configuration_sha256",
    )}
    metadata.update(query_config={"rrf_k": 60}, attempts=1, cost_status="estimated")
    return {
        "metadata": metadata,
        "suite": "core",
        "results": [{"case_id": "a"}],
        "summary": {
            "pass_rate": 1.0,
            "metrics": {"recall@5": 1.0, "safety": 1.0},
            "metric_case_counts": {"recall@5": 1, "safety": 1},
            "latency_ms": {"p95": 100},
            "model_usage": {"estimated_cost_usd": 0.01},
        },
    }


class EvaluationTests(unittest.TestCase):
    def test_stage1_title_compat_diagnostic_does_not_mutate_dataset(self) -> None:
        self.assertEqual(canonical_title("# 1.2 示例简介"), "1.2 示例简介")
        self.assertEqual(
            canonical_title("# 8.2.2 示例问题-2"), "8.2.2 示例问题"
        )
        self.assertEqual(canonical_title("EXAMPLE-100"), "EXAMPLE-100")
        original = {
            "expected": {
                "relevant_sources": [{"file_title": "manual", "parent_title": "# 警告"}],
                "facts": [{"source_titles": ["# 警告"]}],
            }
        }
        normalized = normalized_case(original)
        self.assertEqual(normalized["expected"]["relevant_sources"][0]["title"], "警告")
        self.assertEqual(original["expected"]["relevant_sources"][0]["parent_title"], "# 警告")

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
        baseline = comparison_report()
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

    def test_replay_cannot_pass_as_a_new_full_pipeline_control(self) -> None:
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

        self.assertTrue(any("evaluation_scope" in failure for failure in failures))

    def test_duplicate_gold_hits_do_not_inflate_recall_or_ndcg(self) -> None:
        metrics = retrieval_metrics(
            [{"chunk_id": str(index), "title": "one"} for index in range(4)],
            [{"title": "one"}, {"title": "two"}],
        )
        self.assertEqual(metrics["recall@5"], 0.5)
        self.assertLessEqual(metrics["ndcg@5"], 1.0)
        self.assertAlmostEqual(metrics["ndcg@5"], 0.613147)

    def test_overlapping_selectors_use_unique_earliest_evidence(self) -> None:
        sources = [{"chunk_id": "a", "file_title": "m", "title": "one"}, {"chunk_id": "b", "file_title": "m", "title": "two"}]
        expected = [{"file_title": "m"}, {"file_title": "m", "title": "one"}]
        self.assertEqual(retrieval_metrics(sources, expected)["recall@5"], 1.0)
        self.assertEqual(retrieval_metrics([sources[0], sources[0]], expected)["recall@5"], 0.5)

    def test_source_selectors_fail_closed_and_only_strip_markdown(self) -> None:
        source = {"document_id": "d", "file_title": "m", "title": "8.2 Table", "block_ids": ["b"]}
        self.assertTrue(_source_matches(source, {"document_id": "d", "title": "# 8.2 Table", "block_ids": ["b"]}))
        for selector in ({"unknown": "d"}, {"document_id": "other"}, {"block_ids": ["other"]}, {"title": "# 8.2 Table-2"}, {"parent_title": "8.2 Table"}, {}):
            self.assertFalse(_source_matches(source, selector))

    def test_explicit_alias_is_case_scoped_and_block_pinned(self) -> None:
        expected = {"file_title": "m", "title": "# 8.2 Table-2"}
        contract = {"source_aliases": [{"expected": expected, "actual": {"document_id": "d", "block_ids": ["b"]}}]}
        sources = [{"document_id": "d", "block_ids": ["b"], "title": "8.2 Table"}]
        self.assertEqual(retrieval_metrics(sources, [expected])["recall@5"], 0.0)
        self.assertEqual(retrieval_metrics(sources, [expected], contract=contract)["recall@5"], 1.0)
        sources[0]["block_ids"] = ["different-table"]
        self.assertEqual(retrieval_metrics(sources, [expected], contract=contract)["recall@5"], 0.0)
        sources[0].update(file_title="m", title="# 8.2 Table-2")
        self.assertEqual(retrieval_metrics(sources, [expected], contract=contract)["recall@5"], 0.0)

    def test_faithfulness_does_not_accept_same_title_in_wrong_file(self) -> None:
        case = {"expected": {"facts": [{"any": ["fact"], "source_titles": ["# title"]}], "relevant_sources": [{"file_title": "correct", "title": "# title"}]}}
        metrics = answer_metrics(case, {"answer": "fact [1]", "sources": [{"file_title": "wrong", "title": "title"}]})
        self.assertEqual(metrics["faithfulness"], 0.0)

    def test_image_metric_requires_image_in_cited_source(self) -> None:
        case = {"expected": {"requires_image": True}}
        response = {"answer": "panel [1]", "image_urls": ["https://example.invalid/p.png"], "sources": [{"image_urls": ["https://example.invalid/p.png"]}]}
        self.assertEqual(answer_metrics(case, response)["image_accuracy"], 1.0)
        response["answer"] = "panel"
        self.assertEqual(answer_metrics(case, response)["image_accuracy"], 0.0)
        response["answer"] = "panel [1]"
        response["image_urls"] = ["https://example.invalid/invented.png"]
        self.assertEqual(answer_metrics(case, response)["image_accuracy"], 0.0)
        response["image_urls"].append("https://example.invalid/p.png")
        self.assertEqual(answer_metrics(case, response)["image_accuracy"], 0.0)

    def test_gate_requires_complete_matching_contract_and_metrics(self) -> None:
        baseline = comparison_report()
        gate = {"metric_max_drop": {"recall@5": 0.0}}
        self.assertEqual(compare_with_baseline(copy.deepcopy(baseline), baseline, gate), [])
        for field in ("evaluator_fingerprint", "query_config", "model", "source_contract_sha256", "attempts"):
            candidate = copy.deepcopy(baseline)
            candidate["metadata"].pop(field)
            self.assertTrue(any(field in failure for failure in compare_with_baseline(candidate, baseline, gate)))
        candidate = copy.deepcopy(baseline)
        candidate["summary"]["metrics"].pop("recall@5")
        self.assertTrue(any("unavailable" in failure for failure in compare_with_baseline(candidate, baseline, gate)))
        candidate = copy.deepcopy(baseline)
        candidate["summary"]["metrics"]["recall@5"] = float("nan")
        self.assertTrue(any("not finite" in failure for failure in compare_with_baseline(candidate, baseline, gate)))

    def test_unknown_pricing_is_not_counted_as_free(self) -> None:
        baseline = comparison_report()
        baseline["metadata"]["cost_status"] = "unavailable"
        failures = compare_with_baseline(copy.deepcopy(baseline), baseline, {"cost_max_increase_usd": 0.01})
        self.assertTrue(any("cost comparison unavailable" in failure for failure in failures))

    def test_gate_rejects_nonfinite_latency_or_pass_rate_and_missing_cost(self) -> None:
        baseline = comparison_report()
        candidate = copy.deepcopy(baseline)
        candidate["summary"]["pass_rate"] = float("nan")
        candidate["summary"]["latency_ms"]["p95"] = float("nan")
        candidate["summary"]["model_usage"].pop("estimated_cost_usd")
        failures = compare_with_baseline(candidate, baseline, {"latency_p95_max_increase_ratio": 0.25, "cost_max_increase_usd": 0.01})
        self.assertTrue(any("pass_rate is not finite" in failure for failure in failures))
        self.assertTrue(any("latency p95" in failure for failure in failures))
        self.assertTrue(any("monetary cost is unavailable" in failure for failure in failures))

    def test_source_contract_rejects_wrong_dataset_or_broad_alias(self) -> None:
        import hashlib
        with TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            data = {"schema_version": "1.0", "dataset_sha256": "wrong", "cases": {}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA mismatch"):
                load_source_contract(path, DATASET)
            case = load_dataset(DATASET)[0]
            data["dataset_sha256"] = hashlib.sha256(DATASET.read_bytes()).hexdigest()
            data["cases"] = {case["id"]: {"source_aliases": [{"expected": case["expected"]["relevant_sources"][0], "actual": {"file_title": "m", "title": "intro"}}]}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must pin"):
                load_source_contract(path, DATASET)

    def test_contract_configuration_is_independent_of_environment(self) -> None:
        first = collect_metadata(ROOT, DATASET, "contract", "v1", "contract")
        with patch.dict(os.environ, {
            "MODEL": "different-live-model", "LLM_INPUT_USD_PER_1M": "999",
            "RAG_REFUSAL_MIN_SCORE": "999", "CHUNKS_COLLECTION": "private-live-index",
            "OPENAI_API_KEY": "private-not-for-report", "INDEX_VERSION": "private-live-index",
            "LLM_DEFAULT_TEMPERATURE": "9", "BGE_M3_PATH": "private/model/location",
        }):
            config = contract_query_config()
            second = collect_metadata(ROOT, DATASET, "contract", "v1", "contract")
        self.assertEqual(config.refusal_min_score, 0.3)
        for field in ("query_config", "model", "item_model", "index_version", "collections", "runtime_configuration_sha256", "pricing_configuration_sha256", "cost_status", "prompt_sha256"):
            self.assertEqual(first[field], second[field], field)
        self.assertNotIn("private-not-for-report", json.dumps(second))
        self.assertEqual(second["cost_status"], "not_applicable")

    def test_source_fingerprint_is_portable_but_dataset_hash_is_byte_exact(self) -> None:
        with TemporaryDirectory() as directory:
            one = Path(directory) / "lf.py"
            two = Path(directory) / "crlf.py"
            one.write_bytes(b"one\ntwo\n")
            two.write_bytes(b"one\r\ntwo\r\n")
            self.assertEqual(_source_sha256(one), _source_sha256(two))
            self.assertNotEqual(_sha256(one), _sha256(two))

    def test_live_model_runtime_configuration_is_fingerprinted_without_private_paths(self) -> None:
        first = collect_metadata(ROOT, DATASET, "service", "v1", "full_pipeline")
        with patch.dict(os.environ, {"LLM_DEFAULT_TEMPERATURE": "9", "BGE_M3_PATH": "private/model/location"}):
            second = collect_metadata(ROOT, DATASET, "service", "v1", "full_pipeline")
        self.assertNotEqual(first["runtime_configuration_sha256"], second["runtime_configuration_sha256"])
        self.assertNotIn("private/model/location", json.dumps(second))

    def test_contract_gate_uses_semantic_dataset_hash_for_eol_portability(self) -> None:
        baseline = comparison_report()
        baseline["metadata"].update(provider="contract", dataset_semantic_sha256="same-content")
        candidate = copy.deepcopy(baseline)
        candidate["metadata"]["dataset_sha256"] = "other-line-endings"
        self.assertEqual(compare_with_baseline(candidate, baseline, {}), [])
        candidate["metadata"]["dataset_semantic_sha256"] = "changed-fact"
        self.assertTrue(any("dataset_semantic" in failure for failure in compare_with_baseline(candidate, baseline, {})))

    def test_replay_rejects_duplicate_case_rows(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.jsonl"
            path.write_text('{"case_id":"same"}\n{"case_id":"same"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                ReplayProvider(path)

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
