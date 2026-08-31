"""Agent-driven MCP web search node.

This is an alternative to directly calling the MCP tool. The agent receives the
MCP server as a tool provider and decides when to call the web search tool.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Tuple

from agents import Agent, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from agents.mcp import MCPServerStreamableHttp
from openai import AsyncOpenAI

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.exceptions import ValidationError
from knowledge.processor.query_process.state import QueryGraphState


set_tracing_disabled(True)


class WebSearchMcpAgentNode(BaseNode):
    """Use an Agent with the Bailian MCP web search server as a fallback channel."""

    name = "web_search_mcp_agent"

    _INSTRUCTIONS = """
你是一个网络搜索助手。你必须优先使用 MCP 搜索工具查询用户问题。
请只返回 JSON，不要返回 Markdown，不要解释过程。
最多返回 3 条 pages。
返回格式必须是：
{
  "pages": [
    {
      "title": "结果标题",
      "snippet": "和问题最相关的信息摘要",
      "url": "来源链接"
    }
  ]
}
如果没有结果，返回 {"pages": []}。
""".strip()

    def process(self, state: QueryGraphState) -> QueryGraphState:
        query, _item_names = self._validate_query_inputs(state)

        result = asyncio.run(self._create_execute_web_search(query))
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
        if not self.config.mcp_dashscope_base_url:
            self.logger.warning("MCP_DASHSCOPE_BASE_URL 未配置，跳过 Agent MCP 搜索")
            return []
        if not self.config.openai_api_key:
            self.logger.warning("OPENAI_API_KEY 未配置，跳过 Agent MCP 搜索")
            return []
        if not self.config.openai_api_base:
            self.logger.warning("OPENAI_API_BASE 未配置，跳过 Agent MCP 搜索")
            return []

        model_name = self.config.default_model or "qwen3-max"
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

        model_client = AsyncOpenAI(
            base_url=self.config.openai_api_base,
            api_key=self.config.openai_api_key,
        )

        agent = Agent(
            name="web_search_agent",
            instructions=self._INSTRUCTIONS,
            mcp_servers=[mcp_client],
            model=OpenAIChatCompletionsModel(
                model=model_name,
                openai_client=model_client,
            ),
        )

        try:
            await mcp_client.connect()
            agent_result = await Runner.run(agent, query)
            return self._parse_agent_result(agent_result)
        except Exception as exc:
            self.logger.error("Agent MCP 搜索失败: %s", exc, exc_info=True)
            return []
        finally:
            await mcp_client.cleanup()

    def _parse_agent_result(self, agent_result: Any) -> List[Dict[str, Any]]:
        text = self._extract_final_text(agent_result)
        if not text:
            return []

        payload = self._load_json_payload(text)
        if payload is None:
            return [{"title": "MCP Agent 搜索结果", "snippet": text.strip(), "url": ""}]

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

        return search_result[:3]

    @staticmethod
    def _extract_final_text(agent_result: Any) -> str:
        final_output = getattr(agent_result, "final_output", None)
        if final_output is None:
            final_output = getattr(agent_result, "output", None)
        if final_output is None:
            return str(agent_result or "").strip()
        if isinstance(final_output, str):
            return final_output.strip()
        return json.dumps(final_output, ensure_ascii=False)

    @staticmethod
    def _load_json_payload(text: str) -> Dict[str, Any] | None:
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None


_node_instance = WebSearchMcpAgentNode()


def node_web_search_mcp_agent(state: QueryGraphState) -> QueryGraphState:
    """兼容函数式调用入口。"""
    return _node_instance(state)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    setup_logging()

    test_state = {
        "rewritten_query": "今天北京天气怎么样，并且告诉我今天具体的日期是什么时候",
        "item_names": ["RS-12 数字万用表"],
    }
    result_state = node_web_search_mcp_agent(test_state)
    for item in result_state.get("web_search_docs", []):
        print(json.dumps(item, ensure_ascii=False, indent=2))
