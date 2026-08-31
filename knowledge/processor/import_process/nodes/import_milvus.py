"""
Milvus 向量数据入库节点。

负责把上一个向量化节点生成的 chunks 写入 Milvus，并把 Milvus 自动生成的
chunk_id 回填到 state["chunks"] 中，方便后续节点继续使用。
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pymilvus import DataType

from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.config import get_config
from knowledge.processor.import_process.exceptions import ConfigurationError, MilvusError
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.utils.milvus_utils import get_milvus_client


# Milvus VARCHAR 字段最大长度。文本类字段统一使用这个上限。
MAX_VARCHAR_LENGTH = 65535

# part 字段优先使用 INT8；如果当前 pymilvus 版本不支持 INT8，则兜底使用 INT64。
PART_DATATYPE = getattr(DataType, "INT8", DataType.INT64)


@dataclass(frozen=True)
class ScalarFieldSpec:
    """Milvus 普通字段的配置描述。"""

    name: str
    datatype: DataType
    max_length: Optional[int] = None


# 普通标量字段清单；chunk_id、dense_vector、sparse_vector 比较特殊，单独创建。
SCALAR_FIELDS: Sequence[ScalarFieldSpec] = (
    ScalarFieldSpec("content", DataType.VARCHAR, MAX_VARCHAR_LENGTH),
    ScalarFieldSpec("title", DataType.VARCHAR, MAX_VARCHAR_LENGTH),
    ScalarFieldSpec("parent_title", DataType.VARCHAR, MAX_VARCHAR_LENGTH),
    ScalarFieldSpec("part", PART_DATATYPE),
    ScalarFieldSpec("file_title", DataType.VARCHAR, MAX_VARCHAR_LENGTH),
    ScalarFieldSpec("item_name", DataType.VARCHAR, MAX_VARCHAR_LENGTH),
)


class ImportMilvusNode(BaseNode):
    """将已经向量化的 chunks 导入 Milvus。"""

    name = "import_milvus"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        # Step 1: 读取配置和待入库 chunks。
        config = get_config()
        chunks = state.get("chunks", [])

        # Step 1.1: 空 chunks 没有入库意义，直接跳过。
        if not chunks:
            self.logger.warning("chunks is empty, skip Milvus import")
            return state

        # Step 1.2: 检查 Milvus chunks 集合名是否已配置。
        collection_name = config.chunks_collection
        if not collection_name:
            raise ConfigurationError("CHUNKS_COLLECTION is not configured", node_name=self.name)

        # Step 2: 校验 chunks，并获取 dense_vector 的维度。
        valid_chunks, vector_dim = self._validate_chunks(chunks)
        self.log_step(
            "step_1",
            f"prepare to import {len(valid_chunks)} chunks, vector dim: {vector_dim}",
        )

        try:
            # Step 3: 获取 Milvus 客户端连接。
            client = get_milvus_client()

            # Step 4: 确保目标 collection 存在；不存在时自动创建。
            self._ensure_collection(client, collection_name, vector_dim)

            # Step 7: 批量插入数据，并把 Milvus 返回的 chunk_id 回填到 chunks。
            self._insert_and_backfill_ids(client, collection_name, valid_chunks)
        except (ConfigurationError, MilvusError):
            raise
        except Exception as exc:
            raise MilvusError(
                f"Milvus operation failed: {exc}",
                node_name=self.name,
                cause=exc,
            )

        # Step 8: 将带有 chunk_id 的 chunks 写回 state。
        state["chunks"] = self._strip_vector_fields(valid_chunks)
        return state

    def _validate_chunks(self, chunks: List[Any]) -> Tuple[List[Dict[str, Any]], int]:
        """Step 2: 校验 chunks 是否具备入库所需的向量字段。"""

        if not isinstance(chunks, list):
            raise MilvusError("chunks must be a list", node_name=self.name)

        valid_chunks: List[Dict[str, Any]] = []
        vector_dim: Optional[int] = None

        for index, chunk in enumerate(chunks):
            # Step 2.1: 每个 chunk 必须是字典结构。
            if not isinstance(chunk, dict):
                self.logger.warning("skip chunk %d: not a dict", index + 1)
                continue

            dense_vector = chunk.get("dense_vector")
            sparse_vector = chunk.get("sparse_vector")

            # Step 2.2: dense_vector 和 sparse_vector 都必须存在。
            if dense_vector is None or sparse_vector is None:
                self.logger.warning("skip chunk %d: missing dense or sparse vector", index + 1)
                continue

            # Step 2.3: dense_vector 必须是可计算维度的序列。
            if not isinstance(dense_vector, (list, tuple)):
                self.logger.warning("skip chunk %d: dense_vector is not a sequence", index + 1)
                continue

            current_dim = len(dense_vector)
            if current_dim == 0:
                self.logger.warning("skip chunk %d: dense_vector is empty", index + 1)
                continue

            # Step 2.4: sparse_vector 必须是 Milvus 支持的 dict 结构。
            if not isinstance(sparse_vector, dict):
                self.logger.warning("skip chunk %d: sparse_vector is not a dict", index + 1)
                continue

            if not self._sparse_value(sparse_vector):
                self.logger.warning("skip chunk %d: sparse_vector is empty", index + 1)
                continue

            # Step 2.5: 第一条有效数据决定集合的 dense_vector 维度，后续必须保持一致。
            if vector_dim is None:
                vector_dim = current_dim
            elif current_dim != vector_dim:
                self.logger.warning(
                    "skip chunk %d: dense_vector dim %d does not match %d",
                    index + 1,
                    current_dim,
                    vector_dim,
                )
                continue

            valid_chunks.append(chunk)

        if not valid_chunks or vector_dim is None:
            raise MilvusError("no chunks with valid vectors to import", node_name=self.name)

        return valid_chunks, vector_dim

    def _ensure_collection(self, client, collection_name: str, vector_dim: int) -> None:
        """Step 4: 确保 Milvus collection 已存在。"""

        # Step 4.1: collection 已存在时直接复用，保证节点幂等。
        if client.has_collection(collection_name=collection_name):
            self.logger.info("collection %s already exists, skip creation", collection_name)
            return

        self.log_step("step_2", f"create collection: {collection_name}")

        # Step 4.2: 构建 schema 和索引参数。
        schema = self._build_schema(client, vector_dim)
        index_params = self._build_index_params(client)

        # Step 4.3: 创建 collection。
        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.logger.info("collection %s created", collection_name)

    def _build_schema(self, client, vector_dim: int):
        """Step 5: 构建 Milvus collection schema。"""

        # Step 5.1: 开启动态字段，方便兼容后续可能新增的元数据字段。
        schema = client.create_schema(enable_dynamic_fields=True)

        # Step 5.2: chunk_id 是 Milvus 自增主键，插入时不需要手动传入。
        schema.add_field(
            field_name="chunk_id",
            datatype=DataType.INT64,
            is_primary=True,
            auto_id=True,
        )

        # Step 5.3: 批量添加普通标量字段。
        for spec in SCALAR_FIELDS:
            kwargs: Dict[str, Any] = {
                "field_name": spec.name,
                "datatype": spec.datatype,
            }
            if spec.max_length is not None:
                kwargs["max_length"] = spec.max_length
            schema.add_field(**kwargs)

        # Step 5.4: 添加稀疏向量字段，用于关键词/字面匹配。
        schema.add_field(
            field_name="sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )

        # Step 5.5: 添加稠密向量字段，用于语义检索。
        schema.add_field(
            field_name="dense_vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=vector_dim,
        )

        return schema

    def _build_index_params(self, client):
        """Step 6: 构建 dense_vector 和 sparse_vector 的索引参数。"""

        index_params = client.prepare_index_params()

        # Step 6.1: 稠密向量索引，使用 AUTOINDEX 交给 Milvus 自动选择索引策略。
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="AUTOINDEX",
            metric_type="IP",
        )

        # Step 6.2: 稀疏向量索引，使用倒排索引支持关键词检索。
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_inverted_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )

        return index_params

    def _insert_and_backfill_ids(
        self,
        client,
        collection_name: str,
        chunks: List[Dict[str, Any]],
    ) -> None:
        """Step 7: 批量插入 chunks，并回填 Milvus 自动生成的 chunk_id。"""

        self.log_step("step_3", "insert chunks")

        # Step 7.1: 将业务 chunk 整理成 Milvus insert 需要的字段结构。
        rows = [self._to_insert_row(chunk) for chunk in chunks]

        # Step 7.2: 批量插入 Milvus。
        result = client.insert(collection_name=collection_name, data=rows)

        insert_count = self._result_get(result, "insert_count", 0)
        self.logger.info("inserted %s chunks into %s", insert_count, collection_name)

        # Step 7.3: 从插入结果中取回自动生成的主键 ID。
        inserted_ids = self._result_get(result, "ids", [])
        if inserted_ids and len(inserted_ids) == len(chunks):
            # Step 7.4: 将 ID 回填到对应 chunk，后续节点可继续使用。
            for chunk, chunk_id in zip(chunks, inserted_ids):
                chunk["chunk_id"] = str(chunk_id)
            return

        self.logger.warning(
            "chunk_id backfill skipped: got %d ids, expected %d",
            len(inserted_ids) if inserted_ids else 0,
            len(chunks),
        )

    def _to_insert_row(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """将单个 chunk 转换成 Milvus insert 所需的数据行。"""

        return {
            "content": self._string_value(chunk.get("content")),
            "title": self._string_value(chunk.get("title")),
            "parent_title": self._string_value(chunk.get("parent_title")),
            "part": self._int_value(chunk.get("part", 0)),
            "file_title": self._string_value(chunk.get("file_title")),
            "item_name": self._string_value(chunk.get("item_name")),
            "sparse_vector": self._sparse_value(chunk["sparse_vector"]),
            "dense_vector": list(chunk["dense_vector"]),
        }

    @staticmethod
    def _strip_vector_fields(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        compact_chunks = []
        for chunk in chunks:
            compact_chunk = dict(chunk)
            compact_chunk.pop("dense_vector", None)
            compact_chunk.pop("sparse_vector", None)
            compact_chunks.append(compact_chunk)
        return compact_chunks

    @staticmethod
    def _string_value(value: Any) -> str:
        """将值转成字符串，并限制在 Milvus VARCHAR 最大长度内。"""

        if value is None:
            return ""
        return str(value)[:MAX_VARCHAR_LENGTH]

    @staticmethod
    def _int_value(value: Any) -> int:
        """将 part 等字段转成整数，失败时使用 0 兜底。"""

        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _sparse_value(value: Any) -> Any:
        """将 sparse_vector 的 key/value 转成 Milvus 更稳定接受的 int/float。"""

        if not isinstance(value, dict):
            return value

        normalized = {}
        for key, weight in value.items():
            try:
                normalized[int(key)] = float(weight)
            except (TypeError, ValueError):
                continue
        return normalized

    @staticmethod
    def _result_get(result: Any, key: str, default: Any) -> Any:
        """兼容 dict 和对象两种 Milvus 返回结果。"""

        if isinstance(result, dict):
            return result.get(key, default)
        return getattr(result, key, default)


node_import_milvus = ImportMilvusNode()


def _cli_main() -> None:
    setup_logging()

    input_path = os.getenv("IMPORT_MILVUS_TEST_INPUT")
    config = get_config()

    if input_path:
        source_path = Path(input_path)
        if not source_path.exists():
            print(f"input file does not exist: {source_path}")
            return

        with source_path.open("r", encoding="utf-8") as fh:
            content = json.load(fh)

        chunks = content.get("chunks", []) if isinstance(content, dict) else content
        print(f"loaded {len(chunks)} chunks from {source_path}")
    else:
        test_collection = os.getenv("IMPORT_MILVUS_TEST_COLLECTION", "chunks_smoke_test")
        config.chunks_collection = test_collection
        chunks = [
            {
                "content": "Smoke test chunk about paper jam troubleshooting.",
                "title": "Smoke Test 1",
                "parent_title": "Smoke Test",
                "part": 1,
                "file_title": "import_milvus_smoke_test",
                "item_name": "smoke_test_item",
                "dense_vector": [0.1, 0.2, 0.3],
                "sparse_vector": {101: 0.8, 205: 0.4},
            },
            {
                "content": "Smoke test chunk about device setup.",
                "title": "Smoke Test 2",
                "parent_title": "Smoke Test",
                "part": 2,
                "file_title": "import_milvus_smoke_test",
                "item_name": "smoke_test_item",
                "dense_vector": [0.2, 0.1, 0.4],
                "sparse_vector": {"88": 0.7, "99": 0.2},
            },
        ]
        print(
            "IMPORT_MILVUS_TEST_INPUT is not set; "
            f"running smoke test with collection {test_collection!r}."
        )

    state: ImportGraphState = {"chunks": chunks}
    result_state = node_import_milvus.process(state)

    output_chunks = result_state.get("chunks", [])
    chunks_with_id = sum(1 for chunk in output_chunks if chunk.get("chunk_id"))
    print(f"imported chunks: {len(output_chunks)}")
    print(f"chunks with chunk_id: {chunks_with_id}")
    for index, chunk in enumerate(output_chunks[:3], start=1):
        print(f"{index}. chunk_id={chunk.get('chunk_id')} title={chunk.get('title')}")

    if input_path:
        output_path = source_path.with_name(f"{source_path.stem}_ids{source_path.suffix}")
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(result_state, fh, ensure_ascii=False, indent=2)
        print(f"wrote {output_path}")


if __name__ == "__main__":
    _cli_main()
