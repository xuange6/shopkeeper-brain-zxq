"""
嵌入模型工具。

当前封装 BGE-M3 模型加载逻辑，供导入流程生成稠密向量和稀疏向量。
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from pymilvus.model.hybrid import BGEM3EmbeddingFunction

from knowledge.utils.normalize_sparse_vector import normalize_sparse_vector


load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    """从环境变量读取布尔值。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_bge_m3_model_name() -> str:
    """
    解析 BGE-M3 模型位置。

    ModelScope 的缓存目录可能是：
    - D:/ai_models/modelscope_cache/BAAI/bge-m3
    也可能被用户写成：
    - D:/ai_models/modelscope_cache/models/BAAI/bge-m3

    如果 BGE_M3_PATH 不存在，就尝试根据 MODELSCOPE_CACHE 和 BGE_M3 拼出真实路径。
    """
    bge_path = os.getenv("BGE_M3_PATH")
    if bge_path and Path(bge_path).exists():
        return bge_path

    model_id = os.getenv("BGE_M3", "BAAI/bge-m3")
    cache_dir = os.getenv("MODELSCOPE_CACHE")
    if cache_dir:
        candidate = Path(cache_dir) / Path(model_id)
        if candidate.exists():
            return str(candidate)

    return bge_path or model_id


@lru_cache(maxsize=1)
def get_bge_m3_model() -> BGEM3EmbeddingFunction:
    """
    获取 BGE-M3 嵌入模型实例。

    优先使用 .env 中的本地模型路径 BGE_M3_PATH，避免运行时再去联网下载。
    """
    model_name = _resolve_bge_m3_model_name()
    device = os.getenv("BGE_DEVICE", "cuda:0")
    use_fp16 = _env_bool("BGE_FP16", True)

    return BGEM3EmbeddingFunction(
        model_name=model_name,
        device=device,
        use_fp16=use_fp16,
    )


def _extract_sparse_vectors(raw_embeddings, text_count: int) -> List[Dict[int, float]]:
    """从 BGE-M3 的 CSR 稀疏矩阵中提取 Milvus 需要的字典格式。"""
    sparse_matrix = raw_embeddings["sparse"]
    sparse_vectors = []

    for i in range(text_count):
        row_start = sparse_matrix.indptr[i]
        row_end = sparse_matrix.indptr[i + 1]
        sparse_dict = dict(
            zip(
                sparse_matrix.indices[row_start:row_end].tolist(),
                sparse_matrix.data[row_start:row_end].tolist(),
            )
        )
        sparse_vectors.append(normalize_sparse_vector(sparse_dict))

    return sparse_vectors


def generate_hybrid_embeddings(texts: List[str]) -> Dict[str, list]:
    """为文本生成混合嵌入：稠密向量 + 稀疏向量。"""
    if not texts:
        return {"dense": [], "sparse": []}

    model = get_bge_m3_model()
    raw_embeddings = model.encode_queries(texts)

    dense_vectors = [emb.tolist() for emb in raw_embeddings["dense"]]
    sparse_vectors = _extract_sparse_vectors(raw_embeddings, len(texts))

    return {
        "dense": dense_vectors,
        "sparse": sparse_vectors,
    }


if __name__ == "__main__":
    bge_m3 = get_bge_m3_model()
    sample_text = "福禄克15B+数字万用表"
    vectors = bge_m3.encode_documents([sample_text])

    dense_vector = vectors["dense"][0].tolist()
    sparse_matrix = vectors["sparse"]
    start_idx = sparse_matrix.indptr[0]
    end_idx = sparse_matrix.indptr[1]
    token_ids = sparse_matrix.indices[start_idx:end_idx].tolist()
    weights = sparse_matrix.data[start_idx:end_idx].tolist()
    sparse_vector = dict(zip(token_ids, weights))

    print(f"模型加载成功: {bge_m3}")
    print(f"测试文本: {sample_text}")
    print(f"稠密向量维度: {len(dense_vector)}")
    print(f"稀疏向量非零项数量: {len(sparse_vector)}")
