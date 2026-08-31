"""
商品名称识别节点

当前文件先实现课件中的 Step 1 到 Step 6：
- Step 1：验证输入
- Step 2：构造识别上下文
- Step 3：调用 LLM 识别商品名称
- Step 4：回填 item_name 到 state 和 chunks
- Step 5：生成商品名向量
- Step 6：保存商品名到 Milvus

这个节点后续完整职责是：
1. 验证输入
2. 构造商品名识别上下文
3. 调用 LLM 识别商品名称
4. 回填 item_name 到 state 和 chunks
5. 生成商品名向量
6. 保存商品名到 Milvus
"""

from typing import List, Optional, Tuple

from pymilvus import DataType

from knowledge.processor.import_process.base import BaseNode
from knowledge.processor.import_process.config import get_config
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.processor.import_process.exceptions import ValidationError
from knowledge.utils.embedding_utils import get_bge_m3_model
from knowledge.utils.llm_utils import get_llm_client
from knowledge.utils.milvus_utils import get_milvus_client
from knowledge.utils.normalize_sparse_vector import normalize_sparse_vector
from langchain_core.messages import SystemMessage, HumanMessage


class ItemNameRecognitionNode(BaseNode):
    """
    商品名称识别节点。

    当前先落地完整六步：
    1. 检查当前导入状态中是否有可用于商品名识别的基础数据。
    2. 从前 K 个切片中提取 title/content，构造后续 LLM 使用的识别上下文。
    3. 调用 LLM 从文档标题和切片上下文中识别商品名称。
    4. 将识别结果回填到 state 和每个 chunk 中。
    5. 使用 BGE-M3 为商品名称生成稠密向量和稀疏向量。
    6. 将商品名和向量保存到 Milvus 商品名集合。
    """

    name = "item_name_recognition"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """
        执行商品名称识别流程。

        目前先实现：
        - Step 1：验证输入
        - Step 2：构造识别上下文
        - Step 3：调用 LLM 识别商品名称
        - Step 4：回填 item_name
        - Step 5：生成商品名向量
        - Step 6：保存到 Milvus
        """
        config = get_config()

        file_title, chunks = self._validate_inputs(state)
        context = self._build_context(chunks, config.item_name_chunk_k)
        self.logger.info(f"识别上下文长度: {len(context)}")
        item_name = self._recognize_item_name(file_title, context, config)
        self._backfill_item_name(state, chunks, item_name)
        dense_vector, sparse_vector = self._generate_vectors(item_name)
        self.logger.info(
            f"商品名向量状态: dense={dense_vector is not None}, sparse={sparse_vector is not None}"
        )
        self._save_to_milvus(
            state=state,
            file_title=file_title,
            item_name=item_name,
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            config=config,
        )

        return state

    def _validate_inputs(self, state: ImportGraphState) -> Tuple[str, List[dict]]:
        """
        Step 1：验证输入。

        商品名识别需要依赖两个核心输入：
        - file_title：当前文档标题，常常包含商品名称、品牌或型号。
        - chunks：文档切片列表，后续会从前几个切片中提取 title/content 作为 LLM 上下文。

        如果这两个输入缺失，后续就无法稳定识别商品名，因此这里直接抛出 ValidationError。
        """
        self.log_step("step_1", "验证输入")

        file_title = state.get("file_title", "")
        chunks = state.get("chunks", [])

        if not file_title:
            raise ValidationError("file_title 为空", node_name=self.name)

        if not isinstance(chunks, list) or not chunks:
            raise ValidationError("chunks 为空或无效", node_name=self.name)

        self.logger.info(f"文件标题: {file_title}, 切片数: {len(chunks)}")
        return file_title, chunks

    def _build_context(self, chunks: List[dict], k: int, max_chars: int = 2500) -> str:
        """
        Step 2：构造识别上下文。

        从前 K 个切片中提取 title 和 content，拼成一段结构化文本。
        这段 context 会在下一步交给 LLM，用来辅助识别当前文档对应的商品名称。

        这里不会读取全部 chunks，原因是商品名称通常出现在文档开头，
        只取前几个切片可以减少 token 消耗，也能降低无关内容对 LLM 的干扰。
        """
        self.log_step("step_2", "构造识别上下文")

        parts = []
        total = 0

        for i, chunk in enumerate(chunks[:k]):
            if not isinstance(chunk, dict):
                continue

            title = (chunk.get("title") or "").strip()
            content = (chunk.get("content") or "").strip()

            if not (title or content):
                continue

            # 单个切片正文过长时先截断，避免上下文被某一个 chunk 占满。
            if len(content) > 800:
                content = content[:800] + "..."

            piece = f"【切片{i + 1}】\n标题：{title}\n内容：{content}"
            parts.append(piece)
            total += len(piece)

            if total >= max_chars:
                break

        return "\n\n".join(parts)[:max_chars]

    def _recognize_item_name(self, file_title: str, context: str, config) -> str:
        """
        Step 3：调用 LLM 识别商品名称。

        将文件名和第二步构造的切片上下文一起交给大模型，
        要求模型只返回商品名称字符串。如果模型返回空字符串或调用失败，
        则使用 file_title 作为兜底结果，保证导入流程可以继续往下走。
        """
        self.log_step("step_3", "调用 LLM 识别")

        prompt = f"""
请从以下信息中识别出商品名称与型号：
文件名：{file_title}

正文切片（用于辅助识别）：
{context}

要求：
1. 返回内容为字符串形式，最好是带品牌、型号和名称的完整商品名称。比如：苏伯尓5000W大功率电磁炉；
2. 返回结果应该只包含商品名称，不要添加任何解释或其他内容；
3. 如果无法识别商品名称,请返回空字符串。
"""

        try:
            llm = get_llm_client(model=config.item_model, json_mode=False)
            resp = llm.invoke([
                SystemMessage(content="你是商品识别专家，只输出字符串。"),
                HumanMessage(content=prompt),
            ])

            item_name = getattr(resp, "content", "").strip()

            if not item_name:
                self.logger.warning("LLM 未能识别商品名称，使用文件标题")
                item_name = file_title

            self.logger.info(f"识别结果: {item_name}")
            return item_name

        except Exception as e:
            self.logger.warning(f"LLM 调用失败: {e}，使用文件标题作为商品名称")
            return file_title

    def _backfill_item_name(self, state: ImportGraphState, chunks: List[dict], item_name: str):
        """
        Step 4：回填 item_name 到 state 和 chunks。

        第三步只是识别出商品名称，还没有把结果写回导入流程。
        这一步会把 item_name 写入 state，并给当前文档的每个切片都补上同一个 item_name。

        这样后续节点处理 chunks 时，就能知道每个切片属于哪个商品。
        """
        self.log_step("step_4", "回填 item_name")

        state["item_name"] = item_name

        for chunk in chunks:
            chunk["item_name"] = item_name

        state["chunks"] = chunks

    def _generate_vectors(self, item_name: str) -> Tuple[Optional[List[float]], Optional[dict]]:
        """
        Step 5：生成商品名向量。

        使用 BGE-M3 对 item_name 生成两种向量：
        - dense_vector：稠密向量，适合语义相似度匹配。
        - sparse_vector：稀疏向量，适合关键词、型号、数字符号匹配。

        这里向量化的是商品名本身，不是 chunks 的正文内容。
        """
        self.log_step("step_5", "生成向量")

        try:
            bge_m3_ef = get_bge_m3_model()
            vectors = bge_m3_ef.encode_documents([item_name])

            if vectors:
                dense_vector = vectors["dense"][0].tolist()

                sparse_matrix = vectors["sparse"]
                start_idx = sparse_matrix.indptr[0]
                end_idx = sparse_matrix.indptr[1]
                token_ids = sparse_matrix.indices[start_idx:end_idx].tolist()
                weights = sparse_matrix.data[start_idx:end_idx].tolist()
                sparse_vector = dict(zip(token_ids, weights))

                self.logger.info("向量生成成功")
                return dense_vector, sparse_vector

        except Exception as e:
            self.logger.warning(f"向量生成失败: {e}")

        return None, None

    def _save_to_milvus(
            self,
            state: ImportGraphState,
            file_title: str,
            item_name: str,
            dense_vector: Optional[List[float]],
            sparse_vector: Optional[dict],
            config
    ):
        """
        Step 6：保存商品名到 Milvus。

        这里保存的是商品名记录，不是具体 chunk 内容。
        写入的数据包括 file_title、item_name、商品名稠密向量和商品名稀疏向量。
        """
        self.log_step("step_6", "保存到 Milvus")

        if not config.milvus_url or not config.item_name_collection:
            self.logger.warning("Milvus 配置不完整，跳过保存")
            return

        if dense_vector is None or sparse_vector is None:
            self.logger.warning("商品名向量不完整，跳过 Milvus 保存")
            return

        try:
            client = get_milvus_client()
            collection_name = config.item_name_collection

            if not client.has_collection(collection_name=collection_name):
                self._create_item_name_collection(client, collection_name)

            data = {
                "file_title": file_title,
                "item_name": item_name,
                "dense_vector": dense_vector,
                "sparse_vector": normalize_sparse_vector(sparse_vector),
            }

            result = client.insert(collection_name=collection_name, data=[data])
            ids = result.get("ids", [])
            self.logger.info(f"已保存到 Milvus，ID: {ids[0] if ids else '未知'}")

            state["item_name"] = item_name

        except Exception as e:
            self.logger.warning(f"Milvus 保存失败: {e}")

    def _create_item_name_collection(self, client, collection_name: str):
        """
        创建商品名集合。

        dense_vector 用于语义检索，sparse_vector 用于关键词/型号匹配。
        """
        self.logger.info(f"创建集合: {collection_name}")

        schema = client.create_schema(enable_dynamic_fields=True)
        schema.add_field(
            field_name="pk",
            datatype=DataType.VARCHAR,
            is_primary=True,
            auto_id=True,
            max_length=100,
        )
        schema.add_field(
            field_name="file_title",
            datatype=DataType.VARCHAR,
            max_length=65535,
        )
        schema.add_field(
            field_name="item_name",
            datatype=DataType.VARCHAR,
            max_length=65535,
        )
        schema.add_field(
            field_name="dense_vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=1024,
        )
        schema.add_field(
            field_name="sparse_vector",
            datatype=DataType.SPARSE_FLOAT_VECTOR,
        )

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_name="dense_vector_index",
            index_type="AUTOINDEX",
            metric_type="IP",
        )
        index_params.add_index(
            field_name="sparse_vector",
            index_name="sparse_inverted_index",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
        )

        client.create_collection(
            collection_name=collection_name,
            schema=schema,
            index_params=index_params,
        )
        self.logger.info(f"集合 {collection_name} 创建成功")


node_item_name_recognition = ItemNameRecognitionNode()


if __name__ == "__main__":
    import json
    import os
    from pathlib import Path

    from knowledge.processor.import_process.base import setup_logging

    setup_logging()

    default_chunks_path = (
            Path(__file__).resolve().parents[1]
            / "import_temp_Dir"
            / "hak180使用说明书"
            / "hybrid_auto"
            / "chunks.json"
    )
    chunks_path = Path(os.getenv("ITEM_NAME_TEST_CHUNKS", str(default_chunks_path)))

    if not chunks_path.exists():
        print(f"chunks.json 不存在: {chunks_path}")
        print("请先运行 document_spliter_node.py 生成 chunks.json")
    else:
        with chunks_path.open("r", encoding="utf-8") as f:
            chunks = json.load(f)

        state = {
            "file_title": chunks_path.parent.name,
            "chunks": chunks,
        }

        result_state = node_item_name_recognition.process(state)

        print("\n" + "=" * 60)
        print("商品名识别节点测试完成")
        print("=" * 60)
        print(f"item_name: {result_state.get('item_name', '')}")
        print(f"chunks 数量: {len(result_state.get('chunks', []))}")
        if result_state.get("chunks"):
            print(f"首个 chunk 的 item_name: {result_state['chunks'][0].get('item_name', '')}")
