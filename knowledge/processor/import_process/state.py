"""
导入流程状态类型定义

定义完整的状态结构和辅助函数
"""

from typing import TypedDict, List, Dict, Tuple
import copy


# total=False：不是第二个“参数”，而是告诉 TypedDict「列出的键在类型上都是可选的」。
# 这样 LangGraph 节点可以只 return {"pdf_path": "..."} 这种增量 dict，类型检查仍能通过。
# 注意：TypedDict 不会在运行时自动赋值；下面 class 里的 task_id: str 只是类型说明书。
class ImportGraphState(TypedDict, total=False):
    """
    导入流程图状态（类型说明书，不是 dataclass）

    运行时就是普通 dict。真正的初始值在 GRAPH_DEFAULT_STATE 里用 "键": 值 写入。
    与 ImportConfig 不同：Config 是进程级配置；State 是每个导入任务一份，节点间传递。
    """

    # ==================== 任务标识 ====================
    # 以下写法「task_id: str」是类型标注，不是赋值；没有引号、没有 = 右边的值。
    task_id: str  # 任务 ID，用于任务追踪（web 交互时用于实时查看节点处理日志）

    # ==================== 控制标志 ====================
    is_md_read_enabled: bool  # 是否启用 MD 读取
    is_pdf_read_enabled: bool  # 是否启用 PDF 读取

    # ==================== 路径信息 ====================
    import_file_path: str  # 导入文件路径
    file_dir: str  # 导入(出)文件目录
    pdf_path: str  # PDF 文件路径
    md_path: str  # 转换后 Markdown 文件路径

    # ==================== 文件信息 ====================
    file_title: str  # 文件标题（不含扩展名）
    item_name: str  # 识别出的商品/产品名称

    # ==================== 处理中间数据 ====================
    md_content: str  # Markdown 文档内容
    image_contexts: List[Tuple[str, str, Tuple[str, str, str]]]  # 图片上下文列表
    image_summaries: Dict[str, str]  # 图片摘要列表
    chunks: List  # 文档切片列表
    node_timings: Dict[str, float]  # 每个节点的耗时（秒）


# 这里才是运行时赋值：带引号的是 dict 的键，冒号右边是实际默认值（与 class 里的 task_id: str 不同）。
# 启动一次导入任务时，通常用 create_default_state() 复制本 dict，避免多个任务共用同一份可变对象。
GRAPH_DEFAULT_STATE: ImportGraphState = {
    "task_id": "",
    "is_pdf_read_enabled": False,
    "is_md_read_enabled": False,
    "file_dir": "",
    "import_file_path": "",
    "pdf_path": "",
    "md_path": "",
    "file_title": "",
    "md_content": "",
    "image_contexts": [],
    "image_summaries": {},
    "chunks": [],
    "node_timings": {},
    "item_name": "",
}


def create_default_state(**overrides) -> ImportGraphState:
    """
    创建默认状态，支持覆盖

    Args:
        **overrides: 要覆盖的字段 关键字参数 就是要 a = b， c = d这样来接

    Returns:
        新的状态实例

    Examples:
        >>> state = create_default_state(task_id="task_001", import_file_path="doc.pdf")
    """
    state = copy.deepcopy(GRAPH_DEFAULT_STATE)  # 深拷贝，改 state 不会污染上面的全局模板
    state.update(overrides)
    return state


def get_default_state() -> ImportGraphState:
    """
    获取默认状态副本

    Returns:
        状态副本（避免全局污染）
    """
    return copy.deepcopy(GRAPH_DEFAULT_STATE)
