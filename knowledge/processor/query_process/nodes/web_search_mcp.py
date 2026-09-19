"""MCP 网络搜索节点。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Tuple

from agents.mcp import MCPServerStreamableHttp

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.exceptions import ValidationError
from knowledge.processor.query_process.state import QueryGraphState


class WebSearchMcpNode(BaseNode):
    """通过 MCP 调用百炼通用搜索工具，作为网络检索通道。"""

    name = "web_search_mcp"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query, _item_names = self._validate_query_inputs(state)

        if not self.config.mcp_dashscope_base_url:
            return {
                "web_search_docs": [],
                "retrieval_status": {
                    self.name: {"status": "skipped", "reason": "MCP URL not configured"}
                },
            }
        if not self.config.openai_api_key:
            return {
                "web_search_docs": [],
                "retrieval_status": {
                    self.name: {"status": "skipped", "reason": "API key not configured"}
                },
            }

        try:
            result = asyncio.run(self._create_execute_web_search(query))
        except Exception as exc:
            self.logger.error("MCP 搜索失败: %s", exc, exc_info=True)
            return {
                "web_search_docs": [],
                "retrieval_status": {
                    self.name: {"status": "error", "reason": str(exc)[:500]}
                },
            }
        if not result:
            return {"web_search_docs": []}

        return {"web_search_docs": result}

    def _validate_query_inputs(self, state: QueryGraphState) -> Tuple[str, List[str]]:
        rewritten_query = state.get("rewritten_query") or state.get("original_query") or ""
        item_names = state.get("item_names") or []

        if not rewritten_query or not isinstance(rewritten_query, str):
            raise ValidationError(
                "rewritten_query 为空或类型错误",
                node_name=self.name,
            )

        if not isinstance(item_names, list):
            raise ValidationError(
                "item_names 类型错误",
                node_name=self.name,
            )

        return rewritten_query, item_names

    async def _create_execute_web_search(self, query: str) -> List[Dict[str, Any]]:
        """连接 MCP 服务端，调用 bailian_web_search 工具并解析结果。"""
        authorization = self.config.openai_api_key.strip()
        if not authorization.lower().startswith("bearer "):
            authorization = f"Bearer {authorization}"

        mcp_client = MCPServerStreamableHttp(
            name="通用搜索",
            params={
                "url": self.config.mcp_dashscope_base_url,
                "headers": {"Authorization": authorization},
            },
            cache_tools_list=True,
        )

        try:
            await mcp_client.connect()
            tool_result = await mcp_client.call_tool(
                tool_name="bailian_web_search",
                arguments={"query": query, "count": 3},
            )
            return self._parse_search_result(tool_result)
        finally:
            await mcp_client.cleanup()

    def _parse_search_result(self, tool_result: Any) -> List[Dict[str, Any]]:
        if not tool_result:
            return []

        content = getattr(tool_result, "content", None) or []
        if not content:
            return []

        first_content = content[0]
        text = getattr(first_content, "text", "") or ""
        if not text:
            return []

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.logger.error("反序列化 MCP 搜索结果失败: %.120s", text)
            return []

        pages = payload.get("pages") or []
        if not isinstance(pages, list):
            return []

        search_result: List[Dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            snippet = str(page.get("snippet", "")).strip()
            title = str(page.get("title", "")).strip()
            url = str(page.get("url", "")).strip()
            if title or snippet or url:
                search_result.append(
                    {
                        "snippet": snippet,
                        "title": title,
                        "url": url,
                    }
                )

        return search_result


_node_instance = WebSearchMcpNode()


def node_web_search_mcp(state: QueryGraphState) -> QueryGraphState:
    """兼容函数式调用入口。"""
    return _node_instance(state)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging()

    test_state = {
        "rewritten_query": "今天的小米汽车的股价是多少",
        "item_names": ["RS-12 数字万用表"],
    }
    result_state = node_web_search_mcp(test_state)
    for item in result_state.get("web_search_docs", []):
        print(json.dumps(item, ensure_ascii=False, indent=2))
