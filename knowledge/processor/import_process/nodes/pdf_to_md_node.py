import json
import shutil
import subprocess
import sys
import time
import os

from pathlib import Path
from knowledge.processor.import_process.base import BaseNode, setup_logging
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.processor.import_process.exceptions import ValidationError,FileProcessingError,PdfConversionError

class PdfToMdNode(BaseNode):
    """
    pdf转换md节点
    """
    name = "pdf_to_md_node"

    def process(self, state: ImportGraphState) -> ImportGraphState:
        """

        Args:
            self:
            state:

        Returns:

        """
        # 1 对参数进行校验

        import_file_path, file_dir_path = self._validate_state_inputs_path(state)
        # 2 利用mineru工具解析pdf, 成为md
        mineru_input_path = self._prepare_mineru_input(import_file_path, file_dir_path)
        process_code = self._exexcute_mineru(mineru_input_path, file_dir_path)
        # 3 获取md的path
        md_path = self._get_md_paths(mineru_input_path, file_dir_path)
        md_path_obj = Path(md_path)
        self._log_mineru_outputs(mineru_input_path, file_dir_path, md_path_obj)
        if process_code != 0 and md_path_obj.exists():
            self.logger.warning(
                "MinerU返回非0状态码 %s，但Markdown已生成，继续执行: %s",
                process_code,
                md_path,
            )
        elif process_code != 0:
            raise PdfConversionError("pdf转换md失败", self.name)

        if not md_path_obj.exists():
            raise PdfConversionError(f"pdf转换md失败，未生成md文件: {md_path}", self.name)

        # 4 更新state 字典的md_path
        state['md_path'] = md_path
        # 5 返回state
        return state

    def _validate_state_inputs_path(self, state: ImportGraphState) -> tuple[Path, Path]:
        """
        对state中的inputs进行校验
        Args:
            self:
            state:

        Returns:

        """
        self.log_step("STep1", "对状态的路径输入参数进行校验")
        # 1. 获取输入pdf文件路径
        import_file_path = state.get("import_file_path", '')

        # 2 获取解析后的输出目录
        file_dir = state.get('file_dir', '')
        # 3. 校验输入的文件(非空判断)
        if not import_file_path:
            raise ValidationError("缺少输入文件路径", self.name)

        # 4. 用Path路径标准化
        import_file_path_obj = Path(import_file_path)

        # 5. 校验时一个真实的路径
        if not import_file_path_obj.exists():
            raise FileProcessingError("输入文件路径不存在", self.name)

        # 6. 判断输出是否为空
        if not file_dir:
            # 默认目录
            file_dir = import_file_path_obj.parent

        # 7. 标准输出目录
        file_dir_path_obj = Path(file_dir)
        file_dir_path_obj.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"解析文件的路径:{import_file_path}")
        self.logger.info(f"输出的目录:{file_dir}")
        return import_file_path_obj, file_dir_path_obj

    def _prepare_mineru_input(self, import_file_path: Path, file_dir_path: Path) -> Path:
        if import_file_path.name.isascii():
            self.logger.info("MinerU input path uses original file: %s", import_file_path)
            return import_file_path

        mineru_input_path = file_dir_path / "mineru_input.pdf"
        shutil.copy2(import_file_path, mineru_input_path)
        self.logger.warning(
            "Copied non-ASCII PDF name for MinerU compatibility: original=%s, mineru_input=%s",
            import_file_path,
            mineru_input_path,
        )
        return mineru_input_path

    def _exexcute_mineru(self, import_file_path, file_dir_path):
        """

        :param import_file_path: 解析文件路径
        :param file_dir_path: 解析文件目录
        :return:
        mineru -p <input_path> -o <output_path>
        """
        self.log_step("step2", "执行MinerU解析pdf")
        # 固定使用本地模型和已经验证可用的 lmdeploy pytorch 后端。
        env = os.environ.copy()
        env["MINERU_MODEL_SOURCE"] = "local"
        env["MINERU_LMDEPLOY_BACKEND"] = "pytorch"
        env["MINERU_LMDEPLOY_DEVICE"] = "cuda"

        # 1. 构建命令行
        cmd = [
            self._get_mineru_command(),
            "-b",
            "hybrid-auto-engine",
            "-p",
            str(import_file_path),
            "-o",
            str(file_dir_path),
        ]
        process_start_time = time.time()
        # 2.执行命令行，自动读取到主进程的环境变量
        output_lines = []
        proc = subprocess.Popen(args=cmd,
                         stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,
                         errors = "replace",# ? 或者空心的菱形
                         text=True, # 输出的字符时字符串，不是字节
                         encoding="utf-8", # 用指定的中文字符集 进行编解码
                         bufsize=1, # 按行缓冲区 一行就放出来
                         env=env
                         )

        # 3.获取日志信息
        for line in proc.stdout:
            output_lines.append(line.rstrip())
            self.logger.info(f"执行MineU产出的日志:{line}")

        # 4.等待子进程做完 如果子进程返回的状态码是0，则成功，反之没有成功
        process_code = proc.wait()
        process_end_time = time.time()
        if process_code == 0:
            self.logger.info(f"MinerU成功解析Pdf文件:{import_file_path}耗时:{process_end_time-process_start_time:.2f}")
        else:
            output_tail = "\n".join(output_lines[-30:])
            self.logger.error(
                "MinerU解析Pdf文件失败:%s\nreturn_code=%s\ncmd=%s\noutput_tail:\n%s",
                import_file_path,
                process_code,
                " ".join(cmd),
                output_tail,
            )
        # 5. 返回状态码
        return process_code

    def _get_mineru_command(self) -> str:
        mineru_cmd = shutil.which("mineru")
        if mineru_cmd:
            return mineru_cmd

        executable_name = "mineru.exe" if os.name == "nt" else "mineru"
        venv_mineru = Path(sys.executable).resolve().parent / executable_name
        if venv_mineru.exists():
            return str(venv_mineru)

        return executable_name

    def _get_md_paths(self, import_file_path: Path, file_dir_path: Path) -> str:
        # name 名字.xxx  stem：只拿名字 后缀：suffix
        file_name = import_file_path.stem

        md_path =  file_dir_path / file_name / "hybrid_auto" / f"{file_name}.md"
        self.logger.info("Expected MinerU markdown path: %s", md_path)
        return str(md_path)

    def _log_mineru_outputs(self, import_file_path: Path, file_dir_path: Path, expected_md_path: Path) -> None:
        output_dir = file_dir_path / import_file_path.stem / "hybrid_auto"
        if not output_dir.exists():
            self.logger.warning("MinerU output dir not found: %s", output_dir)
            return

        generated_files = [str(path) for path in output_dir.iterdir()]
        self.logger.info(
            "MinerU output dir=%s, expected_md_exists=%s, generated_files=%s",
            output_dir,
            expected_md_path.exists(),
            generated_files,
        )
###############测试
if __name__ == '__main__':
    setup_logging()
    pdf_to_md_node = PdfToMdNode()
    demo_dir = Path(__file__).resolve().parents[1] / "import_temp_Dir"
    pdf_to_md_node_init_state = {
        "import_file_path": str(demo_dir / "hak180使用说明书.pdf"),
        "file_dir": str(demo_dir),
    }
    process_result = pdf_to_md_node.process(pdf_to_md_node_init_state)
    print(json.dumps(process_result, indent=4, ensure_ascii=False))
