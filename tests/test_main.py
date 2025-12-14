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
