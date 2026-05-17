import importlib
from pathlib import Path

import pytest


main = importlib.import_module("main")


def test_resolve_driver_path_env(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    fake_driver.write_text("driver")
    monkeypatch.setenv("EDGEWEBDRIVER", str(fake_driver))
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)
    assert main._resolve_driver_path() == str(fake_driver)


def test_resolve_driver_path_default(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    fake_driver.write_text("driver")
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", fake_driver)
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)
    assert main._resolve_driver_path() == str(fake_driver)


def test_resolve_driver_path_auto_download(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", fake_driver)
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)

    called = {}

    def fake_download(target):
        called["path"] = target
        target.write_text("driver")

    monkeypatch.setattr(main, "_download_driver", fake_download)
    assert main._resolve_driver_path() == str(fake_driver)
    assert called["path"] == fake_driver


def test_resolve_driver_path_path_fallback(monkeypatch):
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", Path("/tmp/not_exists.exe"))

    def fake_download(target):
        raise RuntimeError("no internet")

    monkeypatch.setattr(main, "_download_driver", fake_download)
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: "C:/driver/msedgedriver.exe")

    assert main._resolve_driver_path() == "C:/driver/msedgedriver.exe"


def test_resolve_driver_path_env_missing(tmp_path, monkeypatch):
    fake_driver = tmp_path / "not_exists.exe"
    monkeypatch.setenv("EDGEWEBDRIVER", str(fake_driver))
    with pytest.raises(FileNotFoundError):
        main._resolve_driver_path()


def test_resolve_driver_path_none(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", fake_driver)

    def fake_download(target):
        raise RuntimeError("network down")

    monkeypatch.setattr(main, "_download_driver", fake_download)
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)

    assert main._resolve_driver_path() is None


def test_is_driver_compatible_mismatch(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    fake_driver.write_text("driver")
    monkeypatch.setattr(main, "_get_driver_version", lambda _: "143.0.0.0")
    assert main._is_driver_compatible(fake_driver, "145") is False


def test_resolve_driver_path_mismatch_then_download(tmp_path, monkeypatch):
    fake_driver = tmp_path / "msedgedriver.exe"
    fake_driver.write_text("old")
    monkeypatch.delenv("EDGEWEBDRIVER", raising=False)
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", fake_driver)
    monkeypatch.setattr(main, "_get_edge_version", lambda: "145.0.3800.70")
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)

    called = {}

    def fake_download(target):
        called["path"] = target
        target.write_text("new")

    def fake_compatible(path, _edge_major):
        return path.read_text() == "new"

    monkeypatch.setattr(main, "_download_driver", fake_download)
    monkeypatch.setattr(main, "_is_driver_compatible", fake_compatible)

    assert main._resolve_driver_path() == str(fake_driver)
    assert called["path"] == fake_driver


def test_resolve_driver_path_ignore_mismatch_env(tmp_path, monkeypatch):
    env_driver = tmp_path / "env_driver.exe"
    env_driver.write_text("old")
    default_driver = tmp_path / "msedgedriver.exe"
    default_driver.write_text("new")
    monkeypatch.setenv("EDGEWEBDRIVER", str(env_driver))
    monkeypatch.setattr(main, "DEFAULT_DRIVER_PATH", default_driver)
    monkeypatch.setattr(main, "_get_edge_version", lambda: "145.0.3800.70")
    monkeypatch.setattr(main, "_download_driver", lambda _: (_ for _ in ()).throw(RuntimeError("should not download")))
    monkeypatch.setattr(main, "_find_driver_on_path", lambda: None)
    monkeypatch.setattr(main, "_is_driver_compatible", lambda path, _edge_major: path == default_driver)

    assert main._resolve_driver_path() == str(default_driver)


def test_ensure_keywords_fallback(monkeypatch):
    result = main._ensure_keywords([], [])
    assert result == main.DEFAULT_KEYWORDS
    assert result is not main.DEFAULT_KEYWORDS


def test_ensure_keywords_pick_first():
    words = ["a", "b"]
    assert main._ensure_keywords([], words, ["c"]) == words


def test_parse_accounts_basic():
    raw = "user1:pwd1;user2:pwd2\nuser3:pwd3"
    result = main._parse_accounts(raw)
    assert result == [("user1", "pwd1"), ("user2", "pwd2"), ("user3", "pwd3")]


def test_parse_accounts_skip_invalid():
    raw = "badentry; user: ; :pwd; valid:ok"
    result = main._parse_accounts(raw)
    assert result == [("valid", "ok")]


def test_load_accounts_raise(monkeypatch):
    monkeypatch.setattr(main, "ACCOUNTS_PATH", Path("/tmp/not_exists_accounts.txt"))
    with pytest.raises(RuntimeError):
        main._load_accounts()


def test_load_accounts_success(monkeypatch, tmp_path):
    cfg_dir = tmp_path / "acc"
    cfg_dir.mkdir()
    cfg = cfg_dir / "accounts.txt"
    cfg.write_text("u1:p1\n#comment\nu2:p2", encoding="utf-8")
    monkeypatch.setattr(main, "ACCOUNTS_PATH", cfg)
    assert main._load_accounts() == [("u1", "p1"), ("u2", "p2")]


def test_run_mobile_flow_skip_when_no_remaining(monkeypatch):
    called = {"init": 0}

    def fake_init(_):
        called["init"] += 1
        raise AssertionError("不应初始化移动端浏览器")

    monkeypatch.setattr(main, "init_mobile_edge_appium", fake_init)
    main._run_mobile_flow("u@example.com", "pwd", None, remaining_mobile=0)
    assert called["init"] == 0


def test_run_mobile_flow_init_when_remaining(monkeypatch):
    class FakeDriver:
        def quit(self):
            return None

    called = {"init": 0, "search_loop": 0}

    def fake_init(_):
        called["init"] += 1
        return FakeDriver()

    monkeypatch.setattr(main, "init_mobile_edge_appium", fake_init)
    monkeypatch.setattr(main, "_detect_logged_in_email", lambda _: "u@example.com")
    monkeypatch.setattr(main, "gohome", lambda _: None)
    monkeypatch.setattr(main, "goSearch", lambda _: None)
    monkeypatch.setattr(main, "getBaiduTrends", lambda: [])
    monkeypatch.setattr(main, "getZhihuTrends", lambda: [])
    monkeypatch.setattr(main, "getDouYinTrends", lambda: [])
    monkeypatch.setattr(main, "_ensure_keywords", lambda *args: ["k1"])

    def fake_search_loop(driver, keyword_list, loops, tag, extra_sleep=False):
        called["search_loop"] += 1
        assert tag == "Mobile"

    monkeypatch.setattr(main, "_search_loop", fake_search_loop)
    main._run_mobile_flow("u@example.com", "pwd", None, remaining_mobile=3)
    assert called["init"] == 1
    assert called["search_loop"] == 1


def test_should_run_mobile():
    assert main._should_run_mobile(1) is True
    assert main._should_run_mobile(0) is False
    assert main._should_run_mobile(-1) is False


def test_get_remaining_searches_uses_config(monkeypatch):
    monkeypatch.setattr(main, "DEFAULT_PC_SEARCHES", 28)
    monkeypatch.setattr(main, "DEFAULT_MOBILE_SEARCHES", 19)
    remaining_desktop, remaining_mobile = main.getRemainingSearches(object())
    assert remaining_desktop == 28
    assert remaining_mobile == 19


def test_get_cached_trends_hit(monkeypatch):
    main._TRENDS_CACHE.clear()
    called = {"n": 0}

    def fetcher():
        called["n"] += 1
        return ["a", "b"]

    assert main._get_cached_trends("k", fetcher) == ["a", "b"]
    assert main._get_cached_trends("k", fetcher) == ["a", "b"]
    assert called["n"] == 1


def test_get_cached_trends_failure_ttl(monkeypatch):
    main._TRENDS_CACHE.clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(main.time, "time", lambda: t["now"])

    called = {"n": 0}

    def fetcher():
        called["n"] += 1
        return []

    assert main._get_cached_trends("k2", fetcher) == []
    t["now"] += 1
    assert main._get_cached_trends("k2", fetcher) == []
    assert called["n"] == 1
    t["now"] += main.TRENDS_CACHE_FAILURE_TTL + 1
    assert main._get_cached_trends("k2", fetcher) == []
    assert called["n"] == 2


def test_search_loop_keyword_cycle_no_mutation(monkeypatch):
    words = ["a", "b", "c"]
    original = words.copy()
    seen = []

    monkeypatch.setattr(main, "tqdm", lambda it, **_: it)
    monkeypatch.setattr(main, "_maybe_take_break", lambda _: None)

    def fake_shuffle(lst):
        lst.reverse()

    monkeypatch.setattr(main.random, "shuffle", fake_shuffle)
    monkeypatch.setattr(main, "bing_search", lambda _driver, keyword: seen.append(keyword))

    main._search_loop(object(), words, searches=5, tag="PC")
    assert words == original
    assert seen == ["c", "b", "a", "c", "b"]


def test_search_loop_skip_when_no_keywords(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(main, "tqdm", lambda it, **_: it)
    monkeypatch.setattr(main, "_maybe_take_break", lambda _: None)
    monkeypatch.setattr(main, "bing_search", lambda *_: called.__setitem__("n", called["n"] + 1))
    main._search_loop(object(), [], searches=3, tag="PC")
    assert called["n"] == 0


def test_parse_headless_flag():
    assert main._parse_headless_flag(["main_old.py"]) is None
    assert main._parse_headless_flag(["main_old.py", "headless"]) == "headless"
    assert main._parse_headless_flag(["main_old.py", "--headless"]) == "--headless"


def test_run_desktop_flow_skip_search_when_no_remaining(monkeypatch):
    class FakeDriver:
        def quit(self):
            return None

    called = {"goSearch": 0, "trends": 0, "search_loop": 0}

    monkeypatch.setattr(main, "init_browser", lambda _: FakeDriver())
    monkeypatch.setattr(main, "_detect_logged_in_email", lambda _: "u@example.com")
    monkeypatch.setattr(main, "gohome", lambda _: None)
    monkeypatch.setattr(main, "daily_set", lambda _: None)
    monkeypatch.setattr(main, "getRemainingSearches", lambda _: (0, 0))

    def fake_go_search(_):
        called["goSearch"] += 1

    monkeypatch.setattr(main, "goSearch", fake_go_search)
    monkeypatch.setattr(main, "getDouYinTrends", lambda: called.__setitem__("trends", called["trends"] + 1) or [])
    monkeypatch.setattr(main, "getBaiduTrends", lambda: called.__setitem__("trends", called["trends"] + 1) or [])
    monkeypatch.setattr(main, "getZhihuTrends", lambda: called.__setitem__("trends", called["trends"] + 1) or [])
    monkeypatch.setattr(main, "_ensure_keywords", lambda *args: ["k1"])

    def fake_search_loop(*_args, **_kwargs):
        called["search_loop"] += 1

    monkeypatch.setattr(main, "_search_loop", fake_search_loop)
    remaining_mobile = main._run_desktop_flow("u@example.com", "pwd", None)
    assert remaining_mobile == 0
    assert called["goSearch"] == 0
    assert called["trends"] == 0
    assert called["search_loop"] == 0


def test_daily_set_uses_enumerate_index(monkeypatch):
    called = {"fallback": 0}

    monkeypatch.setattr(main, "gohome", lambda _: None)
    monkeypatch.setattr(main, "_run_daily_set_dom_fallback", lambda _driver: called.__setitem__("fallback", called["fallback"] + 1) or 2)

    main.daily_set(object())
    assert called["fallback"] == 1


def test_pick_daily_set_key_fallback_latest_date():
    data = {
        "12/01/2024": [{"attributes": {"state": "NotComplete"}}],
        "12/05/2024": [{"attributes": {"state": "NotComplete"}}],
        "bad": [{"attributes": {"state": "NotComplete"}}],
    }
    assert main._pick_daily_set_key(data) == "12/05/2024"


def test_pick_daily_set_key_empty_returns_none():
    assert main._pick_daily_set_key({}) is None


def test_bing_search_retries_on_exception(monkeypatch):
    class FakeElement:
        def __init__(self):
            self.submitted = 0

        def submit(self):
            self.submitted += 1

        def send_keys(self, *_):
            return None

    element = FakeElement()
    behaviors = [RuntimeError("x"), RuntimeError("y"), element]

    class FakeWait:
        def __init__(self, _driver, _timeout):
            return None

        def until(self, _cond):
            next_item = behaviors.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item

    monkeypatch.setattr(main, "WebDriverWait", FakeWait)
    monkeypatch.setattr(main, "_random_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_type_keyword", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_maybe_select_suggestion", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_random_scroll_results", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_random_click_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_jiggle_mouse", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main.random, "random", lambda: 0.0)
    monkeypatch.setattr(main, "goSearch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_reset_search_box", lambda *_args, **_kwargs: None)

    main.bing_search(object(), "k")
    assert element.submitted == 1
    assert behaviors == []


def test_bing_search_falls_back_to_direct_url(monkeypatch):
    calls = {"direct": 0}

    monkeypatch.setattr(main, "_locate_search_box", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "goSearch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "_search_via_direct_url", lambda *_args, **_kwargs: calls.__setitem__("direct", calls["direct"] + 1))

    main.bing_search(object(), "k")
    assert calls["direct"] == 1


def test_collect_daily_set_dom_entries_maps_from_daily_cards(monkeypatch):
    monkeypatch.setattr(
        main,
        "_collect_daily_task_cards",
        lambda *_args, **_kwargs: [
            {"taskId": "2", "title": "每日签到", "text": "每日签到 +10", "href": "https://rewards.bing.com/x", "points": "+10"},
        ],
    )
    result = main._collect_daily_set_dom_entries(object())
    assert [item["taskId"] for item in result] == ["2"]
    assert result[0]["href"] == "https://rewards.bing.com/x"


def test_collect_daily_set_dom_entries_empty_when_no_cards(monkeypatch):
    monkeypatch.setattr(
        main,
        "_collect_daily_task_cards",
        lambda *_args, **_kwargs: [],
    )
    result = main._collect_daily_set_dom_entries(object())
    assert result == []


def test_collect_daily_set_dom_entries_skips_items_without_points(monkeypatch):
    monkeypatch.setattr(
        main,
        "_collect_daily_task_cards",
        lambda *_args, **_kwargs: [
            {"taskId": "2", "title": "琅勃拉邦之美", "text": "琅勃拉邦之美 +10", "href": "https://rewards.bing.com/y", "points": "+10"},
        ],
    )
    result = main._collect_daily_set_dom_entries(object())
    assert [item["taskId"] for item in result] == ["2"]


def test_run_daily_set_prefers_daily_task_cards(monkeypatch):
    calls = {"daily": 0, "fallback": 0}
    monkeypatch.setattr(main, "_collect_daily_task_cards", lambda _driver: [{"taskId": "1", "title": "琅勃拉邦之美", "points": "+10 积分"}])
    monkeypatch.setattr(main, "_click_daily_task_card", lambda _driver, _entry: calls.__setitem__("daily", calls["daily"] + 1) or True)
    monkeypatch.setattr(main, "_collect_daily_set_dom_entries", lambda _driver: [])
    monkeypatch.setattr(main, "gohome", lambda _driver: None)
    result = main._run_daily_set_dom_fallback(object())
    assert result == 1
    assert calls["daily"] == 1
    assert calls["fallback"] == 0


def test_resolve_daily_task_entry_refinds_after_reload(monkeypatch):
    class FakeDriver:
        def find_element(self, *_args, **_kwargs):
            raise Exception("stale")

    monkeypatch.setattr(
        main,
        "_collect_daily_task_cards",
        lambda _driver: [
            {"taskId": "fresh-1", "title": "比才的魔力", "text": "比才的魔力 探索比才歌剧的永恒魅力。 +10", "href": "https://www.bing.com/search?q=a", "points": "+10"},
            {"taskId": "fresh-2", "title": "冰封奇观", "text": "冰封奇观 探索南极洲独特的动物世界。 +10", "href": "https://www.bing.com/search?q=b", "points": "+10"},
        ],
    )
    result = main._resolve_daily_task_entry(
        FakeDriver(),
        {"taskId": "old-2", "title": "冰封奇观", "href": "https://www.bing.com/search?q=b", "points": "+10"},
    )
    assert result["taskId"] == "fresh-2"


def test_collect_claimable_dom_entries_skips_home(monkeypatch):
    monkeypatch.setattr(
        main,
        "_collect_dom_reward_entries",
        lambda *_args, **_kwargs: [
            {"taskId": "1", "text": "首页", "href": "https://rewards.bing.com/", "context": "reward activity home"},
            {"taskId": "2", "text": "领取 10 积分", "href": "https://rewards.bing.com/claim", "context": "reward activity claim points"},
        ],
    )
    result = main._collect_claimable_dom_entries(object())
    assert [item["taskId"] for item in result] == ["2"]


def test_collect_claimable_dom_entries_skips_claim_summary_card(monkeypatch):
    monkeypatch.setattr(
        main,
        "_collect_dom_reward_entries",
        lambda *_args, **_kwargs: [
            {"taskId": "1", "text": "可领取 183 领取", "href": "", "context": "reward activity claim points"},
            {"taskId": "2", "text": "90 积分 默认搜索奖励 上个月赚取的积分: 待领取", "href": "https://rewards.bing.com/x", "context": "reward activity claim points 待领取"},
        ],
    )
    result = main._collect_claimable_dom_entries(object())
    assert [item["taskId"] for item in result] == ["2"]


def test_promotion_is_complete_with_progress():
    item = {"pointProgress": 10, "pointProgressMax": 10}
    assert main._promotion_is_complete(item) is True


def test_receive_points_runs_dom_fallback(monkeypatch):
    calls = []

    monkeypatch.setattr(main, "gohome", lambda _: None)
    monkeypatch.setattr(main, "_claim_available_points", lambda _driver, tag="": calls.append(f"claim:{tag}") or 1)
    monkeypatch.setattr(
        main,
        "_run_dom_reward_fallback",
        lambda _driver, tag: calls.append(tag) or 2,
    )

    main.receive_points(object())
    assert calls == ["claim:积分任务", "积分任务", "claim:积分任务"]


def test_claim_available_points_stops_when_no_entries(monkeypatch):
    monkeypatch.setattr(main, "_claim_points_from_dashboard_sidebar", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(main, "_collect_claim_button_entries", lambda _driver: [])
    assert main._claim_available_points(object(), tag="积分任务") == 0


def test_claim_available_points_includes_sidebar(monkeypatch):
    monkeypatch.setattr(main, "_claim_points_from_dashboard_sidebar", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(main, "_collect_claim_button_entries", lambda _driver: [])
    assert main._claim_available_points(object(), tag="积分任务") == 1


def test_wait_login_complete_returns_when_url_changes(monkeypatch):
    class FakeDriver:
        def __init__(self):
            self.current_url = "https://login.live.com/"
            self.page_source = ""

        def find_elements(self, *_args, **_kwargs):
            return []

    driver = FakeDriver()
    t = {"now": 0.0}

    def fake_time():
        return t["now"]

    def fake_sleep(_seconds):
        t["now"] += 1
        driver.current_url = "https://rewards.bing.com/"

    monkeypatch.setattr(main.time, "time", fake_time)
    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    main._wait_login_complete(driver, timeout=10)


def test_wait_login_complete_clicks_secondary_button(monkeypatch):
    class FakeElement:
        def __init__(self, driver):
            self._driver = driver

        def click(self):
            self._driver.current_url = "https://rewards.bing.com/"

        @property
        def text(self):
            return "否"

    class FakeDriver:
        def __init__(self):
            self.current_url = "https://login.live.com/"
            self.page_source = ""

        def find_elements(self, by, locator):
            if by == main.By.CSS_SELECTOR and locator == "button[data-testid='secondaryButton']":
                return [FakeElement(self)]
            return []

    driver = FakeDriver()

    t = {"now": 0.0}
    monkeypatch.setattr(main.time, "time", lambda: t["now"])

    def fake_sleep(_seconds):
        t["now"] += 1

    monkeypatch.setattr(main.time, "sleep", fake_sleep)
    main._wait_login_complete(driver, timeout=5)


def test_ensure_bing_account_noop_when_match(monkeypatch):
    called = {"sign_out": 0, "sign_in": 0}
    monkeypatch.setattr(main, "_detect_bing_logged_in_email", lambda _d: "u@example.com")
    monkeypatch.setattr(main, "_bing_sign_out", lambda _d: called.__setitem__("sign_out", called["sign_out"] + 1))
    monkeypatch.setattr(main, "_bing_sign_in", lambda _d: called.__setitem__("sign_in", called["sign_in"] + 1))
    main._ensure_bing_account(object(), "u@example.com", tag="PC")
    assert called == {"sign_out": 0, "sign_in": 0}


def test_ensure_bing_account_reauth_when_mismatch(monkeypatch):
    called = {"sign_out": 0, "sign_in": 0}
    seq = iter(["old@example.com", "u@example.com"])
    monkeypatch.setattr(main, "_detect_bing_logged_in_email", lambda _d: next(seq))
    monkeypatch.setattr(main, "_bing_sign_out", lambda _d: called.__setitem__("sign_out", called["sign_out"] + 1))
    monkeypatch.setattr(main, "_bing_sign_in", lambda _d: called.__setitem__("sign_in", called["sign_in"] + 1))
    main._ensure_bing_account(object(), "u@example.com", tag="PC")
    assert called == {"sign_out": 1, "sign_in": 1}


def test_wait_login_complete_raises_on_blocker(monkeypatch):
    class FakeDriver:
        current_url = "https://login.live.com/"
        page_source = "帮助我们保护你的帐户"

        def find_elements(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(main.time, "sleep", lambda *_: None)
    monkeypatch.setattr(main.time, "time", lambda: 0.0)
    with pytest.raises(RuntimeError, match="安全验证"):
        main._wait_login_complete(FakeDriver(), timeout=1)


def test_switch_to_new_tab_with_previous_handles(monkeypatch):
    class FakeSwitchTo:
        def __init__(self):
            self.window_name = None

        def window(self, window_name=None, **_kwargs):
            self.window_name = window_name

    class FakeDriver:
        def __init__(self):
            self.window_handles = ["a"]
            self.switch_to = FakeSwitchTo()

    driver = FakeDriver()

    class FakeWait:
        def __init__(self, _driver, _timeout):
            self._driver = _driver

        def until(self, predicate):
            self._driver.window_handles.append("b")
            return predicate(self._driver)

    monkeypatch.setattr(main, "WebDriverWait", FakeWait)
    main.switchToNewTab(driver, timeToWait=0, previous_handles={"a"})
    assert driver.switch_to.window_name == "b"
