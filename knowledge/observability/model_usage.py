"""Per-query LLM call, token, latency, and estimated cost accounting.

The tracker deliberately stores aggregates only. Prompts and model outputs may
contain private document content, so they remain in the query state and are not
copied into the process-wide registry.
"""

from __future__ import annotations

import math
import time
from threading import RLock
from typing import Any, Dict, Iterable


_lock = RLock()
_traces: Dict[str, list[Dict[str, Any]]] = {}


def begin_model_trace(trace_id: str) -> None:
    if not trace_id:
        return
    with _lock:
        _traces[trace_id] = []


def finish_model_trace(trace_id: str) -> Dict[str, Any]:
    if not trace_id:
        return _empty_summary()
    with _lock:
        calls = _traces.pop(trace_id, [])
    return summarize_calls(calls)


def _empty_summary() -> Dict[str, Any]:
    return {
        "call_count": 0,
        "failed_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_token_count": False,
        "estimated_cost_usd": 0.0,
        "cost": 0.0,
        "currency": "",
        "cost_status": "unavailable",
        "pricing_fingerprint": "",
        "latency_ms": 0.0,
        "models": [],
        "by_operation": {},
    }


def summarize_calls(calls: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    items = list(calls)
    if not items:
        return _empty_summary()
    currencies = {str(item.get("currency")) for item in items if item.get("currency")}
    pricing_fingerprints = {
        str(item.get("pricing_fingerprint"))
        for item in items
        if item.get("pricing_fingerprint")
    }
    by_operation: Dict[str, Dict[str, Any]] = {}
    for item in items:
        operation = str(item.get("operation") or "unspecified")
        bucket = by_operation.setdefault(
            operation,
            {
                "call_count": 0,
                "failed_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost": 0.0,
                "latency_ms": 0.0,
            },
        )
        bucket["call_count"] += 1
        bucket["failed_call_count"] += int(bool(item.get("error")))
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            bucket[name] += int(item.get(name) or 0)
        bucket["cost"] += float(item.get("cost") or 0.0)
        bucket["latency_ms"] += float(item.get("latency_ms") or 0.0)
    for bucket in by_operation.values():
        bucket["cost"] = round(bucket["cost"], 10)
        bucket["latency_ms"] = round(bucket["latency_ms"], 3)

    pricing_available = all(item.get("cost_status") == "available" for item in items)
    return {
        "call_count": len(items),
        "failed_call_count": sum(1 for item in items if item.get("error")),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in items),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in items),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in items),
        "estimated_token_count": any(
            bool(item.get("estimated_token_count")) for item in items
        ),
        "estimated_cost_usd": round(
            sum(float(item.get("estimated_cost_usd") or 0.0) for item in items),
            8,
        ),
        "cost": round(sum(float(item.get("cost") or 0.0) for item in items), 10),
        "currency": next(iter(currencies)) if len(currencies) == 1 else "",
        "cost_status": "available" if pricing_available and len(currencies) == 1 else "unavailable",
        "pricing_fingerprint": (
            next(iter(pricing_fingerprints)) if len(pricing_fingerprints) == 1 else ""
        ),
        "latency_ms": round(
            sum(float(item.get("latency_ms") or 0.0) for item in items),
            3,
        ),
        "models": sorted(
            {str(item.get("model")) for item in items if item.get("model")}
        ),
        "by_operation": by_operation,
    }


def _record(trace_id: str, call: Dict[str, Any]) -> None:
    if not trace_id:
        return
    with _lock:
        _traces.setdefault(trace_id, []).append(call)


def _characters(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return sum(_characters(getattr(item, "content", item)) for item in value)
    if isinstance(value, dict):
        return sum(_characters(item) for item in value.values())
    return len(str(value or ""))


def _token_usage(response: Any, prompt: Any, output: str) -> tuple[int, int, bool]:
    usage = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, dict):
            usage = metadata.get("token_usage") or metadata.get("usage")
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if isinstance(input_tokens, (int, float)) and isinstance(
            output_tokens, (int, float)
        ):
            return int(input_tokens), int(output_tokens), False

    # A conservative portable fallback for OpenAI-compatible providers which do
    # not include usage metadata. Reports mark this explicitly as an estimate.
    return (
        max(1, math.ceil(_characters(prompt) / 4)),
        max(0, math.ceil(len(output) / 4)),
        True,
    )


class ObservedChatModel:
    """Transparent proxy around a LangChain chat model."""

    def __init__(self, client: Any, trace_id: str, model: str, operation: str = "unspecified"):
        self._client = client
        self._trace_id = trace_id
        self._model = model
        self._operation = operation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def invoke(self, prompt: Any, *args: Any, **kwargs: Any) -> Any:
        self._check_budget()
        started = time.perf_counter()
        try:
            response = self._client.invoke(prompt, *args, **kwargs)
        except Exception as exc:
            self._record_call(prompt, "", started, response=None, error=exc)
            raise
        self._record_call(
            prompt,
            str(getattr(response, "content", response) or ""),
            started,
            response=response,
        )
        return response

    def stream(self, prompt: Any, *args: Any, **kwargs: Any):
        self._check_budget()
        started = time.perf_counter()
        output_parts: list[str] = []
        last_chunk: Any = None
        error: Exception | None = None
        try:
            for chunk in self._client.stream(prompt, *args, **kwargs):
                last_chunk = chunk
                output_parts.append(str(getattr(chunk, "content", "") or ""))
                yield chunk
        except Exception as exc:
            error = exc
            raise
        finally:
            self._record_call(
                prompt,
                "".join(output_parts),
                started,
                response=last_chunk,
                error=error,
            )

    def _record_call(
        self,
        prompt: Any,
        output: str,
        started: float,
        response: Any,
        error: Exception | None = None,
    ) -> None:
        input_tokens, output_tokens, estimated = _token_usage(
            response, prompt, output
        )
        try:
            from knowledge.observability.pricing import price_call

            cost, pricing = price_call(self._model, input_tokens, output_tokens)
        except Exception:
            cost = 0.0
            pricing = {
                "status": "unavailable",
                "currency": "",
                "fingerprint": "",
            }
        _record(
            self._trace_id,
            {
                "model": self._model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "estimated_token_count": estimated,
                # Legacy field retained for old report readers. Native pricing
                # is reported in `cost` + `currency`; CNY must not be mislabeled USD.
                "estimated_cost_usd": 0.0,
                "cost": cost,
                "currency": pricing.get("currency", ""),
                "cost_status": pricing.get("status", "unavailable"),
                "pricing_fingerprint": pricing.get("fingerprint", ""),
                "operation": self._operation,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "error": str(error) if error else "",
            },
        )

    def _check_budget(self) -> None:
        from knowledge.processor.query_process.config import get_config

        config = get_config()
        with _lock:
            calls = list(_traces.get(self._trace_id, []))
        if len(calls) >= config.request_max_model_calls:
            raise RuntimeError("request model-call budget exceeded")
        if sum(int(call.get("total_tokens") or 0) for call in calls) >= config.request_max_tokens:
            raise RuntimeError("request token budget exceeded")
        if sum(float(call.get("cost") or 0.0) for call in calls) >= config.request_max_cost:
            raise RuntimeError("request monetary budget exceeded")
