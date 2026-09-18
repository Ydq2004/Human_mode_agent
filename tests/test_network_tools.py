import json
from types import SimpleNamespace

from core.network_policy import check_url
from core.http_client import HttpClient
from core.web_search_service import WebSearchService
from tools.web_search_tools import web_search, web_search_context

def test_network_policy_rejects_unsafe_urls(monkeypatch):
    assert not check_url("ftp://example.com").allowed
    assert not check_url("https://user:pass@example.com").allowed
    monkeypatch.setattr("core.network_policy._resolved_public", lambda host: host == "example.com")
    assert check_url("https://example.com").allowed
    assert not check_url("https://127.0.0.1").allowed

def test_http_client_limits_body_and_headers(monkeypatch):
    class Response:
        status = 200
        headers = SimpleNamespace(get_content_type=lambda: "application/json", get=lambda key: None)
        def read(self, size=None): return b"abcdef"
    seen = {}
    def opener(request, timeout):
        seen.update(request.headers); return Response()
    monkeypatch.setattr("core.http_client.check_url", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    response = HttpClient(max_response_bytes=3, opener=opener).get("https://example.com", headers={"X-Test": "yes"})
    assert response.body == b"abc" and response.truncated
    assert seen.get("X-test") == "yes"

def test_search_normalizes_and_deduplicates(monkeypatch):
    class Client:
        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, body=json.dumps({"web": {"results": [
                {"title": "A", "url": "https://a.test", "description": "one"},
                {"title": "A2", "url": "https://a.test", "description": "dup"},
            ]}}).encode(), truncated=False, elapsed_seconds=0.1)
    service = WebSearchService(client=Client(), provider="brave", api_key="secret")
    result = service.search("hello", max_results=5)
    assert result["status"] == "success" and len(result["results"]) == 1

def test_search_tool_reports_missing_context():
    payload = json.loads(web_search.invoke({"query": "hello"}))
    assert payload["status"] == "unavailable"
