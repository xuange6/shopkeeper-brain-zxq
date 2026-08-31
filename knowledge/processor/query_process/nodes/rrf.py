"""RRF fusion node."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from knowledge.processor.query_process.base import BaseNode
from knowledge.processor.query_process.state import QueryGraphState


class RrfNode(BaseNode):
    name = "rrf"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        embedding_chunks = state.get("embedding_chunks") or []
        hyde_embedding_chunks = state.get("hyde_embedding_chunks") or []
        kg_chunks = state.get("kg_chunks") or []

        search_resources = {
            "embedding": (self._normalize_chunks(embedding_chunks), 1.0),
            "hyde": (self._normalize_chunks(hyde_embedding_chunks), 1.0),
            "kg": (self._normalize_chunks(kg_chunks), self.config.rrf_kg_weight),
        }

        rrf_chunks = self._rrf_merge(
            search_resources=search_resources,
            smoothing_factor=self.config.rrf_k,
            top_n=self.config.rrf_max_results,
        )

        self.log_step(
            "summary",
            (
                f"RRF merged local results: embedding={len(search_resources['embedding'][0])}, "
                f"hyde={len(search_resources['hyde'][0])}, kg={len(search_resources['kg'][0])}, "
                f"output={len(rrf_chunks)}"
            ),
        )

        state["rrf_chunks"] = rrf_chunks
        return state

    @staticmethod
    def _normalize_chunks(raw_chunks: Any) -> List[Dict[str, Any]]:
        """Extract chunk entity payloads from different retrieval result shapes."""
        if not isinstance(raw_chunks, list):
            return []

        normalized_chunks: List[Dict[str, Any]] = []
        for raw_chunk in raw_chunks:
            if not isinstance(raw_chunk, dict):
                continue

            entity = raw_chunk.get("entity")
            if isinstance(entity, dict):
                chunk = dict(entity)
                retrieval_score = raw_chunk.get("distance", raw_chunk.get("score"))
                if retrieval_score is not None:
                    chunk.setdefault("retrieval_score", retrieval_score)
            else:
                chunk = dict(raw_chunk)

            if RrfNode._get_chunk_id(chunk):
                normalized_chunks.append(chunk)

        return normalized_chunks

    @staticmethod
    def _rrf_merge(
        search_resources: Dict[str, Tuple[List[Dict[str, Any]], float]],
        smoothing_factor: int,
        top_n: int,
    ) -> List[Dict[str, Any]]:
        chunk_scores: Dict[str, float] = {}
        chunk_data: Dict[str, Dict[str, Any]] = {}
        chunk_sources: Dict[str, List[str]] = {}
        chunk_ranks: Dict[str, Dict[str, int]] = {}

        for source_name, (docs, weight) in search_resources.items():
            seen_in_source = set()
            for rank, doc in enumerate(docs, 1):
                chunk_id = RrfNode._get_chunk_id(doc)
                if not chunk_id or chunk_id in seen_in_source:
                    continue

                seen_in_source.add(chunk_id)
                chunk_scores[chunk_id] = chunk_scores.get(chunk_id, 0.0) + (
                    float(weight) / (float(smoothing_factor) + rank)
                )

                if (
                    chunk_id not in chunk_data
                    or (
                        not RrfNode._has_content(chunk_data[chunk_id])
                        and RrfNode._has_content(doc)
                    )
                ):
                    chunk_data[chunk_id] = dict(doc)

                chunk_sources.setdefault(chunk_id, []).append(source_name)
                chunk_ranks.setdefault(chunk_id, {})[source_name] = rank

        sorted_chunk_ids = sorted(
            chunk_scores,
            key=lambda chunk_id: (
                -chunk_scores[chunk_id],
                min(chunk_ranks.get(chunk_id, {}).values() or [999999]),
                chunk_id,
            ),
        )
        if top_n:
            sorted_chunk_ids = sorted_chunk_ids[:top_n]

        results: List[Dict[str, Any]] = []
        for chunk_id in sorted_chunk_ids:
            chunk = dict(chunk_data[chunk_id])
            chunk["rrf_score"] = chunk_scores[chunk_id]
            chunk["rrf_sources"] = chunk_sources.get(chunk_id, [])
            chunk["rrf_ranks"] = chunk_ranks.get(chunk_id, {})
            results.append(chunk)

        return results

    @staticmethod
    def _get_chunk_id(doc: Dict[str, Any]) -> str:
        chunk_id = doc.get("chunk_id") or doc.get("id")
        return str(chunk_id).strip() if chunk_id is not None else ""

    @staticmethod
    def _has_content(doc: Dict[str, Any]) -> bool:
        return bool(str(doc.get("content", "")).strip())
