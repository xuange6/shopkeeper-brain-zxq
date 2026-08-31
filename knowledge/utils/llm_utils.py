"""
LLM 客户端工具。

统一封装 ChatOpenAI 客户端创建逻辑，供导入流程中的节点复用。
"""

import os
from typing import Any, Optional

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from knowledge.processor.import_process.exceptions import ConfigurationError


# 先加载 .env 文件，把 OPENAI_API_KEY / OPENAI_API_BASE / ITEM_MODEL 等配置放进环境变量。
load_dotenv()


def get_llm_client(
        model: Optional[str] = None,
        json_mode: bool = False,
        temperature: Optional[float] = None,
) -> ChatOpenAI:
    """
    获取 LLM 客户端。

    Args:
        model: 模型名称。不传时优先使用配置中的 item_model/default_model。
        json_mode: 是否要求模型返回 JSON 对象。
        temperature: 采样温度。不传时读取环境变量，最终兜底为 0.1。

    Returns:
        ChatOpenAI 客户端实例。
    """
    model_name = model or os.getenv("ITEM_MODEL") or os.getenv("LLM_DEFAULT_MODEL")
    api_key = os.getenv("OPENAI_API_KEY")
    api_base = os.getenv("OPENAI_API_BASE")

    if temperature is None:
        temperature = float(os.getenv("LLM_DEFAULT_TEMPERATURE", "0"))

    if not model_name:
        raise ConfigurationError("LLM 模型未配置")

    if not api_key:
        raise ConfigurationError("OPENAI_API_KEY 未配置")

    if not api_base:
        raise ConfigurationError("OPENAI_API_BASE 未配置")

    extra_body: dict[str, Any] = {"enable_thinking": False}
    if json_mode:
        extra_body["response_format"] = {"type": "json_object"}

    return ChatOpenAI(
        model=model_name,
        openai_api_key=api_key,
        openai_api_base=api_base,
        temperature=temperature,
        extra_body=extra_body,
    )


if __name__ == "__main__":
    client = get_llm_client(json_mode=True)
    import json
    ai_message = client.invoke("您好，请给我将一个雄安话，返回json格式")
    print(ai_message.content)
    json_object = json.loads(ai_message.content)
    print(json_object)
