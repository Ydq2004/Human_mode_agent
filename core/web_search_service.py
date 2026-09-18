"""搜索供应商适配器；搜索结果是外部资料，不是个人记忆。"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode

from core.http_client import HttpClient, HttpClientError


class WebSearchService:
    def __init__(self, *, client: HttpClient | None = None, provider: str = "", api_key: str = "", endpoint: str = "https://api.search.brave.com/res/v1/web/search"):
        self.provider = str(provider or "").strip().lower()
        self.api_key = str(api_key or "").strip()
        self.endpoint = str(endpoint or "").strip()
        self.client = client or HttpClient()

    @classmethod
    def from_environment(cls) -> "WebSearchService":
        return cls(provider=os.environ.get("WEB_SEARCH_PROVIDER", "brave"), api_key=os.environ.get("BRAVE_SEARCH_API_KEY", ""), endpoint=os.environ.get("WEB_SEARCH_ENDPOINT", "https://api.search.brave.com/res/v1/web/search"))

    def search(self, query: str, *, max_results: int = 5, recency: str = "", domains: list[str] | None = None) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query or len(query) > 500 or isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= 10:
            return {"status": "invalid_input", "error": "query 不能为空且不能超过 500 个字符"}
        if recency and str(recency) not in {"day", "week", "month", "year"} and not str(recency).isdigit():
            return {"status": "invalid_input", "error": "recency 参数无效"}
        if domains is not None and (not isinstance(domains, list) or len(domains) > 10 or any(not str(item).strip() or len(str(item)) > 200 for item in domains)):
            return {"status": "invalid_input", "error": "domains 参数无效"}
        if self.provider != "brave" or not self.api_key:
            return {"status": "unavailable", "error": "未配置支持的搜索供应商或 API Key"}
        params = {"q": query, "count": str(max_results)}
        if recency:
            params["freshness"] = str(recency)
        if domains:
            params["q"] = query + " " + " ".join(f"site:{item}" for item in domains[:10])
        try:
            response = self.client.get(self.endpoint + "?" + urlencode(params), accept="application/json", headers={"X-Subscription-Token": self.api_key})
            if response.status_code == 429:
                return {"status": "rate_limited", "http_status": 429}
            if response.status_code >= 400:
                return {"status": "upstream_error", "http_status": response.status_code}
            raw = json.loads(response.body.decode("utf-8"))
            rows = []
            seen_urls = set()
            for index, item in enumerate(((raw.get("web") or {}).get("results") or []), 1):
                if not isinstance(item, dict) or not item.get("url"):
                    continue
                url = str(item.get("url"))[:2000]
                if url in seen_urls or len(rows) >= max_results:
                    continue
                seen_urls.add(url)
                rows.append({"title": str(item.get("title") or "")[:500], "url": url, "snippet": str(item.get("description") or "")[:1200], "source": "brave", "rank": len(rows) + 1, "published_at": item.get("age")})
            return {"status": "success", "query": query, "provider": "brave", "results": rows, "truncated": response.truncated, "elapsed_seconds": response.elapsed_seconds}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"status": "upstream_error", "error": f"搜索响应不是有效 JSON：{exc}"}
        except HttpClientError as exc:
            return {"status": exc.status, "error": str(exc), "http_status": exc.http_status}


__all__ = ["WebSearchService"]
