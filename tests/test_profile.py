import os
import smtplib
import socket
import tempfile
import time
from pathlib import Path

import pytest

from utils import config, profile

import main as main_module


# ======================== Edge 定位 ========================

def test_find_edge_executable_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "edge_candidates", lambda: [tmp_path / "nope" / "msedge.exe"])
    assert profile.find_edge_executable() is None


def test_find_edge_executable_returns_first_existing(monkeypatch, tmp_path):
    first = tmp_path / "a" / "msedge.exe"
    second = tmp_path / "b" / "msedge.exe"
    second.parent.mkdir(parents=True)
    second.write_text("", encoding="utf-8")
    monkeypatch.setattr(profile, "edge_candidates", lambda: [first, second])
    assert profile.find_edge_executable() == second


# ======================== profile 占用检测 ========================

def test_is_profile_in_use_false_without_lock_files(tmp_path):
    assert profile.is_profile_in_use(tmp_path) is False


def test_probe_exclusive_reports_missing(tmp_path):
    assert profile._probe_exclusive(tmp_path / "lockfile") == "missing"


def test_probe_exclusive_reports_free_for_plain_file(tmp_path):
    target = tmp_path / "lockfile"
    target.write_text("", encoding="utf-8")
    assert profile._probe_exclusive(target) == "free"


@pytest.mark.skipif(profile.os.name != "nt", reason="仅在 Windows 上验证共享锁语义")
def test_is_profile_in_use_true_when_lockfile_held(tmp_path):
    import ctypes
    from ctypes import wintypes

    lock = tmp_path / "lockfile"
    lock.write_text("", encoding="utf-8")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(lock), 0x80000000, 0, None, 3, 0, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        assert profile._probe_exclusive(lock) == "locked"
        assert profile.is_profile_in_use(tmp_path) is True
    finally:
        kernel32.CloseHandle(handle)
    assert profile.is_profile_in_use(tmp_path) is False


# ======================== 占用进程定位 ========================

def test_parse_holder_lines_skips_invalid_rows():
    output = "123\tmsedge.exe --user-data-dir=D:\\p\nnot-a-pid\tx\n\n456\tchrome.exe\n"
    assert profile._parse_holder_lines(output) == [(123, "msedge.exe --user-data-dir=D:\\p"), (456, "chrome.exe")]


def test_powershell_holders_returns_none_on_failure(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise OSError("powershell missing")

    monkeypatch.setattr(profile.subprocess, "run", boom)
    assert profile._powershell_holders(tmp_path) is None


def test_powershell_holders_parses_output(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = "77\tmsedge.exe --user-data-dir=D:\\p\n"

    monkeypatch.setattr(profile.subprocess, "run", lambda *a, **k: Result())
    assert profile._powershell_holders(tmp_path) == [(77, "msedge.exe --user-data-dir=D:\\p")]


def test_posix_holders_filters_unrelated_processes(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = (
            "11 /usr/bin/chrome --user-data-dir=/tmp/p\n"
            "12 /usr/bin/python main.py\n"
            "13 /usr/bin/msedge --user-data-dir=/tmp/other\n"
        )

    monkeypatch.setattr(profile.subprocess, "run", lambda *a, **k: Result())
    assert profile._posix_holders("/tmp/p") == [(11, "/usr/bin/chrome --user-data-dir=/tmp/p")]


def test_kill_profile_holders_returns_killed_pids(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "_process_table", lambda: None)
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: [(1, "msedge.exe"), (2, "chrome.exe")])
    monkeypatch.setattr(profile, "_kill_pid", lambda pid: pid == 1)
    assert profile.kill_profile_holders(tmp_path, quiet=True) == [1]


def test_kill_profile_holders_handles_unavailable_query(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "_process_table", lambda: None)
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: None)
    assert profile.kill_profile_holders(tmp_path, quiet=True) == []


def _table(*rows):
    return {pid: (ppid, name, session) for pid, ppid, name, session in rows}


def test_process_table_parses_cim_output(monkeypatch):
    class Result:
        returncode = 0
        stdout = "140\t130\tmsedge.exe\t0\nbad line\n160\t150\tchrome.exe\t1\n"

    monkeypatch.setattr(profile.subprocess, "run", lambda *a, **k: Result())
    assert profile._process_table() == {140: (130, "msedge.exe", 0), 160: (150, "chrome.exe", 1)}


def test_ancestor_names_walks_up_and_stops_on_cycle():
    table = _table((10, 20, "node.exe", 0), (20, 10, "python.exe", 0))
    assert profile._ancestor_names(10, table) == ["python.exe"]


def test_self_and_ancestors_protects_self_and_parents():
    table = _table((profile.os.getpid(), 999, "python.exe", 1), (999, 0, "pwsh.exe", 1))
    assert profile._self_and_ancestors(table) == {profile.os.getpid(), 999}


def test_tree_holders_finds_python_launched_browser_main():
    table = _table(
        (100, 0, "svchost.exe", 0),
        (110, 100, "cmd.exe", 0),
        (120, 110, "python.exe", 0),
        (130, 120, "node.exe", 0),
        (140, 130, "msedge.exe", 0),
        (141, 140, "msedge.exe", 0),
    )
    assert profile._tree_holders(table) == [140]


def test_tree_holders_ignores_manually_started_browser():
    table = _table((1, 0, "explorer.exe", 1), (2, 1, "msedge.exe", 1))
    assert profile._tree_holders(table) == []


def test_find_profile_holders_prefers_restart_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "_process_table", lambda: _table((140, 130, "msedge.exe", 0)))
    monkeypatch.setattr(profile, "restart_manager_pids", lambda paths: [140])

    def fail(_dir):
        raise AssertionError("RM 已定位时不应回退")

    monkeypatch.setattr(profile, "_powershell_holders", fail)
    assert profile.find_profile_holders(tmp_path) == [(140, "msedge.exe（会话 0）")]


def test_find_profile_holders_falls_back_to_process_tree(monkeypatch, tmp_path):
    table = _table((120, 0, "python.exe", 0), (140, 120, "msedge.exe", 0))
    monkeypatch.setattr(profile, "_process_table", lambda: table)
    monkeypatch.setattr(profile, "restart_manager_pids", lambda paths: None)
    monkeypatch.setattr(profile, "_powershell_holders", lambda d: [])
    monkeypatch.setattr(profile, "is_profile_in_use", lambda d: True)
    assert profile.find_profile_holders(tmp_path) == [(140, "msedge.exe（会话 0）")]


def test_find_profile_holders_returns_none_when_queries_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "_process_table", lambda: None)
    monkeypatch.setattr(profile, "restart_manager_pids", lambda paths: None)
    monkeypatch.setattr(profile, "_powershell_holders", lambda d: None)
    assert profile.find_profile_holders(tmp_path) is None


def test_kill_profile_holders_never_kills_self(monkeypatch, tmp_path):
    me = profile.os.getpid()
    table = _table((me, 1, "python.exe", 1), (999, 0, "msedge.exe", 0))
    monkeypatch.setattr(profile, "_process_table", lambda: table)
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: [(me, "python.exe"), (999, "msedge.exe")])
    killed = []
    monkeypatch.setattr(profile, "_kill_pid", lambda pid: killed.append(pid) or True)
    assert profile.kill_profile_holders(tmp_path, quiet=True) == [999]
    assert killed == [999]


def test_kill_profile_holders_skips_non_browser_pids(monkeypatch, tmp_path):
    table = _table((500, 1, "explorer.exe", 1))
    monkeypatch.setattr(profile, "_process_table", lambda: table)
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: [(500, "explorer.exe")])
    monkeypatch.setattr(profile, "_kill_pid", lambda pid: True)
    assert profile.kill_profile_holders(tmp_path, quiet=True) == []


# ======================== 占用自愈 ========================

def test_ensure_profile_available_true_when_free(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "is_profile_in_use", lambda d: False)
    assert profile.ensure_profile_available(tmp_path) is True


def test_ensure_profile_available_no_autokill(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "is_profile_in_use", lambda d: True)
    called = {"kill": 0}
    monkeypatch.setattr(profile, "kill_profile_holders", lambda d, **k: called.__setitem__("kill", called["kill"] + 1))
    assert profile.ensure_profile_available(tmp_path, auto_kill=False) is False
    assert called["kill"] == 0


def test_ensure_profile_available_recovers_after_kill(monkeypatch, tmp_path):
    state = {"in_use": True}
    monkeypatch.setattr(profile, "is_profile_in_use", lambda d: state["in_use"])
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: [(1, "msedge.exe")])

    def fake_kill(d, quiet=False):
        state["in_use"] = False
        return [1]

    monkeypatch.setattr(profile, "kill_profile_holders", fake_kill)
    assert profile.ensure_profile_available(tmp_path, delay=0) is True


def test_ensure_profile_available_false_when_still_in_use(monkeypatch, tmp_path):
    monkeypatch.setattr(profile, "is_profile_in_use", lambda d: True)
    monkeypatch.setattr(profile, "find_profile_holders", lambda d: [])
    assert profile.ensure_profile_available(tmp_path, retries=1, delay=0) is False


# ======================== 单实例保护 ========================

def test_acquire_single_instance_exclusive():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    first = profile.acquire_single_instance(port=port)
    assert first is not None
    try:
        assert profile.acquire_single_instance(port=port) is None
    finally:
        first.close()
    released = profile.acquire_single_instance(port=port)
    assert released is not None
    released.close()


# ======================== 临时目录 ========================

@pytest.fixture(autouse=True)
def _restore_temp_env():
    """prepare_temp_dir 会改全局环境，测试结束后统一还原"""
    names = ("TEMP", "TMP", "TMPDIR")
    saved_env = {name: os.environ.get(name) for name in names}
    saved_default = tempfile.tempdir
    yield
    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tempfile.tempdir = saved_default


def test_prepare_temp_dir_sets_env_and_tempfile(tmp_path):
    target = tmp_path / "tmp_playwright_temp"
    result = profile.prepare_temp_dir(target)
    assert result == target
    assert target.is_dir()
    resolved = str(target.resolve())
    for name in ("TEMP", "TMP", "TMPDIR"):
        assert os.environ[name] == resolved
    assert tempfile.tempdir == resolved


def test_warmup_temp_dir_true_when_writable(tmp_path):
    target = tmp_path / "tmp"
    profile.prepare_temp_dir(target)
    assert profile.warmup_temp_dir(target) is True
    assert list(target.iterdir()) == []  # 探测目录被清理


def test_warmup_temp_dir_false_when_mkdtemp_fails(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(profile.tempfile, "mkdtemp", boom)
    assert profile.warmup_temp_dir(tmp_path) is False


def test_cleanup_stale_artifacts_removes_only_old_dirs(tmp_path):
    old = tmp_path / "playwright-artifacts-OLD"
    fresh = tmp_path / "playwright-artifacts-NEW"
    other = tmp_path / "keepme"
    for path in (old, fresh, other):
        path.mkdir()
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))

    assert profile.cleanup_stale_artifacts(tmp_path, max_age_hours=24) == 1
    assert not old.exists()
    assert fresh.exists()
    assert other.exists()


def test_cleanup_stale_artifacts_missing_dir(tmp_path):
    assert profile.cleanup_stale_artifacts(tmp_path / "nope") == 0


def test_temp_dir_error_hints_match_mkdtemp_eperm():
    message = (
        "BrowserType.launch_persistent_context: EPERM: operation not permitted, "
        "mkdtemp 'C:\\Users\\b1184\\AppData\\Local\\Temp\\playwright-artifacts-XXXXXX'"
    )
    assert main_module._matches(message, main_module.TEMP_DIR_ERROR_HINTS) is True
    assert main_module._matches(message, main_module.PROFILE_BUSY_HINTS) is False
    assert main_module._matches(message, main_module.EDGE_MISSING_HINTS) is False


def test_create_context_exits_with_temp_hint(monkeypatch):
    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            raise main_module.PlaywrightError(
                "EPERM: operation not permitted, mkdtemp 'C:\\Temp\\playwright-artifacts-XXXXXX'"
            )

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: None)
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)
    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), False)


# ======================== 命令行入口 ========================

def test_cli_check_reports_free_profiles(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path / "pc")
    monkeypatch.setattr(config, "MOBILE_PROFILE_DIR", tmp_path / "mobile")
    (tmp_path / "pc").mkdir()
    assert profile.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "占用状态: 空闲" in out
    assert "profile 目录不存在" in out


# ======================== main.py 启动逻辑 ========================

@pytest.fixture(autouse=True)
def _legacy_tests_use_isolated_profile(monkeypatch, request):
    """旧启动重试测试只验证隔离 profile。

    正式 profile 现在走正常 Edge + CDP，单独由 CDP 测试覆盖；这里显式把
    EDGE_USER_DATA_DIR 指向隔离目录，避免环境相关配置改变测试分支。
    """
    if request.node.name.startswith(("test_real_edge", "test_create_context_requires_cdp")):
        return
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.USER_DATA_DIR)


def test_matches_profile_busy_message():
    message = "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for your profile directory"
    assert main_module._matches(message, main_module.PROFILE_BUSY_HINTS) is True
    assert main_module._matches(message, main_module.EDGE_MISSING_HINTS) is False


def test_launch_persistent_uses_edge_channel_when_available():
    captured = {}

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return "ctx"

    class FakePlaywright:
        chromium = FakeChromium()

    main_module.launch_persistent(FakePlaywright(), Path("D:/p"), True, True)
    assert captured["channel"] == "msedge"
    assert captured["user_data_dir"].endswith("p")


def test_create_context_retries_after_cleaning_profile(monkeypatch):
    calls = {"n": 0}

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise main_module.PlaywrightError(
                    "Failed to create a ProcessSingleton for your profile directory"
                )
            return "ctx"

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)
    assert main_module.create_context(FakePlaywright(), False) == "ctx"
    assert calls["n"] == 2


def test_create_context_exits_when_profile_stays_locked(monkeypatch):
    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            raise main_module.PlaywrightError("Failed to create a ProcessSingleton for your profile directory")

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: None)
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: False)
    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), False)


def test_create_context_never_falls_back_to_chromium(monkeypatch):
    """Edge-only：Edge 启动失败必须退出，不得静默切回 Chromium。"""
    seen = []

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            seen.append(kwargs.get("channel"))
            raise main_module.PlaywrightError(
                r"Chromium distribution 'msedge' is not found at C:\x\msedge.exe"
            )

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)
    with pytest.raises(SystemExit) as exc:
        main_module.create_context(FakePlaywright(), False)
    assert exc.value.code == 2
    assert seen == ["msedge"]


# ======================== 会话0 窗口站抖动重试 ========================

def test_message_window_hint_constant_matches_real_edge_fatal():
    """真实崩溃文本必须被 MESSAGE_WINDOW_HINTS 命中，否则重试逻辑永不触发"""
    real = (
        "[pid=27052][err] [27052:15972:ERROR:base\\win\\message_window.cc:235] "
        "Failed to create a message-only window: 拒绝访问。 (0x5)\n"
        "[pid=27052][err] [27052:15972:FATAL:base\\message_loop\\message_pump_win.cc:122] "
        "Check failed: . Failed to create message-only Window: 拒绝访问。 (0x5)"
    )
    assert main_module._matches(real, main_module.MESSAGE_WINDOW_HINTS)


def _patch_slow_retry(monkeypatch):
    """跳过重试间的真实 sleep，保持测试秒级完成"""
    monkeypatch.setattr(main_module.time, "sleep", lambda *_: None)


def test_create_context_retries_message_window_flake(monkeypatch):
    """会话0 偶发拿不到窗口站时，应重试而不是直接失败退出"""
    _patch_slow_retry(monkeypatch)
    calls = {"n": 0}

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise main_module.PlaywrightError(
                    "BrowserType.launch_persistent_context: Target page, context or browser "
                    "has been closed\nBrowser logs:\n"
                    "Failed to create message-only Window: 拒绝访问。 (0x5)"
                )
            return "ctx"

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)

    assert main_module.create_context(FakePlaywright(), True) == "ctx"
    assert calls["n"] == 3, f"抖动应在第3次成功，实际尝试 {calls['n']} 次"


def test_create_context_stops_after_window_retries_exhausted(monkeypatch):
    """抖动持续存在时必须收敛为退出码2，不能无限重试挂住计划任务"""
    _patch_slow_retry(monkeypatch)
    calls = {"n": 0}

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls["n"] += 1
            raise main_module.PlaywrightError(
                "Failed to create message-only Window: 拒绝访问。 (0x5)"
            )

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)

    with pytest.raises(SystemExit) as exc:
        main_module.create_context(FakePlaywright(), True)
    assert exc.value.code == 2
    # 1 次首发 + 3 次抖动重试 = 4，重试次数必须有上限
    assert calls["n"] == 4, f"重试次数应封顶，实际 {calls['n']} 次"


def test_create_context_window_retry_cleans_leftover_processes(monkeypatch):
    """重试前必须清理上一次半启动的浏览器，否则下一轮仍会撞同一把锁"""
    _patch_slow_retry(monkeypatch)
    cleaned = {"n": 0}
    calls = {"n": 0}

    def fake_ensure(*a, **k):
        cleaned["n"] += 1
        return True

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise main_module.PlaywrightError(
                    "Failed to create message-only Window: 拒绝访问。 (0x5)"
                )
            return "ctx"

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", fake_ensure)

    assert main_module.create_context(FakePlaywright(), True) == "ctx"
    assert cleaned["n"] >= 2, "抖动重试前应再调用一次 ensure_profile_available 清理残留"


def test_create_context_does_not_treat_window_flake_as_profile_busy(monkeypatch):
    """0x5 不是 profile 被占用：不应打印'profile 被占用'的误导性提示"""
    _patch_slow_retry(monkeypatch)
    calls = {"n": 0}

    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            calls["n"] += 1
            raise main_module.PlaywrightError("Failed to create message-only Window: 拒绝访问。 (0x5)")

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)

    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), True)


# ======================== 计划任务退出码语义 ========================

@pytest.fixture
def _stub_main_deps(monkeypatch, tmp_path):
    """把 main() 的外部依赖替换成可控替身，专注验证退出码"""
    state = {"accounts": [("u@example.com", "p")], "run_results": [True]}
    monkeypatch.setattr(main_module, "load_accounts", lambda: state["accounts"])
    monkeypatch.setattr(main_module, "run_account", lambda *a, **k: state["run_results"].pop(0))
    monkeypatch.setattr(main_module.profile, "prepare_temp_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: True)
    monkeypatch.setattr(main_module.profile, "cleanup_stale_artifacts", lambda *a, **k: 0)
    monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: None)
    monkeypatch.setenv("BING_REWARDS_ALLOW_MULTI", "1")
    return state


def _fake_playwright(monkeypatch):
    class FakeContext:
        def add_init_script(self, script):
            pass

        def close(self):
            pass

    class FakeManager:
        def __enter__(self):
            return object()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(main_module, "sync_playwright", lambda: FakeManager())
    monkeypatch.setattr(main_module, "create_context", lambda p, headless: FakeContext())


def test_main_returns_zero_when_all_accounts_succeed(monkeypatch, _stub_main_deps):
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 0


def test_main_returns_three_when_every_account_fails(monkeypatch, _stub_main_deps):
    _stub_main_deps["run_results"] = [False]
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 3


def test_main_returns_zero_when_partial_success(monkeypatch, _stub_main_deps):
    _stub_main_deps["accounts"] = [("a@example.com", "p"), ("b@example.com", "p")]
    _stub_main_deps["run_results"] = [True, False]
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 0


def test_main_returns_two_when_temp_dir_unusable(monkeypatch, _stub_main_deps):
    monkeypatch.setattr(main_module.profile, "warmup_temp_dir", lambda *a, **k: False)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 2


def test_main_returns_four_when_accounts_unreadable(monkeypatch, _stub_main_deps):
    def boom():
        raise FileNotFoundError("accounts.txt 不存在")

    monkeypatch.setattr(main_module, "load_accounts", boom)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 4


def test_main_returns_zero_when_another_instance_running(monkeypatch, _stub_main_deps):
    monkeypatch.delenv("BING_REWARDS_ALLOW_MULTI", raising=False)
    monkeypatch.setattr(main_module.profile, "acquire_single_instance", lambda **k: None)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    # 并发实例属于“正常让位”，不该让计划任务报失败
    assert main_module.main() == 0


def test_main_reports_launch_failure_code(monkeypatch, _stub_main_deps):
    _fake_playwright(monkeypatch)

    def boom(p, headless):
        raise SystemExit(2)

    monkeypatch.setattr(main_module, "create_context", boom)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    assert main_module.main() == 2


def test_main_ignores_task_flag_and_honours_headless(monkeypatch, _stub_main_deps):
    seen = []
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(
        main_module, "create_context", lambda p, headless: seen.append(headless) or type(
            "C", (), {"add_init_script": lambda self, s: None, "close": lambda self: None}
        )()
    )
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "--task", "headless"])
    assert main_module.main() == 0
    assert seen == [True]


def test_main_task_flag_alone_keeps_gui_mode(monkeypatch, _stub_main_deps):
    seen = []
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(
        main_module, "create_context", lambda p, headless: seen.append(headless) or type(
            "C", (), {"add_init_script": lambda self, s: None, "close": lambda self: None}
        )()
    )
    monkeypatch.setattr(main_module.sys, "argv", ["main.py", "--task"])
    assert main_module.main() == 0
    assert seen == [False]


# ======================== 失败推送 ========================

@pytest.fixture(autouse=True)
def _never_send_real_mail(monkeypatch):
    """兜底：任何测试都不允许真的连 SMTP 发信。

    一旦本机配置了 .env，调用 main() 的测试就会走到推送分支；
    这个 autouse 夹具把发送动作换成空实现，避免测试发真邮件。
    """
    try:
        from utils import notify
    except Exception:
        return

    def _boom(*a, **k):
        raise AssertionError("测试不允许真的发送邮件")

    monkeypatch.setattr(notify, "send_mail", _boom)


def test_notify_skips_when_no_smtp_config(monkeypatch, tmp_path):
    """未配置 SMTP 时静默跳过，视为功能未启用"""
    from utils import notify

    monkeypatch.setattr(notify, "load_smtp_config", lambda *a, **k: None)
    assert notify.notify_failure(3, "log") is False


def test_notify_skips_on_success(monkeypatch):
    """成功不推送：避免每天无意义打扰"""
    from utils import notify

    called = {"n": 0}
    monkeypatch.setattr(notify, "load_smtp_config", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    assert notify.notify_failure(0, "log") is False
    assert called["n"] == 0, "成功时不应去读 SMTP 配置，更不该发信"


def test_notify_sends_on_failure(monkeypatch):
    from utils import notify

    sent = {}
    monkeypatch.setattr(
        notify, "load_smtp_config",
        lambda *a, **k: {"host": "h", "port": 465, "user": "u", "password": "p",
                         "to": ["a@b.c"], "sender": "u", "use_ssl": True},
    )
    monkeypatch.setattr(notify, "send_mail", lambda cfg, s, b, **k: sent.update(subject=s, body=b))

    assert notify.notify_failure(3, "账号全部失败") is True
    assert "失败" in sent["subject"] and "BingPoints" in sent["subject"]
    assert "退出码：3" in sent["body"]
    assert "账号全部失败" in sent["body"]


def test_notify_swallows_send_errors(monkeypatch):
    """发送失败必须被吞掉并返回 False——通知失败不能把成功运行改成失败"""
    from utils import notify

    monkeypatch.setattr(
        notify, "load_smtp_config",
        lambda *a, **k: {"host": "h", "port": 465, "user": "u", "password": "p",
                         "to": ["a@b.c"], "sender": "u", "use_ssl": True},
    )

    def boom(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(notify, "send_mail", boom)
    assert notify.notify_failure(3, "log") is False


def test_notify_swallows_config_errors(monkeypatch):
    from utils import notify

    def boom(*a, **k):
        raise OSError("读 .env 失败")

    monkeypatch.setattr(notify, "load_smtp_config", boom)
    assert notify.notify_failure(3, "log") is False


def test_notify_subject_maps_exit_codes_to_reasons():
    """退出码要翻译成人话，否则收到邮件也不知道哪里坏了"""
    from utils import notify

    for code in (2, 3, 4):
        subject = notify.build_subject(code)
        assert notify.EXIT_CODE_REASONS[code] in subject


def test_notify_subject_handles_unknown_exit_code():
    from utils import notify

    assert "未知错误" in notify.build_subject(99)


def test_load_smtp_config_returns_none_without_host_or_to():
    from utils import notify

    assert notify.load_smtp_config({}) is None
    assert notify.load_smtp_config({"SMTP_HOST": "smtp.qq.com"}) is None
    assert notify.load_smtp_config({"SMTP_TO": "a@b.c"}) is None


def test_load_smtp_config_parses_multiple_recipients_and_defaults():
    from utils import notify

    cfg = notify.load_smtp_config({
        "SMTP_HOST": "smtp.qq.com",
        "SMTP_TO": "a@b.c; d@e.f , g@h.i",
        "SMTP_USER": "u@qq.com",
    })
    assert cfg["to"] == ["a@b.c", "d@e.f", "g@h.i"]
    assert cfg["port"] == 465
    assert cfg["use_ssl"] is True
    assert cfg["sender"] == "u@qq.com", "未给 SMTP_FROM 时应回退到 SMTP_USER"


def test_load_smtp_config_can_disable_ssl_for_starttls():
    from utils import notify

    cfg = notify.load_smtp_config({
        "SMTP_HOST": "smtp.qq.com", "SMTP_TO": "a@b.c", "SMTP_PORT": "587", "SMTP_SSL": "0",
    })
    assert cfg["port"] == 587
    assert cfg["use_ssl"] is False


def test_read_log_tail_missing_file_returns_empty(tmp_path):
    """读日志失败不能抛异常，否则推送会把主流程带崩"""
    from utils import notify

    assert notify.read_log_tail(tmp_path / "nope.log") == ""
    assert notify.read_log_tail(None) == ""


def test_read_log_tail_keeps_only_tail(tmp_path):
    from utils import notify

    p = tmp_path / "run.log"
    p.write_text("\n".join(f"line{i}" for i in range(100)), encoding="utf-8")
    tail = notify.read_log_tail(p, tail_lines=5)
    assert "line99" in tail
    assert "line0" not in tail
    assert len(tail.splitlines()) == 5


def test_build_body_tolerates_empty_log():
    from utils import notify

    body = notify.build_body(2, "")
    assert "退出码：2" in body
    assert "未取到日志内容" in body


def test_main_does_not_notify_on_success(monkeypatch, _stub_main_deps):
    """成功运行不应触发任何推送"""
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])

    calls = []
    import utils.notify as notify_module

    monkeypatch.setattr(notify_module, "notify_failure", lambda *a, **k: calls.append(a))

    assert main_module.main() == 0
    assert calls == [], "成功时不应推送"


def test_main_notifies_on_failure_and_keeps_exit_code(monkeypatch, _stub_main_deps):
    """失败时要推送，且推送不能改变退出码"""
    _stub_main_deps["run_results"] = [False]
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])

    seen = []
    import utils.notify as notify_module

    monkeypatch.setattr(
        notify_module, "notify_failure",
        lambda code, log_text="", logger=print: seen.append(code) or True,
    )

    assert main_module.main() == 3
    assert seen == [3], "失败必须按退出码推送一次"


def test_main_notify_exception_does_not_change_exit_code(monkeypatch, _stub_main_deps):
    """推送自身崩溃时，退出码必须保持原样（否则计划任务会误报成功/失败）"""
    _stub_main_deps["run_results"] = [False]
    _fake_playwright(monkeypatch)
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])

    import utils.notify as notify_module

    def boom(*a, **k):
        raise RuntimeError("推送模块炸了")

    monkeypatch.setattr(notify_module, "notify_failure", boom)

    assert main_module.main() == 3


def test_resolve_log_path_prefers_env_var(monkeypatch, tmp_path):
    """start.bat 传入的确切日志路径优先，保证附带的是本次日志"""
    target = tmp_path / "run_x.log"
    monkeypatch.setenv("BING_REWARDS_LOG_FILE", str(target))
    assert str(main_module._resolve_log_path()) == str(target)


def test_env_example_documents_notify_keys():
    """示例配置必须包含所有推送相关键，否则用户不知道要配什么"""
    from utils import notify

    env_example = main_module.PROJECT_ROOT / ".env.example"
    if not env_example.exists():
        pytest.skip(".env.example 尚未创建")
    text = env_example.read_text(encoding="utf-8")
    for key in notify.ENV_KEYS:
        assert key in text, f"{key} 未在 .env.example 中说明"


def test_env_is_gitignored():
    """凭据绝不能进版本库"""
    gitignore = (main_module.PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore


def test_notify_selftest_reports_missing_config(monkeypatch, capsys):
    """未配置时自检要给出可操作的提示，而不是抛异常"""
    from utils import notify

    monkeypatch.setattr(notify, "load_smtp_config", lambda *a, **k: None)
    assert notify._selftest() == 1
    assert "未配置 SMTP" in capsys.readouterr().out


def test_notify_selftest_sends_and_reports_success(monkeypatch, capsys):
    from utils import notify

    monkeypatch.setattr(
        notify, "load_smtp_config",
        lambda *a, **k: {"host": "h", "port": 465, "user": "u", "password": "p",
                         "to": ["a@b.c"], "sender": "u", "use_ssl": True},
    )
    monkeypatch.setattr(notify, "send_mail", lambda *a, **k: None)

    assert notify._selftest() == 0
    assert "发送成功" in capsys.readouterr().out


def test_notify_selftest_reports_send_failure(monkeypatch, capsys):
    from utils import notify

    monkeypatch.setattr(
        notify, "load_smtp_config",
        lambda *a, **k: {"host": "h", "port": 465, "user": "u", "password": "p",
                         "to": ["a@b.c"], "sender": "u", "use_ssl": True},
    )

    def boom(*a, **k):
        raise smtplib.SMTPAuthenticationError(535, b"bad")

    monkeypatch.setattr(notify, "send_mail", boom)
    assert notify._selftest() == 1
    assert "发送失败" in capsys.readouterr().out


def test_start_bat_passes_log_path_to_python():
    """start.bat 必须把本次日志路径传给 main.py，推送才能附带正确日志"""
    text = (main_module.PROJECT_ROOT / "start.bat").read_text(encoding="ascii")
    assert "BING_REWARDS_LOG_FILE" in text


# ======================== 启动脚本静态约束 ========================
# - .bat 必须纯 ASCII（cmd.exe 用 OEM 代码页解析，UTF-8 中文会吞掉换行）
# - 不能出现无上限的 pause（隐藏窗口启动时会永久挂住）
# - 自动任务必须经 cmd.exe 包装（直接 ShellExecute .bat 会弹安全警告）

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_bytes(name):
    return (PROJECT_ROOT / name).read_bytes()


def test_start_bat_is_ascii_only():
    raw = _read_bytes("start.bat")
    bad = [b for b in raw if b > 127]
    assert bad == [], f"start.bat 含 {len(bad)} 个非 ASCII 字节，cmd.exe 会解析错乱"


def test_install_autostart_bat_is_ascii_only():
    raw = _read_bytes("install_autostart.bat")
    bad = [b for b in raw if b > 127]
    assert bad == [], f"install_autostart.bat 含 {len(bad)} 个非 ASCII 字节"


def test_start_bat_pause_is_confined_to_bounded_wait():
    """pause 只能出现在 :wait_bounded 子过程里，那里有 choice /t 兜底超时。

    直接跟在校验失败后面写 pause，在隐藏窗口（计划任务/开机自启）里会永久
    挂住，而任务策略是 IgnoreNew，一次挂住就会吃掉之后所有天的运行机会。
    """
    lines = (PROJECT_ROOT / "start.bat").read_text(encoding="ascii").splitlines()
    label = next(
        (i for i, line in enumerate(lines) if line.strip() == ":wait_bounded"),
        None,
    )
    assert label is not None, "start.bat 缺少 :wait_bounded 有界等待子过程"

    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.lower() == "rem" or line.lower().startswith("rem "):
            continue  # 注释里出现 pause 不算
        # 括号当分隔符，取出真正的命令词，避免子串误判
        words = line.replace("(", " ").replace(")", " ").split()
        if "pause" in words:
            assert index > label, f"pause 出现在 :wait_bounded 之前: {raw!r}"

    tail = "\n".join(lines[label:])
    assert "choice" in tail and "/t " in tail, ":wait_bounded 缺少 choice /t 兜底超时"


def test_start_bat_waits_only_when_interactive():
    text = (PROJECT_ROOT / "start.bat").read_text(encoding="ascii")
    # 交互判定必须能被 --task / 环境变量强制关闭
    assert "BING_REWARDS_NONINTERACTIVE" in text
    assert '"%~1"=="--task"' in text
    # 等待必须走 :wait_bounded 子过程
    assert "call :wait_bounded" in text


def test_install_autostart_uses_cmd_wrapper():
    """自启必须经 cmd.exe，并且带 --task，否则隐藏窗口里既会弹窗又会挂住。"""
    text = (PROJECT_ROOT / "install_autostart.bat").read_text(encoding="ascii")
    assert "cmd.exe /c" in text
    assert "--task" in text


def test_create_context_passes_edge_executable_path(monkeypatch):
    """隔离 profile 分支仍需把本机 Edge 路径传给 Playwright。"""
    captured = {}
    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return "ctx"
    class FakePlaywright:
        chromium = FakeChromium()
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", main_module.USER_DATA_DIR)
    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)
    assert main_module.create_context(FakePlaywright(), False) == "ctx"
    assert captured["channel"] == "msedge"
    assert captured["executable_path"].endswith("msedge.exe")


def test_real_edge_profile_is_never_auto_killed(monkeypatch, tmp_path):
    import main as main_module
    real = tmp_path / "real-edge-profile"
    real.mkdir()
    calls = []
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", real)
    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    def fake_ensure(path, auto_kill=True):
        calls.append(bool(auto_kill))
        return False
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", fake_ensure)
    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            raise AssertionError("must not launch while real profile is busy")
    class FakePlaywright:
        chromium = FakeChromium()
    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), False)
    assert calls == [False]


def test_launch_persistent_rejects_non_edge():
    with pytest.raises(SystemExit):
        main_module.launch_persistent(type("P", (), {"chromium": object()})(), Path("D:/p"), False, False)


def test_create_context_rejects_default_subdirectory_as_profile(monkeypatch, tmp_path):
    import main as main_module
    default_dir = tmp_path / "User Data" / "Default"
    default_dir.mkdir(parents=True)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", default_dir)
    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    class FakeChromium:
        def launch_persistent_context(self, **kwargs):
            raise AssertionError("must reject Default subdirectory")
    class FakePlaywright:
        chromium = FakeChromium()
    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), False)


def test_create_context_rejects_profile_subdirectory(monkeypatch, tmp_path):
    import main as main_module
    profile_dir = tmp_path / "User Data" / "Profile 1"
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", profile_dir)
    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    class FakePlaywright:
        class chromium:
            @staticmethod
            def launch_persistent_context(**kwargs):
                raise AssertionError("must reject Profile N")
    with pytest.raises(SystemExit):
        main_module.create_context(FakePlaywright(), False)


def test_real_edge_profile_uses_cdp_bridge(monkeypatch, tmp_path):
    """正式 profile 必须走正常 Edge 启动 + CDP，不得再用 Playwright 抢 profile。"""
    import ctypes

    real = tmp_path / "User Data"
    real.mkdir()
    launched = {}
    monkeypatch.setattr(main_module, "EDGE_USER_DATA_DIR", real)
    monkeypatch.setattr(main_module.profile, "find_edge_executable", lambda: Path("D:/edge/msedge.exe"))
    monkeypatch.setattr(main_module.profile, "ensure_profile_available", lambda *a, **k: True)
    monkeypatch.setattr(main_module, "_free_loopback_port", lambda: 9333)
    monkeypatch.setattr(main_module, "_wait_cdp", lambda endpoint, timeout=30: True)

    def fake_short_path(path, buf, size):
        buf.value = str(path)
        return 1

    monkeypatch.setattr(ctypes.windll.kernel32, "GetShortPathNameW", fake_short_path)

    class Process:
        def wait(self, timeout=None):
            return 0

    def fake_popen(args, **kwargs):
        launched["args"] = args
        launched["kwargs"] = kwargs
        return Process()

    class Browser:
        contexts = [object()]

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, endpoint, timeout=30000):
            launched["endpoint"] = endpoint
            return Browser()

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(main_module.subprocess, "Popen", fake_popen)
    context = main_module.create_context(FakePlaywright(), False)
    assert launched["endpoint"] == "http://127.0.0.1:9333"
    assert "--remote-debugging-port=9333" in launched["args"]
    assert any(arg.startswith("--user-data-dir=") for arg in launched["args"])
    assert context is not None
