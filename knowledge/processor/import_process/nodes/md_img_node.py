import json
import logging
import os
import base64
import mimetypes
import time
from collections import deque
from pathlib import Path
from typing import Tuple, Deque
import re

from openai import OpenAI

from knowledge.processor.import_process.base import BaseNode, setup_logging, T
from knowledge.processor.import_process.state import ImportGraphState
from knowledge.processor.import_process.exceptions import ValidationError, FileProcessingError, ImageProcessingError
from knowledge.processor.import_process.config import get_config, ImportConfig
from knowledge.utils.minio_util import get_minio_client


class MarkDownImageNode(BaseNode):
    """
    处理markdown图片节点类
    """

    name = "md_img_node"
    def process(self, state: ImportGraphState) -> ImportGraphState:
        """
        执行图片处理流程。

        Args:
            state (ImportGraphState): 当前导入图的状态字典。

        Returns:
            ImportGraphState: 更新后的状态字典。
        """
        config = get_config()
        md_content, md_path_obj, image_dir = self._get_md_content_and_path(state)
        state["md_content"] = md_content

        if not image_dir.exists():
            self.logger.info("未找到 images 目录，跳过图片处理流程。")
            return state

        target_images_info = self._scan_and_filter_images(
            md_content, image_dir, config.image_extensions
        )

        if not target_images_info:
            self.logger.info("未在 Markdown 中找到需要处理的有效图片引用。")
            return state

        minio_client = get_minio_client()

        image_summaries = self._generate_image_summaries(
            md_path_obj.stem,
            target_images_info,
            config.requests_per_minute,
            config
        )
        state["image_contexts"] = target_images_info
        state["image_summaries"] = image_summaries

        new_md_content = self._upload_images_and_replace_links(
            minio_client,
            md_path_obj.stem,
            target_images_info,
            image_summaries,
            md_content,
            config
        )
        state["md_content"] = new_md_content

        new_md_file_path = self._backup_new_md_file(state["md_path"], new_md_content)
        state["md_path"] = new_md_file_path

        return state

    def _get_md_content_and_path(self, state: ImportGraphState) -> Tuple[str, Path, Path]:
        """

        :param ImportGraphState: 上一个节点处理之后state的最新状态
        :return:
            md_content:md的内容
            md_path:md的路径
            image_dir:图片目录
        """
        self.log_step("step_1", "读取md内容以及构建图片目录")
        md_path = state.get('md_path', '')
        if not md_path:
            raise ValidationError("缺少md_path", self.name)
        md_path_obj = Path(md_path)
        if not md_path_obj.exists():
            raise FileProcessingError("md_path路径不存在", self.name)
        with open(md_path_obj, 'r', encoding='utf-8') as f:
            md_content = f.read()
        image_dir = md_path_obj.parent / "images"
        return md_content, md_path_obj, image_dir

    def _scan_and_filter_images(
            self,
            md_content: str,
            images_dir_obj: Path,
            image_extensions
    ):
        self.log_step("step_2", f"扫描文件目录{images_dir_obj}")
        target_images_info = []
        for img_name in sorted(os.listdir(images_dir_obj)):
            file_ext = os.path.splitext(img_name)[1].lower()
            if file_ext not in image_extensions:
                continue

            img_path_obj = images_dir_obj / img_name
            if not img_path_obj.is_file():
                continue

            if img_name not in md_content:
                continue

            target_images_info.append((img_name, str(img_path_obj)))

        return target_images_info

    def _generate_image_summaries(
            self,
            document_name: str,
            target_images_info,
            requests_per_minute: int,
            config: ImportConfig
    ):
        summaries = {}
        self.log_step("step_3", "提取图片摘要")
        if not config.vl_model:
            self.logger.error("缺少 VL_MODEL 配置，无法生成图片摘要")
            return summaries

        request_timestamps: Deque[float] = deque()
        requests_per_minute = max(1, requests_per_minute)
        try:
            client = OpenAI(
                api_key=config.openai_api_key,
                base_url=config.openai_api_base,
            )
        except Exception as e:
            self.logger.error(f"VLM客户端创建失败: {e}")
            return summaries

        for img_name, img_path in target_images_info:
            self._enforce_rate_limit(request_timestamps, requests_per_minute)
            try:
                summaries[img_name] = self._get_img_summary(config, client, document_name, img_path, ("", "", ""))
            except Exception as e:
                self.logger.error(f"{img_name} 图片摘要生成失败: {e}")
                summaries[img_name] = ""
        return summaries

    def _upload_images_and_replace_links(
            self,
            minio_client,
            document_name,
            target_images_info,
            image_summaries,
            md_content,
            config,
    ):
        self.log_step("step_4", "上传图片并替换 Markdown 链接")
        if minio_client is None:
            self.logger.warning("无法获取MinIO客户端，跳过图片上传和MD替换")
            return md_content
        if not config.minio_bucket:
            self.logger.warning("缺少 MINIO_BUCKET_NAME 配置，跳过图片上传和MD替换")
            return md_content

        remote_urls = {}
        for img_name, img_path in target_images_info:
            object_name = f"{document_name}/{img_name}"
            try:
                minio_client.fput_object(config.minio_bucket, object_name, img_path)
                remote_urls[img_name] = (
                    config.get_minio_base_url() + '/' + config.minio_bucket + '/' + object_name
                )
            except Exception:
                remote_urls[img_name] = ""

        return self._replace_md_images_with_remote_urls(md_content, image_summaries, remote_urls)

    def _scan_images_and_context(
            self,
            image_dir: Path,
            md_content: str,
            config: ImportConfig
    ) -> list[tuple[str, str, tuple[str, str, str] | list[tuple[str, str, str]]]]:
        """
        扫描并处理图片
        返回所有有效图片的丰富信息(image.name, image.path, 图片上下文)
        图片上下文 上文 图片 下文 找上文和下文的策略通过max_char_num(max_total) 丢失语义 混合策略（再使用混合切）
        最终获取上下文策略：1.先找到当前图片的最近一个标题（定位位置以及标题的内容）
                        2.从图片内容的上一行开始向上找，一直找到最近的标题的下一行
                        3.根据开始索引和结束索引定位到两个索引间的内容，再利用段落和最大字符数，选择留下多少

        :param md_path_obj: md文件的路径
        :param image_dir: 图片目录
        :param md_content: md内容
        :param config: 配置信息
        :return:
        """
        self.log_step("step2", f"扫描文件目录{image_dir}")
        image_extensions = config.image_extensions
        # 1 遍历图片文件目录,每一张图片
        target_images_context = []
        for img_name in sorted(os.listdir(image_dir)):
            file_ext = os.path.splitext(img_name)[1].lower()
            # 1.1后缀不是有效的
            if file_ext not in image_extensions:
                continue # 继续处理下一个

            # 1.2构建image_path
            img_path_obj = image_dir / img_name
            if not img_path_obj.is_file():
                continue
            img_path = str(img_path_obj)

            # 1.3构建图片（上下文）

            img_context = self._find_img_context_with_limit(md_content, img_name, config.img_context_length)
            # 1.4 如果读到了上下文，就只保留第一份有效上下文。
            if not img_context:
                self.logger.warning(f"MD文件中暂未提取到可用的图片: {img_name}")
                continue

            primary_img_context = img_context[0]

            # 1.5 存储到列表
            target_images_context.append((img_name, img_path, primary_img_context))

        self.logger.info(f"共扫描到 {len(target_images_context)} 张有效图片")
        return target_images_context


    def _find_img_context_with_limit(self, md_context: str, img_name: str, max_chars: int = 200) -> list[Tuple[str, str, str]]:
        """
        从md中提取图片上下文信息
        :param md_context: 要操作的md
        :param img_name: 要定位的图片地址
        :return: list[Tuple[str, str, str]]
        离图片最近的上一个标题，图片的上文，图片的下文
        """

        # 1. 定义正则规则：从 Markdown 或 HTML 中找到包含 img_name 的图片语法。
        # 支持两种常见写法：
        # - Markdown: ![图片描述](图片地址 "可选提示")
        # - HTML: <img src="图片地址"/>
        # re.compile 是“预编译正则表达式”，会把字符串规则变成 Pattern 对象，后面可用 search/finditer 查找。
        # re.escape(img_name) 会把文件名里的 "." 等特殊字符转成普通字符，避免 a.jpg 被当成 a + 任意字符 + jpg。
        re_pattern = re.compile(
            r"(?:!\[.*?\]\([^)]*" + re.escape(img_name) + r"(?:\s+['\"].*?['\"])?\s*\)"
            r"|<img[^>]*src=['\"][^'\"]*" + re.escape(img_name) + r"[^'\"]*['\"][^>]*>)"
        )

        # 2.从md中先定位图片位置(按行切分md内容 遍历每一行看是否满足按图片的正则模式)
        img_context = []
        md_lines = md_context.split("\n")
        for line_idx, line in enumerate(md_lines):
            # a.没有找到继续找下一行
            if not re_pattern.search(line):
                continue

            # b.找到了图片所在行：定位最近标题、提取上文、提取下文。
            # 找图片的上文
            # b.1 找标题：从图片上一行开始，向上倒着找最近的 Markdown 标题。
            head_title = ""
            head_index = -1
            for current_idx in range(line_idx - 1, -1, -1):
                current_line = md_lines[current_idx]
                if re.match(r"^#{1,6}\s+", current_line):
                    head_title = current_line.strip()
                    head_index = current_idx
                    break

            # b.2 定义要截取的上文范围：标题下一行 -> 图片上一行。
            pre_content_start_index = head_index + 1
            pre_content = md_lines[pre_content_start_index:line_idx]
            img_pre_context = self._extract_img_context(pre_content, max_chars, direction="front")

            # b.3 找图片的下文：从图片下一行开始，向下找到下一个 Markdown 标题。
            md_len = len(md_lines)
            section_index = md_len
            for current_idx in range(line_idx + 1, md_len):
                current_line = md_lines[current_idx]
                if re.match(r"^#{1,6}\s+", current_line):
                    section_index = current_idx
                    break

            # b.4 定义要截取的下文范围：图片下一行 -> 下一个标题上一行。
            post_content_start_index = line_idx + 1
            post_content = md_lines[post_content_start_index:section_index]
            img_post_context = self._extract_img_context(post_content, max_chars, direction="back")

            img_context.append((head_title, img_pre_context, img_post_context))

        return img_context

    def _extract_img_context(self, content_lines: list[str], max_chars: int, direction: str) -> str:
        """
        按段落提取图片上下文。

        :param content_lines: 候选上下文行列表
        :param max_chars: 最多保留多少字符
        :param direction: front 表示保留末尾内容自下而上，back 表示保留开头内容自上而下
        :return: 截取后的上下文字符串
        """
        current_paragraph = []
        final_paragraphs = []

        # 1. 遍历每一行，按空行和图片语法切分段落。
        for line in content_lines:
            clean_strip = line.strip()

            # 空行代表一个自然段结束。
            if not clean_strip:
                if current_paragraph:
                    final_paragraphs.append("\n".join(current_paragraph))
                    current_paragraph = []
                continue

            # 如果上下文里又遇到其他图片，也把当前段落结束，并跳过这张图片。
            if re.match(r"!\[.*?\]\(.*?\)\s*", clean_strip) or re.match(r"<img[^>]*src=['\"].*?['\"][^>]*>", clean_strip):
                if current_paragraph:
                    final_paragraphs.append("\n".join(current_paragraph))
                    current_paragraph = []
                continue

            current_paragraph.append(line)

        # 最后一段下面可能没有空行，也要收进去。
        if current_paragraph:
            final_paragraphs.append("\n".join(current_paragraph))

        # 2. 按方向调整段落顺序。
        # front 是图片上文，需要从离图片最近的段落开始向上取。
        if direction == "front":
            final_paragraphs.reverse()

        # 3. 判断 final_paragraphs 中收集到的段落字符长度是否超过了限制。
        total = 0
        selected_paragraphs = []
        for para in final_paragraphs:
            para = para.strip()
            if not para:
                continue

            para_len = len(para)
            if total + para_len > max_chars and selected_paragraphs:
                break

            selected_paragraphs.append(para)
            total += para_len

        # 4. 返回 selected。
        # front 前面反转过，返回前再反转回来，保证阅读顺序和原文一致。
        if direction == "front":
            selected_paragraphs.reverse()

        return "\n".join(selected_paragraphs)

    def _extract_img_summary(
            self,
            document_title: str,
            target_images_context: list[tuple[str, str, tuple[str, str, str]]],
            config: ImportConfig,
    ) :
        """
        所有图片生成图片摘要和图像描述

        Args:
            document_title: （文件）文档名字
            target_images_context: 所有图片信息
            config: 配置信息

        Returns:
            list[dict]
        """
        summaries = {}
        self.log_step("step3", "提取图片摘要")
        # 1 构建openai客户端
        if not config.vl_model:
            self.logger.error("缺少 VL_MODEL 配置，无法生成图片摘要")
            return summaries
        request_timestamps: Deque[float] = deque()
        requests_per_minute = max(1, config.requests_per_minute)
        try:
            client = OpenAI(
                api_key=config.openai_api_key,
                base_url=config.openai_api_base,
            )
        except Exception as e:
            logging.error(f"VLM客户端创建失败")
            return summaries
        # 2 发送请求（提取摘要）
        for img_name, img_path, image_context in target_images_context:
            self._enforce_rate_limit(request_timestamps, requests_per_minute)
            self.logger.debug(f"正在生成摘要: {img_name}")
            try:
                summary = self._get_img_summary(config, client, document_title, img_path, image_context)
            except Exception as e:
                self.logger.error(f"{img_name} 图片摘要生成失败: {e}")
                summary = ""
            summaries[img_name] = summary
        # 3 返回图片名 -> 图片摘要
        return summaries

    def _get_img_summary(
            self,
            config: ImportConfig,
            client: OpenAI,
            document_title: str,
            img_path: str,
            images_context: Tuple[str, str, str],
    ) -> str:
        """
        调用 VLM，根据图片本身和图片附近的 Markdown 上下文生成图片摘要。

        Args:
            config: 配置信息
            client: OpenAI 兼容客户端
            document_title: 文档标题
            img_path: 本地图片路径
            images_context: 图片附近上下文，结构是(最近标题, 图片上文, 图片下文)

        Returns:
            图片摘要文本
        """
        # 1. 解包 images_context，构建上下文。
        section_title, pre_context, post_context = images_context

        # 2. 判断上下文，把有内容的部分收集起来。
        context_parts = []
        if section_title:
            context_parts.append(section_title)
        if pre_context:
            context_parts.append(pre_context)
        if post_context:
            context_parts.append(post_context)

        # 3. 构建最终上下文。
        final_context = "\n".join(context_parts) if context_parts else "暂无可用上下文"

        # 4. 读取图片文件，拿到原始字节数据。
        with open(img_path, "rb") as f:
            content = f.read()

        # 原始字节不能直接放进 JSON 请求体，所以先转 base64，再拼成 data URL。
        mime_type, _ = mimetypes.guess_type(img_path)
        if not mime_type:
            mime_type = "image/jpeg"
        image_base64 = base64.b64encode(content).decode("utf-8")
        image_url = f"data:{mime_type};base64,{image_base64}"

        prompt = f"""
请结合图片内容和它在文档中的上下文，生成一段简洁准确的中文图片摘要。

文档标题：{document_title}
图片上下文：{final_context}
""".strip()

        # 5. 真正发起请求的位置。
        # 调用到 client.chat.completions.create(...) 这一行时，请求已经发给模型服务了。
        completion = client.chat.completions.create(
            model=config.vl_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            # 这一块是图片输入：type=image_url 表示图片，image_url.url 放图片地址或 data URL。
                            "type": "image_url",
                            "image_url": {
                                "url": image_url,
                            },
                        },
                        {
                            # 这一块是文本输入：告诉模型要完成什么任务，并提供图片附近的上下文。
                            "type": "text",
                            "text": prompt,
                        },
                    ],
                },
            ],
        )

        # 6. 这里不会再次发请求，只是从返回结果里取答案。
        if not completion.choices:
            return ""
        answer = completion.choices[0].message.content
        return (answer or "").strip()

    # 从现在往回看 60 秒，我一共发了几次
    def _enforce_rate_limit(
            self,
            request_timestamps: Deque[float],
            max_requests: int,
            window_seconds: int = 60
    ):
        """
        强制执行 API 请求速率限制。

        Args:
            request_timestamps: 请求时间戳队列。
            max_requests: 窗口内最大请求数。
            window_seconds: 时间窗口大小（秒）。
        """
        # 当前时刻，用来判断“最近一段时间内”到底发了多少次请求。
        current_time = time.time()

        # 把超出时间窗口的旧请求移除掉。
        # 例如 window_seconds=60，就只保留最近 60 秒内的请求时间。
        while request_timestamps and current_time - request_timestamps[0] >= window_seconds:
            request_timestamps.popleft()

        # 如果当前窗口内的请求数已经达到上限，就先等一会。
        if len(request_timestamps) >= max_requests:
            # 计算还要等多久，才能让最早那次请求滑出窗口。
            sleep_duration = window_seconds - (current_time - request_timestamps[0])

            # 如果等待时间大于 0，就真的暂停执行。
            if sleep_duration > 0:
                self.logger.info(f"达到速率限制，暂停 {sleep_duration:.2f} 秒...")
                time.sleep(sleep_duration)

            # 睡完之后重新取一次当前时间。
            current_time = time.time()

            # 再清理一遍过期时间戳。
            while request_timestamps and current_time - request_timestamps[0] >= window_seconds:
                request_timestamps.popleft()

        # 记录这一次请求的时间，供下一次限速判断使用。
        request_timestamps.append(current_time)

    def _upload_img_and_update_new_md(
            self,
            document_name,
            md_content,
            image_summaries,
            target_images_context,
            config,
    ):
        """
        上传图片到minio以及替换md中的图片url和摘要
        :param md_content:
        :param image_summaries:
        :param target_images_context:
        :return:
        """

        # 1. 构建minio客户端
        minio_client = get_minio_client()
        if minio_client is None:
            self.logger.warning("无法获取MinIO客户端，跳过图片上传和MD替换")
            return md_content

        if not config.minio_bucket:
            self.logger.warning("缺少 MINIO_BUCKET_NAME 配置，跳过图片上传和MD替换")
            return md_content

        remote_urls = {}

        # 2 遍历图片信息列表
        for img_name, img_path, _ in target_images_context:
            # object_name 是 MinIO 里的对象名，不是本地文件路径。
            # 用 document_name/img_name，是为了按文档分组保存图片，避免不同文档里的同名图片互相覆盖。
            # 例如 doc_a/a.png 和 doc_b/a.png 在 MinIO 里就是两个不同对象。
            object_name = f"{document_name}/{img_name}"

            try:
                minio_client.fput_object(
                    config.minio_bucket,
                    object_name,
                    img_path,
                )

                # 2.3 手动拼接远程地址
                remote_url = config.get_minio_base_url() + '/' + config.minio_bucket + '/' + object_name
                self.logger.info(f"{img_name}图片上传到minio成功")
                remote_urls[img_name] = remote_url

            except Exception as e:
                self.logger.warning(f"{img_name}上传到minio失败")
                remote_urls[img_name] = "http://minio_mock/" + document_name + "/" + img_name

        self.logger.info(f"成功上传{len(remote_urls)}张图片到minio")
        return self._replace_md_images_with_remote_urls(md_content, image_summaries, remote_urls)

    def _replace_md_images_with_remote_urls(self, md_content, image_summaries, remote_urls):
        """
        替换md中的图片url和摘要
        """
        new_md_content = md_content
        for img_name, remote_url in remote_urls.items():
            # 1. remote_url 是这张图上传到 MinIO 后的远程地址；没有地址就跳过
            if not remote_url:
                continue

            # 2. 生成替换后的 Markdown 图片语法：![图片摘要](远程图片URL)
            image_summary = image_summaries.get(img_name, "")
            safe_summary = (image_summary or "").replace("\n", " ").strip()
            safe_summary = safe_summary.replace("[", " ").replace("]", " ")
            safe_summary = safe_summary.replace("(", " ").replace(")", " ")
            replacement = f"![{safe_summary}]({remote_url})"

            # 3. 匹配原 MD 里的本地图片写法，例如：
            # ![](images/a.png)、![旧描述](./images/a.png)、![旧描述](../images/a.png)
            replace_pattern = re.compile(
                r"!\[(.*?)\]\([^)]*"
                + re.escape(img_name)
                + r"[^)]*\)",
                re.IGNORECASE,
            )

            # 4. sub 会把所有匹配到的本地图片语法，替换成上面生成的远程图片语法
            new_md_content = replace_pattern.sub(lambda _: replacement, new_md_content)

        return new_md_content

    def _backup_new_md_file(
            self,
            original_md_path_str: str,
            new_md_content: str
    ) -> str:
        """
        将处理后的 Markdown 内容写入新文件。
        """
        self.log_step("step_5", "备份新文件")

        original_path = Path(original_md_path_str)
        new_file_path = original_path.with_name(
            f"{original_path.stem}_new{original_path.suffix}"
        )

        try:
            with open(new_file_path, "w", encoding="utf-8") as f:
                f.write(new_md_content)
            self.logger.info(f"处理后的文件已备份至: {new_file_path}")
        except IOError as e:
            self.logger.error(f"写入新文件失败 {new_file_path}: {e}")
            raise ImageProcessingError(f"文件写入失败: {e}", node_name=self.name)

        return str(new_file_path)


if __name__ == '__main__':
    setup_logging()
    img_md_node= MarkDownImageNode()
    demo_dir = Path(__file__).resolve().parents[1] / "import_temp_Dir"
    state = {
        "md_path": str(
            demo_dir
            / "hak180使用说明书"
            / "hybrid_auto"
            / "hak180使用说明书.md"
        )
    }
    result = img_md_node.process(state)
    print(json.dumps(result, indent=4, ensure_ascii=False))
