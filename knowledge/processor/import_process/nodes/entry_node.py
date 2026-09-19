import json
from pathlib import  Path

from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.processor.import_process.exceptions import ValidationError

class EntryNode(BaseNode):
    """
    实体节点

    整个导入流程中的位置（第一位）
    作用：对上传的问文件类型做判断（.pdf文件 or 。md文件）
    """
    name = "entry"
    def process(self, state: ImportGraphState) -> ImportGraphState:
        """
        处理文件类型的检测
        Args:
            state: ImportGraphState 该节点处理之前的状态

        Returns:ImportGraphState 该节点处理之后的状态

        """
        self.log_step("Step1", "{获取文件路径")
        import_file_path = state.get("import_file_path")
        file_dir = state.get("file_dir")

        self.log_step("Step2", "{检测文件路径")
        if not file_dir or not import_file_path:
            raise ValidationError("文件目录或者文件不存在", self.name)
        # 使用标准的path对象操作文件逻辑
        # 4. 获取上传文件的后缀
        path = Path(import_file_path)
        suffix = path.suffix.lower()

        #5 判断文件的后缀
        if suffix == '.pdf':
            state["is_pdf_read_enabled"] = True
            state["pdf_path"] = import_file_path
        elif suffix in {'.md', '.markdown'}:
            state["is_md_read_enabled"] = True
            state["md_path"] = import_file_path
        else:
            # 这条是调试细节日志：仅在日志级别设为 DEBUG 时会显示。
            self.logger.debug(f"文件类型{suffix}不支持")
            raise ValidationError(f"文件类型{suffix}不支持", self.name)
        # 6.获取文件的标题名
        file_title = path.stem
        state["file_title"] = file_title
        return state

if __name__ == '__main__':
    demo_dir = Path(__file__).resolve().parents[1] / "import_temp_Dir"
    pdf_path = demo_dir / "hak180使用说明书.pdf"
    setup_logging()
    # 方式1：直接实例该节点对象 调用process的的方法
    test_entry_state = {
        "file_dir": str(demo_dir),
        "import_file_path": str(pdf_path),
    }
    # 实例化节点
    entry_node = EntryNode()

    # 调用process方法
    process_state = entry_node(test_entry_state)

    # 序列化打印
    print(json.dumps(process_state, ensure_ascii=False, indent=4))
