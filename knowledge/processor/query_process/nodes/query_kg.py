"""知识图谱查询节点。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from langchain_core.messages import HumanMessage, SystemMessage

from knowledge.processor.import_process.nodes.kg_graph_node import MAX_ENTITY_NAME_LENGTH
from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.state import QueryGraphState


ALLOWED_ENTITY_LABELS_CN = (
    "设备(Device)、部件(Part)、操作(Operation)、步骤(Step)、"
    "警告(Warning)、条件(Condition)、工具(Tool)"
)

ENTITY_ALIGN_TOP_K = 3
SEED_NODE_WEIGHT = 2.0
NEIGHBOR_NODE_WEIGHT = 1.0

EntityItemPair = Dict[str, Any]
EntityNode = Dict[str, Any]
Neo4jTriple = Dict[str, Any]

_CYPHER_EXACT_SEEDS = """
MATCH (n:Entity)
WHERE n.item_name = $item_name AND n.name = $entity_name
RETURN n.item_name AS item_name, n.name AS name
"""

_CYPHER_FUZZY_SEEDS = """
MATCH (n:Entity)
WHERE toLower(n.name) CONTAINS toLower($entity_name)
  AND n.item_name = $item_name
RETURN n.item_name AS item_name, n.name AS name
LIMIT $limit
"""

_CYPHER_ONE_HOP_RELATIONS = """
MATCH (seed:Entity {name: $entity_name, item_name: $item_name})-[r]-(nbr:Entity)
WHERE type(r) <> 'MENTIONED_IN' AND nbr.item_name = $item_name
RETURN
  CASE WHEN startNode(r) = seed THEN seed.name ELSE nbr.name END AS head,
  type(r) AS rel,
  CASE WHEN startNode(r) = seed THEN nbr.name ELSE seed.name END AS tail
LIMIT $limit
"""

_CYPHER_LOOKUP_CHUNK = """
UNWIND $nodes_with_weight AS n
MATCH (e:Entity {name: n.entity_name, item_name: n.item_name})
      -[:MENTIONED_IN]->(c:Chunk {item_name: n.item_name})
WITH c, sum(n.weight) AS score, count(e) AS cnt
RETURN c.id AS chunk_id, c.item_name AS item_name, score, cnt
ORDER BY score DESC, cnt DESC, chunk_id ASC
LIMIT $limit
"""

_ENTITY_EXTRACT_SYSTEM_PROMPT = f"""
你是一个知识图谱问答系统的"实体识别"模块。
请从用户问题中抽取用于查询图数据库(Neo4j)的实体名称。

【图谱中存在的实体类型】
{ALLOWED_ENTITY_LABELS_CN}

【约束】
1) 优先抽取上述类型的名词短语（设备名、部件名、操作名、工具名、步骤名、条件、警告等）
2) 每个实体名称不超过 {MAX_ENTITY_NAME_LENGTH} 个字符，超过请截取核心部分
3) 不要输出完整句子，只输出实体关键词
4) 输出必须是严格 JSON，只含一个字段 entities（字符串数组）

【输出示例】
{{"entities": ["电池安装", "螺丝刀", "表笔"]}}
"""


class _EntityExtractor:
    """使用 LLM 从用户问题中抽取图谱实体名。"""

    def __init__(self):
        self._logger = logging.getLogger(self.__class__.__name__)

    def extract(self, question: str) -> List[str]:
        """抽取实体。失败时返回空列表，不阻断查询流程。"""
        if not question.strip():
            return []

        try:
            from knowledge.utils.llm_utils import get_llm_client

            llm = get_llm_client(json_mode=True)
            response = llm.invoke(
                [
                    SystemMessage(content=_ENTITY_EXTRACT_SYSTEM_PROMPT),
                    HumanMessage(content=f"用户问题：{question}"),
                ]
            )
            entities = _parse_entity_json(str(response.content or ""))
            self._logger.info("实体抽取完成: %d 个 %s", len(entities), entities)
            return entities
        except Exception as exc:
            self._logger.error("LLM 实体抽取失败: %s", exc, exc_info=True)
            return []


class _EntityAligner:
    """使用 Milvus 实体向量集合，将抽取实体对齐到入库实体名。"""

    def __init__(self, collection_name: str, min_score: Optional[float] = None):
        self._collection_name = collection_name
        self._min_score = min_score
        self._logger = logging.getLogger(self.__class__.__name__)

    def align(self, entities: List[str], item_names: Optional[List[str]]) -> Dict[str, Any]:
        """
        Returns:
            {
                # 去重后的标准实体名列表，后续用它匹配 Neo4j 的 Entity.name。
                # 如果不同商品下命中同名实体，这里可能出现相同名称；精确配对应看 alignments。
                "aligned_entities": [...],
                # 每个原始实体名的完整对齐记录，用于调试、审计和 fallback。
                "alignments": [...]
            }
        """
        if not entities:
            return {"aligned_entities": [], "alignments": []}

        if not self._collection_name:
            self._logger.warning("ENTITY_NAME_COLLECTION 未配置，跳过实体对齐")
            return self._fallback(entities, item_names, reason="collection_not_configured")

        try:
            from knowledge.utils.embedding_utils import generate_hybrid_embeddings

            # 导入侧写入 ENTITY_NAME_COLLECTION 时，向量化的是实体 name。
            # 查询侧也只对实体名向量化，用来找最接近的入库 entity_name。
            embeddings = generate_hybrid_embeddings(entities)
            dense_list = embeddings.get("dense") or []
            sparse_list = embeddings.get("sparse") or []
        except Exception as exc:
            self._logger.error("实体向量化失败: %s", exc, exc_info=True)
            return self._fallback(entities, item_names, reason="embedding_failed")

        # 用商品名限制实体对齐范围，避免跨商品匹配到同名或近义实体。
        filter_expr = _build_item_filter_expr(item_names)
        alignments: List[Dict[str, Any]] = []
        aligned_entities: List[str] = []
        seen_aligned_keys: Set[Tuple[str, str]] = set()
        item_count = len(item_names or [])
        search_top_k = max(ENTITY_ALIGN_TOP_K, item_count * ENTITY_ALIGN_TOP_K)

        for index, entity in enumerate(entities):
            entity_alignments = self._align_one(
                entity,
                index,
                dense_list,
                sparse_list,
                item_names,
                filter_expr,
                search_top_k,
            )
            alignments.extend(entity_alignments)

            # aligned_entities 只保留后续 Neo4j 查询需要的标准实体名。
            # 去重 key 是 (item_name, aligned_name)：同商品下去重，不同商品下同名实体都保留。
            # 原词 -> 标准词的逐条映射不要依赖下标关系，应看 alignments。
            for alignment in entity_alignments:
                aligned_name = str(alignment.get("aligned", "")).strip()
                item_name = str(alignment.get("item_name", "")).strip()
                if not aligned_name:
                    continue
                key = (item_name, aligned_name)
                if key not in seen_aligned_keys:
                    seen_aligned_keys.add(key)
                    aligned_entities.append(aligned_name)

        self._logger.info("实体对齐完成: %d 个 %s", len(aligned_entities), aligned_entities)
        return {"aligned_entities": aligned_entities, "alignments": alignments}

    def _align_one(
        self,
        entity: str,
        index: int,
        dense_list: List,
        sparse_list: List,
        item_names: Optional[List[str]],
        filter_expr: Optional[str],
        search_top_k: int,
    ) -> List[Dict[str, Any]]:
        if index >= len(dense_list) or index >= len(sparse_list):
            return self._fallback([entity], item_names, reason="embedding_empty")["alignments"]

        try:
            from knowledge.utils.milvus_utils import (
                build_hybrid_search_requests,
                execute_hybrid_search,
                get_milvus_client,
            )

            reqs = build_hybrid_search_requests(
                dense_vector=dense_list[index],
                sparse_vector=sparse_list[index],
                dense_search_params={"metric_type": "COSINE"},
                sparse_search_params={"metric_type": "IP"},
                filter_expr=filter_expr,
                top_k=search_top_k,
            )
            results = execute_hybrid_search(
                client=get_milvus_client(),
                collection_name=self._collection_name,
                search_requests=reqs,
                ranker_weights=(0.5, 0.5),
                normalize_score=True,
                top_k=search_top_k,
                output_fields=["entity_name", "source_chunk_id", "item_name", "context"],
            )
        except Exception as exc:
            self._logger.error("实体对齐失败: %s entity=%s", exc, entity, exc_info=True)
            return self._fallback([entity], item_names, reason="align_failed")["alignments"]

        hits = results[0] if results else []
        if not hits:
            return self._fallback([entity], item_names, reason="no_hit")["alignments"]

        # Milvus 返回的是同一个 filtered result set；这里按 item_name 分组，
        # 每个商品只保留最高分 hit，避免不同商品下同名实体被全局 top1 吞掉。
        best_by_item: Dict[str, Dict[str, Any]] = {}
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            entity_payload = hit.get("entity", hit)
            if not isinstance(entity_payload, dict):
                continue
            item_name = str(entity_payload.get("item_name", "")).strip()
            if not item_name:
                continue

            previous = best_by_item.get(item_name)
            hit_score = _get_hit_score(hit)
            previous_score = _get_hit_score(previous) if previous else None
            if previous is None or (
                hit_score is not None
                and (previous_score is None or hit_score > previous_score)
            ):
                best_by_item[item_name] = hit

        if not best_by_item:
            return self._fallback([entity], item_names, reason="no_valid_item_name")["alignments"]

        alignments: List[Dict[str, Any]] = []
        for item_name, best in best_by_item.items():
            score = _get_hit_score(best)
            if (
                score is not None
                and self._min_score is not None
                and float(score) < float(self._min_score)
            ):
                continue

            entity_payload = best.get("entity", best)
            if not isinstance(entity_payload, dict):
                continue

            # aligned_name 是 Milvus 命中的标准实体名，对应 Neo4j 节点的 name 属性。
            # Neo4j 实际节点通常是 (:Entity:Tool {name: ...}) 这种双标签结构。
            aligned_name = str(entity_payload.get("entity_name", "")).strip()
            if not aligned_name:
                continue

            alignments.append(
                {
                    "original": entity,
                    "aligned": aligned_name,
                    "item_name": item_name,
                    "score": score,
                    # 这里的 chunk 信息来自实体名向量表，仅说明该实体名曾从哪个切片入库。
                    # 它不是最终 KG 证据 chunk；第二天会通过 Neo4j 关系和 MENTIONED_IN 再找证据。
                    "source_chunk_id": entity_payload.get("source_chunk_id"),
                    "context": entity_payload.get("context"),
                    "reason": "top1_per_item",
                }
            )

        if not alignments:
            return self._fallback([entity], item_names, reason="all_below_threshold")["alignments"]

        return alignments

    @staticmethod
    def _fallback(
        entities: List[str],
        item_names: Optional[List[str]],
        *,
        reason: str,
    ) -> Dict[str, Any]:
        cleaned_item_names = [str(name).strip() for name in (item_names or []) if str(name).strip()]
        if not cleaned_item_names:
            cleaned_item_names = [""]

        alignments = [
            {
                "original": entity,
                "aligned": entity,
                "item_name": item_name,
                "reason": reason,
            }
            for entity in entities
            for item_name in cleaned_item_names
        ]

        seen_keys: Set[Tuple[str, str]] = set()
        aligned_entities: List[str] = []
        for alignment in alignments:
            aligned_name = str(alignment.get("aligned", "")).strip()
            item_name = str(alignment.get("item_name", "")).strip()
            key = (item_name, aligned_name)
            if aligned_name and key not in seen_keys:
                seen_keys.add(key)
                aligned_entities.append(aligned_name)

        return {
            "aligned_entities": aligned_entities,
            "alignments": alignments,
        }


class _Neo4jGraphReader:
    """Read seed nodes, one-hop triples, and related chunk ids from Neo4j."""

    def __init__(
        self,
        database: str,
        max_seed_per_node: int,
        max_total_seeds: int,
        max_triples_per_seed: int,
        max_total_triples: int,
        max_total_chunks: int,
    ):
        self._database = database
        self._max_seed_per_node = max_seed_per_node
        self._max_total_seeds = max_total_seeds
        self._max_triples_per_seed = max_triples_per_seed
        self._max_total_triples = max_total_triples
        self._max_total_chunks = max_total_chunks
        self._logger = logging.getLogger(self.__class__.__name__)

    def _session(self):
        from knowledge.utils.neo4j_util import get_neo4j_driver

        driver = get_neo4j_driver()
        if self._database:
            return driver.session(database=self._database)
        return driver.session()

    def find_seed_nodes(self, pairs: List[EntityItemPair]) -> List[EntityNode]:
        """Find Neo4j Entity seed nodes by exact match, then fuzzy fallback."""
        if not pairs:
            return []

        seed_nodes: List[EntityNode] = []
        seen: Set[Tuple[str, str]] = set()

        try:
            with self._session() as session:
                for pair in pairs:
                    item_name = str(pair.get("item_name", "")).strip()
                    entity_name = str(pair.get("entity_name", "")).strip()
                    if not item_name or not entity_name:
                        continue

                    rows = self._execute_seed_query(session, item_name, entity_name)
                    for row in rows:
                        key = (row["item_name"], row["entity_name"])
                        if key not in seen:
                            seen.add(key)
                            seed_nodes.append(row)

                    if len(seed_nodes) >= self._max_total_seeds:
                        seed_nodes = seed_nodes[: self._max_total_seeds]
                        break
        except Exception as exc:
            self._logger.error("种子节点查询异常: %s", exc, exc_info=True)

        self._logger.info("种子节点: %d 个", len(seed_nodes))
        return seed_nodes

    def _execute_seed_query(
        self,
        session: Any,
        item_name: str,
        entity_name: str,
    ) -> List[EntityNode]:
        exact_rows = session.execute_read(
            lambda tx: tx.run(
                _CYPHER_EXACT_SEEDS,
                item_name=item_name,
                entity_name=entity_name,
            ).data()
        )
        exact_nodes = _clean_seed_rows(exact_rows)
        if exact_nodes:
            return exact_nodes

        fuzzy_rows = session.execute_read(
            lambda tx: tx.run(
                _CYPHER_FUZZY_SEEDS,
                item_name=item_name,
                entity_name=entity_name,
                limit=self._max_seed_per_node,
            ).data()
        )
        return _clean_seed_rows(fuzzy_rows)

    def find_one_hop_relations(self, seed_nodes: List[EntityNode]) -> List[Neo4jTriple]:
        """Expand one hop from seed nodes, preserving stored edge direction."""
        if not seed_nodes:
            return []

        triples: List[Neo4jTriple] = []
        seen: Set[Tuple[str, str, str, str]] = set()

        try:
            with self._session() as session:
                for seed in seed_nodes:
                    item_name = str(seed.get("item_name", "")).strip()
                    entity_name = str(seed.get("entity_name", "")).strip()
                    if not item_name or not entity_name:
                        continue

                    seed_triples = session.execute_read(
                        self._execute_one_hop_query,
                        item_name,
                        entity_name,
                        self._max_triples_per_seed,
                    )
                    for triple in seed_triples:
                        key = (
                            triple["item_name"],
                            triple["head"],
                            triple["rel"],
                            triple["tail"],
                        )
                        if key not in seen:
                            seen.add(key)
                            triples.append(triple)

                    if len(triples) >= self._max_total_triples:
                        triples = triples[: self._max_total_triples]
                        break
        except Exception as exc:
            self._logger.error("一跳关系查询异常: %s", exc, exc_info=True)

        self._logger.info("一跳关系: %d 条", len(triples))
        return triples

    @staticmethod
    def _execute_one_hop_query(
        tx: Any,
        item_name: str,
        entity_name: str,
        limit: int,
    ) -> List[Neo4jTriple]:
        rows = tx.run(
            _CYPHER_ONE_HOP_RELATIONS,
            item_name=item_name,
            entity_name=entity_name,
            limit=limit,
        ).data()

        triples: List[Neo4jTriple] = []
        for row in rows:
            head = str(row.get("head", "")).strip()
            rel = str(row.get("rel", "")).strip()
            tail = str(row.get("tail", "")).strip()
            if head and rel and tail:
                triples.append(
                    {
                        "head": head,
                        "rel": rel,
                        "tail": tail,
                        "item_name": item_name,
                    }
                )

        return triples

    def find_nodes_chunk_id(
        self,
        seed_nodes: List[EntityNode],
        one_hop_triples: List[Neo4jTriple],
    ) -> List[Dict[str, Any]]:
        """Find weighted chunk ids through Entity -[:MENTIONED_IN]-> Chunk."""
        nodes_with_weight = _collect_nodes_with_weight(seed_nodes, one_hop_triples)
        if not nodes_with_weight:
            return []

        try:
            with self._session() as session:
                rows = session.execute_read(
                    lambda tx: tx.run(
                        _CYPHER_LOOKUP_CHUNK,
                        nodes_with_weight=nodes_with_weight,
                        limit=self._max_total_chunks,
                    ).data()
                )
        except Exception as exc:
            self._logger.error("chunk 反查异常: %s", exc, exc_info=True)
            return []

        hits: List[Dict[str, Any]] = []
        for row in rows:
            chunk_id = str(row.get("chunk_id", "")).strip()
            item_name = str(row.get("item_name", "")).strip()
            score = _safe_float(row.get("score"), default=0.0)
            if chunk_id and item_name:
                hits.append(
                    {
                        "id": None,
                        "distance": score,
                        "entity": {
                            "chunk_id": chunk_id,
                            "item_name": item_name,
                        },
                    }
                )

        self._logger.info("chunk 反查: %d 条", len(hits))
        return hits


class _ChunkBackfiller:
    """Backfill chunk content from CHUNKS_COLLECTION by chunk_id."""

    def __init__(self, collection_name: str):
        self._collection_name = collection_name
        self._logger = logging.getLogger(self.__class__.__name__)

    def backfill(self, chunk_hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not chunk_hits:
            return []
        if not self._collection_name:
            self._logger.warning("CHUNKS_COLLECTION 未配置，跳过 chunk 回填")
            return []

        chunk_ids = self._extract_chunk_ids(chunk_hits)
        if not chunk_ids:
            return []

        try:
            from knowledge.utils.milvus_utils import get_milvus_client

            chunk_rows = get_milvus_client().query(
                collection_name=self._collection_name,
                filter=_build_chunk_id_filter_expr(chunk_ids),
                output_fields=[
                    "chunk_id",
                    "content",
                    "title",
                    "parent_title",
                    "file_title",
                    "part",
                    "item_name",
                ],
            )
        except Exception as exc:
            self._logger.error("Milvus chunk 回填异常: %s", exc, exc_info=True)
            return []

        chunk_map = {
            str(row.get("chunk_id")): row
            for row in (chunk_rows or [])
            if isinstance(row, dict) and row.get("chunk_id") is not None
        }

        chunks: List[Dict[str, Any]] = []
        seen_chunk_ids: Set[str] = set()
        for hit in chunk_hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            chunk_id = str(entity.get("chunk_id", "")).strip()
            if not chunk_id or chunk_id in seen_chunk_ids:
                continue

            chunk = chunk_map.get(chunk_id)
            if not chunk:
                self._logger.debug("chunk_id=%s 未找到，跳过", chunk_id)
                continue

            seen_chunk_ids.add(chunk_id)
            chunks.append({"entity": chunk, "distance": hit.get("distance", 0.0)})

        self._logger.info("chunk 回填完成: %d / %d", len(chunks), len(chunk_hits))
        return chunks

    @staticmethod
    def _extract_chunk_ids(chunk_hits: List[Dict[str, Any]]) -> List[Union[int, str]]:
        chunk_ids: List[Union[int, str]] = []
        seen: Set[str] = set()

        for hit in chunk_hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            chunk_id = entity.get("chunk_id")
            if chunk_id is None:
                continue

            chunk_id_text = str(chunk_id).strip()
            if not chunk_id_text or chunk_id_text in seen:
                continue

            seen.add(chunk_id_text)
            try:
                chunk_ids.append(int(chunk_id_text))
            except ValueError:
                chunk_ids.append(chunk_id_text)

        return chunk_ids


class QueryKgNode(BaseNode):
    """知识图谱检索节点：实体抽取、对齐、Neo4j 子图查询与 chunk 回填。"""

    name = "query_kg"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        question, item_names = self._parse_input(state)
        if not question:
            return self._empty_result()

        self.log_step("step_1", "抽取查询实体")
        entities = _EntityExtractor().extract(question)

        self.log_step("step_2", "对齐入库实体")
        align_result = _EntityAligner(
            collection_name=self.config.entity_name_collection,
            min_score=self.config.kg_entity_align_min_score,
        ).align(entities, item_names)

        aligned_entities = align_result.get("aligned_entities") or entities
        alignments = align_result.get("alignments") or []

        self.log_step("step_3", "提取商品-实体配对")
        entity_item_pairs = _build_entity_item_pairs(alignments)

        self.log_step("step_4", "查询 Neo4j 种子节点")
        graph_reader = _Neo4jGraphReader(
            # Neo4j 数据库名；为空时使用 Neo4j driver 的默认数据库。
            database=self.config.neo4j_database,
            # 单个实体精确查不到时，模糊匹配最多返回几个候选种子节点。
            max_seed_per_node=self.config.kg_max_seed_candidates,
            # 所有实体累计最多保留多少个种子节点，避免后续一跳扩展过大。
            max_total_seeds=self.config.kg_max_total_seeds,
            # 每个种子节点最多查询多少条一跳业务关系。
            max_triples_per_seed=self.config.kg_max_triples_per_seed,
            # 所有种子累计最多保留多少条一跳关系，控制 prompt 和检索规模。
            max_total_triples=self.config.kg_max_total_triples,
            # 通过 Entity -[:MENTIONED_IN]-> Chunk 反查时最多返回多少个 chunk 候选。
            max_total_chunks=self.config.kg_max_total_chunks,
        )
        seed_nodes = graph_reader.find_seed_nodes(entity_item_pairs)

        self.log_step("step_5", "查询一跳关系")
        one_hop_triples = graph_reader.find_one_hop_relations(seed_nodes)

        self.log_step("step_6", "反查关联 chunk_id")
        chunk_hits = graph_reader.find_nodes_chunk_id(seed_nodes, one_hop_triples)

        self.log_step("step_7", "回填 chunk 文本")
        kg_chunks = _ChunkBackfiller(self.config.chunks_collection).backfill(chunk_hits)
        triples_docs = _one_hop_triples_to_texts(one_hop_triples)

        # step_8 只做流程汇总日志，方便观察本次 KG 检索链路是否正常；
        # 真正写回 graph state、供后续融合/回答节点使用的是下面的 return 字典。
        self.log_step(
            "step_8",
            (
                f"知识图谱流程完成，实体 {len(entities)} 个，对齐 {len(aligned_entities)} 个，"
                f"种子 {len(seed_nodes)} 个，关系 {len(one_hop_triples)} 条，"
                f"chunk {len(kg_chunks)} 个"
            ),
        )

        return {
            # 已经从 chunks_collection 回填正文后的 KG 证据 chunk；
            # entity 内包含 chunk_id/content/title/item_name，distance 是 KG 关联分数。
            "kg_chunks": kg_chunks,
            # 文本版一跳三元组，形如：[商品名] 头实体 -(关系)-> 尾实体；
            # 适合直接作为 LLM 的图谱关系上下文。
            "kg_triples": triples_docs,
            # Neo4j 中实际确认存在的种子节点，由 (item_name, entity_name) 组成。
            "kg_seed_nodes": seed_nodes,
            # 结构化原始一跳三元组，保留给调试、审计或后续代码继续处理。
            "kg_triples_raw": one_hop_triples,
            # LLM 从问题中抽出来的原始实体名。
            "kg_entities": entities,
            # Milvus ENTITY_NAME_COLLECTION 对齐后的标准实体名，用于后续查 Neo4j Entity.name。
            "kg_aligned_entities": aligned_entities,
            # 原始实体名、标准实体名、分数、来源 chunk 等完整对齐记录。
            "kg_alignments": alignments,
        }

    @staticmethod
    def _parse_input(state: QueryGraphState) -> Tuple[str, List[str]]:
        question = state.get("rewritten_query") or state.get("original_query") or ""
        item_names = state.get("item_names") or []
        if not isinstance(item_names, list):
            item_names = []

        # 商品名是查询范围约束，会进入 Milvus/Neo4j 的 item_name 过滤；
        # 这里先从问题里移除，避免 LLM 把商品名误抽成图谱实体名。
        for name in item_names:
            name_text = str(name).strip()
            if not name_text:
                continue
            pattern = r"\s*".join(re.escape(ch) for ch in name_text.replace(" ", ""))
            question = re.sub(pattern, "", question, flags=re.IGNORECASE)

        question = " ".join(question.split()).strip()
        return question, [str(name).strip() for name in item_names if str(name).strip()]

    @staticmethod
    def _empty_result() -> QueryGraphState:
        return {
            "kg_chunks": [],
            "kg_triples": [],
            "kg_seed_nodes": [],
            "kg_triples_raw": [],
            "kg_entities": [],
            "kg_aligned_entities": [],
            "kg_alignments": [],
        }


def _parse_entity_json(llm_response: str) -> List[str]:
    if not llm_response:
        return []

    text = llm_response.strip()
    # LLM 即使开启 json_mode，也可能包一层 ```json 代码围栏。
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logging.getLogger(__name__).error("实体 JSON 解析失败: %.120s", text)
        return []

    raw_entities = data.get("entities", [])
    if not isinstance(raw_entities, list):
        return []

    seen: Set[str] = set()
    entities: List[str] = []
    for item in raw_entities:
        if not isinstance(item, str):
            continue
        # 查询侧截断规则必须和导入侧一致，否则可能匹配不上已入库实体名。
        name = _truncate_entity_name(item)
        if name and name not in seen:
            seen.add(name)
            entities.append(name)

    return entities


def _truncate_entity_name(entity_name: str) -> str:
    return str(entity_name).strip()[:MAX_ENTITY_NAME_LENGTH]


def _build_item_filter_expr(item_names: Optional[List[str]]) -> Optional[str]:
    if not item_names:
        return None

    quoted = ", ".join(json.dumps(str(name), ensure_ascii=False) for name in item_names)
    return f"item_name in [{quoted}]"


def _build_chunk_id_filter_expr(chunk_ids: List[Union[int, str]]) -> str:
    formatted = []
    for chunk_id in chunk_ids:
        if isinstance(chunk_id, int):
            formatted.append(str(chunk_id))
        else:
            formatted.append(json.dumps(str(chunk_id), ensure_ascii=False))
    return f"chunk_id in [{', '.join(formatted)}]"


def _build_entity_item_pairs(alignments: List[Dict[str, Any]]) -> List[EntityItemPair]:
    """Build unique (item_name, entity_name) pairs for exact Neo4j lookup."""
    if not alignments:
        return []

    pairs: List[EntityItemPair] = []
    seen: Set[Tuple[str, str]] = set()
    for alignment in alignments:
        item_name = str(alignment.get("item_name", "")).strip()
        entity_name = str(alignment.get("aligned", "")).strip()
        if not item_name or not entity_name:
            continue

        key = (item_name, entity_name)
        if key not in seen:
            seen.add(key)
            pairs.append({"item_name": item_name, "entity_name": entity_name})

    return pairs


def _clean_seed_rows(rows: List[Dict[str, Any]]) -> List[EntityNode]:
    if not rows:
        return []

    seed_nodes: List[EntityNode] = []
    for row in rows:
        item_name = str(row.get("item_name", "")).strip()
        entity_name = str(row.get("name", "")).strip()
        if item_name and entity_name:
            seed_nodes.append({"item_name": item_name, "entity_name": entity_name})

    return seed_nodes


def _collect_nodes_with_weight(
    seed_nodes: List[EntityNode],
    one_hop_triples: List[Neo4jTriple],
) -> List[Dict[str, Any]]:
    weight_map: Dict[Tuple[str, str], float] = {}

    for seed in seed_nodes or []:
        item_name = str(seed.get("item_name", "")).strip()
        entity_name = str(seed.get("entity_name", "")).strip()
        if item_name and entity_name:
            weight_map[(item_name, entity_name)] = SEED_NODE_WEIGHT

    for triple in one_hop_triples or []:
        item_name = str(triple.get("item_name", "")).strip()
        if not item_name:
            continue
        head = str(triple.get("head", "")).strip()
        tail = str(triple.get("tail", "")).strip()
        if head and (item_name, head) not in weight_map:
            weight_map[(item_name, head)] = NEIGHBOR_NODE_WEIGHT
        if tail and (item_name, tail) not in weight_map:
            weight_map[(item_name, tail)] = NEIGHBOR_NODE_WEIGHT

    return [
        {"item_name": item_name, "entity_name": entity_name, "weight": weight}
        for (item_name, entity_name), weight in weight_map.items()
    ]


def _one_hop_triples_to_texts(triples: List[Neo4jTriple]) -> List[str]:
    docs: List[str] = []
    for triple in triples or []:
        item_name = str(triple.get("item_name", "")).strip()
        head = str(triple.get("head", "")).strip()
        rel = str(triple.get("rel", "")).strip()
        tail = str(triple.get("tail", "")).strip()
        if not head or not rel or not tail:
            continue

        if item_name:
            docs.append(f"[{item_name}] {head} -({rel})-> {tail}")
        else:
            docs.append(f"{head} -({rel})-> {tail}")

    return docs


def _get_hit_score(hit: Any) -> Optional[float]:
    if not isinstance(hit, dict):
        return None

    raw_score = hit.get("distance", hit.get("score"))
    if raw_score is None:
        return None

    try:
        return float(raw_score)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_node_instance = QueryKgNode()


def node_query_kg(state: QueryGraphState) -> QueryGraphState:
    """兼容函数式调用入口。"""
    return _node_instance(state)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging()

    result = node_query_kg(
        {
            "session_id": "test_kg",
            "original_query": "Brother HAK 180 的 LED 指示灯有什么作用？",
            "rewritten_query": "Brother HAK 180 的 LED 指示灯有什么作用？",
            "item_names": ["Brother HAK 180"],
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
