from datetime import datetime
from pathlib import Path

import pytest

from utils import accounts, browser, config, dashboard, tasks, trends


# ======================== 账号管理 ========================

def test_load_accounts_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACCOUNTS_PATH", tmp_path / "not_exists.txt")
    with pytest.raises(RuntimeError):
        accounts.load_accounts()


def test_load_accounts_parses_and_skips_comments(tmp_path, monkeypatch):
    cfg = tmp_path / "accounts.txt"
    cfg.write_text("u1:p1\n# 注释行\nu2:p2; u3:p3", encoding="utf-8")
    monkeypatch.setattr(config, "ACCOUNTS_PATH", cfg)
    assert accounts.load_accounts() == [("u1", "p1"), ("u2", "p2"), ("u3", "p3")]


def test_load_accounts_empty_raises(tmp_path, monkeypatch):
    cfg = tmp_path / "accounts.txt"
    cfg.write_text("# 只有注释\n", encoding="utf-8")
    monkeypatch.setattr(config, "ACCOUNTS_PATH", cfg)
    with pytest.raises(RuntimeError):
        accounts.load_accounts()


def test_load_accounts_explicit_path_overrides_default(tmp_path):
    cfg = tmp_path / "other.txt"
    cfg.write_text("a:b", encoding="utf-8")
    assert accounts.load_accounts(cfg) == [("a", "b")]


# ======================== 热词 ========================

def test_ensure_keywords_fallback():
    result = trends.ensure_keywords([], [], None)
    assert result == config.FALLBACK_KEYWORDS
    assert result is not config.FALLBACK_KEYWORDS


def test_ensure_keywords_pick_first_non_empty():
    words = ["a", "b"]
    assert trends.ensure_keywords([], words, ["c"]) is words


def test_get_cached_trends_hit():
    trends._TRENDS_CACHE.clear()
    called = {"n": 0}

    def fetcher():
        called["n"] += 1
        return ["a", "b"]

    assert trends._get_cached_trends("k", fetcher) == ["a", "b"]
    assert trends._get_cached_trends("k", fetcher) == ["a", "b"]
    assert called["n"] == 1


def test_get_cached_trends_failure_ttl(monkeypatch):
    trends._TRENDS_CACHE.clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(trends.time, "time", lambda: t["now"])

    called = {"n": 0}

    def fetcher():
        called["n"] += 1
        return []

    assert trends._get_cached_trends("k2", fetcher) == []
    t["now"] += 1
    assert trends._get_cached_trends("k2", fetcher) == []
    assert called["n"] == 1
    t["now"] += config.TRENDS_CACHE_FAILURE_TTL + 1
    assert trends._get_cached_trends("k2", fetcher) == []
    assert called["n"] == 2


def test_get_cached_trends_cached_list_not_mutated():
    trends._TRENDS_CACHE.clear()
    words = trends._get_cached_trends("k3", lambda: ["x"])
    words.append("y")
    assert trends._get_cached_trends("k3", lambda: ["x"]) == ["x"]


# ======================== JSON 提取 ========================

def test_extract_json_array_handles_nesting():
    html = 'prefix "dailySetItems":[{"a":[1,2],"b":{"c":3}}] suffix'
    assert dashboard._extract_json_array(html, "dailySetItems") == '[{"a":[1,2],"b":{"c":3}}]'


def test_extract_json_array_missing_key():
    assert dashboard._extract_json_array("no key here", "dailySetItems") is None


def test_extract_json_object_handles_nesting():
    html = 'x "pointClaim":{"points":10,"entries":[{"a":1}]} y'
    assert dashboard._extract_json_object(html, "pointClaim") == '{"points":10,"entries":[{"a":1}]}'


def test_extract_json_object_missing_key():
    assert dashboard._extract_json_object("nothing", "pointClaim") is None


# ======================== Dashboard 数据解析 ========================

class FakePage:
    def __init__(self, html: str, body_text: str = ""):
        self._html = html
        self._body_text = body_text
        self.url = "https://rewards.bing.com/dashboard"

    def content(self) -> str:
        return self._html

    def evaluate(self, _script) -> str:
        return self._body_text


def test_parse_dashboard_data_full():
    today = datetime.now().strftime("%m/%d/%Y")
    html = (
        '"dailySetItems":['
        f'{{"title":"任务A","points":10,"destination":"https://x/a","offerId":"o1","date":"{today}","isCompleted":false}},'
        f'{{"title":"任务B","points":5,"destination":"https://x/b","offerId":"o2","date":"{today}","isCompleted":true}},'
        '{"title":"昨天的","points":5,"destination":"https://x/c","offerId":"o3","date":"12/01/2024","isCompleted":false}'
        ']'
        '"pointClaim":{"points":183,"entries":[{"id":1}]}'
    )
    body = "搜索: 5/37 移动搜索: 2/20 活动: 1/3"
    data = dashboard.parse_dashboard_data(FakePage(html, body))

    assert data["claimable_points"] == 183
    assert data["claim_entries"] == [{"id": 1}]
    assert [item["title"] for item in data["daily_set_items"]] == ["任务A"]
    assert data["pc_search_done"] == 5
    assert data["pc_search_total"] == 37
    assert data["mobile_search_done"] == 2
    assert data["mobile_search_total"] == 20
    assert data["daily_activities_done"] == 1
    assert data["daily_activities_total"] == 3


def test_parse_dashboard_data_fallback_latest_date():
    html = (
        '"dailySetItems":['
        '{"title":"旧","points":1,"destination":"https://x/a","offerId":"o1","date":"12/01/2024","isCompleted":false},'
        '{"title":"新","points":2,"destination":"https://x/b","offerId":"o2","date":"12/05/2024","isCompleted":false}'
        ']'
    )
    data = dashboard.parse_dashboard_data(FakePage(html))
    assert [item["title"] for item in data["daily_set_items"]] == ["新"]


def test_parse_dashboard_data_defaults_without_data():
    data = dashboard.parse_dashboard_data(FakePage("<html></html>"))
    assert data["claimable_points"] == 0
    assert data["daily_set_items"] == []
    assert data["pc_search_total"] == config.DEFAULT_PC_SEARCHES
    assert data["pc_search_done"] == 0


# ======================== 关键词循环 / 批量搜索 ========================

def test_keyword_cycle_no_mutation_and_no_repeat():
    words = ["a", "b", "c"]
    original = words.copy()
    cycle = browser._keyword_cycle(words)
    seen = [next(cycle) for _ in range(6)]
    assert words == original
    # 相邻两次不重复
    for prev, cur in zip(seen, seen[1:]):
        assert prev != cur
    assert set(seen) == set(words)


def test_do_searches_skips_when_count_zero(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    browser.do_searches(object(), 0, ["a"], "PC")
    assert called["n"] == 0


def test_do_searches_skips_when_no_keywords(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    browser.do_searches(object(), 3, [], "PC")
    assert called["n"] == 0


def test_do_searches_runs_requested_count(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(browser.time, "sleep", lambda *_: None)
    browser.do_searches(object(), 3, ["a", "b"], "PC")
    assert called["n"] == 3


# ======================== 领取积分 ========================

class FakeLocator:
    def __init__(self, count=0):
        self._count = count

    def count(self):
        return self._count

    def is_visible(self):
        return False


class EmptyPage:
    url = "https://rewards.bing.com/dashboard"

    def locator(self, _selector):
        return FakeLocator(0)

    def wait_for_timeout(self, *_):
        return None

    def wait_for_selector(self, *_, **__):
        return None


def test_claim_available_points_returns_zero_without_button():
    assert tasks.claim_available_points(EmptyPage()) == 0


# ======================== 日常任务（/earn 页） ========================

def test_extract_daily_earn_links_filters_and_dedupes():
    panel = '''
    <button aria-label="日常任务" slot="trigger"></button>
    <div class="react-aria-DisclosurePanel">
      <a href="https://www.bing.com/search?q=a&amp;form=ML2W4J&amp;OCID=ML2W4J" target="_blank">
        <img alt="任务A"><p>任务A</p></a>
      <a href="https://www.bing.com/search?q=b&amp;form=ML2Y2H" target="_blank">
        <img alt="任务B"><p>已完成</p></a>
      <a href="https://rewards.bing.com/referandearn/" target="_blank">
        <img alt="邀请"><p>邀请</p></a>
      <a href="https://www.bing.com/search?q=a&amp;form=ML2W4J&amp;OCID=ML2W4J" target="_blank">
        <img alt="任务A重复"><p>重复</p></a>
    </div>
    <button aria-label="其他" slot="trigger"></button>
    '''
    items = tasks.extract_daily_earn_links(panel)
    assert len(items) == 1
    assert items[0]["title"] == "任务A"
    assert "bing.com/search" in items[0]["url"]
    assert "&" in items[0]["url"]  # &amp; 已还原


def test_extract_daily_earn_links_matches_span_cards():
    panel = '''
    <button aria-label="日常任务" slot="trigger"></button>
    <div class="react-aria-DisclosurePanel">
      <span href="https://www.bing.com/search?q=c&amp;form=ML2Y2H" target="_blank">
        <img alt="任务C"><p>任务C</p></span>
    </div>
    '''
    items = tasks.extract_daily_earn_links(panel)
    assert [item["title"] for item in items] == ["任务C"]


def test_extract_daily_earn_links_missing_panel():
    assert tasks.extract_daily_earn_links("<html></html>") == []
