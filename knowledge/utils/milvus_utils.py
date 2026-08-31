"""
Milvus 客户端工具。

统一封装 MilvusClient 创建逻辑，供导入流程中的节点复用。
"""

import os
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from pymilvus import AnnSearchRequest, MilvusClient, WeightedRanker

from knowledge.processor.import_process.exceptions import ConfigurationError


load_dotenv()


@lru_cache(maxsize=1)
def get_milvus_client() -> MilvusClient:
    """
    获取 Milvus 客户端。

    Milvus 是外部向量数据库服务，这里只负责创建 Python 客户端连接。
    """
    milvus_url = os.getenv("MILVUS_URL", "")
    if not milvus_url:
        raise ConfigurationError("MILVUS_URL 未配置")

    return MilvusClient(uri=milvus_url)


def build_hybrid_search_requests(
    dense_vector: List[float],
    sparse_vector: Dict[int, float],
    *,
    dense_search_params: Optional[Dict] = None,
    sparse_search_params: Optional[Dict] = None,
    filter_expr: Optional[str] = None,
    top_k: int = 5,
) -> List[AnnSearchRequest]:
    """构建稠密向量和稀疏向量两路 ANN 检索请求。"""
    dense_search_params = dense_search_params or {"metric_type": "IP"}
    sparse_search_params = sparse_search_params or {"metric_type": "IP"}

    dense_request = AnnSearchRequest(
        data=[dense_vector],
        anns_field="dense_vector",
        param=dense_search_params,
        expr=filter_expr,
        limit=top_k,
    )
    sparse_request = AnnSearchRequest(
        data=[sparse_vector],
        anns_field="sparse_vector",
        param=sparse_search_params,
        expr=filter_expr,
        limit=top_k,
    )

    return [dense_request, sparse_request]


def execute_hybrid_search(
    client: MilvusClient,
    collection_name: str,
    search_requests: List[AnnSearchRequest],
    *,
    ranker_weights: Tuple[float, float] = (0.5, 0.5),
    normalize_score: bool = False,
    top_k: int = 5,
    output_fields: Optional[List[str]] = None,
) -> Optional[List]:
    """执行 Milvus 混合检索，并用 WeightedRanker 融合结果。"""
    output_fields = output_fields or ["item_name"]
    ranker = WeightedRanker(
        ranker_weights[0],
        ranker_weights[1],
        norm_score=normalize_score,
    )

    return client.hybrid_search(
        collection_name=collection_name,
        reqs=search_requests,
        ranker=ranker,
        limit=top_k,
        output_fields=output_fields,
    )
