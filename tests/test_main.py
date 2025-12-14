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


def test_load_accounts_success(monkeypatch):
    cfg = monkeypatch.tmpdir.mkdir("acc").join("accounts.txt")
    cfg.write("u1:p1\n#comment\nu2:p2")
    monkeypatch.setattr(main, "ACCOUNTS_PATH", Path(str(cfg)))
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


def test_get_remaining_searches_mobile_only(monkeypatch):
    dashboard = {
        "userStatus": {
            "levelInfo": {"activeLevel": "Level2"},
            "counters": {
                "pcSearch": [],
                "mobileSearch": [{"pointProgress": 0, "pointProgressMax": 33}],
            },
        }
    }
    monkeypatch.setattr(main, "getDashboardData", lambda _: dashboard)
    remaining_desktop, remaining_mobile = main.getRemainingSearches(object())
    assert remaining_desktop == 0
    assert remaining_mobile == 11


def test_get_remaining_searches_mobile_sum(monkeypatch):
    dashboard = {
        "userStatus": {
            "levelInfo": {"activeLevel": "Level2"},
            "counters": {
                "pcSearch": [],
                "mobileSearch": [
                    {"pointProgress": 3, "pointProgressMax": 33},
                    {"pointProgress": 0, "pointProgressMax": 33},
                ],
            },
        }
    }
    monkeypatch.setattr(main, "getDashboardData", lambda _: dashboard)
    remaining_desktop, remaining_mobile = main.getRemainingSearches(object())
    assert remaining_desktop == 0
    assert remaining_mobile == 21


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
    assert main._parse_headless_flag(["main.py"]) is None
    assert main._parse_headless_flag(["main.py", "headless"]) == "headless"
    assert main._parse_headless_flag(["main.py", "--headless"]) == "--headless"


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
