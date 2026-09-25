from __future__ import annotations

import io
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from knowledge.evaluation.providers import HttpProvider, ServiceProvider
from knowledge.evaluation.runner import run_evaluation
from knowledge.evaluation.usage import attach_turn_usage, combine_model_usage
from knowledge.observability.model_usage import (
    ObservedChatModel,
    begin_model_trace,
    finish_model_trace,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation/datasets/shopkeeper_qa.v0.1.0.jsonl"


def usage(tokens=10):
    return {
        "call_count": 1, "failed_call_count": 0,
        "input_tokens": tokens, "output_tokens": 2, "total_tokens": tokens + 2,
        "estimated_cost_usd": tokens / 1000, "latency_ms": 5,
        "models": ["test-model"], "estimated_token_count": False,
    }


class FakeQueryService:
    raise_second = False

    def __init__(self):
        self.turn = 0

    def generate_task_id(self):
        self.turn += 1
        return str(self.turn)

    def submit_query(self, *args, **kwargs):
        pass

    def run_query_graph(self, **kwargs):
        if self.raise_second and self.turn == 2:
            raise RuntimeError("synthetic second-turn failure")

    def get_answer(self, task_id):
        return "final answer " + task_id

    def get_sources(self, task_id):
        return [{"chunk_id": task_id}]

    def get_image_urls(self, task_id):
        return []

    def get_diagnostics(self, task_id):
        return {"model_usage": usage(self.turn * 10), "trace_id": task_id}

    def get_error(self, task_id):
        return ""


class EvaluationUsageTests(unittest.TestCase):
    def test_observed_model_records_operation_and_native_cost(self):
        class Response:
            content = "ok"
            usage_metadata = {"input_tokens": 1000, "output_tokens": 100}

        class Client:
            def invoke(self, _prompt):
                return Response()

        trace_id = "operation-native-cost"
        begin_model_trace(trace_id)
        ObservedChatModel(Client(), trace_id, "qwen-flash", "hyde").invoke("prompt")
        summary = finish_model_trace(trace_id)
        self.assertEqual(summary["cost_status"], "available")
        self.assertEqual(summary["currency"], "CNY")
        self.assertEqual(summary["by_operation"]["hyde"]["total_tokens"], 1100)
        self.assertGreater(summary["by_operation"]["hyde"]["cost"], 0)

    def test_combines_counts_without_storing_prompt_or_answer(self):
        first, second = usage(10), usage(20)
        first["prompt"] = "private"
        second["estimated_token_count"] = True
        combined = combine_model_usage([first, second])
        self.assertEqual(combined["call_count"], 2)
        self.assertEqual(combined["total_tokens"], 34)
        self.assertEqual(combined["estimated_cost_usd"], 0.03)
        self.assertTrue(combined["usage_complete"])
        self.assertTrue(combined["estimated_token_count"])
        self.assertNotIn("private", json.dumps(combined))

    def test_missing_and_invalid_values_do_not_become_free_usage(self):
        for invalid in (None, {}, {**usage(), "input_tokens": "bad"},
                        {**usage(), "estimated_cost_usd": float("nan")},
                        {**usage(), "call_count": -1},
                        {**usage(), "total_tokens": 999}):
            with self.subTest(invalid=invalid):
                self.assertFalse(combine_model_usage([invalid])["usage_complete"])

    def test_partial_turns_keep_known_cost_but_mark_incomplete(self):
        response = {"answer": "last", "diagnostics": {"trace_id": "last"}}
        result = attach_turn_usage(response, [usage()], 2)
        self.assertFalse(result["diagnostics"]["model_usage"]["usage_complete"])
        self.assertEqual(result["diagnostics"]["model_usage"]["call_count"], 1)
        self.assertNotIn("model_usage", response["diagnostics"])

    def test_service_counts_all_turns_but_keeps_final_answer_and_trace(self):
        module = ModuleType("knowledge.service.query_service")
        module.QueryService = FakeQueryService
        provider = ServiceProvider()
        provider._preflight = {"passed": True, "checks": [], "failures": []}
        with patch.dict(sys.modules, {module.__name__: module}):
            raw = provider.run({"id": "turns", "input": {"turns": ["one", "two"]}})
        self.assertEqual(raw["response"]["answer"], "final answer 2")
        self.assertEqual(raw["response"]["sources"], [{"chunk_id": "2"}])
        diagnostics = raw["response"]["diagnostics"]
        self.assertEqual(diagnostics["trace_id"], "2")
        self.assertEqual(diagnostics["model_usage"]["total_tokens"], 34)
        self.assertEqual(diagnostics["evaluation_usage"]["recorded_turn_count"], 2)
        self.assertTrue(diagnostics["model_usage"]["usage_complete"])

    def test_service_failure_does_not_discard_earlier_turn_usage(self):
        module = ModuleType("knowledge.service.query_service")
        module.QueryService = FakeQueryService
        provider = ServiceProvider()
        provider._preflight = {"passed": True, "checks": [], "failures": []}
        with patch.dict(sys.modules, {module.__name__: module}), patch.object(FakeQueryService, "raise_second", True):
            raw = provider.run({"id": "failure", "input": {"turns": ["one", "two"]}})
        self.assertIn("synthetic", raw["error"])
        combined = raw["response"]["diagnostics"]["model_usage"]
        self.assertEqual(combined["total_tokens"], 12)
        self.assertFalse(combined["usage_complete"])

    def test_http_counts_all_turns(self):
        responses = [io.BytesIO(json.dumps({"answer": str(n), "diagnostics": {"model_usage": usage(n)}}).encode()) for n in (10, 20)]
        with patch("urllib.request.urlopen", side_effect=responses):
            raw = HttpProvider("http://example.invalid").run({"id": "http", "input": {"turns": ["one", "two"]}})
        self.assertEqual(raw["response"]["answer"], "20")
        self.assertEqual(raw["response"]["diagnostics"]["model_usage"]["total_tokens"], 34)

    def test_http_error_keeps_all_returned_usage_instead_of_crashing(self):
        responses = [io.BytesIO(json.dumps({"error": "failed" if n == 20 else "", "diagnostics": {"model_usage": usage(n)}}).encode()) for n in (10, 20)]
        with patch("urllib.request.urlopen", side_effect=responses):
            raw = HttpProvider("http://example.invalid").run({"id": "http", "input": {"turns": ["one", "two"]}})
        self.assertEqual(raw["error"], "failed")
        self.assertEqual(raw["response"]["diagnostics"]["model_usage"]["total_tokens"], 34)

    def test_http_network_failure_keeps_partial_usage(self):
        first = io.BytesIO(json.dumps({"diagnostics": {"model_usage": usage(10)}}).encode())
        with patch("urllib.request.urlopen", side_effect=[first, OSError("offline")]):
            raw = HttpProvider("http://example.invalid").run({"id": "http", "input": {"turns": ["one", "two"]}})
        self.assertEqual(raw["error"], "offline")
        self.assertEqual(raw["response"]["diagnostics"]["model_usage"]["total_tokens"], 12)
        self.assertFalse(raw["response"]["diagnostics"]["model_usage"]["usage_complete"])

    def test_all_attempts_are_counted_not_only_the_quality_representative(self):
        class Provider:
            name = "contract"
            evaluation_scope = "contract"

            def run(self, case, attempt=1):
                return {
                    "response": {"answer": "answer", "diagnostics": {"model_usage": usage(attempt * 10)}},
                    "attempt": attempt, "evaluation_scope": "contract", "latency_ms": attempt,
                }

        report = run_evaluation(root=ROOT, dataset_path=DATASET, provider=Provider(), attempts=3)
        count = report["summary"]["case_count"]
        self.assertEqual(report["summary"]["executed_attempt_count"], count * 3)
        self.assertEqual(report["summary"]["model_usage"]["call_count"], count * 3)
        self.assertEqual(report["summary"]["model_usage"]["total_tokens"], count * 66)
        self.assertEqual(report["summary"]["representative_model_usage"]["call_count"], count)
        self.assertEqual(report["metadata"]["usage_scope"], "all_turns_all_attempts")

    def test_nonrepresentative_failure_cannot_be_hidden_by_successful_median(self):
        class Provider:
            name = "service"
            evaluation_scope = "full_pipeline"

            def run(self, case, attempt=1):
                return {
                    "response": {"answer": "answer", "diagnostics": {
                        "model_usage": usage(10),
                        "retrieval_trace": {"stages": {}, "status": {
                            name: {"status": "ok"} for name in (
                                "search_embedding", "search_embedding_hyde", "query_kg", "rrf", "rerank_node"
                            )
                        }},
                    }},
                    "error": "synthetic failure" if attempt == 1 else "",
                    "attempt": attempt, "evaluation_scope": "full_pipeline", "latency_ms": 1,
                }

        report = run_evaluation(root=ROOT, dataset_path=DATASET, provider=Provider(), attempts=3)
        self.assertTrue(all(not result["error"] for result in report["results"]))
        self.assertEqual(report["execution_health"]["provider_error_count"], 9)
        self.assertFalse(report["baseline_eligibility"]["rag_quality"])
        self.assertTrue(any("executed attempts failed" in value for value in report["baseline_eligibility"]["reasons"]))

    def test_earlier_turn_degradation_is_preserved_for_eligibility(self):
        module = ModuleType("knowledge.service.query_service")
        module.QueryService = FakeQueryService
        provider = ServiceProvider()
        provider._preflight = {"passed": True, "checks": [], "failures": []}
        with patch.dict(sys.modules, {module.__name__: module}), patch("knowledge.evaluation.metrics._pipeline_complete", side_effect=[0.0, 1.0]):
            raw = provider.run({"id": "turns", "input": {"turns": ["one", "two"]}})
        self.assertEqual(raw["response"]["diagnostics"]["evaluation_usage"]["turn_pipeline_complete"], [False, True])

    def test_invalid_usage_is_reported_not_crashed_or_counted_as_free(self):
        class Provider:
            name = "service"
            evaluation_scope = "full_pipeline"

            def run(self, case, attempt=1):
                return {"response": {"diagnostics": {"model_usage": {**usage(), "input_tokens": "bad"}}}, "error": "incomplete"}

        report = run_evaluation(root=ROOT, dataset_path=DATASET, provider=Provider())
        self.assertFalse(report["summary"]["model_usage"]["usage_complete"])
        self.assertEqual(report["metadata"]["cost_status"], "unavailable")
        self.assertEqual(report["summary"]["cost_budget"]["status"], "unavailable")
        self.assertIsNone(report["summary"]["cost_budget"]["passed"])
        self.assertIn(
            "versioned monetary cost is unavailable",
            report["baseline_eligibility"]["reasons"],
        )


if __name__ == "__main__":
    unittest.main()
