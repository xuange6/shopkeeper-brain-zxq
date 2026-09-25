"""MCP 网络搜索节点。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from urllib.parse import urlsplit

from knowledge.processor.query_process.base import BaseNode, setup_logging
from knowledge.processor.query_process.evidence import evidence_coverage, structure_match
from knowledge.processor.query_process.exceptions import ValidationError
from knowledge.processor.query_process.state import QueryGraphState


class WebSearchMcpNode(BaseNode):
    """通过 MCP 调用百炼通用搜索工具，作为网络检索通道。"""

    name = "web_search_mcp"

    def process(self, state: QueryGraphState) -> QueryGraphState:
        plan = state.get("retrieval_plan") or {}
        channels = plan.get("channels") if isinstance(plan, dict) else {}
        web_plan = channels.get("web") if isinstance(channels, dict) else {}
        if isinstance(web_plan, dict) and not web_plan.get("enabled", True):
            return self._skipped("disabled by retrieval plan")

        mode = str(web_plan.get("mode") or "fallback") if isinstance(web_plan, dict) else "fallback"
        if mode == "fallback":
            sufficient, sufficiency = self._local_evidence_sufficient(state, plan)
            if sufficient:
                return self._skipped(
                    "local-first satisfied: "
                    f"count={sufficiency['count']}, coverage={sufficiency['coverage']:.3f}, "
                    f"structure={sufficiency['structure']:.3f}"
                )

        query, item_names = self._validate_query_inputs(state)
        search_query, query_policy = self._build_search_query(
            query, item_names, plan
        )

        if not self.config.mcp_dashscope_base_url:
            return self._skipped("MCP URL not configured")
        if not self.config.openai_api_key:
            return self._skipped("API key not configured")

        try:
            timeout_seconds = max(
                0.001,
                float(web_plan.get("timeout_ms") or self.config.web_timeout_ms) / 1000.0,
            )
            result = asyncio.run(
                self._execute_with_timeout(search_query, timeout_seconds)
            )
        except TimeoutError:
            self.logger.error("MCP 搜索超时")
            return {
                "web_search_docs": [],
                "web_search_diagnostics": {
                    **query_policy,
                    "status": "error",
                    "error_type": "timeout",
                    "raw_count": 0,
                    "accepted_count": 0,
                },
                "retrieval_status": {
                    self.name: {
                        "status": "error",
                        "error_type": "timeout",
                        "reason": f"timeout after {timeout_seconds:.3f}s",
                    }
                },
            }
        except Exception as exc:
            self.logger.error("MCP 搜索失败: %s", exc, exc_info=True)
            return {
                "web_search_docs": [],
                "web_search_diagnostics": {
                    **query_policy,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "raw_count": 0,
                    "accepted_count": 0,
                },
                "retrieval_status": {
                    self.name: {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "reason": str(exc)[:500],
                    }
                },
            }
        accepted, source_policy = self._apply_source_policy(result, plan)
        for doc in accepted:
            doc.setdefault("item_names", list(item_names))
        for doc in accepted:
            doc.setdefault("item_names", list(item_names))
        status = "ok" if accepted else "degraded"
        reason = "" if accepted else str(
            source_policy.get("reason") or "no accepted web results"
        )
        return {
            "web_search_docs": accepted,
            "web_search_diagnostics": {
                **query_policy,
                **source_policy,
                "status": status,
            },
            "retrieval_status": {
                self.name: {
                    "status": status,
                    "reason": reason,
                    "raw_count": len(result),
                    "accepted_count": len(accepted),
                    "official_count": int(source_policy.get("official_count") or 0),
                }
            },
        }

    def _local_evidence_sufficient(
        self, state: QueryGraphState, plan: Dict[str, Any]
    ) -> Tuple[bool, Dict[str, float | int]]:
        """Gate Web fallback on local relevance, not candidate count alone."""

        docs = [doc for doc in state.get("rrf_chunks") or [] if isinstance(doc, dict)]
        query = str(
            state.get("rewritten_query")
            or state.get("policy_query")
            or state.get("original_query")
            or ""
        )
        # Product names are retrieval filters, not evidence that the manual
        # actually answers the remaining question.
        for item_name in state.get("item_names") or []:
            query = query.replace(str(item_name), " ")
        features = plan.get("query_features") if isinstance(plan, dict) else {}
        coverage = evidence_coverage(query, docs[:3])
        structure = max(
            (structure_match(doc, query, features or {}) for doc in docs[:3]),
            default=0.0,
        )
        count_ok = len(docs) >= self.config.web_fallback_min_local_candidates
        relevance_ok = (
            coverage >= self.config.web_fallback_min_local_coverage
            or structure >= 1.0
        )
        audit: Dict[str, float | int] = {
            "count": len(docs),
            "coverage": coverage,
            "structure": structure,
        }
        return count_ok and relevance_ok, audit

    def _skipped(self, reason: str) -> QueryGraphState:
        return {
            "web_search_docs": [],
            "web_search_diagnostics": {
                "status": "skipped",
                "reason": reason,
                "raw_count": 0,
                "accepted_count": 0,
                "official_count": 0,
            },
            "retrieval_status": {
                self.name: {"status": "skipped", "reason": reason}
            },
        }

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

    def _official_domains(self) -> List[str]:
        return list(dict.fromkeys(
            value.strip().lower()
            for value in self.config.web_official_domains.split(",")
            if value.strip()
        ))

    def _build_search_query(
        self,
        query: str,
        item_names: List[str],
        plan: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        features = plan.get("query_features") if isinstance(plan, dict) else {}
        is_freshness = bool((features or {}).get("freshness"))
        domains = self._official_domains()
        expanded = bool(
            is_freshness
            and domains
            and self.config.web_official_query_expansion
        )
        if not expanded:
            return query, {
                "query_mode": "original",
                "official_domains_configured": len(domains),
                "query_expanded": False,
            }

        limit = max(1, int(self.config.web_official_query_domain_limit))
        scoped_domains = domains[:limit]
        site_clause = " OR ".join(f"site:{domain}" for domain in scoped_domains)
        product_clause = " ".join(
            str(name).strip() for name in item_names if str(name).strip()
        )
        expanded_query = f"{query} {product_clause} 官方 ({site_clause})".strip()
        return expanded_query, {
            "query_mode": "official_domain_expansion",
            "official_domains_configured": len(domains),
            "query_domains_used": scoped_domains,
            "query_expanded": True,
        }

    def _apply_source_policy(
        self,
        docs: List[Dict[str, Any]],
        plan: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        features = plan.get("query_features") if isinstance(plan, dict) else {}
        is_freshness = bool((features or {}).get("freshness"))
        valid_docs: List[Dict[str, Any]] = []
        official_domains = set(self._official_domains())
        for raw_doc in docs:
            if not isinstance(raw_doc, dict):
                continue
            doc = dict(raw_doc)
            url = str(doc.get("url") or "").strip()
            try:
                parsed = urlsplit(url)
                domain = (parsed.hostname or "").lower()
                valid_url = (
                    parsed.scheme in {"http", "https"}
                    and bool(domain)
                    and not parsed.username
                    and not parsed.password
                )
            except ValueError:
                valid_url = False
                domain = ""
            if not valid_url:
                continue
            is_official = any(
                domain == allowed or domain.endswith("." + allowed)
                for allowed in official_domains
            )
            doc["domain"] = domain
            doc["source_type"] = "official_web" if is_official else "web"
            doc["authority"] = (
                self.config.web_official_authority
                if is_official
                else self.config.web_default_authority
            )
            valid_docs.append(doc)
        official = [
            doc for doc in valid_docs if doc.get("source_type") == "official_web"
        ]
        require_official = bool(
            is_freshness and self.config.web_freshness_require_official
        )
        if require_official:
            if not self._official_domains():
                accepted: List[Dict[str, Any]] = []
                reason = "official domains are not configured"
            elif official:
                accepted = official
                reason = ""
            else:
                accepted = []
                reason = "no official-domain result; generic results filtered"
        else:
            accepted = valid_docs
            reason = ""
        return accepted, {
            "raw_count": len(docs),
            "valid_url_count": len(valid_docs),
            "official_count": len(official),
            "accepted_count": len(accepted),
            "filtered_count": len(docs) - len(accepted),
            "official_required": require_official,
            "reason": reason,
        }

    async def _create_execute_web_search(self, query: str) -> List[Dict[str, Any]]:
        """连接 MCP 服务端，调用 bailian_web_search 工具并解析结果。"""
        # Lazy import keeps local routing/contract tests independent of the
        # optional Windows MCP transport runtime.
        from agents.mcp import MCPServerStreamableHttp

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
                arguments={"query": query, "count": self.config.web_search_limit},
            )
            return self._parse_search_result(tool_result)
        finally:
            await mcp_client.cleanup()

    async def _execute_with_timeout(
        self, query: str, timeout_seconds: float
    ) -> List[Dict[str, Any]]:
        return await asyncio.wait_for(
            self._create_execute_web_search(query), timeout=timeout_seconds
        )

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
                try:
                    parsed_url = urlsplit(url)
                    domain = (parsed_url.hostname or "").lower()
                    valid_url = (
                        parsed_url.scheme in {"http", "https"}
                        and bool(domain)
                        and not parsed_url.username
                        and not parsed_url.password
                    )
                except ValueError:
                    valid_url = False
                    domain = ""
                if not valid_url:
                    continue
                official_domains = {
                    value.strip().lower()
                    for value in self.config.web_official_domains.split(",")
                    if value.strip()
                }
                is_official = any(
                    domain == allowed or domain.endswith("." + allowed)
                    for allowed in official_domains
                )
                search_result.append(
                    {
                        "snippet": snippet,
                        "content": snippet,
                        "title": title,
                        "url": url,
                        "source": "web",
                        "source_type": "official_web" if is_official else "web",
                        "domain": domain,
                        "authority": (
                            self.config.web_official_authority
                            if is_official
                            else self.config.web_default_authority
                        ),
                        # Retrieval time is not publication time. Unknown-date
                        # results receive a bounded prior; official authority is
                        # represented independently above.
                        "freshness": 0.7 if is_official else 0.3,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "retrieved_date": datetime.now().astimezone().date().isoformat(),
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
