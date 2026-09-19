"""查询接口相关 Schema。"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000, description="用户查询内容")
    session_id: Optional[str] = Field(
        None,
        min_length=1,
        max_length=128,
        description="会话 ID，不传则自动生成",
    )
    item_names: List[str] = Field(
        default_factory=list,
        max_length=20,
        description="前端可选传入的商品名",
    )
    is_stream: bool = Field(False, description="是否使用 SSE 流式返回")


class SourceReference(BaseModel):
    """答案引用的结构化证据。"""

    index: int = Field(..., ge=1)
    source: str = Field("local", description="local / web")
    title: str = ""
    file_title: str = ""
    parent_title: str = ""
    chunk_id: str = ""
    url: str = ""
    score: Optional[float] = None
    preview: str = ""


class QueryDiagnostics(BaseModel):
    """可安全展示给前端的检索诊断信息，不包含向量或提示词正文。"""

    trace_id: str = ""
    retrieval_counts: Dict[str, int] = Field(default_factory=dict)
    node_timings: Dict[str, float] = Field(default_factory=dict)
    total_time: Optional[float] = None
    model_usage: Dict[str, object] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    message: str = Field(..., description="响应消息")
    session_id: str = Field(..., description="会话 ID")
    answer: str = Field("", description="最终答案")
    done_list: List[str] = Field(default_factory=list, description="已完成节点")
    error: str = Field("", description="错误信息")
    image_urls: List[str] = Field(default_factory=list, description="答案关联图片")
    sources: List[SourceReference] = Field(
        default_factory=list,
        description="答案引用的结构化证据",
    )
    diagnostics: QueryDiagnostics = Field(default_factory=QueryDiagnostics)


class StreamSubmitResponse(BaseModel):
    message: str = Field(..., description="响应消息")
    session_id: str = Field(..., description="会话 ID")
    task_id: str = Field(..., description="任务 ID，前端用它建立 SSE 连接")


class HistoryItem(BaseModel):
    id: str = Field("", alias="_id")
    session_id: str = ""
    role: str = ""
    text: str = ""
    rewritten_query: str = ""
    item_names: List[str] = Field(default_factory=list)
    image_urls: List[str] = Field(default_factory=list)
    sources: List[SourceReference] = Field(default_factory=list)
    diagnostics: QueryDiagnostics = Field(default_factory=QueryDiagnostics)
    ts: Optional[float] = None


class HistoryResponse(BaseModel):
    session_id: str
    items: List[HistoryItem]
