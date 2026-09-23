"""
BGE-M3 chunk embedding node.

Generate dense and sparse vectors for document chunks.
"""

from typing import List

from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.exceptions import EmbeddingError
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.utils.embedding_utils import get_bge_m3_model
from knowledge.utils.normalize_sparse_vector import normalize_sparse_vector


class BgeEmbeddingNode(BaseNode):
    """
    BGE-M3 embedding node.

    Generate dense and sparse vectors for each chunk for later vector search.
    """

    name = "bge_embedding"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """Execute chunk embedding."""
        config = self.config

        # Step 1: get and validate chunks.
        chunks = state.get("chunks", [])
        if not isinstance(chunks, list) or not chunks:
            raise EmbeddingError("chunks is empty or invalid", node_name=self.name)

        self.log_step("step_1", f"start embedding {len(chunks)} chunks")

        # Step 2: initialize BGE-M3.
        try:
            bge_m3_ef = get_bge_m3_model()
        except Exception as e:
            raise EmbeddingError(
                f"failed to initialize BGE-M3: {e}",
                node_name=self.name,
                cause=e,
            )

        # Step 3: process chunks in batches.
        output_data = []
        batch_size = config.embedding_batch_size

        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            batch_output = self._process_batch(bge_m3_ef, batch, i, len(chunks))
            output_data.extend(batch_output)

        incomplete_ir_rows = [
            row.get("chunk_id")
            for row in output_data
            if row.get("document_id")
            and (row.get("dense_vector") is None or row.get("sparse_vector") is None)
        ]
        if incomplete_ir_rows:
            raise EmbeddingError(
                f"embedding incomplete for {len(incomplete_ir_rows)} IR chunks",
                node_name=self.name,
            )

        # Step 9: update state and return.
        self.log_step("step_2", f"embedding finished, {len(output_data)} chunks")
        state["chunks"] = output_data

        return state

    def _process_batch(
            self,
            bge_m3_ef,
            batch: List[dict],
            start_idx: int,
            total: int,
    ) -> List[dict]:
        """Process one batch of chunks."""
        try:
            # Step 4: build input texts with item_name and content.
            texts = [
                (doc.get("item_name", "") or "") + "\n" + (doc.get("content", "") or "")
                for doc in batch
            ]

            # Step 5: call BGE-M3 to generate embeddings.
            embeddings = bge_m3_ef.encode_documents(texts)

            if not embeddings:
                self.logger.warning(
                    "batch %d-%d failed to generate embeddings",
                    start_idx + 1,
                    start_idx + len(batch),
                )
                return batch

            output = []
            for j, doc in enumerate(batch):
                # Step 6: extract dense vector.
                dense_vector = embeddings["dense"][j].tolist()

                # Step 7: extract and normalize sparse vector.
                start = embeddings["sparse"].indptr[j]
                end = embeddings["sparse"].indptr[j + 1]
                token_ids = embeddings["sparse"].indices[start:end].tolist()
                weights = embeddings["sparse"].data[start:end].tolist()
                sparse_dict = dict(zip(token_ids, weights))
                sparse_vector = normalize_sparse_vector(sparse_dict)

                # Step 8: assemble output item.
                # Preserve parser-neutral identity and provenance metadata. The
                # embedding stage owns vectors only; it must not reshape chunks.
                item = dict(doc)
                item["dense_vector"] = dense_vector
                item["sparse_vector"] = sparse_vector
                output.append(item)

            self.logger.info(
                "successfully processed batch %d-%d/%d",
                start_idx + 1,
                min(start_idx + len(batch), total),
                total,
            )
            return output

        except Exception as e:
            self.logger.error(
                "batch %d-%d failed: %s",
                start_idx + 1,
                start_idx + len(batch),
                e,
            )
            return batch


if __name__ == "__main__":
    setup_logging()

    sample_state: ImportGraphState = {
        "chunks": [
            {
                "title": "Safety",
                "content": "Read the safety instructions before use.",
                "file_title": "Example manual",
                "item_name": "Example Device",
            }
        ]
    }

    BgeEmbeddingNode().process(sample_state)
