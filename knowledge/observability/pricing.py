"""Versioned model pricing loaded outside business logic."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Tuple


DEFAULT_PRICING_FILE = Path(__file__).resolve().parents[2] / "config/model_pricing.json"


@lru_cache(maxsize=4)
def load_pricing(path_text: str = "") -> Dict[str, Any]:
    path = Path(path_text) if path_text else DEFAULT_PRICING_FILE
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported pricing schema")
    if not payload.get("currency") or not isinstance(payload.get("models"), dict):
        raise ValueError("pricing currency/models are required")
    result = dict(payload)
    result["fingerprint"] = hashlib.sha256(raw).hexdigest()
    result["path"] = str(path)
    return result


def price_call(model: str, input_tokens: int, output_tokens: int) -> Tuple[float, Dict[str, Any]]:
    pricing = load_pricing()
    model_config = (pricing.get("models") or {}).get(model)
    if not isinstance(model_config, dict):
        return 0.0, pricing_metadata(pricing, status="unavailable", model=model)
    tiers = [tier for tier in model_config.get("tiers") or [] if isinstance(tier, dict)]
    tier = next(
        (item for item in tiers if input_tokens <= int(item.get("max_input_tokens") or 0)),
        None,
    )
    if not tier:
        return 0.0, pricing_metadata(pricing, status="unavailable", model=model)
    input_rate = float(tier["input"])
    output_rate = float(tier["output"])
    cost = (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
    metadata = pricing_metadata(pricing, status="available", model=model)
    metadata.update(
        {
            "input_rate_per_1m": input_rate,
            "output_rate_per_1m": output_rate,
            "tier_max_input_tokens": int(tier["max_input_tokens"]),
        }
    )
    return round(cost, 10), metadata


def pricing_metadata(
    pricing: Dict[str, Any] | None = None,
    *,
    status: str = "available",
    model: str = "",
) -> Dict[str, Any]:
    value = pricing or load_pricing()
    return {
        "status": status,
        "provider": value.get("provider", ""),
        "region": value.get("region", ""),
        "currency": value.get("currency", ""),
        "billing_unit": value.get("billing_unit", ""),
        "effective_date": value.get("effective_date", ""),
        "observed_at": value.get("observed_at", ""),
        "source": value.get("source", ""),
        "fingerprint": value.get("fingerprint", ""),
        "model": model,
    }
