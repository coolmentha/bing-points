import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from utils import accounts, browser, clients, config, dashboard, tasks, trends
from utils.tasks import EdgeStreakResult, EdgeStreakStatus


@pytest.fixture(autouse=True)
def _never_send_real_mail_from_main(monkeypatch):
    """main/_run 现在会自动通知；测试绝不能连接真实 SMTP。"""
    from utils import notify

    def _boom(*_args, **_kwargs):
        raise AssertionError("测试不允许真的发送邮件")

    monkeypatch.setattr(notify, "send_mail", _boom)


@pytest.fixture(autouse=True)
def edge_browser_logged_in(tmp_path_factory, monkeypatch):
    """Edge 打卡行为测试默认使用已登录的临时 profile。"""
    root = tmp_path_factory.mktemp("synthetic_edge_user_data")
    profile = root / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    (root / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Default": {"user_name": "synthetic@example.invalid"}
                }
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(tasks, "EDGE_USER_DATA_DIR", root)


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


def _stub_main_run_for_notifications(
    monkeypatch, *, run_results=(False,), allow_multi=True, guard=None
):
    import main as main_module

    state = {"run_results": list(run_results)}
    monkeypatch.setattr(main_module, "load_accounts", lambda: [("u@example.com", "p")])
    monkeypatch.setattr(
        main_module, "run_account", lambda *a, **k: state["run_results"].pop(0)
    )
    monkeypatch.setattr(main_module.profile, "prepare_temp_dir", lambda *a, **k: None)
    monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: True)
    monkeypatch.setattr(main_module.profile, "cleanup_stale_artifacts", lambda *a, **k: 0)
    if allow_multi:
        monkeypatch.setenv("BING_REWARDS_ALLOW_MULTI", "1")
        monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: guard)
    else:
        monkeypatch.delenv("BING_REWARDS_ALLOW_MULTI", raising=False)
        monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: guard)

    class Context:
        def add_init_script(self, _script):
            return None

        def close(self):
            return None

    class Manager:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(main_module, "sync_playwright", lambda: Manager())
    monkeypatch.setattr(main_module, "create_context", lambda *a, **k: Context())
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    return main_module


def test_failure_notification_is_sent_before_single_instance_guard_close(monkeypatch):
    events = []

    class Guard:
        def close(self):
            events.append("guard.close")

    main_module = _stub_main_run_for_notifications(
        monkeypatch, allow_multi=False, guard=Guard()
    )
    from utils import notify

    monkeypatch.setattr(
        notify,
        "notify_failure",
        lambda *a, **k: events.append("notify") or True,
    )

    assert main_module.main() == 3
    assert events == ["notify", "guard.close"]


def test_main_sends_failure_notification_once(monkeypatch):
    main_module = _stub_main_run_for_notifications(monkeypatch)
    from utils import notify

    calls = []
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module.main() == 3
    assert len(calls) == 1
    assert calls[0][0] == 3


def test_main_does_not_send_notification_on_success(monkeypatch):
    main_module = _stub_main_run_for_notifications(monkeypatch, run_results=(True,))
    from utils import notify

    calls = []
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module.main() == 0
    assert calls == []


def test_main_notification_exception_preserves_exit_code_and_releases_guard(monkeypatch):
    events = []

    class Guard:
        def close(self):
            events.append("guard.close")

    main_module = _stub_main_run_for_notifications(
        monkeypatch, allow_multi=False, guard=Guard()
    )
    from utils import notify

    def boom(*_args, **_kwargs):
        events.append("notify.boom")
        raise RuntimeError("notification failed")

    monkeypatch.setattr(notify, "notify_failure", boom)

    assert main_module.main() == 3
    assert events == ["notify.boom", "guard.close"]


def test_main_notifies_on_early_account_read_failure_without_guard(monkeypatch):
    main_module = _stub_main_run_for_notifications(monkeypatch, allow_multi=True)
    from utils import notify

    def fail_load():
        raise FileNotFoundError("accounts.txt missing")

    calls = []
    monkeypatch.setattr(main_module, "load_accounts", fail_load)
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module.main() == 4
    assert len(calls) == 1
    assert calls[0][0] == 4


def test_direct_run_early_failure_still_notifies(monkeypatch):
    main_module = _stub_main_run_for_notifications(monkeypatch, allow_multi=True)
    from utils import notify

    calls = []
    monkeypatch.setattr(
        main_module, "load_accounts",
        lambda: (_ for _ in ()).throw(FileNotFoundError("accounts.txt missing")),
    )
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module._run() == 4
    assert len(calls) == 1
    assert calls[0][0] == 4
    assert isinstance(calls[0][1], notify.RunLog)


@pytest.mark.parametrize("exit_code", [2, 3, 4, 5])
def test_each_failure_exit_code_notifies_exactly_once(monkeypatch, exit_code):
    main_module = _stub_main_run_for_notifications(monkeypatch, run_results=(False,))
    from utils import notify

    if exit_code == 2:
        monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: False)
    elif exit_code == 4:
        monkeypatch.setattr(
            main_module, "load_accounts",
            lambda: (_ for _ in ()).throw(FileNotFoundError("missing")),
        )
    elif exit_code == 5:
        monkeypatch.setattr(
            main_module, "sync_playwright",
            lambda: (_ for _ in ()).throw(main_module.RiskDetectionError("risk")),
        )

    calls = []
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module.main() == exit_code
    assert len(calls) == 1
    assert calls[0][0] == exit_code


def test_failure_body_contains_run_identity_and_no_smtp_secret(monkeypatch, tmp_path):
    from utils import notify

    log_path = tmp_path / "run_current.log"
    run_log = notify.RunLog(
        "本次日志内容",
        run_info={
            "pid": 4321,
            "log_path": str(log_path),
            "started_at": "2026-11-08 19:58:38",
            "log_mtime": "2026-11-08 20:05:00",
        },
    )
    sent = {}
    monkeypatch.setattr(
        notify,
        "load_smtp_config",
        lambda *a, **k: {
            "host": "smtp.example.invalid",
            "port": 465,
            "user": "u",
            "password": "synthetic-smtp-secret",
            "to": ["a@b.c"],
            "sender": "u",
            "use_ssl": True,
        },
    )
    monkeypatch.setattr(notify, "send_mail", lambda cfg, s, b, **k: sent.update(body=b))

    assert notify.notify_failure(3, run_log) is True
    body = sent["body"]
    assert "本次运行 PID：4321" in body
    assert f"本次运行日志：{log_path}" in body
    assert "2026-11-08 19:58:38" in body
    assert "2026-11-08 20:05:00" in body
    assert "synthetic-smtp-secret" not in body


def test_notification_log_tail_uses_this_run_log_not_newer_log(monkeypatch, tmp_path):
    current_log = tmp_path / "run_current.log"
    newer_log = tmp_path / "run_newer.log"
    current_log.write_text("CURRENT RUN ONLY\n", encoding="utf-8")
    newer_log.write_text("NEW INSTANCE LOG\n", encoding="utf-8")
    monkeypatch.setenv("BING_REWARDS_LOG_FILE", str(current_log))
    main_module = _stub_main_run_for_notifications(
        monkeypatch, allow_multi=True
    )
    from utils import notify

    calls = []
    monkeypatch.setattr(notify, "notify_failure", lambda *a, **k: calls.append(a) or True)

    assert main_module.main() == 3
    assert len(calls) == 1
    run_log = calls[0][1]
    body = notify.build_body(calls[0][0], run_log)
    assert f"本次运行日志：{current_log}" in body
    assert "CURRENT RUN ONLY" in body
    assert "NEW INSTANCE LOG" not in body


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


@pytest.mark.parametrize(
    "url",
    [
        "https://rewards.bing.com/promo/edge-browsing-streak",
        "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL&OCID=AIDcmm",
    ],
)
def test_is_edge_streak_url_accepts_official_activity_url(url):
    assert tasks._is_edge_streak_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/promo/edge-browsing-streak",
        "https://www.bing.com/search?q=edge-browsing-streak",
        "http://rewards.bing.com/promo/edge-browsing-streak",
        "file:///promo/edge-browsing-streak",
        "https://rewards.bing.com/promo/edge-browsing-streak/extra",
        "https://rewards.bing.com:8443/promo/edge-browsing-streak",
    ],
)
def test_is_edge_streak_url_rejects_untrusted_urls(url):
    assert tasks._is_edge_streak_url(url) is False


def test_edge_browser_profile_login_detects_user_name(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    local_state = {
        "profile": {
            "info_cache": {
                "Default": {"user_name": "synthetic@example.invalid"}
            }
        }
    }
    (tmp_path / "Local State").write_text(
        json.dumps(local_state), encoding="utf-8"
    )

    assert tasks.edge_browser_profile_logged_in(tmp_path) is True


def test_edge_browser_profile_login_detects_account_info(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        json.dumps({"account_info": [{"account_name": "synthetic"}]}),
        encoding="utf-8",
    )
    local_state = {"profile": {"info_cache": {"Default": {"user_name": ""}}}}
    (tmp_path / "Local State").write_text(
        json.dumps(local_state), encoding="utf-8"
    )

    assert tasks.edge_browser_profile_logged_in(tmp_path) is True


def test_edge_browser_profile_login_requires_positive_account_evidence(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(
        json.dumps({"account_info": []}), encoding="utf-8"
    )
    local_state = {"profile": {"info_cache": {"Default": {"user_name": ""}}}}
    (tmp_path / "Local State").write_text(
        json.dumps(local_state), encoding="utf-8"
    )

    assert tasks.edge_browser_profile_logged_in(tmp_path) is False


def test_edge_browser_profile_login_missing_or_corrupt_json_is_not_logged_in(tmp_path):
    missing = tmp_path / "missing"
    assert tasks.edge_browser_profile_logged_in(missing) is False

    corrupt = tmp_path / "corrupt"
    profile = corrupt / "Default"
    profile.mkdir(parents=True)
    (corrupt / "Local State").write_text("{broken", encoding="utf-8")
    (profile / "Preferences").write_text("{broken", encoding="utf-8")
    assert tasks.edge_browser_profile_logged_in(corrupt) is False


def test_edge_profile_identity_uses_current_profile_and_matches_exact_email(tmp_path):
    account_a = "account-a@example.invalid"
    account_b = "account-b@example.invalid"
    for name in ("Default", "Profile 1"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "Preferences").write_text("{}", encoding="utf-8")
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Default": {"user_name": account_a},
                    "Profile 1": {"user_name": account_b},
                },
                "last_used": "Profile 1",
            }
        }),
        encoding="utf-8",
    )

    assert tasks.edge_profile_identity(tmp_path) == account_b
    assert tasks.edge_browser_profile_matches(tmp_path, f" {account_b.upper()} ") is True
    assert tasks.edge_browser_profile_matches(tmp_path, account_a) is False


def test_edge_profile_identity_does_not_guess_between_multiple_profiles(tmp_path):
    for name in ("Default", "Profile 1"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "Preferences").write_text("{}", encoding="utf-8")
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Default": {"user_name": "account-a@example.invalid"},
                    "Profile 1": {"user_name": "account-b@example.invalid"},
                }
            }
        }),
        encoding="utf-8",
    )

    assert tasks.edge_profile_identity(tmp_path) is None
    assert tasks.edge_browser_profile_matches(
        tmp_path, "account-a@example.invalid"
    ) is False
    assert tasks.edge_browser_profile_logged_in(tmp_path) is False


def test_edge_profile_identity_reads_preferences_email_without_local_user_name(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    expected = "profile-email@example.invalid"
    (profile / "Preferences").write_text(
        json.dumps({"account_info": [{"email": expected}]}),
        encoding="utf-8",
    )
    (tmp_path / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {"Default": {"user_name": ""}}}}),
        encoding="utf-8",
    )

    assert tasks.edge_profile_identity(tmp_path) == expected
    assert tasks.edge_browser_profile_matches(tmp_path, expected) is True


def test_edge_profile_identity_corrupt_json_is_unknown(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{broken", encoding="utf-8")
    (tmp_path / "Local State").write_text("{broken", encoding="utf-8")

    assert tasks.edge_profile_identity(tmp_path) is None
    assert tasks.edge_browser_profile_matches(
        tmp_path, "account@example.invalid"
    ) is False


def test_edge_streak_rejects_unexpected_profile_before_opening(monkeypatch, capsys):
    page = type("Page", (), {"url": "https://rewards.bing.com/dashboard"})()
    actions = []
    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (0, 30))
    monkeypatch.setattr(tasks, "edge_browser_profile_matches", lambda *_args: False)
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda *_args: actions.append("open") or tasks.EdgeStreakEntry(page, (), False),
    )

    assert tasks.do_edge_browsing_streak(
        page, minutes=30, poll_seconds=60, expected_email="target@example.invalid"
    ) == 0
    assert actions == []
    output = capsys.readouterr().out
    assert "目标账号不匹配" in output


def test_edge_streak_identity_mismatch_cannot_be_overridden_by_complete_progress(monkeypatch):
    page = type("Page", (), {"url": "https://rewards.bing.com/dashboard"})()
    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (30, 30))
    monkeypatch.setattr(
        tasks, "edge_profile_identity", lambda *_args: "other@example.invalid"
    )
    opened = []
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda *_args: opened.append(True) or tasks.EdgeStreakEntry(page, (), False),
    )

    result = tasks.do_edge_browsing_streak_detail(
        page, expected_email="target@example.invalid"
    )

    assert result.status is tasks.EdgeStreakStatus.IDENTITY_MISMATCH
    assert result.progress is None
    assert result.success is False
    assert opened == []
    assert tasks.do_edge_browsing_streak(
        page, expected_email="target@example.invalid"
    ) == 0


def test_edge_streak_identity_unverified_is_explicit(monkeypatch):
    page = type("Page", (), {"url": "https://rewards.bing.com/dashboard"})()
    monkeypatch.setattr(tasks, "edge_profile_identity", lambda *_args: None)

    result = tasks.do_edge_browsing_streak_detail(
        page, expected_email="target@example.invalid"
    )

    assert result.status is tasks.EdgeStreakStatus.IDENTITY_UNVERIFIED
    assert result.success is False


@pytest.mark.parametrize("progress", [(0, 0), (1, 0), (-1, 30), (31, 30)])
def test_valid_edge_progress_rejects_invalid_ranges(progress):
    assert tasks.valid_edge_progress(progress) is None


@pytest.mark.parametrize("progress", [(0, 30), (29, 30), (30, 30)])
def test_valid_edge_progress_accepts_inclusive_range(progress):
    assert tasks.valid_edge_progress(progress) == progress


@pytest.mark.parametrize("progress", [(0, 0), (1, 0), (-1, 30), (31, 30)])
def test_edge_streak_rejects_invalid_progress(monkeypatch, progress):
    page = type("Page", (), {"url": "https://rewards.bing.com/dashboard"})()
    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: progress)
    monkeypatch.setattr(tasks, "edge_browser_profile_logged_in", lambda *_args: True)

    result = tasks.do_edge_browsing_streak_detail(page)

    assert result.status is tasks.EdgeStreakStatus.INVALID_PROGRESS
    assert result.success is False
    assert tasks.do_edge_browsing_streak(page) == 0


class _PageExpectation:
    def __init__(self, context):
        self.context = context

    @property
    def value(self):
        return self.context.next_page

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self.context.next_page is None:
            raise TimeoutError("expect_page timeout")
        return False


class _ExpectPageContext:
    def __init__(self, pages=()):
        self.pages = list(pages)
        self.handlers = {}
        self.next_page = None

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers[event].remove(handler)

    def emit(self, event, page):
        if page not in self.pages:
            self.pages.append(page)
        for handler in list(self.handlers.get(event, ())):
            handler(page)

    def expect_page(self, **_kwargs):
        return _PageExpectation(self)


def test_prepare_edge_streak_does_not_adopt_unowned_historical_activity_page(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Context(_ExpectPageContext):
        def __init__(self, pages):
            super().__init__(pages)

    class Link(EmptyLocator):
        def __init__(self, page):
            super().__init__()
            self.page = page

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            # 模拟点击没有产生 page 事件，但上下文里同时出现一个活动页。
            self.page.context.pages.append(self.page.historical_activity)

    class Page:
        closed = False

        def __init__(self, name, url, context):
            self.name = name
            self.url = url
            self.context = context
            self.historical_activity = None

        def is_closed(self):
            return self.closed

        def locator(self, selector):
            if "role='dialog'" in selector:
                return Link(self)
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    historical = Page(
        "historical",
        "https://rewards.bing.com/promo/edge-browsing-streak?form=STALE",
        None,
    )
    context = Context([historical])
    page = Page("dashboard", "https://rewards.bing.com/dashboard", context)
    historical.context = context
    context.pages = [historical, page]
    page.historical_activity = historical
    monkeypatch.setattr(tasks, "EDGE_STREAK_REDIRECT_TIMEOUT_MS", 1)

    entry = tasks._prepare_edge_streak(page)

    assert entry.activity_page is None
    assert entry.created_pages == ()
    assert entry.can_cleanup is False


def test_edge_streak_expect_page_owns_only_clicked_tabs(monkeypatch):
    class EventContext(_ExpectPageContext):
        def expect_page(self, **_kwargs):
            raise AssertionError("有 expect_popup 时不得使用 context 级 expect_page")

    class Locator:
        def __init__(self, page, kind):
            self.page = page
            self.kind = kind
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def wait_for(self, **_kwargs):
            return None

        def click(self):
            if self.kind == "prepare":
                # 用户并发打开的页面触发全局 page 事件，但不是 expect_page 的结果。
                self.page.context.emit("page", self.page.user_tab)
                self.page.context.next_page = self.page.activity
                self.page.context.pages.append(self.page.activity)
            elif self.kind == "activate":
                self.page.context.emit("page", self.page.user_tab_2)
                self.page.context.next_page = self.page.activation
                self.page.context.pages.append(self.page.activation)

    class Page:
        def __init__(self, name, url, context=None):
            self.name = name
            self.url = url
            self.closed = False
            self.context = context
            self.wheel_count = 0
            self.mouse = type(
                "Mouse", (), {"wheel": lambda _self, *_args: setattr(
                    self, "wheel_count", self.wheel_count + 1
                )}
            )()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, selector):
            if "role='dialog'" in selector and "使用 Edge" in selector:
                return Locator(self, "prepare")
            if "开始在 Edge 上浏览" in selector:
                return Locator(self, "activate")
            return Locator(self, "empty")

        def expect_popup(self, **_kwargs):
            return _PageExpectation(self.context)

        def wait_for_timeout(self, _ms):
            return None

    context = EventContext([])
    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard", context)
    historical = Page("historical", "https://example.com/user-tab", context)
    activity = Page(
        "activity",
        "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL",
        context,
    )
    activation = Page(
        "activation", "https://cn.bing.com/?rwgbopen=1&FORM=rwbsl1", context
    )
    user_tab = Page("user-tab", "https://example.com/new-user-tab", context)
    user_tab_2 = Page("user-tab-2", "https://example.com/another-user-tab", context)
    context.pages = [historical, dashboard]
    dashboard.user_tab = user_tab
    dashboard.activity = activity
    activity.user_tab_2 = user_tab_2
    activity.activation = activation

    progress = iter([(0, 30), (30, 30)])
    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: next(progress))
    monkeypatch.setattr(tasks, "safe_goto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "edge_browser_profile_logged_in", lambda *_args: True)

    assert tasks.do_edge_browsing_streak(dashboard, minutes=1, poll_seconds=60) == 1
    assert activity.closed is True
    assert activation.closed is True
    assert dashboard.closed is False
    assert historical.closed is False
    assert user_tab.closed is False
    assert user_tab_2.closed is False


def test_open_edge_streak_waits_for_delayed_redirect():
    """新标签页可慢跳转；不关闭它以免打断活动上报，残余标签页风险已知。"""
    class Locator:
        def __init__(self, page, kind, visible=True):
            self.page = page
            self.kind = kind
            self.visible = visible
            self.first = self

        def count(self):
            return 1 if self.visible else 0

        def is_visible(self):
            return self.visible

        def wait_for(self, **_):
            if not self.visible:
                raise TimeoutError("not visible")

        def click(self):
            if self.kind == "card":
                self.page.card_clicked = True
                self.page.dialog_open = True
            elif self.kind == "link":
                self.page.link_clicked = True
                self.page.context.next_page = self.page.target

    class Context(_ExpectPageContext):
        def __init__(self, page):
            super().__init__([page])

    class Target:
        def __init__(self, page):
            self.page = page
            self.url = tasks.EDGE_STREAK_LAUNCH_URL
            self.closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, ms):
            self.page._advance(ms)

    class Page:
        def __init__(self):
            self.url = "https://rewards.bing.com/dashboard"
            self.context = Context(self)
            self.dialog_open = False
            self.card_clicked = False
            self.link_clicked = False
            self.closed = False
            self.elapsed_ms = 0
            self.target = Target(self)

        def _advance(self, ms):
            self.elapsed_ms += ms
            if self.elapsed_ms >= 7000:
                self.target.url = "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"

        def locator(self, selector):
            if 'button:has-text("Microsoft Edge")' in selector:
                return Locator(self, "card")
            if "role='dialog'" in selector and "使用 Edge" in selector:
                return Locator(self, "link", visible=self.dialog_open and not self.link_clicked)
            return Locator(self, "other", visible=False)

        def wait_for_timeout(self, ms):
            self._advance(ms)

    page = Page()
    page.context.next_page = page.target

    original_pages = list(page.context.pages)
    assert tasks._open_edge_streak(page) is True
    assert page.card_clicked is True
    assert page.link_clicked is True
    assert "edge-browsing-streak" in page.target.url
    assert original_pages == page.context.pages[:1]
    assert all(not item.closed for item in original_pages)
    assert page.target.closed is False


def test_edge_streak_wait_skips_bad_and_closed_pages_and_finds_new_target():
    class BadPage:
        def is_closed(self):
            return False

        @property
        def url(self):
            raise RuntimeError("page gone")

    class ClosedPage:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=CLOSED"

        def is_closed(self):
            return True

    class Target:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
        closed = False

        def is_closed(self):
            return self.closed

    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False

        def __init__(self):
            self.bad = BadPage()
            self.closed_page = ClosedPage()
            self.target = Target()
            self.context = type(
                "Context", (), {"pages": [self, self.bad, self.closed_page, self.target]}
            )()

        def is_closed(self):
            return self.closed

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    result = tasks._wait_for_edge_streak_target(
        page,
        [page, page.bad, page.closed_page],
        tasks.EDGE_STREAK_REDIRECT_TIMEOUT_MS,
        existing_pages={page},
    )
    assert result is page.target


def test_edge_streak_wait_skips_is_closed_error_and_finds_later_redirect(monkeypatch):
    class BrokenPage:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=BROKEN"

        def is_closed(self):
            raise RuntimeError("cannot read page state")

    class DelayedTarget:
        def __init__(self):
            self.url = tasks.EDGE_STREAK_LAUNCH_URL
            self.closed = False

        def is_closed(self):
            return self.closed

    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False

        def __init__(self):
            self.broken = BrokenPage()
            self.target = DelayedTarget()
            self.context = type(
                "Context", (), {"pages": [self, self.broken, self.target]}
            )()

        def is_closed(self):
            return self.closed

        def wait_for_timeout(self, _ms):
            self.target.url = (
                "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
            )

    page = Page()
    monkeypatch.setattr(tasks, "EDGE_STREAK_POLL_INTERVAL_MS", 1)

    result = tasks._wait_for_edge_streak_target(
        page,
        [page, page.broken],
        tasks.EDGE_STREAK_REDIRECT_TIMEOUT_MS,
        existing_pages={page, page.broken},
    )
    assert result is page.target


def test_open_edge_streak_does_not_click_without_page_snapshot(monkeypatch):
    class Locator:
        def __init__(self, page, kind):
            self.page = page
            self.kind = kind
            self.first = self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def wait_for(self, **_):
            return None

        def click(self):
            if self.kind == "link":
                self.page.link_clicked = True
                self.page.context.pages.append(self.page.stale)

    class BrokenContext:
        @property
        def pages(self):
            raise RuntimeError("context pages unavailable")

    class StalePage:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=STALE"
        closed = False

        def is_closed(self):
            return self.closed

    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False

        def __init__(self):
            self.stale = StalePage()
            self.context = BrokenContext()
            self.link_clicked = False

        def is_closed(self):
            return self.closed

        def locator(self, selector):
            if "role='dialog'" in selector:
                return Locator(self, "link")
            return Locator(self, "card")

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    calls = []
    monkeypatch.setattr(tasks, "safe_goto", lambda *args, **kwargs: calls.append(args[1]))
    monkeypatch.setattr(tasks, "EDGE_STREAK_REDIRECT_TIMEOUT_MS", 50)

    assert tasks._open_edge_streak(page) is False
    assert page.link_clicked is False
    assert calls == [tasks.EDGE_STREAK_LAUNCH_URL]
    assert page.stale.closed is False


def test_open_edge_streak_ignores_stale_activity_page_when_fallback_does_not_navigate(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class StalePage:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=STALE"
        closed = False

        def is_closed(self):
            return self.closed

    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False

        def __init__(self):
            self.stale = StalePage()
            self.context = type("Context", (), {"pages": [self, self.stale]})()

        def is_closed(self):
            return self.closed

        def locator(self, _selector):
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    monkeypatch.setattr(tasks, "safe_goto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "EDGE_STREAK_REDIRECT_TIMEOUT_MS", 50)

    assert tasks._open_edge_streak(page) is False
    assert page.closed is False
    assert page.stale.closed is False


def test_open_edge_streak_falls_back_to_official_launch_url(monkeypatch):
    """卡片/弹窗不可用时必须使用官方稳定链接兜底，而不是直接报告入口消失。"""
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Context:
        pages = []

    class Page:
        url = "https://rewards.bing.com/dashboard"

        def __init__(self):
            self.context = Context()
            self.context.pages = [self]

        def locator(self, _selector):
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    calls = []

    def fake_goto(target, url, **_):
        calls.append(url)
        target.url = "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"

    monkeypatch.setattr(tasks, "safe_goto", fake_goto)

    assert tasks._open_edge_streak(page) is True
    assert calls == [tasks.EDGE_STREAK_LAUNCH_URL]


def test_edge_streak_activates_new_bing_page_and_closes_only_owned_pages(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Mouse:
        def __init__(self, owner):
            self.owner = owner

        def wheel(self, *_args):
            self.owner.wheel_count += 1

    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False
            self.wheel_count = 0
            self.context = None
            self.mouse = Mouse(self)

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def wait_for_timeout(self, _ms):
            return None

        def locator(self, _selector):
            return EmptyLocator()

    class ActivationLink:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.activity.link_clicked = True
            self.activity.context.pages.append(self.activity.activation)

    class ActivityPage(Page):
        def __init__(self, name, url):
            super().__init__(name, url)
            self.activation = None
            self.link_clicked = False

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return ActivationLink(self)
            return EmptyLocator()

    class Context:
        def __init__(self):
            self.pages = []

    context = Context()
    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = ActivityPage(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    activation = Page("activation", "https://cn.bing.com/?rwgbopen=1&FORM=rwbsl1")
    historical = Page("historical", "https://example.com/user-tab")
    dashboard.context = context
    activity.context = context
    activation.context = context
    historical.context = context
    activity.activation = activation
    context.pages = [historical, dashboard]

    progress = iter([(0, 30), (30, 30)])
    progress_calls = []
    browsing_calls = []

    def fake_read(target, label):
        assert label == "edge"
        assert target is dashboard
        progress_calls.append(target)
        return next(progress)

    prepare_calls = []

    def fake_prepare(_target):
        prepare_calls.append(_target)
        if len(prepare_calls) == 1:
            context.pages.append(activity)
            return tasks.EdgeStreakEntry(activity, (activity,), True)
        context.pages.append(activity)
        return tasks.EdgeStreakEntry(activity, (), False)

    def fake_goto(target, url, **_kwargs):
        if target is dashboard:
            assert url == tasks.REWARDS_DASHBOARD
        else:
            assert target is activation
            browsing_calls.append((target, url))

    monkeypatch.setattr(tasks, "read_streak_progress", fake_read)
    monkeypatch.setattr(tasks, "_prepare_edge_streak", fake_prepare)
    def fake_activation(*_args, **_kwargs):
        activity.link_clicked = True
        return tasks.EdgeStreakActivation(activation, (activation,), True)

    monkeypatch.setattr(tasks, "_activate_edge_streak", fake_activation)
    monkeypatch.setattr(tasks, "safe_goto", fake_goto)

    assert tasks.do_edge_browsing_streak(dashboard, minutes=6, poll_seconds=60) == 1
    assert activity.link_clicked is True
    assert activation.wheel_count > 0
    assert dashboard.wheel_count == 0
    assert browsing_calls
    assert all(target is activation for target, _url in browsing_calls)
    assert progress_calls == [dashboard, dashboard]
    assert prepare_calls == [dashboard]
    assert activity.closed is True
    assert activation.closed is True
    assert dashboard.closed is False
    assert historical.closed is False


@pytest.mark.parametrize(
    "url",
    [
        "https://cn.bing.com/?rwgbopen=1&FORM=rwbsl1",
        "https://www.bing.com/",
        "https://bing.com",
    ],
)
def test_is_edge_activation_url_accepts_bing_home(url):
    assert tasks._is_edge_activation_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://cn.bing.com/search?q=edge",
        "http://cn.bing.com/",
        "https://cn.bing.com:8443/",
        "https://evil.example/",
    ],
)
def test_is_edge_activation_url_rejects_untrusted_urls(url):
    assert tasks._is_edge_activation_url(url) is False


def test_activate_edge_streak_collects_page_created_after_click_timeout(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class ActivationLink:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.activity.context.next_page = self.activity.delayed

    class Context(_ExpectPageContext):
        pass

    class Page:
        def __init__(self, name, url, context=None):
            self.name = name
            self.url = url
            self.closed = False
            self.context = context or Context()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return ActivationLink(self)
            return EmptyLocator()

        def wait_for_timeout(self, ms):
            self.context.emit("page", self.delayed)

    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    delayed = Page("delayed", "about:blank", activity.context)
    delayed.wait_for_timeout = lambda _ms: None
    activity.delayed = delayed
    activity.context.pages = [activity]
    monkeypatch.setattr(tasks, "EDGE_STREAK_ACTIVATION_POLL_MS", 1)

    result = tasks._activate_edge_streak(
        activity, timeout_ms=1, activation_timeout_ms=1
    )

    assert result.activated is False
    assert result.browsing_page is None
    assert delayed in result.created_pages


def test_activate_edge_streak_collects_page_when_click_raises(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class ActivationLink:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.activity.context.next_page = self.activity.delayed
            raise RuntimeError("click interrupted after navigation")

    class Context(_ExpectPageContext):
        pass

    class Page:
        def __init__(self, name, url, context=None):
            self.name = name
            self.url = url
            self.closed = False
            self.context = context or Context()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return ActivationLink(self)
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    delayed = Page("delayed", "about:blank", activity.context)
    activity.delayed = delayed
    activity.context.pages = [activity]
    monkeypatch.setattr(tasks, "EDGE_STREAK_ACTIVATION_POLL_MS", 1)

    result = tasks._activate_edge_streak(
        activity, timeout_ms=1, activation_timeout_ms=1
    )

    assert result.activated is False
    assert result.browsing_page is None
    assert delayed in result.created_pages


@pytest.mark.parametrize("click_raises", [False, True])
def test_edge_streak_unexpected_new_page_is_cleaned_by_finally(monkeypatch, click_raises):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class ActivationLink:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.activity.context.next_page = self.activity.delayed
            if click_raises:
                raise RuntimeError("click interrupted")

    class Context(_ExpectPageContext):
        pass

    class Page:
        def __init__(self, name, url, context=None):
            self.name = name
            self.url = url
            self.closed = False
            self.context = context or Context()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return ActivationLink(self)
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            if self.context.next_page is None:
                self.context.next_page = self.delayed

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    delayed = Page("delayed", "about:blank", activity.context)
    delayed.wait_for_timeout = lambda _ms: None
    historical = Page("historical", "https://example.com/user-tab", activity.context)
    activity.delayed = delayed
    activity.context.pages = [historical, dashboard, activity]
    dashboard.context = activity.context
    historical.context = activity.context

    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (0, 30))
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda _page: tasks.EdgeStreakEntry(activity, (activity,), True),
    )
    monkeypatch.setattr(tasks, "EDGE_STREAK_ACTIVATION_TIMEOUT_MS", 1)
    monkeypatch.setattr(tasks, "EDGE_STREAK_ACTIVATION_POLL_MS", 1)
    goto_calls = []
    monkeypatch.setattr(
        tasks, "safe_goto", lambda target, url, **kwargs: goto_calls.append(target)
    )

    assert tasks.do_edge_browsing_streak(dashboard, minutes=1, poll_seconds=60) == 0
    assert goto_calls == []
    assert delayed.closed is True
    assert activity.closed is True
    assert dashboard.closed is False
    assert historical.closed is False


def test_edge_streak_activation_without_new_page_is_failure(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Link:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self):
            self.activity.link_clicked = True

    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False
            self.wheel_count = 0
            self.mouse = self

        def wheel(self, *_args):
            self.wheel_count += 1

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def wait_for_timeout(self, _ms):
            return None

    class Activity(Page):
        def __init__(self):
            super().__init__(
                "activity",
                "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL",
            )
            self.link_clicked = False

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return Link(self)
            return EmptyLocator()

    class Context:
        pages = []

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Activity()
    context = Context()
    dashboard.context = context
    activity.context = context
    context.pages = [dashboard, activity]

    activation = tasks._activate_edge_streak(
        activity, dashboard, timeout_ms=1, activation_timeout_ms=1
    )

    assert activity.link_clicked is True
    assert activation.activated is False
    assert activation.browsing_page is None
    assert activation.created_pages == ()


def test_edge_streak_waits_for_delayed_activation_link(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class DelayedLink:
        first = None

        def __init__(self, activity):
            self.first = self
            self.activity = activity

        def count(self):
            return 1 if self.activity.rendered else 0

        def is_visible(self):
            return self.activity.rendered

        def click(self):
            self.activity.link_clicked = True

    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.url = url
            self.closed = False
            self.context = None

        def is_closed(self):
            return self.closed

        def locator(self, _selector):
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    class Activity(Page):
        def __init__(self):
            super().__init__(
                "activity",
                "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL",
            )
            self.rendered = False
            self.link_clicked = False
            self.polls = 0

        def locator(self, selector):
            if "开始在 Edge 上浏览" in selector:
                return DelayedLink(self)
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            self.polls += 1
            if self.polls >= 2:
                self.rendered = True

    class Context:
        pages = []

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Activity()
    context = Context()
    dashboard.context = context
    activity.context = context
    context.pages = [dashboard, activity]
    monkeypatch.setattr(tasks, "EDGE_STREAK_ACTIVATION_POLL_MS", 1)

    activation = tasks._activate_edge_streak(
        activity,
        dashboard,
        timeout_ms=1,
        activation_timeout_ms=20,
    )

    assert activity.link_clicked is True
    assert activation.activated is False
    assert activation.browsing_page is None
    assert activation.created_pages == ()


def test_edge_streak_activation_failure_does_not_navigate_dashboard(monkeypatch):
    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False
            self.context = type("Context", (), {"pages": []})()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    historical = Page("historical", "https://example.com/user-tab")
    dashboard.context.pages = [historical, dashboard, activity]

    def fail_activation(*_args, **_kwargs):
        new_page = Page("new", tasks.EDGE_STREAK_LAUNCH_URL)
        dashboard.context.pages.append(new_page)
        return tasks.EdgeStreakActivation(None, (new_page,), False)

    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (0, 30))
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda _page: tasks.EdgeStreakEntry(activity, (activity,), True),
    )
    monkeypatch.setattr(tasks, "_activate_edge_streak", fail_activation)
    goto_calls = []
    monkeypatch.setattr(
        tasks, "safe_goto", lambda target, url, **kwargs: goto_calls.append(target)
    )

    assert tasks.do_edge_browsing_streak(dashboard, minutes=1, poll_seconds=60) == 0
    assert goto_calls == []
    assert dashboard.context.pages[-1].closed is True
    assert activity.closed is True
    assert dashboard.closed is False
    assert historical.closed is False


def test_prepare_edge_streak_returns_opened_page_when_redirect_times_out(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Link:
        first = None

        def __init__(self, page):
            self.first = self
            self.page = page

        def count(self):
            return 1

        def is_visible(self):
            return True

        def wait_for(self, **_kwargs):
            return None

        def click(self):
            self.page.context.next_page = self.page.opened

    class Context(_ExpectPageContext):
        pass

    class Page:
        url = "https://rewards.bing.com/dashboard"

        def __init__(self, name, url=None, context=None):
            self.name = name
            if url is not None:
                self.url = url
            self.opened = None
            self.closed = False
            self.context = context or Context()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, selector):
            if "Microsoft Edge" in selector:
                return EmptyLocator()
            if "role='dialog'" in selector:
                return Link(self)
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    page = Page("dashboard")
    page.context.pages = [page]
    page.opened = Page(
        "opened",
        "https://www.microsoft.com/edge/launch/rewards-browsing-streak1",
        page.context,
    )
    monkeypatch.setattr(tasks, "EDGE_STREAK_REDIRECT_TIMEOUT_MS", 1)

    entry = tasks._prepare_edge_streak(page)

    assert entry.activity_page is None
    assert entry.created_pages == (page.opened,)
    assert entry.can_cleanup is True


def test_edge_streak_new_unconfirmed_page_is_cleaned_by_owner():
    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    new_page = Page("new", "https://www.microsoft.com/edge/launch/rewards-browsing-streak1")

    entry = tasks.EdgeStreakEntry(None, (new_page,), True)

    assert entry.activity_page is None
    assert entry.created_pages == (new_page,)
    assert entry.can_cleanup is True
    tasks._close_edge_streak_pages(object(), entry.created_pages)
    assert new_page.closed is True


def test_edge_streak_reuses_browsing_page_for_progress_15_to_30(monkeypatch):
    class Mouse:
        def __init__(self, page):
            self.page = page

        def wheel(self, *_args):
            self.page.wheel_count += 1

    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False
            self.wheel_count = 0
            self.mouse = Mouse(self)
            self.context = type("Context", (), {"pages": []})()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def wait_for_timeout(self, _ms):
            return None

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    activation = Page("activation", "https://cn.bing.com/?rwgbopen=1&FORM=rwbsl1")
    historical = Page("historical", "https://example.com/user-tab")
    dashboard.context.pages = [historical, dashboard]
    activity.context = dashboard.context
    activation.context = dashboard.context

    progress_values = iter([(15, 30), (20, 30), (25, 30), (30, 30)])
    progress_targets = []
    browsing_targets = []
    prepare_calls = []
    activation_calls = []

    def fake_read(target, label):
        assert label == "edge"
        progress_targets.append(target)
        return next(progress_values)

    def fake_prepare(target):
        prepare_calls.append(target)
        return tasks.EdgeStreakEntry(activity, (activity,), True)

    def fake_activate(*_args, **_kwargs):
        activation_calls.append(True)
        return tasks.EdgeStreakActivation(activation, (activation,), True)

    def fake_goto(target, _url, **_kwargs):
        if target is dashboard:
            return None
        assert target is activation
        browsing_targets.append(target)

    monkeypatch.setattr(tasks, "read_streak_progress", fake_read)
    monkeypatch.setattr(tasks, "_prepare_edge_streak", fake_prepare)
    monkeypatch.setattr(tasks, "_activate_edge_streak", fake_activate)
    monkeypatch.setattr(tasks, "safe_goto", fake_goto)

    assert tasks.do_edge_browsing_streak(dashboard, minutes=20, poll_seconds=60) == 1
    assert prepare_calls == [dashboard]
    assert activation_calls == [True]
    assert progress_targets == [dashboard] * 4
    assert len(browsing_targets) == 15
    assert all(target is activation for target in browsing_targets)
    assert activation.wheel_count > 0
    assert dashboard.wheel_count == 0
    assert activity.closed is True
    assert activation.closed is True
    assert dashboard.closed is False
    assert historical.closed is False


def test_edge_streak_not_logged_in_does_not_open_or_wait(monkeypatch):
    actions = []
    page = type("Page", (), {"url": "https://rewards.bing.com/dashboard"})()

    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (0, 30))
    monkeypatch.setattr(tasks, "edge_browser_profile_logged_in", lambda *_args: False)
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda *_args: actions.append("open") or tasks.EdgeStreakEntry(page, (), False),
    )
    monkeypatch.setattr(
        tasks,
        "_activate_edge_streak",
        lambda *_args, **_kwargs: actions.append("activate"),
    )
    monkeypatch.setattr(
        tasks, "safe_goto", lambda *_args, **_kwargs: actions.append("navigate")
    )

    assert tasks.do_edge_browsing_streak(page, minutes=30, poll_seconds=60) == 0
    assert actions == []


def test_edge_streak_waits_full_30_minutes_when_logged_in_but_zero(
    monkeypatch, capsys
):
    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False
        mouse = type("Mouse", (), {"wheel": lambda *_args: None})()

        def is_closed(self):
            return self.closed

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    progress_calls = []
    elapsed_marks = []

    def fake_read(*_args):
        progress_calls.append(len(progress_calls) + 1)
        return (0, 30)

    def fake_goto(_target, url, **_kwargs):
        if url == tasks.REWARDS_DASHBOARD:
            elapsed_marks.append(len(progress_calls))

    monkeypatch.setattr(tasks, "read_streak_progress", fake_read)
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda target: tasks.EdgeStreakEntry(target, (), False),
    )
    monkeypatch.setattr(
        tasks,
        "_activate_edge_streak",
        lambda *_args, **_kwargs: tasks.EdgeStreakActivation(page, (), True),
    )
    monkeypatch.setattr(tasks, "safe_goto", fake_goto)

    assert tasks.do_edge_browsing_streak(page, minutes=30, poll_seconds=60) == 0
    output = capsys.readouterr().out
    assert len(progress_calls) == 7
    assert "已等待 30 分钟" in output
    assert "未回写打卡进度" in output


def test_edge_streak_zero_poll_seconds_does_not_loop(monkeypatch):
    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False
        mouse = type("Mouse", (), {"wheel": lambda *_args: None})()

        def is_closed(self):
            return self.closed

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    waits = []

    def fake_wait(ms):
        waits.append(ms)
        assert len(waits) < 1000

    page.wait_for_timeout = fake_wait
    progress_values = iter([(0, 30)] + [(0, 30)] * 100)
    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: next(progress_values))
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda target: tasks.EdgeStreakEntry(target, (), False),
    )
    activation = tasks.EdgeStreakActivation(page, (), True)
    monkeypatch.setattr(
        tasks,
        "_activate_edge_streak",
        lambda *_args, **_kwargs: activation,
    )
    monkeypatch.setattr(tasks, "safe_goto", lambda *_args, **_kwargs: None)

    assert tasks.do_edge_browsing_streak(page, minutes=1, poll_seconds=0) == 0
    assert waits


def test_edge_streak_cleanup_continues_when_one_close_raises():
    class Page:
        def __init__(self, name, fail=False):
            self.name = name
            self.closed = False
            self.fail = fail

        def is_closed(self):
            return self.closed

        def close(self):
            if self.fail:
                raise RuntimeError("close failed")
            self.closed = True

    current = Page("current")
    broken = Page("broken", fail=True)
    owned = Page("owned")

    tasks._close_edge_streak_pages(current, (broken, owned))

    assert current.closed is False
    assert broken.closed is False
    assert owned.closed is True


def test_edge_streak_cleanup_runs_on_failure(monkeypatch):
    class Page:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.closed = False
            self.context = type("Context", (), {"pages": []})()

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    dashboard = Page("dashboard", "https://rewards.bing.com/dashboard")
    activity = Page(
        "activity", "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
    )
    historical = Page("historical", "https://example.com/user-tab")
    dashboard.context.pages = [historical, dashboard]
    activity.context = dashboard.context

    monkeypatch.setattr(tasks, "read_streak_progress", lambda *_args: (0, 30))
    monkeypatch.setattr(
        tasks,
        "_prepare_edge_streak",
        lambda _page: tasks.EdgeStreakEntry(activity, (activity,), True),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("activation failed")

    monkeypatch.setattr(tasks, "_activate_edge_streak", fail)

    assert tasks.do_edge_browsing_streak(dashboard, minutes=1) == 0
    assert activity.closed is True
    assert dashboard.closed is False
    assert historical.closed is False


def test_edge_streak_current_activity_page_is_not_owned_or_closed(monkeypatch):
    class Page:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=ML2WHL"
        closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

        def locator(self, _selector):
            raise AssertionError("已确认活动页时不应再次点击入口")

    page = Page()

    entry = tasks._prepare_edge_streak(page)

    assert entry.activity_page is page
    assert entry.created_pages == ()
    assert entry.can_cleanup is False
    assert page.closed is False


def test_edge_streak_cleanup_preserves_current_page_and_history():
    class Page:
        def __init__(self, name):
            self.name = name
            self.closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    current = Page("current")
    historical = Page("historical")
    owned = Page("owned")

    tasks._close_edge_streak_pages(
        current,
        (current, historical, owned),
        protected_pages={current, historical},
    )

    assert current.closed is False
    assert historical.closed is False
    assert owned.closed is True


def test_prepare_edge_streak_ignores_historical_activity_page(monkeypatch):
    class EmptyLocator:
        first = None

        def __init__(self):
            self.first = self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class StalePage:
        url = "https://rewards.bing.com/promo/edge-browsing-streak?form=STALE"
        closed = False

        def is_closed(self):
            return self.closed

        def close(self):
            self.closed = True

    class Page:
        url = "https://rewards.bing.com/dashboard"
        closed = False

        def __init__(self):
            self.stale = StalePage()
            self.context = type("Context", (), {"pages": [self, self.stale]})()

        def is_closed(self):
            return self.closed

        def locator(self, _selector):
            return EmptyLocator()

        def wait_for_timeout(self, _ms):
            return None

    page = Page()
    monkeypatch.setattr(tasks, "safe_goto", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks, "EDGE_STREAK_REDIRECT_TIMEOUT_MS", 1)

    entry = tasks._prepare_edge_streak(page)

    assert entry.activity_page is None
    assert entry.created_pages == ()
    assert page.stale.closed is False


def test_edge_streak_returns_success_only_at_30_30(monkeypatch):
    values = iter([(0, 30)] + [(5, 30)] * 20)
    class Page:
        url = "https://rewards.bing.com/dashboard"
        def wait_for_timeout(self, *_):
            return None
        def mouse(self):
            return None
    monkeypatch.setattr(tasks, "read_streak_progress", lambda page, label: next(values))
    monkeypatch.setattr(
        tasks, "_prepare_edge_streak",
        lambda target: tasks.EdgeStreakEntry(target, (), False),
    )
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
    monkeypatch.setattr(
        tasks, "_prepare_edge_streak",
        lambda target: tasks.EdgeStreakEntry(target, (), False),
    )
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
    monkeypatch.setattr(main_module, "do_edge_browsing_streak_detail", lambda *a, **k: calls.append(("edge", k.get("expected_email"))) or EdgeStreakResult(EdgeStreakStatus.COMPLETED, progress=(30, 30)))
    monkeypatch.setattr(main_module, "mobile_checkin", lambda *a, **k: calls.append("mobile") or 1)
    monkeypatch.setattr(main_module, "run_rewards_desktop_task", lambda *a, **k: calls.append(("webview", a[0] if a else k.get("expected_email"))) or 0)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    # Use a minimal page factory; all real interactions are stubbed.
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    main_module.run_account(None, ctx, "u", "p", True)
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]


def _run_account_with_mobile_result(monkeypatch, mobile_result, devices, webview_result=0):
    import main as main_module
    calls = []
    claim_calls = []

    class Page:
        url = "https://rewards.bing.com/dashboard"

        def goto(self, *_a, **_k):
            pass

        def wait_for_timeout(self, *_):
            pass

        def close(self):
            pass

    monkeypatch.setattr(main_module, "do_login", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "ensure_account", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "safe_goto", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "detect_risk_control", lambda _p: None)
    monkeypatch.setattr(main_module, "parse_dashboard_data", lambda _p: {
        "claimable_points": 0, "daily_set_items": [],
        "daily_activities_done": 3, "daily_activities_total": 3,
        "pc_search_done": 0, "pc_search_total": 0,
    })
    monkeypatch.setattr(main_module, "claim_available_points", lambda *a, **k: claim_calls.append("claim") or 0)
    monkeypatch.setattr(main_module, "do_daily_set_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_dashboard_activities", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_daily_offer_tasks", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_point_punch_cards", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "do_edge_browsing_streak_detail", lambda *a, **k: calls.append(("edge", k.get("expected_email"))) or EdgeStreakResult(EdgeStreakStatus.COMPLETED, progress=(30, 30)))
    monkeypatch.setattr(main_module, "mobile_checkin", lambda *a, **k: calls.append("mobile") or mobile_result)
    monkeypatch.setattr(main_module, "list_android_devices", lambda: devices)
    monkeypatch.setattr(main_module, "ENABLE_MOBILE_CLIENT", True)
    monkeypatch.setattr(main_module, "run_rewards_desktop_task", lambda *a, **k: calls.append(("webview", a[0] if a else k.get("expected_email"))) or webview_result)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    return main_module.run_account(None, ctx, "u", "p", True), calls, claim_calls


def test_run_account_skips_mobile_without_android_device(monkeypatch, capsys):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch, mobile_result=0, devices=[]
    )

    assert result is True
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 4
    assert "未连接 Android 设备，跳过必应应用签到" in capsys.readouterr().out


def test_run_account_does_not_report_mobile_success_when_device_present(monkeypatch, capsys):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch, mobile_result=0, devices=["emulator-5554"]
    )

    assert result is False
    assert calls == [("edge", "u"), "mobile"]
    assert len(claim_calls) == 3
    assert "必应应用签到未确认" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (clients.REWARDS_DESKTOP_IDENTITY_UNVERIFIED, "WebView 无法确认桌面应用账号身份"),
        (clients.REWARDS_DESKTOP_IDENTITY_MISMATCH, "WebView 桌面应用账号与目标账号不一致"),
        (clients.REWARDS_DESKTOP_TASK_ITEM_MISSING, "WebView 已进入任务页但未找到可点击任务项"),
    ],
)
def test_run_account_fails_on_unsafe_webview_states(monkeypatch, capsys, status, message):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch,
        mobile_result=1,
        devices=[],
        webview_result=status,
    )

    assert result is False
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 3
    assert message in capsys.readouterr().out


def test_run_account_skips_webview_when_uia_environment_unavailable(monkeypatch, capsys):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch,
        mobile_result=1,
        devices=[],
        webview_result=clients.REWARDS_DESKTOP_UIA_UNAVAILABLE,
    )

    assert result is True
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 4
    assert "WebView UIA 不可用，跳过（不影响其他任务）" in capsys.readouterr().out


def test_run_account_fails_on_actual_webview_task_error(monkeypatch, capsys):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch,
        mobile_result=1,
        devices=[],
        webview_result=clients.REWARDS_DESKTOP_ERROR,
    )

    assert result is False
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 3
    assert "Rewards 桌面 WebView 任务执行失败，账号不报告成功" in capsys.readouterr().out


def test_run_account_succeeds_when_webview_has_no_task(monkeypatch):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch,
        mobile_result=1,
        devices=[],
        webview_result=clients.REWARDS_DESKTOP_NO_TASK,
    )

    assert result is True
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 4


def test_run_account_succeeds_when_webview_task_grows(monkeypatch):
    result, calls, claim_calls = _run_account_with_mobile_result(
        monkeypatch,
        mobile_result=1,
        devices=[],
        webview_result=clients.REWARDS_DESKTOP_TASK_GROWN,
    )

    assert result is True
    assert calls == [("edge", "u"), "mobile", ("webview", "u")]
    assert len(claim_calls) == 4


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
    monkeypatch.setattr(main_module, "do_edge_browsing_streak_detail", lambda *a, **k: calls.append("edge") or EdgeStreakResult(EdgeStreakStatus.COMPLETED, progress=(30, 30)))
    monkeypatch.setattr(main_module, "mobile_checkin", lambda *a, **k: calls.append("mobile") or 1)
    monkeypatch.setattr(main_module, "run_rewards_desktop_task", lambda *a, **k: 0)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.USER_DATA_DIR)
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()
    main_module.run_account(None, ctx, "u", "p", True)
    assert calls == ["mobile"]


@pytest.mark.parametrize(
    ("edge_status", "message"),
    [
        (EdgeStreakStatus.IDENTITY_UNVERIFIED, "Edge 浏览器 profile 无法确认目标账号"),
        (EdgeStreakStatus.IDENTITY_MISMATCH, "Edge 浏览器 profile 账号与目标账号不一致"),
        (EdgeStreakStatus.INVALID_PROGRESS, "Edge 浏览进度异常"),
        (EdgeStreakStatus.ENTRY_FAILED, "Edge 浏览任务失败"),
    ],
)
def test_run_account_edge_identity_failure_cannot_be_overridden_by_30_30(
    monkeypatch, capsys, edge_status, message
):
    import main as main_module

    class Page:
        url = "https://rewards.bing.com/dashboard"

        def goto(self, *_a, **_k):
            pass

        def wait_for_timeout(self, *_):
            pass

        def close(self):
            pass

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
    monkeypatch.setattr(
        main_module,
        "do_edge_browsing_streak_detail",
        lambda *a, **k: EdgeStreakResult(edge_status, progress=(30, 30)),
    )
    monkeypatch.setattr(main_module, "read_streak_progress", lambda *a, **k: (30, 30))
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()

    assert main_module.run_account(None, ctx, "u", "p", True) is False
    assert message in capsys.readouterr().out


@pytest.mark.parametrize("invalid_progress", [(31, 30), (-1, 30), (0, 0), (1, 0)])
def test_run_account_rejects_invalid_edge_recheck_progress(monkeypatch, invalid_progress):
    import main as main_module

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
    monkeypatch.setattr(
        main_module,
        "do_edge_browsing_streak_detail",
        lambda *a, **k: EdgeStreakResult(EdgeStreakStatus.INCOMPLETE, progress=(10, 30)),
    )
    monkeypatch.setattr(main_module, "read_streak_progress", lambda *a, **k: invalid_progress)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.PROJECT_ROOT / "real-edge-profile")
    monkeypatch.setattr(main_module, "DEFAULT_PC_SEARCHES", 0)
    monkeypatch.setattr(main_module, "DEFAULT_MOBILE_SEARCHES", 0)
    ctx = type("C", (), {"new_page": lambda self: Page()})()

    assert main_module.run_account(None, ctx, "u", "p", True) is False


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
    monkeypatch.setattr(main_module, "do_edge_browsing_streak_detail", lambda *a, **k: EdgeStreakResult(EdgeStreakStatus.INCOMPLETE, progress=(10, 30)))
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
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="state", expected_email=None: {
        "available": False, "task_progress": None, "success": False, "output": "",
    })
    result = clients.rewards_desktop_state()
    assert result["available"] is False
    assert result["task_progress"] is None


def test_rewards_desktop_state_parses_progress_and_uia_ready(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\nDOC=桌面奖励应用每周专属任务 – Microsoft Rewards\nTASK_PROGRESS=3/8\n",
    )
    result = clients.rewards_desktop_state()
    assert result["available"] is True
    assert result["task_progress"] == "3/8"
    assert result["uia_ready"] is True


def _fake_rewards_uia_output(
    monkeypatch,
    output,
    *,
    returncode=0,
    run_id_override=None,
    write_result=True,
    captured=None,
    side_effect=None,
):
    monkeypatch.setattr(clients, "_rewards_desktop_hwnd", lambda: 123)

    def fake_run(args, **kwargs):
        if captured is not None:
            captured.append(list(args))
        if side_effect is not None:
            raise side_effect
        result_path = Path(args[args.index("-ResultPath") + 1])
        run_id = args[args.index("-RunId") + 1] if "-RunId" in args else ""
        content = f"RUN_ID={run_id_override or run_id}\n{output}"
        if write_result:
            result_path.write_text(content, encoding="utf-8")

        class Result:
            pass

        result = Result()
        result.returncode = returncode
        result.stdout = output
        return result

    monkeypatch.setattr(clients.subprocess, "run", fake_run)


def test_rewards_desktop_home_without_task_is_safe_noop(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=首页 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=no_task\n"
        "SUCCESS=0\n",
    )
    result = clients.rewards_desktop_state()
    assert result["available"] is True
    assert result["uia_ready"] is True
    assert result["task_state"] == "no_task"
    assert result["error"] is False

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == 0


def test_rewards_desktop_home_text_does_not_count_as_task_success(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=首页 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=no_task\n"
        "Microsoft Edge 分钟: 30/30\n"
        "SUCCESS=0\n",
    )
    result = clients.rewards_desktop_state()
    assert result["task_progress"] is None
    assert result["success"] is False
    assert result["error"] is False

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == 0


def test_rewards_desktop_missing_root_web_area_is_uia_unavailable(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=\n"
        "UIA_READY=0\n"
        "TASK_STATE=uia_unavailable\n"
        "ERROR=RootWebArea not found\n",
    )
    result = clients.rewards_desktop_state()
    assert result["available"] is True
    assert result["uia_ready"] is False
    assert result["task_state"] == "uia_unavailable"
    assert result["error"] is True

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_UIA_UNAVAILABLE


def test_rewards_desktop_run_returns_success_after_task_progress_growth(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_PROGRESS=1/3\n"
        "TASK_AFTER=2/3\n"
        "SUCCESS=1\n",
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == 1


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


def test_rewards_desktop_parses_mouse_fallback_and_progress_growth(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_ELEMENT_TYPE=ControlType.Hyperlink\n"
        "TASK_RECT=100,200,300,40\n"
        "TASK_INVOKE_MODE=mouse\n"
        "INVOKE_FALLBACK=mouse\n"
        "TASK_BEFORE=2/8\n"
        "TASK_AFTER=3/8\n"
        "SUCCESS=1\n",
    )
    result = clients.rewards_desktop_state()
    assert result["task_before"] == "2/8"
    assert result["task_after"] == "3/8"
    assert result["task_progress"] == "3/8"
    assert result["task_element_type"] == "ControlType.Hyperlink"
    assert result["task_rect"] == "100,200,300,40"
    assert result["task_invoke_mode"] == "mouse"
    assert result["invoke_fallback"] == "mouse"
    assert result["success"] is True

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_TASK_GROWN


def test_rewards_desktop_mouse_fallback_failure_remains_error(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_ELEMENT_TYPE=ControlType.Hyperlink\n"
        "TASK_RECT=100,200,300,40\n"
        "TASK_INVOKE_MODE=none\n"
        "INVOKE_FALLBACK=none\n"
        "TASK_BEFORE=2/8\n"
        "TASK_AFTER=2/8\n"
        "TASK_ERROR=weekly_item_not_invokable\n"
        "SUCCESS=0\n",
    )
    result = clients.rewards_desktop_state()
    assert result["task_state"] == "error"
    assert result["error"] is True
    assert result["success"] is False

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_rejects_non_hyperlink_task_item(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_ELEMENT_TYPE=ControlType.Text\n"
        "TASK_RECT=100,200,300,40\n"
        "TASK_BEFORE=2/8\n"
        "TASK_AFTER=3/8\n"
        "SUCCESS=1\n",
    )
    result = clients.rewards_desktop_state()
    assert result["task_state"] == "error"
    assert result["error"] is True
    assert result["success"] is False

    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_run_reports_uia_environment_unavailable(monkeypatch):
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="run": {
        "available": True,
        "task_progress": None,
        "success": False,
        "uia_ready": False,
        "uia_unavailable": True,
        "output": "AVAILABLE=1\nUIA_READY=0\n",
    })
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_UIA_UNAVAILABLE


def test_rewards_desktop_timeout_is_task_error_not_uia_unavailable(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "",
        write_result=False,
        side_effect=subprocess.TimeoutExpired(cmd="powershell.exe", timeout=90),
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    result = clients.rewards_desktop_state()
    assert result["protocol_error"] is True
    assert result["uia_unavailable"] is False
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_oserror_is_task_error_not_uia_unavailable(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "",
        write_result=False,
        side_effect=OSError("cannot launch powershell"),
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    result = clients.rewards_desktop_state()
    assert result["protocol_error"] is True
    assert result["uia_unavailable"] is False
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


@pytest.mark.parametrize(
    "output",
    [
        "AVAILABLE=0\nERROR=NO_PROCESS\n",
        "AVAILABLE=1\nUIA_READY=0\nTASK_STATE=uia_unavailable\nERROR=RootWebArea not found\n",
    ],
)
def test_rewards_desktop_explicit_uia_unavailable_still_skips(monkeypatch, output):
    _fake_rewards_uia_output(monkeypatch, output)
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    result = clients.rewards_desktop_state()
    assert result["uia_unavailable"] is True
    assert result["protocol_error"] is False
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_UIA_UNAVAILABLE


def test_rewards_desktop_run_reports_actual_task_error(monkeypatch):
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(clients, "_rewards_uia", lambda mode="run": {
        "available": True,
        "task_progress": None,
        "success": False,
        "uia_ready": True,
        "task_state": "task",
        "error": True,
        "output": "TASK_ERROR=click failed",
    })
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


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


def test_rewards_desktop_identity_mismatch_blocks_task_click(monkeypatch):
    calls = []
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=首页 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "DESKTOP_IDENTITY=other@example.invalid\n"
        "IDENTITY_STATE=identity_mismatch\n"
        "SUCCESS=0\n",
        captured=calls,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task("target@example.invalid") == (
        clients.REWARDS_DESKTOP_IDENTITY_MISMATCH
    )
    assert "-ExpectedEmail" in calls[0]
    assert "target@example.invalid" in calls[0]


def test_rewards_desktop_identity_unverified_blocks_task_click(monkeypatch):
    captured = []
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=首页 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "DESKTOP_IDENTITY=target@example.invalid\n"
        "IDENTITY_STATE=identity_unverified\n"
        "SUCCESS=1\n",
        captured=captured,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    state = clients.rewards_desktop_state("target@example.invalid")
    assert state["success"] is False
    assert clients.run_rewards_desktop_task("target@example.invalid") == (
        clients.REWARDS_DESKTOP_IDENTITY_UNVERIFIED
    )
    assert "-ExpectedEmail" in captured[0]


def test_rewards_desktop_verified_identity_continues_task_flow(monkeypatch):
    captured = []
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "DESKTOP_IDENTITY=Target@Example.invalid\n"
        "IDENTITY_STATE=verified\n"
        "TASK_STATE=task\n"
        "TASK_BEFORE=1/3\n"
        "TASK_AFTER=2/3\n"
        "SUCCESS=1\n",
        captured=captured,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task("target@example.invalid") == (
        clients.REWARDS_DESKTOP_TASK_GROWN
    )
    assert "target@example.invalid" in captured[0]


def test_rewards_desktop_rejects_run_id_mismatch(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_BEFORE=1/3\n"
        "TASK_AFTER=2/3\n"
        "SUCCESS=1\n",
        run_id_override="stale-run-id",
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_rejects_nonzero_powershell_exit(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_AFTER=2/3\n"
        "SUCCESS=1\n",
        returncode=1,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_does_not_read_legacy_fixed_result_file(
    monkeypatch, tmp_path
):
    legacy_result = tmp_path / "tmp_rewards_desktop_result.txt"
    legacy_result.write_text(
        "AVAILABLE=1\nTASK_STATE=task\nTASK_AFTER=2/3\nSUCCESS=1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(clients, "REWARDS_DESKTOP_UIA_RESULT", legacy_result)
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\nUIA_READY=1\nTASK_STATE=error\nERROR=boom\n",
        write_result=False,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_task_invoke_failed_is_error(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK_ELEMENT_TYPE=ControlType.Hyperlink\n"
        "TASK_ERROR=weekly_item_not_invokable\n"
        "SUCCESS=0\n",
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    result = clients.rewards_desktop_state()
    assert result["task_state"] == "error"
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_ERROR


def test_rewards_desktop_identity_comparison_is_case_insensitive(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "DESKTOP_IDENTITY=Target@Example.invalid\n"
        "IDENTITY_STATE=verified\n"
        "TASK_STATE=no_task\n"
        "SUCCESS=0\n",
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task("target@example.invalid") == (
        clients.REWARDS_DESKTOP_NO_TASK
    )


def test_rewards_desktop_task_page_without_task_item_is_failure(monkeypatch):
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\n"
        "DOC=桌面奖励应用每周专属任务 – Microsoft Rewards\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK=0\n"
        "SUCCESS=0\n",
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    result = clients.rewards_desktop_state()
    assert result["task_state"] == "task_item_missing"
    assert result["error"] is True
    assert clients.run_rewards_desktop_task() == (
        clients.REWARDS_DESKTOP_TASK_ITEM_MISSING
    )


def test_rewards_desktop_powershell_state_smoke_writes_run_id(tmp_path):
    """真实执行脚本初始化路径；不依赖 Rewards 窗口，不点击任务。"""
    script = Path(__file__).resolve().parents[1] / "utils" / "rewards_desktop_uia.ps1"
    result_path = tmp_path / "smoke_result.txt"
    run_id = "smoke-run-id"
    proc = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Mode", "state", "-Hwnd", "0", "-RunId", run_id,
            "-ResultPath", str(result_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert result_path.is_file()
    output = result_path.read_text(encoding="utf-8-sig")
    assert f"RUN_ID={run_id}" in output
    assert "SHELL" not in output


def test_rewards_desktop_each_call_uses_and_removes_unique_result_file(monkeypatch):
    captured = []
    _fake_rewards_uia_output(
        monkeypatch,
        "AVAILABLE=1\nUIA_READY=1\nTASK_STATE=no_task\nSUCCESS=0\n",
        captured=captured,
    )
    monkeypatch.setattr(clients, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(clients, "rewards_desktop_available", lambda: True)

    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_NO_TASK
    assert clients.run_rewards_desktop_task() == clients.REWARDS_DESKTOP_NO_TASK
    first = Path(captured[0][captured[0].index("-ResultPath") + 1])
    second = Path(captured[1][captured[1].index("-ResultPath") + 1])
    assert first != second
    assert not first.exists()
    assert not second.exists()


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
