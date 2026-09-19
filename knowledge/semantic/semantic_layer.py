"""可解释的查询语义层。

语义层的职责不是替代 LLM，而是先把问题拆成可观测的路由信号，
让检索策略和证据来源可解释。它对中文、型号、错误码和常见关系问法
做了不依赖第三方分词器的保守处理。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, List


_IDENTIFIER_RE = re.compile(
    r"(?i)(?:[a-z]{1,12}[\-_ ]?\d+[a-z0-9\-_]*|\b\d{2,}[a-z]?\b|[a-z]{1,5}\d{1,})"
)
_SECTION_RE = re.compile(r"(?:第[一二三四五六七八九十百\d]+[章节部分]|目录|章节|概述|简介|讲了什么|有哪些内容)")
_RELATION_RE = re.compile(r"(?:关系|区别|对比|比较|依赖|关联|影响|适配|兼容|先后|为什么)")
_PROCEDURE_RE = re.compile(r"(?:怎么|如何|步骤|操作|安装|排查|维修|故障|解决|配置|测量)")


@dataclass(frozen=True)
class SemanticQueryPlan:
    """查询路由结果，序列化后可直接放入 diagnostics。"""

    mode: str
    exact_terms: List[str]
    intent: str
    needs_tree: bool
    needs_graph: bool
    needs_dense_fallback: bool
    reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _unique(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        value = value.strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            result.append(value)
    return result


def build_query_plan(query: str, retrieval_mode: str = "hybrid") -> SemanticQueryPlan:
    """根据问题特征生成可解释的检索计划。

    ``pageindex_first`` / ``lexical`` 模式不会要求 dense fallback；默认
    ``hybrid`` 模式仍保留 BGE 路径，便于和旧版本做 A/B 对照。
    """

    text = str(query or "").strip()
    exact_terms = _unique(_IDENTIFIER_RE.findall(text))
    reasons: List[str] = []
    section = bool(_SECTION_RE.search(text))
    relation = bool(_RELATION_RE.search(text))
    procedure = bool(_PROCEDURE_RE.search(text))

    if exact_terms:
        reasons.append("命中型号/编号模式，优先精确词法检索")
    if section:
        reasons.append("命中章节导航意图，优先树索引")
    if relation:
        reasons.append("命中关系意图，可补充知识图谱")
    if procedure:
        reasons.append("命中操作/排障意图，保留相邻上下文")

    normalized_mode = (retrieval_mode or "hybrid").strip().lower()
    vectorless = normalized_mode in {"pageindex", "pageindex_first", "lexical", "semantic"}

    if relation:
        intent = "relation"
    elif section:
        intent = "section_navigation"
    elif procedure:
        intent = "procedure"
    elif exact_terms:
        intent = "exact_lookup"
    else:
        intent = "semantic_lookup"

    if vectorless:
        mode = "pageindex_fts"
        needs_dense = False
    elif exact_terms or section:
        mode = "fts_pageindex_dense_fallback"
        needs_dense = True
    else:
        mode = "hybrid_pageindex_dense"
        needs_dense = True

    return SemanticQueryPlan(
        mode=mode,
        exact_terms=exact_terms,
        intent=intent,
        needs_tree=section or not exact_terms,
        needs_graph=relation,
        needs_dense_fallback=needs_dense,
        reasons=reasons or ["未命中特殊模式，使用默认语义检索"] ,
    )
