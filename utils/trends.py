"""热榜关键词获取（带内存缓存）"""
import time
from datetime import datetime

import requests

from utils.config import (
    FALLBACK_KEYWORDS,
    HTTP_HEADERS,
    REQUEST_TIMEOUT,
    TRENDS_CACHE_FAILURE_TTL,
)

_TRENDS_CACHE: dict = {}


def _get_cached_trends(cache_key: str, fetcher) -> list:
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    cached = _TRENDS_CACHE.get(cache_key)
    if cached:
        if cached.get("date") == today and cached.get("ok") and isinstance(cached.get("words"), list):
            return cached["words"].copy()
        if not cached.get("ok") and isinstance(cached.get("ts"), (int, float)) and now - cached["ts"] < TRENDS_CACHE_FAILURE_TTL:
            return []
    words = fetcher() or []
    ok = bool(words)
    _TRENDS_CACHE[cache_key] = {"date": today, "ts": now, "ok": ok, "words": words.copy()}
    return words


def get_baidu_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://v2.xxapi.cn/api/baiduhot", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("title") for t in r.json().get("data", []) if t.get("title")]
        except Exception:
            return []
    return _get_cached_trends("baidu", _fetch)


def get_zhihu_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://v2.xxapi.cn/api/douyinhot", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("word") for t in r.json().get("data", []) if t.get("word")]
        except Exception:
            return []
    return _get_cached_trends("zhihu", _fetch)


def get_weibo_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=weibo", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("title") for t in r.json().get("data", []) if t.get("title")]
        except Exception:
            return []
    return _get_cached_trends("weibo", _fetch)


def ensure_keywords(*lists) -> list:
    for words in lists:
        if words:
            return words
    return FALLBACK_KEYWORDS.copy()
