"""任务状态接口响应模型。"""

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    done_list: List[str] = Field(default_factory=list)
    running_list: List[str] = Field(default_factory=list)
    failed_list: List[str] = Field(default_factory=list)
    error: str = ""
    answer: str = ""
    image_urls: List[str] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)
    filename: str = ""
    document_id: str = ""
    sha256: str = ""
    size_bytes: int = 0
    item_name: str = ""
    chunk_count: int = 0
    image_count: int = 0
    node_timings: Dict[str, float] = Field(default_factory=dict)
    created_at: float | None = None
    updated_at: float | None = None
