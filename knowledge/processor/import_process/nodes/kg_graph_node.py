"""
知识图谱构建节点。
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from pymilvus import DataType

from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.exceptions import LLMError, MilvusError, Neo4jError
from knowledge.processor.import_process.prompts.knowledge_graph_prompt import KNOWLEDGE_GRAPH_SYSTEM_PROMPT
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.utils.embedding_utils import get_bge_m3_model
from knowledge.utils.llm_utils import get_llm_client
from knowledge.utils.milvus_utils import get_milvus_client
from knowledge.utils.neo4j_util import get_neo4j_driver
from knowledge.security.access_control import normalize_access_metadata


MAX_ENTITY_NAME_LENGTH = 20
MAX_VARCHAR_LENGTH = 65535
DEFAULT_RELATION_TYPE = "RELATED_TO"

ALLOWED_ENTITY_LABELS: Set[str] = {
    "Device", "Part", "Operation", "Step",
    "Warning", "Condition", "Tool",
}

ALLOWED_RELATION_TYPES: Set[str] = {
    "HAS_OPERATION", "HAS_PART", "HAS_STEP", "USES_TOOL",
    "HAS_WARNING", "NEXT_STEP", "AFFECTS", "REQUIRES",
    "MENTIONED_IN", "RELATED_TO",
}

CYPHER_CLEAR_ITEM = """
    MATCH (n {item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version})
    DETACH DELETE n
"""

CYPHER_ENTITY_SCOPE_CONSTRAINT = """
CREATE CONSTRAINT shopkeeper_entity_scope IF NOT EXISTS
FOR (n:Entity)
REQUIRE (n.name, n.item_name, n.tenant_id, n.graph_version) IS UNIQUE
"""

CYPHER_CHUNK_SCOPE_CONSTRAINT = """
CREATE CONSTRAINT shopkeeper_chunk_scope IF NOT EXISTS
FOR (n:Chunk)
REQUIRE (n.id, n.item_name, n.tenant_id, n.graph_version) IS UNIQUE
"""

CYPHER_MERGE_CHUNK = """
    MERGE (c:Chunk {id: $chunk_id, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version})
    SET c.visibility = $visibility, c.acl_readers = $acl_readers
"""

CYPHER_MERGE_ENTITY_TEMPLATE = """
    MERGE (n:Entity {{name: $name, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version}})
    ON CREATE SET
        n.source_chunk_id = $chunk_id,
        n.description = $description
    ON MATCH SET
        n.description = CASE
            WHEN $description <> "" THEN $description
            ELSE coalesce(n.description, "")
        END
    SET n:`{label}`, n.visibility = $visibility, n.acl_readers = $acl_readers
"""

CYPHER_LINK_ENTITY_TO_CHUNK = """
    MATCH (n:Entity {name: $name, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version})
    MATCH (c:Chunk {id: $chunk_id, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version})
    MERGE (n)-[:MENTIONED_IN]->(c)
"""

CYPHER_MERGE_RELATION_TEMPLATE = """
    MATCH (h:Entity {{name: $head, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version}})
    MATCH (t:Entity {{name: $tail, item_name: $item_name, tenant_id: $tenant_id, graph_version: $graph_version}})
    MERGE (h)-[:{rel_type}]->(t)
"""


@dataclass
class ProcessingStats:
    """处理过程统计信息，用于日志和监控。"""

    total_chunks: int = 0
    processed_chunks: int = 0
    failed_chunks: int = 0
    empty_chunks: int = 0
    total_entities: int = 0
    total_relations: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"处理完成: {self.processed_chunks}/{self.total_chunks} 切片成功, "
            f"{self.failed_chunks} 失败, "
            f"{self.empty_chunks} 无实体, "
            f"共 {self.total_entities} 实体 / {self.total_relations} 关系"
        )


class Neo4jGraphWriter:
    """Write extracted graph data into Neo4j."""

    def __init__(self, database: str = ""):
        self.database = database
        self.logger = logging.getLogger(self.__class__.__name__)

    def _session(self, driver):
        if self.database:
            return driver.session(database=self.database)
        return driver.session()

    def clear(self, driver, item_name: str, tenant_id: str, graph_version: str) -> None:
        if not driver:
            raise Neo4jError("Neo4j driver is not available")
        if not item_name:
            raise Neo4jError("item_name is required when clearing Neo4j data")

        try:
            with self._session(driver) as session:
                session.run(CYPHER_ENTITY_SCOPE_CONSTRAINT).consume()
                session.run(CYPHER_CHUNK_SCOPE_CONSTRAINT).consume()
                session.execute_write(
                    lambda tx, name, tenant, version: tx.run(
                        CYPHER_CLEAR_ITEM,
                        item_name=name,
                        tenant_id=tenant,
                        graph_version=version,
                    ),
                    item_name,
                    tenant_id,
                    graph_version,
                )
            self.logger.info("Neo4j old data cleared: item_name=%s", item_name)
        except Exception as exc:
            raise Neo4jError(f"Neo4j clear failed: {exc}", cause=exc)

    def insert(
            self,
            driver,
            entities: List[Dict[str, Any]],
            relations: List[Dict[str, Any]],
            chunk_id: str,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
    ) -> None:
        if not entities:
            return
        if not driver:
            raise Neo4jError("Neo4j driver is not available")

        try:
            with self._session(driver) as session:
                session.execute_write(
                    self._write_graph_tx,
                    entities,
                    relations,
                    chunk_id,
                    item_name,
                    access,
                    graph_version,
                )
            self.logger.debug(
                "Neo4j wrote %d entities and %d relations for chunk %s",
                len(entities),
                len(relations),
                chunk_id,
            )
        except Exception as exc:
            raise Neo4jError(f"Neo4j insert failed: {exc}", cause=exc)

    def _write_graph_tx(
            self,
            tx,
            entities: List[Dict[str, Any]],
            relations: List[Dict[str, Any]],
            chunk_id: str,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
    ) -> None:
        # This transaction writes graph data for the current chunk only.
        # 1. Create or reuse the current Chunk node.
        common = {
            "chunk_id": chunk_id,
            "item_name": item_name,
            "tenant_id": access["tenant_id"],
            "graph_version": graph_version,
            "visibility": access["visibility"],
            "acl_readers": access["acl_readers"],
        }
        tx.run(CYPHER_MERGE_CHUNK, **common)

        for entity in entities:
            name = str(entity.get("name", "")).strip()
            label = str(entity.get("label", "")).strip()
            description = str(entity.get("description", "")).strip()
            if not name or label not in ALLOWED_ENTITY_LABELS:
                continue

            # Neo4j parameters can bind property values, not label names.
            # The label is Cypher syntax, so whitelist it before formatting.
            cypher = CYPHER_MERGE_ENTITY_TEMPLATE.format(label=label)
            # 2. Create or reuse the current Entity node.
            tx.run(
                cypher,
                name=name,
                description=description,
                chunk_id=chunk_id,
                item_name=item_name,
                tenant_id=access["tenant_id"],
                graph_version=graph_version,
                visibility=access["visibility"],
                acl_readers=access["acl_readers"],
            )
            # 3. Link the Entity to the current Chunk.
            tx.run(
                CYPHER_LINK_ENTITY_TO_CHUNK,
                name=name,
                chunk_id=chunk_id,
                item_name=item_name,
                tenant_id=access["tenant_id"],
                graph_version=graph_version,
            )

        # 4. Create business relations between Entity nodes.
        for relation in relations:
            head = str(relation.get("head", "")).strip()
            tail = str(relation.get("tail", "")).strip()
            rel_type = str(relation.get("type", "")).strip()
            if not head or not tail:
                continue
            if rel_type not in ALLOWED_RELATION_TYPES:
                rel_type = DEFAULT_RELATION_TYPE

            # Relationship types are also Cypher syntax, not parameters.
            # Whitelist rel_type before formatting it into the query.
            cypher = CYPHER_MERGE_RELATION_TEMPLATE.format(rel_type=rel_type)
            tx.run(
                cypher,
                head=head,
                tail=tail,
                item_name=item_name,
                tenant_id=access["tenant_id"],
                graph_version=graph_version,
            )


class KnowledgeGraphNode(BaseNode):
    """知识图谱构建节点。"""

    name = "knowledge_graph"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._milvus_writer = MilvusEntityWriter(
            self.config.milvus_url,
            self.config.entity_name_collection,
        )
        self._neo4j_writer = Neo4jGraphWriter(database=self.config.neo4j_database)

    def process(self, state: ImportGraphState) -> ImportGraphState:
        # Step1: 参数校验
        validate_chunks, item_name = self._validate_get_inputs(state)
        access = normalize_access_metadata(
            tenant_id=state.get("tenant_id", "public"),
            visibility=state.get("visibility", "public"),
            acl_readers=state.get("acl_readers") or [],
        )
        graph_version = str(state.get("graph_version") or self.config.kg_graph_version).strip()
        if not graph_version:
            raise ValueError("graph_version is required for isolated KG writes")
        stats = ProcessingStats(total_chunks=len(validate_chunks))
        self.logger.info(f"开始构建知识图谱：{len(validate_chunks)}切片")

        # Step2: 获取客户端
        milvus_client = get_milvus_client()
        neo4j_driver = get_neo4j_driver()

        # Step3: 幂等性处理（清理旧数据）
        self._clear_existing_data(
            item_name,
            access,
            graph_version,
            milvus_client,
            neo4j_driver,
        )

        # Step4: 并发处理每个切片
        self._process_chunks_concurrently(
            stats,
            validate_chunks,
            access,
            graph_version,
            milvus_client,
            neo4j_driver,
        )

        # Step5: 日志打印处理进度
        self.logger.info(stats.summary())
        state["kg_import_stats"] = asdict(stats)
        state["graph_version"] = graph_version
        state["tenant_id"] = access["tenant_id"]
        state["visibility"] = access["visibility"]

        if stats.failed_chunks and self.config.kg_fail_on_partial:
            raise LLMError(
                f"KG import incomplete: {stats.failed_chunks}/{stats.total_chunks} chunks failed",
                node_name=self.name,
            )
        if stats.total_entities == 0 and self.config.kg_require_nonempty:
            raise LLMError(
                "KG import produced no entities",
                node_name=self.name,
            )

        return state

    def _validate_get_inputs(self, state: ImportGraphState) -> Tuple[List[Dict[str, Any]], str]:
        self.log_step("step1", "知识图谱构建参数校验")

        # 1. 获取基础字段
        chunks = state.get("chunks") or []
        global_item_name = str(state.get("item_name", "")).strip()

        # 2. 校验整体 chunks 是否存在
        if not chunks:
            raise ValueError("待提取图谱的切块(chunks)不存在，跳过图谱构建。")

        # 3. 逐个校验 Chunk 的有效性
        validated_chunks = []
        for i, chunk in enumerate(chunks):

            # 3.1 chunk 是否是字典
            if not isinstance(chunk, dict):
                self.logger.warning(f"第 {i} 个 chunk 不是字典类型，已抛弃。")
                continue

            # 3.2 处理 chunk_id
            raw_id = chunk.get("chunk_id")
            chunk_id = str(raw_id).strip() if raw_id is not None else f"kg_chunk_temp_{i}"

            # 3.3 获取 content 内容
            content = str(chunk.get("content", "")).strip()
            if not content:
                self.logger.warning(f"Chunk {chunk_id} 缺少 content，已抛弃。")
                continue

            # 3.4 获取 item_name（chunk 级别优先，全局兜底）
            chunk_item = str(chunk.get("item_name", "")).strip() or global_item_name
            if not chunk_item:
                self.logger.warning(f"Chunk {chunk_id} 缺少 item_name 归属，已抛弃。")
                continue

            # 3.5 更新 chunk 字段
            chunk["chunk_id"] = chunk_id
            chunk["item_name"] = chunk_item
            chunk["content"] = content

            # 3.6 加入有效列表
            validated_chunks.append(chunk)

        # 4. 校验清洗后是否还有有效数据
        if not validated_chunks:
            raise ValueError(f"经过清洗后，没有任何有效的 chunk（{len(validated_chunks)}）可用于构建图谱。")

        self.logger.info(f"参数校验完成: 原始 {len(chunks)} 块 -> 有效 {len(validated_chunks)} 块。")

        item_names = {str(chunk["item_name"]).strip() for chunk in validated_chunks}
        if len(item_names) != 1:
            raise ValueError(
                "one KG import must contain exactly one item_name; "
                f"got {sorted(item_names)}"
            )
        normalized_item_name = next(iter(item_names))
        if global_item_name and global_item_name != normalized_item_name:
            raise ValueError(
                "state item_name does not match chunk item_name: "
                f"{global_item_name!r} != {normalized_item_name!r}"
            )

        return validated_chunks, normalized_item_name

    def _clear_existing_data(
            self,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
            milvus_client: Optional[Any],
            neo4j_driver: Optional[Any],
    ) -> None:
        """导入前清理该 item_name 下的 Neo4j 图谱和 Milvus 实体数据。"""

        # 1. 清理 Neo4j 图谱数据
        self._neo4j_writer.clear(
            neo4j_driver,
            item_name,
            access["tenant_id"],
            graph_version,
        )

        # 2. 清理 Milvus 实体向量数据
        if not milvus_client:
            raise MilvusError("Milvus 客户端获取失败", node_name=self.name)

        collection_name = self.config.entity_name_collection
        if not collection_name:
            raise MilvusError("ENTITY_NAME_COLLECTION 未配置", node_name=self.name)

        try:
            if milvus_client.has_collection(collection_name):
                milvus_client.delete(
                    collection_name=collection_name,
                    filter=(
                        f'item_name == {json.dumps(item_name)} and '
                        f'tenant_id == {json.dumps(access["tenant_id"])} and '
                        f'graph_version == {json.dumps(graph_version)}'
                    ),
                )
                self.logger.info(f"Milvus 旧数据已清理: item_name={item_name}")
        except Exception as e:
            raise MilvusError(f"Milvus 清理失败: {e}", node_name=self.name, cause=e)

    def _process_chunks_concurrently(
            self,
            stats: ProcessingStats,
            validate_chunks: List[Dict[str, Any]],
            access: Dict[str, Any],
            graph_version: str,
            milvus_client: Any,
            neo4j_driver: Any,
    ) -> None:
        """使用线程池并发处理所有切片。"""

        with ThreadPoolExecutor(max_workers=4) as pool:
            # 1. 提交所有任务
            future_to_idx = {}
            for i, chunk in enumerate(validate_chunks):
                content = chunk.get("content")
                chunk_id = str(chunk.get("chunk_id"))
                chunk_item = chunk.get("item_name")

                future = pool.submit(
                    self._process_single_chunk,
                    content,
                    chunk_id,
                    chunk_item,
                    access,
                    graph_version,
                    milvus_client,
                    neo4j_driver,
                )
                future_to_idx[future] = (i, chunk_id)

            # 2. 收集结果（按完成顺序）这里的future是按完成得顺序我们能一个过一个拿出来
            for future in as_completed(future_to_idx):
                idx, chunk_id = future_to_idx[future]
                try:
                    entity_count, relation_count = future.result()
                    stats.processed_chunks += 1
                    stats.total_entities += entity_count
                    stats.total_relations += relation_count
                    if entity_count == 0:
                        stats.empty_chunks += 1
                except Exception as e:
                    stats.failed_chunks += 1
                    msg = f"切片 {chunk_id} 处理失败: {e}"
                    stats.errors.append(msg)
                    self.logger.error(msg)

    def _process_single_chunk(
            self,
            content: str,
            chunk_id: str,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
            milvus_client: Any,
            neo4j_driver: Any,
    ) -> Tuple[int, int]:
        """处理单个切片：LLM 提取 → 解析清洗 → 写入存储。"""

        # 1. LLM 提取结果（实体、关系）
        llm_response = self._llm_extract_graph_with_retry(content)

        # 2. 解析并清洗 LLM 结果
        graph_data = self._parse_and_clean(llm_response)

        # 3. 获取实体、关系
        entities = graph_data.get("entities") or []
        relations = graph_data.get("relations") or []

        # 4. 写入存储
        # 4.1 写入 Milvus，neo4j
        if entities:
            self._milvus_writer.insert(
                milvus_client,
                entities,
                chunk_id,
                content,
                item_name,
                access,
                graph_version,
            )
            # 4.2 写入 Neo4j 图谱结构
            self._neo4j_writer.insert(
                neo4j_driver,
                entities,
                relations,
                chunk_id,
                item_name,
                access,
                graph_version,
            )

        return len(entities), len(relations)

    def _llm_extract_graph_with_retry(self, content: str) -> str:
        """LLM 提取实体、关系，带重试（最多 3 次）。"""

        llm_client = get_llm_client(
            model=os.getenv("KG_MODEL") or None,
            json_mode=True,
        )

        last_error = None
        for attempt in range(1, 4):
            try:
                llm_response = llm_client.invoke([
                    SystemMessage(content=KNOWLEDGE_GRAPH_SYSTEM_PROMPT),
                    HumanMessage(content=f"文本切片\n\n{content}"),
                ])

                result = getattr(llm_response, "content", "").strip()
                if result:
                    return result

            except Exception as e:
                last_error = e
                if attempt < 3:
                    delay = 0.5 * (2 ** (attempt - 1))
                    self.logger.warning(f"LLM 调用失败（第 {attempt} 次），{delay:.1f}s 后重试: {e}")
                    time.sleep(delay)

        raise LLMError(
            "KG extraction returned no content after 3 attempts",
            node_name=self.name,
            cause=last_error,
        )

    def _parse_and_clean(self, llm_response: str) -> Dict[str, List]:
        """解析 LLM 输出的 JSON 并清洗实体和关系。"""

        empty_graph = {"entities": [], "relations": []}

        # 1. 判断 LLM 输出是否有内容
        if not llm_response:
            return empty_graph

        # 2. 清洗 JSON 围栏标记
        cleaned = re.sub(r"^```(?:json)?\s*", "", llm_response.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # 3. 反序列化
        try:
            parsed: Dict[str, Any] = json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise LLMError(
                "KG extraction returned invalid JSON",
                node_name=self.name,
                cause=error,
            ) from error

        # 4. 获取并清洗实体
        raw_entities = parsed.get("entities", [])
        clean_entities = self._clean_entities(raw_entities)

        # 5. 获取清洗后的实体名集合（用于关系校验）
        clean_entity_names = {e["name"] for e in clean_entities}

        # 6. 获取并清洗关系（基于清洗后的实体名）
        raw_relations = parsed.get("relations", [])
        clean_relations = self._clean_relationships(raw_relations, clean_entity_names)

        # 7. 返回清洗后的结果
        return {"entities": clean_entities, "relations": clean_relations}

    def _clean_entities(self, entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        清洗所有实体：
        1. 清理无效项
        2. 截断过长实体名
        3. 去重
        """

        unique_entities: Set[Tuple[str, str]] = set()
        result: List[Dict[str, Any]] = []

        # 1. 遍历每一个实体
        for entity in entities:
            # 1.1 获取并校验实体名
            entity_name = str(entity.get("name", "")).strip()
            if not entity_name:
                continue

            # 1.2 截断过长实体名
            if len(entity_name) > MAX_ENTITY_NAME_LENGTH:
                entity_name = entity_name[:MAX_ENTITY_NAME_LENGTH]

            # 1.3 获取实体标签
            entity_label = str(entity.get("label", "")).strip()
            if entity_label not in ALLOWED_ENTITY_LABELS:
                continue

            # 1.4 去重检查
            unique_key = (entity_name, entity_label)
            if unique_key in unique_entities:
                continue
            unique_entities.add(unique_key)

            # 1.5 构建实体信息
            entity_info: Dict[str, Any] = {"name": entity_name, "label": entity_label}

            # 1.6 获取可选的描述字段
            description = str(entity.get("description", "")).strip()
            if description:
                entity_info["description"] = description

            result.append(entity_info)

        return result

    def _clean_relationships(
            self,
            relations: List[Dict[str, Any]],
            entity_names: Set[str],
    ) -> List[Dict[str, Any]]:
        """
        清理关系：
        1. 白名单校验关系类型
        2. 清除悬空引用（头尾实体不在有效集合中）
        """

        result: List[Dict[str, Any]] = []

        # 1. 遍历所有关系
        for relation in relations:
            # 1.1 获取并截断头实体
            head_entity = str(relation.get("head", "")).strip()
            if not head_entity:
                continue
            head_entity = head_entity[:MAX_ENTITY_NAME_LENGTH]

            # 1.2 获取并截断尾实体
            tail_entity = str(relation.get("tail", "")).strip()
            if not tail_entity:
                continue
            tail_entity = tail_entity[:MAX_ENTITY_NAME_LENGTH]

            # 1.3 校验头尾实体是否在有效实体集合中
            if head_entity not in entity_names or tail_entity not in entity_names:
                continue

            # 1.4 获取并校验关系类型（不在白名单则使用默认值）
            relation_type = str(relation.get("type", "")).strip()
            if relation_type not in ALLOWED_RELATION_TYPES:
                relation_type = DEFAULT_RELATION_TYPE

            # 1.5 添加到结果
            result.append({"head": head_entity, "tail": tail_entity, "type": relation_type})

        return result


class MilvusEntityWriter:
    """负责将实体向量化并写入 Milvus，仅供本模块内部使用。"""

    def __init__(self, milvus_url: str, collection_name: str):
        self.milvus_url = milvus_url
        self.collection_name = collection_name
        self.logger = logging.getLogger(self.__class__.__name__)
        self._collection_lock = threading.Lock()

    def insert(
            self,
            milvus_client: Any,
            entities: List[Dict],
            chunk_id: str,
            content: str,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
    ) -> None:
        """对外唯一入口：将实体写入 Milvus。"""

        # 1. 判断实体是否存在
        if not entities:
            raise ValueError("参数校验失败，实体不存在")

        # 2. 获取去重后的实体名
        entity_names = self._dedupe_entity_names(entities)
        if not entity_names:
            raise ValueError("参数校验失败，无有效实体名")

        # 3. 获取嵌入模型
        bge_ef_model = get_bge_m3_model()
        if bge_ef_model is None:
            raise MilvusError("嵌入模型获取失败")

        # 4. 嵌入向量化
        try:
            embedded_result = bge_ef_model.encode_documents(entity_names)
        except Exception as e:
            raise MilvusError(f"实体嵌入失败: {e}", cause=e)

        vector_dim = self._get_dense_vector_dim(embedded_result)

        # 5. 创建集合（不存在则创建）
        try:
            self._ensure_collection(milvus_client, self.collection_name, vector_dim)
        except Exception as e:
            raise MilvusError(f"Milvus 创建集合失败: {e}", cause=e)

        # 6. 构建记录
        records = self._build_records(
            entity_names,
            embedded_result,
            chunk_id,
            content,
            item_name,
            access,
            graph_version,
        )
        if not records:
            raise MilvusError("构建 Milvus 记录为空")

        # 7. 写入 Milvus
        try:
            milvus_client.insert(collection_name=self.collection_name, data=records)
            self.logger.debug(f"Milvus 写入 {len(records)} 条实体向量")
        except Exception as e:
            raise MilvusError(f"Milvus 插入数据失败: {e}", cause=e)

    @staticmethod
    def _dedupe_entity_names(entities: List[Dict]) -> List[str]:
        """按实体名去重，并保留首次出现顺序。"""

        seen = set()
        entity_names = []
        for entity in entities:
            entity_name = str(entity.get("name", "")).strip()
            if not entity_name or entity_name in seen:
                continue
            seen.add(entity_name)
            entity_names.append(entity_name)
        return entity_names

    @staticmethod
    def _get_dense_vector_dim(embedded_result: Dict[str, Any]) -> int:
        """从嵌入结果中读取稠密向量维度。"""

        if not embedded_result:
            raise ValueError("嵌入结果为空")

        dense_vector_list = embedded_result.get("dense")
        if dense_vector_list is None or len(dense_vector_list) == 0:
            raise ValueError("参数校验失败，稠密向量不存在")

        first_dense = dense_vector_list[0]
        return len(first_dense)

    def _ensure_collection(self, client: Any, collection_name: str, vector_dim: int) -> None:
        """集合不存在则创建（schema + 索引）。"""

        if not collection_name:
            raise MilvusError("ENTITY_NAME_COLLECTION 未配置")

        # Multiple chunk workers may reach first use together. Serialize the
        # check/create section so a collection race cannot turn a healthy chunk
        # into a partial KG import.
        with self._collection_lock:
            if client.has_collection(collection_name=collection_name):
                return

            schema = client.create_schema(enable_dynamic_fields=True)
            schema.add_field(
                field_name="pk",
                datatype=DataType.INT64,
                is_primary=True,
                auto_id=True,
            )
            schema.add_field(
                field_name="entity_name",
                datatype=DataType.VARCHAR,
                max_length=MAX_VARCHAR_LENGTH,
            )
            schema.add_field(
                field_name="dense_vector",
                datatype=DataType.FLOAT_VECTOR,
                dim=vector_dim,
            )
            schema.add_field(
                field_name="sparse_vector",
                datatype=DataType.SPARSE_FLOAT_VECTOR,
            )
            schema.add_field(
                field_name="source_chunk_id",
                datatype=DataType.VARCHAR,
                max_length=MAX_VARCHAR_LENGTH,
            )
            schema.add_field(
                field_name="context",
                datatype=DataType.VARCHAR,
                max_length=MAX_VARCHAR_LENGTH,
            )
            schema.add_field(
                field_name="item_name",
                datatype=DataType.VARCHAR,
                max_length=MAX_VARCHAR_LENGTH,
            )
            schema.add_field(
                field_name="tenant_id",
                datatype=DataType.VARCHAR,
                max_length=128,
            )
            schema.add_field(
                field_name="visibility",
                datatype=DataType.VARCHAR,
                max_length=16,
            )
            schema.add_field(
                field_name="graph_version",
                datatype=DataType.VARCHAR,
                max_length=128,
            )
            schema.add_field(
                field_name="acl_readers",
                datatype=DataType.ARRAY,
                element_type=DataType.VARCHAR,
                max_capacity=128,
                max_length=256,
            )

            index_params = client.prepare_index_params()
            index_params.add_index(
                field_name="dense_vector",
                index_name="dense_vector_index",
                index_type="IVF_FLAT",
                metric_type="COSINE",
                params={"nlist": 128},
            )
            index_params.add_index(
                field_name="sparse_vector",
                index_name="sparse_vector_index",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )

            client.create_collection(
                collection_name=collection_name,
                schema=schema,
                index_params=index_params,
            )

    @staticmethod
    def _build_records(
            entity_names: List[str],
            embedded_result: Dict[str, Any],
            chunk_id: str,
            content: str,
            item_name: str,
            access: Dict[str, Any],
            graph_version: str,
    ) -> List[Dict[str, Any]]:
        """组装插入记录。"""

        # 1. 校验嵌入结果
        if not embedded_result:
            raise ValueError("嵌入结果为空")

        # 2. 获取稠密向量和稀疏向量
        dense_vector_list = embedded_result.get("dense")
        sparse_matrix = embedded_result.get("sparse")

        # 3. 校验向量是否存在
        if dense_vector_list is None or sparse_matrix is None:
            raise ValueError("参数校验失败，向量不存在")

        # 4. 获取对应块的部分内容作为上下文
        context = content[:200]
        records: List[Dict[str, Any]] = []

        # 5. 遍历每一个实体名，构建记录
        for idx, entity_name in enumerate(entity_names):
            # 5.1 边界检查
            if idx >= len(dense_vector_list):
                break

            # 5.2 获取稠密向量
            dense = dense_vector_list[idx]
            if hasattr(dense, "tolist"):
                dense = dense.tolist()

            # 5.3 解构稀疏向量（从 CSR 矩阵中提取当前实体的稀疏向量）
            start = sparse_matrix.indptr[idx]
            end = sparse_matrix.indptr[idx + 1]
            indices = sparse_matrix.indices[start:end].tolist()
            data = sparse_matrix.data[start:end].tolist()
            sparse_dict = dict(zip(indices, data))

            # 5.4 构建单条记录
            record = {
                "entity_name": entity_name,
                "context": context,
                "item_name": item_name,
                "tenant_id": access["tenant_id"],
                "visibility": access["visibility"],
                "acl_readers": access["acl_readers"],
                "graph_version": graph_version,
                "source_chunk_id": chunk_id,
                "dense_vector": dense,
                "sparse_vector": sparse_dict,
            }
            records.append(record)

        return records


def test_kg_extraction():
    """测试：模拟单个切片，跑通 LLM → 解析 → 清洗 → Milvus 写入流程。"""

    setup_logging()

    mock_state: ImportGraphState = {
        "item_name": "万用表",
        "chunks": [
            {
                "content": """# 电池安装
警告: 为防触电，打开电池后盖前，请勿操作仪表并把表笔与电源断开。
1. 把表笔与仪表断开。
2. 用螺丝刀拧开电池后盖上的螺母。
3. 正确安装电池，正负极应一致。
4. 盖上电池后盖并拧紧螺丝钉。
警告: 为防触电，在电池后盖安装和固定之前，请勿操作仪表。
注意: 若仪表出现工作不正常，请检测保险丝和电池是否完好以及是否放在正确的位置。""",
                "chunk_id": "chunk_test_001",
                "item_name": "万用表",
            }
        ],
    }

    node = KnowledgeGraphNode()
    result_state = node.process(mock_state)
    print("知识图谱构建节点测试完成")
    print(f"item_name: {result_state.get('item_name', '')}")
    print(f"chunks 数量: {len(result_state.get('chunks', []))}")


if __name__ == "__main__":
    test_kg_extraction()
