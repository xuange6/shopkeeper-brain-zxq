"""BGE reranker utility."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    load_dotenv = None


if load_dotenv:
    load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")


logger = logging.getLogger(__name__)
_reranker_model = None


def get_reranker_model() -> Any:
    """Get the singleton reranker model instance."""
    global _reranker_model

    try:
        if _reranker_model is None:
            from FlagEmbedding import FlagReranker

            model_path = os.getenv("BGE_RERANKER_LARGE")
            device = os.getenv("BGE_RERANKER_DEVICE", "cpu")
            use_fp16 = os.getenv("BGE_RERANKER_FP16", "False").lower() == "true"

            logger.info(
                "Initializing Reranker model, path=%s, device=%s, fp16=%s",
                model_path,
                device,
                use_fp16,
            )

            _reranker_model = FlagReranker(
                model_name_or_path=model_path,
                device=device,
                use_fp16=use_fp16,
            )

            logger.info("Reranker model initialized")

        return _reranker_model
    except Exception as exc:
        logger.error("Failed to initialize Reranker model: %s", exc, exc_info=True)
        return None
