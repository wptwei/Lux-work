"""联网知识搜索：配置、超时、搜索适配、结果标准化、有限重试与安全错误。

只实现 Tavily 一个适配器；搜索结果只做标准化，不做正文抓取。
密钥只从服务端环境变量读取，绝不进入前端、日志或错误详情。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from core.settings import settings

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class WebSearchError(RuntimeError):
    """可安全展示给界面的搜索错误，不包含密钥、查询原文或服务端原始响应。"""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class SearchResult:
    """标准化后的搜索结果。published_at 可为空，不得用抓取时间冒充发布日期。"""

    id: str
    title: str
    url: str
    content: str
    published_at: str = ""
    retrieved_at: str = ""


def _api_key() -> str:
    return os.getenv("TAVILY_API_KEY", "").strip()


def web_search_status() -> str:
    if not settings.web_search_enabled:
        return "联网搜索 · 关闭"
    if settings.web_search_provider != "tavily":
        return f"联网搜索 · 未支持的提供方 {settings.web_search_provider}"
    key = "已配置" if _api_key() else "缺少密钥"
    return f"联网搜索 · Tavily（{key}）"


def web_search_configured() -> tuple[bool, str]:
    """返回 (是否可用, 说明)；不发起任何网络请求。"""
    if not settings.web_search_enabled:
        return False, "服务器未开启联网搜索（LUX_WEB_SEARCH_ENABLED=false）"
    if settings.web_search_provider != "tavily":
        return False, f"不支持的搜索提供方：{settings.web_search_provider}"
    if not _api_key():
        return False, "联网搜索未配置：缺少 TAVILY_API_KEY"
    return True, "已配置 Tavily 联网搜索"


def _post_json(url: str, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise WebSearchError("联网搜索密钥无效或额度不足，本次未取得资料。") from exc
        if exc.code == 429:
            raise WebSearchError("联网搜索请求过于频繁，请稍后再试。") from exc
        raise WebSearchError(f"[http] 搜索服务返回 HTTP {exc.code}", retryable=exc.code >= 500) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise WebSearchError("联网搜索超时，本次未取得联网资料。", retryable=True) from exc
        raise WebSearchError("无法连接联网搜索服务，本次未取得资料。", retryable=True) from exc
    except TimeoutError as exc:
        raise WebSearchError("联网搜索超时，本次未取得联网资料。", retryable=True) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WebSearchError("搜索服务返回异常，本次未取得资料。", retryable=True) from exc


def _normalize(data: dict, retrieved_at: str) -> list[SearchResult]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return []
    out: list[SearchResult] = []
    for item in data["results"]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append(
            SearchResult(
                id="",
                title=str(item.get("title") or "").strip(),
                url=url,
                content=str(item.get("content") or "").strip(),
                published_at=str(item.get("published_date") or "").strip(),
                retrieved_at=retrieved_at,
            )
        )
    return out


def search_web(query: str, *, max_results: int | None = None, timeout: int | None = None) -> list[SearchResult]:
    """执行一次搜索，返回标准化结果（可为空列表）。硬失败抛 WebSearchError。"""
    if not settings.web_search_enabled:
        raise WebSearchError("联网搜索未启用：服务器未开启 LUX_WEB_SEARCH_ENABLED。")
    if settings.web_search_provider != "tavily":
        raise WebSearchError(f"不支持的搜索提供方：{settings.web_search_provider}")
    api_key = _api_key()
    if not api_key:
        raise WebSearchError("联网搜索未配置：缺少 TAVILY_API_KEY")
    query = (query or "").strip()
    if not query:
        raise WebSearchError("搜索内容为空，未发起请求。")

    max_results = max(1, min(5, int(max_results or settings.web_search_max_results)))
    timeout = int(timeout or settings.web_search_timeout)
    body = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }
    retrieved_at = time.strftime("%Y-%m-%d %H:%M:%S")

    last_exc: WebSearchError | None = None
    for attempt in range(2):
        try:
            data = _post_json(TAVILY_SEARCH_URL, body, timeout)
            return _normalize(data, retrieved_at)
        except WebSearchError as exc:
            last_exc = exc
            if not exc.retryable:
                break
    assert last_exc is not None
    raise last_exc
