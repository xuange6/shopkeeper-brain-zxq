"""
导入流程节点基类

定义统一的节点接口规范，提供通用功能
"""

from abc import ABC, abstractmethod
from typing import TypeVar, Optional
import logging
import time

from knowledge.processor.import_process.config import ImportConfig, get_config
from knowledge.processor.import_process.exceptions import ImportProcessError
from knowledge.utils.task_utils import add_done_task, add_failed_task, add_running_task
T = TypeVar("T")  # 泛型状态类型


class BaseNode(ABC):
    """
    导入流程节点基类

    所有节点类都应继承此基类，实现 process 方法。
    基类提供统一的日志、任务追踪和错误处理。

    使用示例:
        class MyNode(BaseNode):
            name = "my_node"

            def process(self, state):
                # 实现具体逻辑
                return state

        # 作为 LangGraph 节点使用
        node = MyNode()
        workflow.add_node("my_node", node)
    """

    name: str = "base_node"  # 节点名称，子类应覆盖

    def __init__(self, config: Optional[ImportConfig] = None):
        """
        初始化节点

        Args:
            config: 配置对象，默认使用全局配置
        """
        self.config = config or get_config()
        self.logger = logging.getLogger(f"import.{self.name}")

    def __call__(self, state: T) -> T: # 用call来调用process，自动来调，node(state)
        """
        节点执行入口

        LangGraph 调用节点时会调用此方法。
        提供统一的日志输出、任务追踪和异常处理。

        Args:
            state: 图状态字典

        Returns:
            更新后的状态字典

        Raises:
            ImportProcessError: 节点执行失败时抛出
        """

        task_id = state.get("task_id", "") if isinstance(state, dict) else ""
        if task_id:
            add_running_task(task_id, self.name)

        start_time = time.perf_counter()
        self.logger.info(f"--- {self.name} 开始 ---")


        try:
            result = self.process(state)
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(result, elapsed_seconds)
            self.logger.info(f"--- {self.name} 完成，耗时 {elapsed_seconds:.2f}s ---")
            if task_id:
                add_done_task(task_id, self.name)
            return result
        except ImportProcessError as e:
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(state, elapsed_seconds)
            self.logger.error(f"--- {self.name} 失败，耗时 {elapsed_seconds:.2f}s: {e} ---")
            if task_id:
                add_failed_task(task_id, self.name, str(e))
            # 已经是自定义异常，直接抛出
            raise
        except Exception as e:
            elapsed_seconds = time.perf_counter() - start_time
            self._record_timing(state, elapsed_seconds)
            self.logger.error(f"{self.name} 执行失败，耗时 {elapsed_seconds:.2f}s: {e}")
            if task_id:
                add_failed_task(task_id, self.name, str(e))
            raise ImportProcessError(
                message=str(e),
                node_name=self.name,
                cause=e
            )

    def _record_timing(self, state: T, elapsed_seconds: float) -> None:
        if not isinstance(state, dict):
            return

        timings = state.get("node_timings")
        if not isinstance(timings, dict):
            timings = {}
            state["node_timings"] = timings

        timings[self.name] = round(elapsed_seconds, 3)

    @abstractmethod # 子类必须得实现
    def process(self, state: T) -> T:
        """
        节点核心处理逻辑

        子类必须实现此方法。

        Args:
            state: 图状态字典

        Returns:
            更新后的状态字典
        """
        pass

    def log_step(self, step_name: str, message: str = ""):
        """
        记录步骤日志

        Args:
            step_name: 步骤名称
            message: 附加信息
        """
        log_msg = f"[{step_name}]"
        if message:
            log_msg += f" {message}"
        self.logger.info(log_msg)



# 配置日志格式
def setup_logging(level: int = logging.INFO):
    """
    配置导入流程日志

    Args:
        level: 日志级别
    """
    # 日志输出遵循“阈值过滤”：
    # 只有日志等级 >= level 才会输出。
    # 例如 level=INFO 时，DEBUG 不输出；level=DEBUG 时，DEBUG/INFO 都会输出。
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
