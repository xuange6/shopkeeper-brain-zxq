"""
导入流程自定义异常类

统一错误处理，提供更清晰的错误信息
"""


class ImportProcessError(Exception):
    """导入流程基础异常"""

    def __init__(self, message: str, node_name: str = "", cause: Exception = None):
        # 把自己负责保管的“零件”存到当前对象的肚子里
        self.node_name = node_name
        self.cause = cause

        # 将核心错误描述 (message) 提交给父类 Exception 去保管
        super().__init__(message)

    def __str__(self):
        parts = []
        # 1. 拼装节点名称：如果有 node_name，就套上中括号，比如 "[excel_parser]"
        if self.node_name:
            parts.append(f"[{self.node_name}]")

        # 2. 拼装核心信息：向父类要回当初传给它的 message，比如 "格式不支持！"
        # 因为在 __init__ 里我们把 message 交给 super().__init__ 保管了
        parts.append(super().__str__())

        # 3. 拼装底层原因：如果有 cause（被捕获的原生错误），就拼在最后
        if self.cause:
            parts.append(f"(原因: {self.cause})")

        # 4. 最后用空格把这个列表里的字符串连起来，形成完整的一句话
        # 最终效果类似："[excel_parser] 格式不支持！ (原因: KeyError('name'))"
        return " ".join(parts)


class ConfigurationError(ImportProcessError):
    """配置错误：环境变量缺失或配置值无效"""
    pass


class FileProcessingError(ImportProcessError):
    """文件处理错误：文件不存在、格式错误、读写失败"""
    pass


class PdfConversionError(FileProcessingError):
    """PDF 转换错误：MinerU 转换失败"""
    pass


class ImageProcessingError(FileProcessingError):
    """图片处理错误：图片总结、上传失败"""
    pass


class DocumentSplitError(ImportProcessError):
    """文档切分错误：切分逻辑异常"""
    pass


class EmbeddingError(ImportProcessError):
    """向量化错误：模型调用失败、向量生成异常"""
    pass


class LLMError(ImportProcessError):
    """LLM 调用错误：API 调用失败、响应解析失败"""
    pass


class StorageError(ImportProcessError):
    """存储错误：数据库操作失败"""
    pass


class MilvusError(StorageError):
    """Milvus 存储错误"""
    pass


class Neo4jError(StorageError):
    """Neo4j 存储错误"""
    pass


class MinioError(StorageError):
    """MinIO 存储错误"""
    pass


class ValidationError(ImportProcessError):
    """数据验证错误：输入数据不符合预期"""
    pass
