"""Deterministic retrieval and answer metrics used by the stage-0 gate."""

from __future__ import annotations

import math
import json
import re
import statistics
import unicodedata
from typing import Any, Dict, Iterable, List, Sequence

from knowledge.evaluation.usage import combine_model_usage
from knowledge.processor.query_process.evidence import evidence_supports_claim


_CITATION_PATTERN = re.compile(r"\[(\d+)]")
EVALUATOR_VERSION = "3.2"
_SCALAR_SOURCE_FIELDS = {
    "source", "chunk_id", "file_title", "title", "parent_title", "url",
    "document_id", "revision_id", "section_id", "source_uri",
}
_LIST_SOURCE_FIELDS = {"block_ids", "block_lineage_ids", "page_numbers", "page_uids", "title_path"}
_REFUSAL_MARKERS = (
    "没有足够依据",
    "没有检索到足够",
    "证据不足",
    "不足以支持",
    "无法回答",
    "无法提供",
    "不能提供",
    "不具备",
    "未找到",
    "不确定",
)
_CLARIFY_MARKERS = ("请提供", "请补充", "您指的是", "具体产品", "具体型号")


def percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def canonical_heading(value: Any) -> str:
    """Ignore Markdown heading syntax, never section numbers or part suffixes."""
    return re.sub(r"^#{1,6}\s+", "", str(value or "").strip()).strip()


def _source_matches(actual: Dict[str, Any], expected: Dict[str, Any]) -> bool:
    if not expected or set(expected) - (_SCALAR_SOURCE_FIELDS | _LIST_SOURCE_FIELDS):
        return False
    constrained = False
    for field, wanted in expected.items():
        if field in _LIST_SOURCE_FIELDS:
            if not isinstance(wanted, list) or not wanted:
                return False
            received = actual.get(field)
            if not isinstance(received, list):
                return False
            # A path is ordered, whereas location/id lists express containment.
            if field == "title_path":
                if list(map(canonical_heading, received)) != list(map(canonical_heading, wanted)):
                    return False
            elif not all(value in received for value in wanted):
                return False
        else:
            wanted = str(wanted or "").strip()
            if not wanted:
                return False
            received = str(actual.get(field) or "").strip()
            if field in {"title", "parent_title"}:
                received, wanted = canonical_heading(received), canonical_heading(wanted)
            if received != wanted:
                return False
        constrained = True
    return constrained


def _matches_with_aliases(actual: dict, expected: dict, contract: dict | None = None) -> bool:
    aliases = [alias for alias in (contract or {}).get("source_aliases", []) if alias.get("expected") == expected]
    # A reviewed mapping replaces ambiguous legacy labels, rather than adding
    # permissive alternatives while silently retaining the ambiguous fallback.
    if aliases:
        return any(_source_matches(actual, alias.get("actual") or {}) for alias in aliases)
    return _source_matches(actual, expected)


def _relevant_indices(
    sources: Sequence[Dict[str, Any]], expected_sources: Sequence[Dict[str, Any]],
    contract: dict | None = None,
) -> set[int]:
    return {
        index
        for index, source in enumerate(sources)
        if any(_matches_with_aliases(source, expected, contract) for expected in expected_sources)
    }


def retrieval_metrics(
    sources: Sequence[Dict[str, Any]],
    expected_sources: Sequence[Dict[str, Any]],
    k: int = 5,
    contract: dict | None = None,
) -> Dict[str, float]:
    if not expected_sources:
        return {
            "empty_retrieval_accuracy": 1.0 if not sources else 0.0,
        }

    top_sources = list(sources[:k])
    relevant = _relevant_indices(top_sources, expected_sources, contract)
    # Credit each expected evidence unit once. Incremental augmenting paths
    # preserve the earliest possible ranks even when selectors overlap.
    gold = list({json.dumps(item, sort_keys=True): item for item in expected_sources}.values())
    assigned: dict[int, int] = {}
    def assign(rank: int, visited: set[int]) -> bool:
        for gold_index, expected in enumerate(gold):
            if gold_index in visited or not _matches_with_aliases(top_sources[rank], expected, contract):
                continue
            visited.add(gold_index)
            if gold_index not in assigned or assign(assigned[gold_index], visited):
                assigned[gold_index] = rank
                return True
        return False
    credited = []
    seen_candidates: set[str] = set()
    for rank, source in enumerate(top_sources):
        if source.get("chunk_id"):
            identity = (source.get("source", "local"), source.get("document_id", source.get("file_title", "")), source["chunk_id"])
        elif source.get("url"):
            identity = ("url", source["url"])
        else:
            identity = {key: value for key, value in source.items() if key in _SCALAR_SOURCE_FIELDS | _LIST_SOURCE_FIELDS}
        key = json.dumps(identity, sort_keys=True)
        if key not in seen_candidates and assign(rank, set()):
            credited.append(rank)
        seen_candidates.add(key)
    recall = len(assigned) / len(gold)
    precision = len(relevant) / len(top_sources) if top_sources else 0.0
    mrr = 1.0 / (min(relevant) + 1) if relevant else 0.0
    dcg = sum(1.0 / math.log2(index + 2) for index in credited)
    ideal_count = min(len(gold), k)
    ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return {
        f"recall@{k}": round(recall, 6),
        f"precision@{k}": round(precision, 6),
        "mrr": round(mrr, 6),
        f"ndcg@{k}": round(dcg / ideal_dcg if ideal_dcg else 0.0, 6),
    }


def grouped_retrieval_metrics(
    sources: Sequence[Dict[str, Any]],
    expected_sources: Sequence[Dict[str, Any]],
    *,
    level: str,
    k: int = 5,
    contract: dict | None = None,
) -> Dict[str, float]:
    """Score canonical evidence units so chunk splits/merges do not game quality."""

    if not expected_sources:
        return {f"{level}_empty_retrieval_accuracy": 1.0 if not sources else 0.0}
    grouped: list[list[Dict[str, Any]]] = []
    group_positions: dict[str, int] = {}
    for source in sources:
        key = _retrieval_group_key(source, level)
        if key not in group_positions:
            group_positions[key] = len(grouped)
            grouped.append([])
        grouped[group_positions[key]].append(source)
    grouped = grouped[:k]

    gold = list({json.dumps(item, sort_keys=True): item for item in expected_sources}.values())
    relevant_ranks: list[int] = []
    matched_gold: set[int] = set()
    credited_ranks: list[int] = []
    for rank, members in enumerate(grouped):
        matched_here = {
            gold_index
            for gold_index, expected in enumerate(gold)
            if any(_matches_with_aliases(member, expected, contract) for member in members)
        }
        if matched_here:
            relevant_ranks.append(rank)
        new_matches = matched_here - matched_gold
        if new_matches:
            credited_ranks.append(rank)
            matched_gold.update(new_matches)

    recall = len(matched_gold) / len(gold)
    precision = len(relevant_ranks) / len(grouped) if grouped else 0.0
    mrr = 1.0 / (min(relevant_ranks) + 1) if relevant_ranks else 0.0
    dcg = sum(1.0 / math.log2(rank + 2) for rank in credited_ranks)
    ideal_dcg = sum(
        1.0 / math.log2(index + 2)
        for index in range(min(len(gold), k))
    )
    return {
        f"{level}_recall@{k}": round(recall, 6),
        f"{level}_precision@{k}": round(precision, 6),
        f"{level}_mrr": round(mrr, 6),
        f"{level}_ndcg@{k}": round(dcg / ideal_dcg if ideal_dcg else 0.0, 6),
    }


def _retrieval_group_key(source: Dict[str, Any], level: str) -> str:
    if source.get("source") == "web" or source.get("url"):
        return f"web:{source.get('url') or source.get('domain') or source.get('title')}"
    document = str(source.get("document_id") or source.get("file_title") or "unknown")
    if level == "document":
        return f"document:{document}"
    section = str(
        source.get("section_id") or source.get("parent_title") or source.get("title") or "unknown"
    )
    if level == "section":
        return f"section:{document}:{canonical_heading(section)}"
    explicit = str(source.get("evidence_group_id") or "")
    if explicit:
        return explicit
    lineage = source.get("block_lineage_ids") or source.get("block_ids") or []
    if isinstance(lineage, list) and lineage:
        return f"evidence:{document}:{section}:{'|'.join(sorted(map(str, lineage)))}"
    return f"evidence:{document}:{canonical_heading(section)}"


def classify_behavior(answer: str) -> str:
    normalized = str(answer or "")
    if any(marker in normalized for marker in _REFUSAL_MARKERS):
        return "refuse"
    if any(marker in normalized for marker in _CLARIFY_MARKERS):
        return "clarify"
    return "answer"


def _fact_present(answer: str, fact: Dict[str, Any]) -> bool:
    alternatives = [str(value) for value in fact.get("any", []) if str(value)]
    normalized_answer = _normalize_fact_text(answer)
    return bool(alternatives) and any(
        _normalize_fact_text(value) in normalized_answer for value in alternatives
    )


def _normalize_fact_text(value: Any) -> str:
    """Normalize presentation-only differences before deterministic scoring.

    NFKC makes `m²`, `m2`, and the CJK compatibility glyph `㎡`
    comparable. Whitespace is ignored because model formatting around units is
    not a semantic answer difference.
    """

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Deterministic, meaning-preserving Chinese modal normalization. This is
    # intentionally narrow: it recognizes a grammatical equivalent, not a
    # fuzzy topical match, so a fluent answer is not penalized for saying
    # “无需安装” where the reviewed fact says “不需要安装”.
    normalized = normalized.replace("无需", "不需要")
    return re.sub(r"\s+", "", normalized)


def _fact_is_cited(
    answer: str,
    fact: Dict[str, Any],
    sources: Sequence[Dict[str, Any]],
    expected_sources: Sequence[Dict[str, Any]] = (),
    contract: dict | None = None,
) -> bool:
    alternatives = [str(value) for value in fact.get("any", []) if str(value)]
    supporting_titles = {
        canonical_heading(value) for value in fact.get("source_titles", []) if str(value)
    }
    # Citations commonly follow Chinese sentence punctuation ("事实。[1]").
    # Attach such markers to the preceding clause before sentence splitting.
    normalized_answer = re.sub(r"[。！？]\s*(?=\[\d+])", "", answer)
    for sentence in re.split(r"[。！？\n]", normalized_answer):
        normalized_sentence = _normalize_fact_text(sentence)
        if not any(
            _normalize_fact_text(value) in normalized_sentence for value in alternatives
        ):
            continue
        citations = [int(value) for value in _CITATION_PATTERN.findall(sentence)]
        for citation in citations:
            if not 1 <= citation <= len(sources):
                continue
            source = sources[citation - 1]
            matches_reviewed_source = not expected_sources or any(
                _matches_with_aliases(source, expected, contract) for expected in expected_sources
            )
            grounded_in_preview = evidence_supports_claim(sentence, source, 0.08)
            if not matches_reviewed_source and not grounded_in_preview:
                continue
            source_titles = {
                canonical_heading(source.get("title")),
                canonical_heading(source.get("parent_title")),
            }
            if grounded_in_preview or not supporting_titles or source_titles & supporting_titles:
                return True
            if any(
                canonical_heading(alias.get("expected_title")) in supporting_titles
                and _source_matches(source, alias.get("actual") or {})
                for alias in (contract or {}).get("fact_source_aliases", [])
            ):
                return True
    return False


def answer_metrics(case: Dict[str, Any], response: Dict[str, Any], contract: dict | None = None) -> Dict[str, float]:
    expected = case.get("expected") or {}
    answer = str(response.get("answer") or "")
    sources = [item for item in response.get("sources") or [] if isinstance(item, dict)]
    expected_behavior = str(expected.get("behavior") or "answer")
    behavior_accuracy = float(classify_behavior(answer) == expected_behavior)

    facts = [item for item in expected.get("facts") or [] if isinstance(item, dict)]
    present_facts = [fact for fact in facts if _fact_present(answer, fact)]
    answer_correctness = (
        len(present_facts) / len(facts)
        if facts
        else behavior_accuracy
    )

    citations = [int(value) for value in _CITATION_PATTERN.findall(answer)]
    valid_citations = [value for value in citations if 1 <= value <= len(sources)]
    if expected.get("requires_citation"):
        relevant = _relevant_indices(sources, expected.get("relevant_sources") or [], contract)
        correct_count = 0
        normalized_answer = re.sub(r"[。！？]\s*(?=\[\d+])", "", answer)
        for sentence in re.split(r"[。！？\n]", normalized_answer):
            for value in (int(item) for item in _CITATION_PATTERN.findall(sentence)):
                if not 1 <= value <= len(sources):
                    continue
                if value - 1 in relevant or evidence_supports_claim(
                    sentence, sources[value - 1], 0.08
                ):
                    correct_count += 1
        citation_correctness = correct_count / len(citations) if citations else 0.0
    else:
        citation_correctness = 1.0 if len(valid_citations) == len(citations) else 0.0

    if facts and expected_behavior == "answer":
        supported = sum(_fact_is_cited(answer, fact, sources, expected.get("relevant_sources") or [], contract) for fact in present_facts)
        faithfulness = supported / len(present_facts) if present_facts else 0.0
    else:
        faithfulness = behavior_accuracy

    forbidden = [str(value) for value in expected.get("must_not_contain") or []]
    safety = float(not any(value and value.lower() in answer.lower() for value in forbidden))
    image_accuracy = 1.0
    if expected.get("requires_image"):
        returned_images = {image for image in response.get("image_urls") or [] if isinstance(image, str)}
        evidence_images = {
            image
            for citation in valid_citations
            for image in sources[citation - 1].get("image_urls") or []
            if isinstance(image, str)
        }
        image_accuracy = float(bool(returned_images) and returned_images <= evidence_images)

    return {
        "behavior_accuracy": behavior_accuracy,
        "answer_correctness": round(answer_correctness, 6),
        "citation_correctness": round(citation_correctness, 6),
        "faithfulness": round(faithfulness, 6),
        "safety": safety,
        "image_accuracy": image_accuracy,
    }


def evaluate_case(case: Dict[str, Any], raw_result: Dict[str, Any], contract: dict | None = None) -> Dict[str, Any]:
    response = raw_result.get("response") or {}
    expected = case.get("expected") or {}
    scope = str(raw_result.get("evaluation_scope") or "unknown")
    provider_error = str(raw_result.get("error") or "")
    if provider_error:
        return {
            "case_id": case["id"],
            "category": list(case.get("category") or []),
            "ci_core": bool(case.get("ci_core")),
            "passed": False,
            "score": 0.0,
            "failures": [f"provider_error: {provider_error}"],
            "metrics": {},
            "response": response,
            "latency_ms": round(float(raw_result.get("latency_ms") or 0.0), 3),
            "attempt": int(raw_result.get("attempt") or 1),
            "error": provider_error,
            "evaluation_scope": scope,
        }
    k = int(expected.get("retrieval_k") or 5)
    expected_sources = expected.get("relevant_sources") or []
    trace = (
        response.get("diagnostics", {}).get("retrieval_trace", {})
        if isinstance(response.get("diagnostics"), dict)
        else {}
    )
    stages = trace.get("stages") if isinstance(trace, dict) else None
    metrics: Dict[str, float] = {}
    if scope == "full_pipeline" and isinstance(stages, dict):
        final_candidates = stages.get("rerank") or []
        metrics.update(retrieval_metrics(final_candidates, expected_sources, k=k, contract=contract))
        for level in ("evidence_group", "section", "document"):
            metrics.update(
                grouped_retrieval_metrics(
                    final_candidates,
                    expected_sources,
                    level=level,
                    k=k,
                    contract=contract,
                )
            )
        if expected_sources:
            for stage in ("direct", "hyde", "knowledge_graph", "web", "rrf", "rerank"):
                stage_metrics = retrieval_metrics(
                    stages.get(stage) or [], expected_sources, k=k, contract=contract
                )
                metrics[f"{stage}_recall@{k}"] = stage_metrics[f"recall@{k}"]
    metrics.update(answer_metrics(case, response, contract))

    if scope == "full_pipeline":
        metrics["pipeline_complete"] = _pipeline_complete(case, response, trace)

    thresholds = {
        "behavior_accuracy": 1.0,
        "safety": 1.0,
        **(expected.get("metric_thresholds") or {}),
    }
    if scope == "full_pipeline" and (
        (case.get("input") or {}).get("item_names")
        or expected.get("relevant_sources")
    ):
        thresholds["pipeline_complete"] = 1.0
    failures = []
    for metric, minimum in thresholds.items():
        if metric not in metrics:
            if scope != "full_pipeline" and (
                metric.startswith("recall@")
                or metric.startswith("precision@")
                or metric == "mrr"
                or metric.startswith("ndcg@")
            ):
                continue
            failures.append(f"{metric}=unavailable")
            continue
        value = float(metrics.get(metric, 0.0))
        if value < float(minimum):
            failures.append(f"{metric}={value:.4f} < {float(minimum):.4f}")

    score_metrics = (
        "behavior_accuracy",
        "answer_correctness",
        "citation_correctness",
        "faithfulness",
        "safety",
    )
    score = statistics.fmean(float(metrics[name]) for name in score_metrics)
    return {
        "case_id": case["id"],
        "category": list(case.get("category") or []),
        "ci_core": bool(case.get("ci_core")),
        "passed": not failures,
        "score": round(score, 6),
        "failures": failures,
        "metrics": metrics,
        "response": response,
        "latency_ms": round(float(raw_result.get("latency_ms") or 0.0), 3),
        "attempt": int(raw_result.get("attempt") or 1),
        "error": str(raw_result.get("error") or ""),
        "evaluation_scope": scope,
    }


def _pipeline_complete(
    case: Dict[str, Any],
    response: Dict[str, Any],
    trace: Dict[str, Any],
) -> float:
    """Return 1 only when an intended end-to-end run did not silently degrade."""

    expected = case.get("expected") or {}
    needs_retrieval = bool(
        (case.get("input") or {}).get("item_names")
        or expected.get("relevant_sources")
    )
    if not needs_retrieval:
        return 1.0
    if not isinstance(trace, dict) or not isinstance(trace.get("stages"), dict):
        return 0.0

    statuses = trace.get("status") or {}
    plan = trace.get("plan") or {}
    channels = plan.get("channels") if isinstance(plan, dict) else {}
    required = {"rrf", "rerank_node"}
    mapping = {
        "direct": "search_embedding",
        "hyde": "search_embedding_hyde",
        "kg": "query_kg",
    }
    if isinstance(channels, dict):
        required.update(
            node_name
            for channel, node_name in mapping.items()
            if isinstance(channels.get(channel), dict) and channels[channel].get("enabled")
        )
        web = channels.get("web") or {}
        if isinstance(web, dict) and web.get("mode") == "required":
            required.add("web_search_mcp")
    else:
        # Compatibility for stage-1 traces without a versioned plan.
        required.update(mapping.values())
    if any(
        not isinstance(statuses.get(stage), dict)
        or statuses[stage].get("status") not in {"ok"}
        for stage in required
    ):
        return 0.0
    if any(
        isinstance(value, dict) and value.get("status") == "error"
        for value in statuses.values()
    ):
        return 0.0

    usage = response.get("diagnostics", {}).get("model_usage", {})
    if int(usage.get("failed_call_count") or 0) > 0:
        return 0.0
    return 1.0


def aggregate_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    metric_names = sorted(
        {name for result in results for name in (result.get("metrics") or {})}
    )
    metrics = {}
    metric_case_counts = {}
    for name in metric_names:
        values = [
            float(result["metrics"][name])
            for result in results
            if name in (result.get("metrics") or {})
        ]
        if values:
            metrics[name] = round(statistics.fmean(values), 6)
            metric_case_counts[name] = len(values)
    latencies = [float(result.get("latency_ms") or 0.0) for result in results]
    model_usages = [
        result.get("response", {}).get("diagnostics", {}).get("model_usage", {})
        for result in results
    ]
    return {
        "case_count": len(results),
        "passed_count": sum(bool(result.get("passed")) for result in results),
        "pass_rate": round(
            sum(bool(result.get("passed")) for result in results) / len(results), 6
        ) if results else 0.0,
        "metrics": metrics,
        "metric_case_counts": metric_case_counts,
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
        },
        "model_usage": combine_model_usage(model_usages),
        "failed_case_ids": [
            result["case_id"] for result in results if not result.get("passed")
        ],
    }
