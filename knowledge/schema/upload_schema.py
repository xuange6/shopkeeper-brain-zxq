"""文件上传接口响应模型。"""

from pydantic import BaseModel


class UploadResponse(BaseModel):
    message: str
    task_id: str
