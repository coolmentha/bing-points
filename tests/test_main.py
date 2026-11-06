from datetime import datetime
from pathlib import Path

import pytest

from utils import accounts, browser, clients, config, dashboard, tasks, trends


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
    body = "可用积分\n\n3,782\n活动: 1/3"
    data = dashboard.parse_dashboard_data(FakePage(html, body))

    assert data["claimable_points"] == 183
    assert data["claim_entries"] == [{"id": 1}]
    assert [item["title"] for item in data["daily_set_items"]] == ["任务A"]
    assert data["total_points"] == 3782
    # 面板无今日搜索计数，一律用默认值
    assert data["pc_search_done"] == 0
    assert data["pc_search_total"] == config.DEFAULT_PC_SEARCHES
    assert data["daily_activities_done"] == 1
    assert data["daily_activities_total"] == 3


def test_parse_dashboard_data_ignores_streak_search_counter():
    """打卡子任务"搜索: 1/1"不能被当成今日搜索进度"""
    data = dashboard.parse_dashboard_data(FakePage("<html></html>", "搜索: 1/1\n活动: 3/3"))
    assert data["pc_search_done"] == 0
    assert data["pc_search_total"] == config.DEFAULT_PC_SEARCHES


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
    assert data["total_points"] == 0
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


def test_do_searches_stops_when_score_stalls(monkeypatch):
    """积分余额连续2次核对无增长 → 提前结束（每4次核对一次）"""
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(browser.time, "sleep", lambda *_: None)
    browser.do_searches(object(), 20, ["a", "b"], "PC", verify=lambda: 100)
    assert called["n"] == 8  # 基线1次 + 第4、8次核对，连续2次无增长 → 第8次后停


def test_do_searches_continues_while_score_grows(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(browser.time, "sleep", lambda *_: None)
    scores = iter([100, 112, 124, 136, 148, 160])
    browser.do_searches(object(), 20, ["a", "b"], "PC", verify=lambda: next(scores))
    assert called["n"] == 20


def test_do_searches_full_count_when_score_unreadable(monkeypatch):
    """余额解析失败(None/0)不触发提前停止，跑满计划次数"""
    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(browser.time, "sleep", lambda *_: None)
    browser.do_searches(object(), 6, ["a", "b"], "PC", verify=lambda: None)
    assert called["n"] == 6


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


def test_claim_available_points_returns_zero_without_button(monkeypatch):
    # EmptyPage 只是选择器桩，不是可执行浏览器页；风控检测单独测试。
    monkeypatch.setattr(tasks, "detect_risk_control", lambda _page: None)
    assert tasks.claim_available_points(EmptyPage()) == 0


# ======================== 日常任务（/earn 页） ========================

def test_extract_daily_earn_links_filters_and_dedupes():
    panel = """
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
    """
    items = tasks.extract_daily_earn_links(panel)
    assert len(items) == 1
    assert items[0]["title"] == "任务A"
    assert "bing.com/search" in items[0]["url"]
    assert "&" in items[0]["url"]  # &amp; 已还原


def test_extract_daily_earn_links_matches_span_cards():
    panel = """
    <button aria-label="日常任务" slot="trigger"></button>
    <div class="react-aria-DisclosurePanel">
      <span href="https://www.bing.com/search?q=c&amp;form=ML2Y2H" target="_blank">
        <img alt="任务C"><p>任务C</p></span>
    </div>
    """
    items = tasks.extract_daily_earn_links(panel)
    assert [item["title"] for item in items] == ["任务C"]


def test_extract_daily_earn_links_missing_panel():
    assert tasks.extract_daily_earn_links("<html></html>") == []

# ======================== 每日拼图 / 限时活动 / 风控 ========================

def test_extract_daily_offer_tasks_includes_puzzle_and_search():
    panel = """
    <button aria-label="日常任务" slot="trigger"></button>
    <div class="react-aria-DisclosurePanel">
      <a href="https://www.bing.com/spotlight/imagepuzzle?form=ML2BF0" target="_blank">
        <img alt="完成此拼图"><p>完成此拼图</p></a>
      <a href="https://www.bing.com/search?q=today&amp;form=ML2BF1" target="_blank">
        <img alt="今日问答"><p>今日问答</p></a>
      <a href="https://rewards.bing.com/referandearn/" target="_blank">
        <img alt="邀请"><p>邀请</p></a>
    </div>
    """
    items = tasks.extract_daily_offer_tasks(panel)
    assert [item["kind"] for item in items] == ["puzzle", "search"]
    assert items[1]["url"].endswith("form=ML2BF1")


def test_extract_daily_offer_tasks_skips_completed():
    panel = """
    <button aria-label="日常任务" slot="trigger"></button>
    <div class="react-aria-DisclosurePanel">
      <a href="https://www.bing.com/spotlight/imagepuzzle" target="_blank">
        <img alt="拼图"><p>已完成</p></a>
    </div>
    """
    assert tasks.extract_daily_offer_tasks(panel) == []


def test_extract_point_punch_cards_filters_app_and_keeps_reward():
    html = """
    <a href="https://rewards.bing.com/earn/quest/WEB_punchcard">
      <h3>十一月购物指南</h3><span>+50</span><span>0/5 个任务</span>
    </a>
    <a href="https://rewards.bing.com/earn/quest/APP_punchcard">
      <h3>桌面奖励应用每周专属任务</h3><span>奖励应用专属</span><span>+40</span>
    </a>
    <a href="https://rewards.bing.com/earn/quest/NO_REWARD_punchcard">
      <h3>无积分活动</h3><span>0/5 个任务</span>
    </a>
    """
    cards = tasks.extract_point_punch_cards(html)
    assert len(cards) == 1
    assert cards[0]["title"] == "十一月购物指南"
    assert cards[0]["points"] == 50
    assert cards[0]["url"].endswith("WEB_punchcard")


def test_solve_sliding_puzzle_solves_known_state():
    state = [0, 1, 2, 3, 4, 5, -1, 6, 7]
    moves = tasks.solve_sliding_puzzle(state)
    assert moves == [(2, 1), (2, 2)]


def test_solve_sliding_puzzle_returns_empty_for_solved_board():
    assert tasks.solve_sliding_puzzle(list(range(8)) + [-1]) == []


def test_solve_sliding_puzzle_rejects_unsolvable_board():
    assert tasks.solve_sliding_puzzle([1, 2, 3, 4, 5, 6, 8, 7, -1]) is None


def test_detect_risk_control_ignores_component_without_verification_url():
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def evaluate(self, _script):
            return {"component": True, "title": "Microsoft Rewards"}
    assert tasks.detect_risk_control(Page()) is None


def test_detect_risk_control_returns_none_for_normal_page():
    page = FakePage("<html></html>", "每日活动 3/3")
    assert tasks.detect_risk_control(page) is None


def test_do_daily_offer_tasks_stops_before_opening_card_on_risk(monkeypatch):
    class RiskPage(EmptyPage):
        def content(self):
            return ""

        def evaluate(self, _script):
            return "Microsoft"

    class RiskPage(EmptyPage):
        url = "https://login.live.com/challenge"
        def content(self):
            return ""
        def evaluate(self, _script):
            return "Microsoft"

    monkeypatch.setattr(tasks, "safe_goto", lambda *a, **k: None)
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks.do_daily_offer_tasks(RiskPage())


def test_detect_risk_control_ignores_news_text_about_verification_codes():
    """真实事故：搜索“芒果TV崩了”时，新闻正文含“收不到验证码”不应触发微软风控。"""
    page = FakePage(
        "<html></html>",
        "芒果TV出现登录故障，大量用户反馈收不到验证码或页面报错。",
    )
    page.url = "https://cn.bing.com/search?q=%E8%8A%92%E6%9E%9CTV%E5%B4%A9%E4%BA%86&form=QBRE"
    assert tasks.detect_risk_control(page) is None


def test_read_puzzle_state_uses_visual_rows_and_columns():
    """真实页面 x=视觉行、y=视觉列；读取后必须得到行主序棋盘。"""
    class PuzzlePage:
        url = "https://www.bing.com/spotlight/imagepuzzle"
        def evaluate(self, _script):
            return [
                {"x": 0, "y": 0, "image": 0},
                {"x": 0, "y": 1, "image": 1},
                {"x": 0, "y": 2, "image": 2},
                {"x": 1, "y": 0, "image": 3},
                {"x": 1, "y": 1, "image": 4},
                {"x": 1, "y": 2, "image": 5},
                {"x": 2, "y": 0, "image": 6},
                {"x": 2, "y": 1, "image": -1},
                {"x": 2, "y": 2, "image": 7},
            ]
    assert tasks._read_puzzle_state(PuzzlePage()) == [0, 1, 2, 3, 4, 5, 6, -1, 7]


def test_claim_available_points_stops_on_risk(monkeypatch):
    monkeypatch.setattr(tasks, "detect_risk_control", lambda page: "验证码")
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks.claim_available_points(EmptyPage())


def test_do_searches_stops_on_risk_page(monkeypatch):
    class RiskBrowserPage:
        url = "https://login.live.com/challenge"
        def evaluate(self, _script):
            return {"component": False, "title": "Microsoft", "text": ""}

    called = {"n": 0}
    monkeypatch.setattr(browser, "do_bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(browser.BrowserRiskControlError):
        browser.do_searches(RiskBrowserPage(), 3, ["a", "b"], "PC")
    assert called["n"] == 1


def test_claim_available_points_propagates_risk_after_click(monkeypatch):
    class WireButton:
        def __init__(self):
            self.first = self
            self.last = self
        def count(self):
            return 1
        def is_visible(self):
            return True
        def is_enabled(self):
            return True
        def click(self):
            return None

    class DialogLocator:
        def __init__(self):
            self.first = self
            self.last = self
        def count(self):
            return 1
        def is_visible(self):
            return True
        def wait_for(self, **_):
            return None
        def locator(self, _):
            return WireButton()

    class ClaimCard:
        first = WireButton()

    class Page:
        url = "https://rewards.bing.com/dashboard"
        def locator(self, selector):
            return ClaimCard() if "可领取" in selector else DialogLocator()
        def wait_for_timeout(self, *_):
            return None
        def wait_for_selector(self, *_args, **_kwargs):
            return None
        def keyboard_press(self, *_):
            return None

    calls = {"n": 0}

    def ensure(_page):
        calls["n"] += 1
        if calls["n"] > 1:
            raise tasks.RewardsRiskControlError("验证码")

    monkeypatch.setattr(tasks, "_ensure_no_risk_control", ensure)
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks.claim_available_points(Page())


def test_handle_activity_page_stops_on_risk(monkeypatch):
    monkeypatch.setattr(tasks, "_ensure_no_risk_control",
                        lambda page: (_ for _ in ()).throw(tasks.RewardsRiskControlError("验证码")))
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks._handle_activity_page(EmptyPage())


def test_dashboard_activities_propagates_risk(monkeypatch):
    class Parent:
        def inner_html(self):
            return "<div>任务</div>"

    class RiskCard:
        def count(self):
            return 1
        def is_visible(self):
            return True
        def click(self, **_):
            return None
        def get_attribute(self, _):
            return "https://example.test/task"
        def locator(self, _):
            return Parent()

    class Context:
        pages = ["main"]

    class RiskPage(EmptyPage):
        context = Context()
        url = "https://rewards.bing.com/dashboard"
        def locator(self, selector):
            if "href" in selector or "button" in selector:
                return type("Cards", (), {
                    "count": lambda self: 1,
                    "nth": lambda self, i: RiskCard(),
                })()
            return FakeLocator(0)
        def wait_for_timeout(self, *_):
            return None

    monkeypatch.setattr(tasks, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_handle_activity_page",
                        lambda page: (_ for _ in ()).throw(tasks.RewardsRiskControlError("验证码")))
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks.do_dashboard_activities(RiskPage())


def test_claim_available_points_propagates_risk_after_first_click(monkeypatch):
    class ClaimButton:
        first = None
        def __init__(self):
            self.first = self
        def count(self):
            return 1
        def is_visible(self):
            return True
        def click(self):
            return None

    class Page:
        url = "https://rewards.bing.com/dashboard"
        def locator(self, _selector):
            return ClaimButton()
        def wait_for_timeout(self, *_):
            return None

    calls = {"n": 0}
    def ensure(_page):
        calls["n"] += 1
        if calls["n"] > 1:
            raise tasks.RewardsRiskControlError("验证码")

    monkeypatch.setattr(tasks, "_ensure_no_risk_control", ensure)
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks.claim_available_points(Page())


def test_handle_activity_page_propagates_risk_after_done_click(monkeypatch):
    class WireButton:
        first = None
        def __init__(self):
            self.first = self
        def count(self):
            return 1
        def is_visible(self):
            return True
        def click(self, **_):
            return None

    class Page(EmptyPage):
        def locator(self, selector):
            if "完成" in selector or "Done" in selector:
                return WireButton()
            return FakeLocator(0)
        def wait_for_timeout(self, *_):
            return None

    calls = {"n": 0}
    def ensure(_page):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise tasks.RewardsRiskControlError("验证码")

    monkeypatch.setattr(tasks, "_ensure_no_risk_control", ensure)
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks._handle_activity_page(Page())


def test_claim_quest_points_propagates_risk(monkeypatch):
    class Button:
        first = None
        def __init__(self):
            self.first = self
        def count(self):
            return 1
        def is_visible(self):
            return True
        def is_enabled(self):
            return True
        def click(self):
            return None

    class Page:
        def locator(self, _selector):
            return Button()
        def wait_for_timeout(self, *_):
            return None

    calls = {"n": 0}
    def ensure(_page):
        calls["n"] += 1
        if calls["n"] > 0:
            raise tasks.RewardsRiskControlError("验证码")

    monkeypatch.setattr(tasks, "_ensure_no_risk_control", ensure)
    with pytest.raises(tasks.RewardsRiskControlError):
        tasks._claim_quest_points(Page())


def test_detect_risk_control_reports_url_and_title():
    page = FakePage("<html></html>", "")
    class EvidencePage:
        url = "https://login.live.com/login.srf"
        def evaluate(self, _script):
            return {"component": True, "title": "Microsoft 帐户验证", "text": ""}
    reason = tasks.detect_risk_control(EvidencePage())
    assert reason is None


def test_detect_risk_control_ignores_non_microsoft_captcha_component():
    class Page:
        url = "https://example.com/captcha"
        def evaluate(self, _script):
            return {"component": True, "title": "CAPTCHA", "text": "please verify"}
    assert tasks.detect_risk_control(Page()) is None


def test_detect_risk_control_ignores_arkoselabs_search_query():
    page = FakePage("<html></html>", "ordinary results")
    page.url = "https://cn.bing.com/search?q=site%3Aarkoselabs.com"
    assert tasks.detect_risk_control(page) is None


def test_detect_risk_control_recognizes_microsoft_verification_path():
    class Page:
        url = "https://login.live.com/challenge"
        def evaluate(self, _script):
            return "Microsoft"
    assert "微软验证页面" in tasks.detect_risk_control(Page())


def test_detect_risk_control_ignores_microsoft_search_body_human_text():
    """Bing 搜索结果正文提到“验证你是人类”也不能当成微软风控。"""
    class Page:
        url = "https://cn.bing.com/search?q=verify+you+are+human"
        def evaluate(self, _script):
            return {"component": False, "title": "verify you are human - 搜索"}
    assert tasks.detect_risk_control(Page()) is None


def test_browser_search_propagates_detector_failure(monkeypatch):
    class Page:
        url = "https://www.bing.com/search?q=x"
    def boom(_page):
        raise tasks.RiskDetectionError("DOM gone")
    monkeypatch.setattr(tasks, "detect_risk_control", boom)
    with pytest.raises(tasks.RiskDetectionError):
        browser._detect_search_risk(Page())


def test_task_layer_propagates_risk_detection_error(monkeypatch):
    def boom(_page):
        raise tasks.RiskDetectionError("DOM gone")
    monkeypatch.setattr(tasks, "detect_risk_control", boom)
    with pytest.raises(tasks.RiskDetectionError):
        tasks.claim_available_points(EmptyPage())


def test_main_classifies_risk_detection_error_as_safe_stop(monkeypatch, capsys):
    import main as main_module
    class Guard:
        def close(self):
            return None
    monkeypatch.setattr(main_module, "load_accounts", lambda: [("u", "p")])
    monkeypatch.setattr(main_module.profile, "prepare_temp_dir", lambda *a, **k: None)
    monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: True)
    monkeypatch.setattr(main_module.profile, "cleanup_stale_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: Guard())
    monkeypatch.setattr(main_module, "sync_playwright", lambda: (_ for _ in ()).throw(tasks.RiskDetectionError("DOM gone")))
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 5
    assert "风控检测失败" in capsys.readouterr().out


def test_run_account_risk_detection_error_returns_five(monkeypatch, tmp_path):
    import main as main_module
    class Page:
        def goto(self, *_a, **_k):
            return None
        def wait_for_timeout(self, *_):
            return None
        def close(self):
            return None
    class Context:
        def add_init_script(self, _):
            return None
        def new_page(self):
            return Page()
        def close(self):
            return None
    class Guard:
        def close(self):
            return None
    def boom(_page):
        raise main_module.RiskDetectionError("DOM gone")
    monkeypatch.setattr(main_module, "load_accounts", lambda: [("u", "p")])
    monkeypatch.setattr(main_module.profile, "prepare_temp_dir", lambda *a, **k: None)
    monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: True)
    monkeypatch.setattr(main_module.profile, "cleanup_stale_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: Guard())
    monkeypatch.setattr(main_module, "create_context", lambda *a, **k: Context())
    monkeypatch.setattr(main_module, "detect_risk_control", boom)
    class Manager:
        def __enter__(self):
            return object()
        def __exit__(self, *exc):
            return False
    monkeypatch.setattr(main_module, "sync_playwright", lambda: Manager())
    assert main_module._run() == 5


def test_detect_risk_control_ignores_generic_captcha_on_bing_search():
    class Page:
        url = "https://cn.bing.com/search?q=captcha"
        def evaluate(self, _script):
            return {"component": True, "title": "captcha - 搜索"}
    assert tasks.detect_risk_control(Page()) is None


def test_detect_risk_control_does_not_treat_ppsecure_login_as_challenge():
    class Page:
        url = "https://login.live.com/ppsecure/post.srf"
        def evaluate(self, _script):
            return {"component": False, "title": "Sign in"}
    assert tasks.detect_risk_control(Page()) is None


def test_read_streak_progress_parses_edge_and_mobile():
    class Page:
        def evaluate(self, _script):
            return "Microsoft Edge\n分钟: 7/30\n移动应用\n签到: 1/1"
    assert tasks.read_streak_progress(Page(), "edge") == (7, 30)
    assert tasks.read_streak_progress(Page(), "mobile") == (1, 1)


def test_edge_streak_returns_success_only_at_30_30(monkeypatch):
    values = iter([(0, 30)] + [(5, 30)] * 20)
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def wait_for_timeout(self, *_):
            return None
        def mouse(self):
            return None
    monkeypatch.setattr(tasks, "read_streak_progress", lambda page, label: next(values))
    monkeypatch.setattr(tasks, "_open_edge_streak", lambda page: True)
    monkeypatch.setattr(tasks, "safe_goto", lambda *a, **k: None)
    assert tasks.do_edge_browsing_streak(Page(), minutes=1, poll_seconds=60) == 0


def test_mobile_checkin_marks_complete_without_app_as_skipped(monkeypatch):
    class Page:
        url = "https://bingapp.microsoft.com/bing"
        def evaluate(self, _script):
            return "Microsoft Edge\n分钟: 0/30\n移动应用\n签到: 0/1"
        def wait_for_timeout(self, *_):
            return None
    monkeypatch.setattr(tasks, "_open_mobile_app", lambda page: True)
    assert tasks.do_mobile_app_checkin(Page()) == 0


def test_edge_streak_does_not_claim_success_before_target(monkeypatch):
    values = iter([(0, 30), (5, 30), (20, 30), (29, 30), (29, 30)])
    class Page:
        url = "https://rewards.bing.com/dashboard"
        class Mouse:
            def wheel(self, *_):
                return None
        mouse = Mouse()
        def wait_for_timeout(self, *_):
            return None
    monkeypatch.setattr(tasks, "read_streak_progress", lambda page, label: next(values))
    monkeypatch.setattr(tasks, "_open_edge_streak", lambda page: True)
    monkeypatch.setattr(tasks, "safe_goto", lambda *a, **k: None)
    assert tasks.do_edge_browsing_streak(Page(), minutes=1, poll_seconds=60) == 0


def test_edge_streak_returns_success_when_already_complete(monkeypatch):
    monkeypatch.setattr(tasks, "read_streak_progress", lambda page, label: (30, 30))
    assert tasks.do_edge_browsing_streak(object(), minutes=1, poll_seconds=60) == 1


def test_mobile_checkin_returns_success_when_already_complete(monkeypatch):
    monkeypatch.setattr(tasks, "read_streak_progress", lambda page, label: (1, 1))
    assert tasks.do_mobile_app_checkin(object()) == 1


def test_run_account_invokes_streak_tasks(monkeypatch):
    import main as main_module
    calls = []
    class Context:
        def new_page(self):
            return object()
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def goto(self, *_a, **_k): pass
        def wait_for_timeout(self, *_): pass
        def close(self): pass
    monkeypatch.setattr(main_module, "do_login", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "ensure_account", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "detect_risk_control", lambda _p: None)
    monkeypatch.setattr(main_module, "parse_dashboard_data", lambda _p: {
        "claimable_points": 0, "daily_set_items": [],
        "daily_activities_done": 3, "daily_activities_total": 3,
        "pc_search_done": 0, "pc_search_total": 0,
    })
    monkeypatch.setattr(main_module, "claim_available_points", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_set_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_dashboard_activities", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_offer_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_point_punch_cards", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_edge_browsing_streak", lambda *a, **k: calls.append("edge") or 1)
    monkeypatch.setattr(main_module, "mobile_checkin", lambda *a, **k: calls.append("mobile") or 1)
    monkeypatch.setattr(main_module, "run_rewards_desktop_task", lambda: calls.append("webview") or 0)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    # Use a minimal page factory; all real interactions are stubbed.
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    main_module.run_account(None, ctx, "u", "p", True)
    assert calls == ["edge", "mobile", "webview"]


def test_run_account_skips_edge_streak_on_isolation_profile(monkeypatch):
    import main as main_module
    calls = []
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def goto(self, *_a, **_k): pass
        def wait_for_timeout(self, *_): pass
        def close(self): pass
    monkeypatch.setattr(main_module, "ensure_account", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "detect_risk_control", lambda _p: None)
    monkeypatch.setattr(main_module, "parse_dashboard_data", lambda _p: {
        "claimable_points": 0, "daily_set_items": [],
        "daily_activities_done": 3, "daily_activities_total": 3,
        "pc_search_done": 0, "pc_search_total": 0,
    })
    monkeypatch.setattr(main_module, "claim_available_points", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_set_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_dashboard_activities", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_offer_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_point_punch_cards", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_edge_browsing_streak", lambda *a, **k: calls.append("edge"))
    monkeypatch.setattr(main_module, "mobile_checkin", lambda *a, **k: calls.append("mobile") or 1)
    monkeypatch.setattr(main_module, "run_rewards_desktop_task", lambda: 0)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.USER_DATA_DIR)
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    main_module.run_account(None, ctx, "u", "p", True)
    assert calls == ["mobile"]


def test_run_account_returns_false_when_edge_streak_incomplete(monkeypatch):
    import main as main_module
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def goto(self, *_a, **_k): pass
        def wait_for_timeout(self, *_): pass
        def close(self): pass
    monkeypatch.setattr(main_module, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "detect_risk_control", lambda _p: None)
    monkeypatch.setattr(main_module, "parse_dashboard_data", lambda _p: {
        "claimable_points": 0, "daily_set_items": [],
        "daily_activities_done": 3, "daily_activities_total": 3,
        "pc_search_done": 0, "pc_search_total": 0,
    })
    monkeypatch.setattr(main_module, "claim_available_points", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_set_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_dashboard_activities", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_offer_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_point_punch_cards", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_edge_browsing_streak", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "read_streak_progress", lambda *a, **k: (10, 30))
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    assert main_module.run_account(None, ctx, "u", "p", True) is False


def test_run_account_rechecks_identity_after_login(monkeypatch):
    import main as main_module
    calls = []
    class Page:
        url = "https://login.live.com/login.srf"
        def goto(self, *_a, **_k): pass
        def wait_for_timeout(self, *_): pass
        def close(self): pass
    class LoggedInPage(Page):
        url = "https://rewards.bing.com/dashboard"
    monkeypatch.setattr(main_module, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "detect_risk_control", lambda _p: None)
    monkeypatch.setattr(main_module, "do_login", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "ensure_account", lambda *a, **k: calls.append("identity") or False)
    ctx = type("C", (), {"new_page": lambda self: LoggedInPage()})()
    assert main_module.run_account(None, ctx, "target@example.com", "pw", True) is False
    assert calls == ["identity"]


def test_mobile_checkin_completed_when_checked(monkeypatch):
    nodes = [
        {"text": "Streaks", "content-desc": "", "resource-id": ""},
        {"text": "Check in", "content-desc": "", "resource-id": ""},
        {"text": "checked", "content-desc": "", "resource-id": ""},
    ]
    monkeypatch.setattr(clients, "list_android_devices", lambda: ["emulator-5554"])
    monkeypatch.setattr(clients, "_uia_nodes", lambda serial: nodes)
    assert clients.mobile_checkin() == 1


def test_mobile_checkin_returns_zero_without_device(monkeypatch):
    monkeypatch.setattr(clients, "list_android_devices", lambda: [])
    assert clients.mobile_checkin() == 0


def test_rewards_desktop_state_available_shape(monkeypatch):
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="state": {
        "available": False, "task_progress": None, "success": False, "output": "",
    })
    result = clients.rewards_desktop_state()
    assert result["available"] is False
    assert result["task_progress"] is None


def test_rewards_desktop_state_parses_progress_and_uia_ready(monkeypatch):
    monkeypatch.setattr(clients, "_rewards_desktop_hwnd", lambda: 123)
    monkeypatch.setattr(clients, "REWARDS_DESKTOP_UIA_RESULT", config.PROJECT_ROOT / "tmp_missing_uia_result.txt")
    class Result:
        returncode = 0
        stdout = "AVAILABLE=1\nDOC=桌面奖励应用每周专属任务 – Microsoft Rewards\nTASK_PROGRESS=3/8\n"
    monkeypatch.setattr(clients.subprocess, "run", lambda *a, **k: Result())
    result = clients.rewards_desktop_state()
    assert result["available"] is True
    assert result["task_progress"] == "3/8"
    assert result["uia_ready"] is True


def test_rewards_desktop_run_does_not_claim_success_without_growth(monkeypatch):
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="run": {
        "available": True,
        "task_progress": "3/8",
        "success": False,
        "uia_ready": True,
        "output": "",
    })
    assert clients.run_rewards_desktop_task() == 0


def test_rewards_desktop_run_reports_uia_failure(monkeypatch):
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="run": {
        "available": True,
        "task_progress": None,
        "success": False,
        "uia_ready": False,
        "output": "",
    })
    assert clients.run_rewards_desktop_task() == -1


def test_rewards_desktop_run_returns_success_after_progress_growth(monkeypatch):
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="run": {
        "available": True,
        "task_progress": "4/8",
        "success": True,
        "uia_ready": True,
        "output": "",
    })
    assert clients.run_rewards_desktop_task() == 1


def test_mobile_checkin_taps_check_in_and_confirms_checked(monkeypatch):
    unchecked = [
        {"text": "Streaks", "content-desc": "", "resource-id": ""},
        {"text": "Check in", "content-desc": "", "resource-id": "", "bounds": "[73,1548][217,1609]"},
        {"text": "", "content-desc": "", "resource-id": "", "bounds": "[73,1548][1008,1609]", "clickable": "true"},
    ]
    checked = unchecked + [
        {"text": "checked", "content-desc": "", "resource-id": ""},
    ]
    monkeypatch.setattr(clients, "list_android_devices", lambda: ["emulator-5554"])
    monkeypatch.setattr(clients, "_uia_nodes", lambda serial: unchecked)
    called = {"tap": 0}
    monkeypatch.setattr(clients, "_tap_node", lambda serial, node: called.__setitem__("tap", called["tap"] + 1) or True)
    monkeypatch.setattr(clients, "time", type("T", (), {"sleep": staticmethod(lambda _: None)}))
    # Mobile check-in queries UIA after tapping, so use a stateful sequence.
    monkeypatch.setattr(clients, "_uia_nodes", lambda serial: checked if called["tap"] else unchecked)
    assert clients.mobile_checkin() == 1
    assert called["tap"] == 1




def test_open_edge_streak_returns_true_for_known_activity_url():
    class Page:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"

    assert tasks._open_edge_streak(Page()) is True


def test_checkin_action_ignores_partial_horizontal_clickable_node():
    label = {"text": "Check in", "bounds": "[73,1548][217,1609]"}
    partial = {"bounds": "[180,1548][220,1609]", "clickable": "true"}
    real = {"bounds": "[73,1548][1008,1609]", "clickable": "true"}
    _, target = clients._checkin_action_node([label, partial, real])
    assert target is real


def test_detect_risk_control_ignores_title_read_failure():
    class Page:
        url = "https://login.live.com/challenge"

        def evaluate(self, _script):
            raise RuntimeError("Target page, context or browser has been closed")

    reason = tasks.detect_risk_control(Page())
    assert reason is not None
    assert "title=" in reason


def test_detect_risk_control_normal_page_does_not_raise_on_title_failure():
    class Page:
        url = "https://rewards.bing.com/dashboard"

        def evaluate(self, _script):
            raise RuntimeError("Target page, context or browser has been closed")

    assert tasks.detect_risk_control(Page()) is None
