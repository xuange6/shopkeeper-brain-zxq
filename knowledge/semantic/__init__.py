"""结构化语义层。

这个包不依赖向量模型，提供一条可以单独运行的 PageIndex-style 路径：

* 按章节和页面建立可持久化的层级树；
* 用轻量词法索引兜底型号、错误码和参数检索；
* 根据问题信号生成可解释的检索计划；
* 统一输出带 ``node_id``、``page_no`` 的证据记录。

BGE/Milvus 仍然可以作为 dense fallback，由上层通过环境变量选择。
"""

from .page_index import (
    PageIndex,
    build_page_index,
    discover_index_paths,
    load_page_index,
)
from .semantic_layer import SemanticQueryPlan, build_query_plan

__all__ = [
    "PageIndex",
    "SemanticQueryPlan",
    "build_page_index",
    "build_query_plan",
    "discover_index_paths",
    "load_page_index",
]
