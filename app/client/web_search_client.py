"""
联网搜索客户端（Tavily）

Tavily 是专为 LLM Agent 设计的搜索 API：一次调用返回已清洗的网页正文片段和来源链接，
不需要自己抓取和解析网页。配置 TAVILY_API_KEY 后启用；未配置时 enabled=False，调用方应跳过联网步骤。
"""
import logging
from typing import Any, Dict, List

from app.config.settings import settings

logger = logging.getLogger(__name__)


class WebSearchClient:
    def __init__(self):
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(settings.TAVILY_API_KEY)

    def _get_client(self):
        # 延迟创建：key 可能在运行时通过配置更新，且未配置时不应在导入阶段报错
        if self._client is None or self._client_key != settings.TAVILY_API_KEY:
            from tavily import AsyncTavilyClient
            self._client = AsyncTavilyClient(api_key=settings.TAVILY_API_KEY)
            self._client_key = settings.TAVILY_API_KEY
        return self._client

    async def search(self, query: str, max_results: int = None) -> List[Dict[str, Any]]:
        """
        搜索并返回结果列表：[{"title", "url", "content", "score"}]，按相关性降序。
        未配置 key 时返回空列表；调用失败时抛出异常，由调用方决定如何处理（不吞错，避免把"没搜到"当成"搜了没结果"）
        """
        if not self.enabled:
            logger.warning("Web search skipped: TAVILY_API_KEY not configured")
            return []
        kwargs = {}
        prefer = [d.strip() for d in settings.WEB_SEARCH_PREFER_DOMAINS.split(",") if d.strip()]
        if prefer:
            # prefer 模式只是优先，搜不到时仍会返回其他站点；restrict 会让冷门话题直接没有结果
            kwargs = {"include_domains": prefer, "include_domains_mode": "prefer"}
        resp = await self._get_client().search(
            query=query,
            search_depth="basic",
            max_results=max_results or settings.WEB_SEARCH_MAX_RESULTS,
            timeout=settings.WEB_SEARCH_TIMEOUT,
            **kwargs,
        )
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": (r.get("content") or "").strip(),
                "score": float(r.get("score") or 0.0),
            }
            for r in resp.get("results", [])
            if r.get("content")
        ]
        results.sort(key=lambda r: r["score"], reverse=True)
        logger.info(f"Web search '{query[:40]}': {len(results)} results")
        return results


web_search_client = WebSearchClient()
