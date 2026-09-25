"""Aggregate evaluation resource usage without discarding earlier turns.

Quality may be scored on the final turn or representative attempt, but every
executed turn/attempt consumes resources. This module stores no prompt content.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


USAGE_ACCOUNTING_VERSION = "3.0-all-turns-all-attempts-operation-cost"
_COUNTERS = (
    "call_count", "failed_call_count", "input_tokens", "output_tokens", "total_tokens",
)


def combine_model_usage(items: Iterable[dict[str, Any] | None]) -> dict[str, Any]:
    """Sum known usage and fail closed on missing/invalid measurements.

    An explicit zero-call summary is valid. Missing counters or monetary values
    are not silently interpreted as free usage; completeness is tracked separately
    from whether the configured price is known.
    """
    result: dict[str, Any] = {
        **{field: 0 for field in _COUNTERS},
        "estimated_cost_usd": 0.0,
        "cost": 0.0,
        "currency": "",
        "cost_status": "unavailable",
        "pricing_fingerprint": "",
        "by_operation": {},
        "latency_ms": 0.0,
        "estimated_token_count": False,
        "models": [],
        "usage_complete": True,
    }
    models: set[str] = set()
    currencies: set[str] = set()
    pricing_fingerprints: set[str] = set()
    native_cost_seen = False
    for item in items:
        if not isinstance(item, dict):
            result["usage_complete"] = False
            continue
        result["usage_complete"] &= item.get("usage_complete", True) is True
        for field in (*_COUNTERS, "estimated_cost_usd", "latency_ms"):
            value = item.get(field)
            valid = (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0
                and (field not in _COUNTERS or int(value) == value)
            )
            if not valid:
                result["usage_complete"] = False
                continue
            result[field] += int(value) if field in _COUNTERS else value
        token_values = [item.get(field) for field in ("input_tokens", "output_tokens", "total_tokens")]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in token_values):
            result["usage_complete"] = False
        elif token_values[2] != token_values[0] + token_values[1]:
            result["usage_complete"] = False
        result["estimated_token_count"] |= bool(item.get("estimated_token_count"))
        if item.get("cost_status") == "available":
            native_cost_seen = True
            cost = item.get("cost")
            currency = str(item.get("currency") or "")
            fingerprint = str(item.get("pricing_fingerprint") or "")
            if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
                result["usage_complete"] = False
            else:
                result["cost"] += float(cost)
            if currency:
                currencies.add(currency)
            else:
                result["usage_complete"] = False
            if fingerprint:
                pricing_fingerprints.add(fingerprint)
            else:
                result["usage_complete"] = False
        for operation, values in (item.get("by_operation") or {}).items():
            if not isinstance(values, dict):
                result["usage_complete"] = False
                continue
            bucket = result["by_operation"].setdefault(
                str(operation),
                {"call_count": 0, "failed_call_count": 0, "input_tokens": 0,
                 "output_tokens": 0, "total_tokens": 0, "cost": 0.0, "latency_ms": 0.0},
            )
            for field in ("call_count", "failed_call_count", "input_tokens", "output_tokens", "total_tokens"):
                bucket[field] += int(values.get(field) or 0)
            bucket["cost"] += float(values.get("cost") or 0.0)
            bucket["latency_ms"] += float(values.get("latency_ms") or 0.0)
        model_names = item.get("models", [])
        if isinstance(model_names, list):
            models.update(str(value) for value in model_names if value)
        else:
            result["usage_complete"] = False
    result["estimated_cost_usd"] = round(result["estimated_cost_usd"], 8)
    result["cost"] = round(result["cost"], 10)
    if native_cost_seen and len(currencies) == 1 and len(pricing_fingerprints) == 1:
        result["currency"] = next(iter(currencies))
        result["pricing_fingerprint"] = next(iter(pricing_fingerprints))
        result["cost_status"] = "available"
    elif native_cost_seen:
        result["usage_complete"] = False
    for bucket in result["by_operation"].values():
        bucket["cost"] = round(bucket["cost"], 10)
        bucket["latency_ms"] = round(bucket["latency_ms"], 3)
    result["latency_ms"] = round(result["latency_ms"], 3)
    result["models"] = sorted(models)
    return result


def attach_turn_usage(
    response: dict, turn_usages: list, expected_turns: int,
    turn_pipeline_complete: list[bool] | None = None,
) -> dict:
    """Keep final-turn answer/trace, attach all-turn usage to its diagnostics."""
    result = dict(response)
    diagnostics = dict(result.get("diagnostics") or {})
    total = combine_model_usage(turn_usages)
    total["usage_complete"] &= len(turn_usages) == expected_turns
    diagnostics["model_usage"] = total
    diagnostics["evaluation_usage"] = {
        "version": USAGE_ACCOUNTING_VERSION,
        "scope": "all_turns",
        "expected_turn_count": expected_turns,
        "recorded_turn_count": len(turn_usages),
        "turn_model_usage": turn_usages,
    }
    if turn_pipeline_complete is not None:
        diagnostics["evaluation_usage"]["turn_pipeline_complete"] = turn_pipeline_complete
    result["diagnostics"] = diagnostics
    return result
