"""utils/login.py 登录流程测试：凭据选择页跳转与导航重试"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import login  # noqa: E402


class FakeLocator:
    def __init__(self, visible=True, count=1, click_ok=True):
        self._visible = visible
        self._count = count
        self._click_ok = click_ok
        self.typed = ""

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return True

    def wait_for(self, **kwargs):
        if not self._visible:
            raise TimeoutError("not visible")

    def click(self):
        if not self._click_ok:
            raise RuntimeError("click failed")

    def fill(self, value):
        self.typed = value

    def type(self, value, delay=0):
        self.typed += value

    def press(self, key):
        pass


class FakePage:
    """按选择器返回可见性的假页面。

    visible_map   : {选择器: 是否可见}
    goto_fail_times: 前 N 次 goto 抛出 HTTP/2 错误后成功后落到登录页
    """

    def __init__(self, visible_map=None, goto_fail_times=0):
        self.visible_map = visible_map or {}
        self.goto_fail_times = goto_fail_times
        self.goto_calls = []
        self.clicked = []
        self.waits = []
        self.submitted = False
        self.url = "https://rewards.bing.com/"

    def locator(self, sel):
        if self.visible_map.get(sel):
            return FakeLocator(visible=True)
        return FakeLocator(visible=False, count=0)

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)
        if len(self.goto_calls) <= self.goto_fail_times:
            self.url = "chrome-error://chromewebdata/"
            raise RuntimeError("net::ERR_HTTP2_PROTOCOL_ERROR")
        self.url = "https://login.live.com/"

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def wait_for_url(self, predicate, timeout=0):
        """提交后视为登录完成，离开 login.live.com"""
        if self.submitted:
            self.url = "https://www.bing.com/"
            return
        raise TimeoutError("still on login page")

    def wait_for_load_state(self, *a, **k):
        pass


def _install_tracker(page, monkeypatch):
    """记录 _click_first 实际匹配到的那个选择器（不是整组的第一项）"""
    real = login._click_first

    def tracked(pg, selectors):
        matched = None
        for sel in selectors:
            loc = login._first_visible(pg, (sel,))
            if loc is not None:
                matched = sel
                break
        ok = real(pg, selectors)
        if ok and matched:
            page.clicked.append(matched)
        return ok

    monkeypatch.setattr(login, "_click_first", tracked)


def _make_email_flow_page(visible_map):
    """构造一个"已看到邮箱框、点下一步即登录成功"的页面"""
    page = FakePage(visible_map)
    real_locator = page.locator

    def locator(sel):
        loc = real_locator(sel)
        if sel == "#idSIButton9" and loc.count() > 0:
            loc.click = lambda: setattr(page, "submitted", True)
        return loc

    page.locator = locator
    return page


class _SwitchPage(FakePage):
    """落在凭据选择页：必须先点切换入口，邮箱框才会出现。

    只暴露 switch_selector 这一个入口，从而能区分究竟命中了哪一组候选。
    """

    def __init__(self, switch_selector):
        super().__init__({})
        self.switch_selector = switch_selector
        self.url = "https://login.live.com/"
        self.switched = False
        self.submitted = False

    def locator(self, sel):
        if sel in login.EMAIL_INPUT_SELECTORS and self.switched:
            return FakeLocator(visible=True)
        if sel == self.switch_selector and not self.switched:
            return FakeLocator(visible=True)
        if sel == "#idSIButton9" and self.switched:
            loc = FakeLocator(visible=True)
            loc.click = lambda: setattr(self, "submitted", True)
            return loc
        return FakeLocator(visible=False, count=0)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        # 点击切换入口后页面跳转，邮箱框随后渲染
        if self.clicked:
            self.switched = True


# ---------- 常量与选择器 ----------


def test_email_input_selectors_cover_legacy_and_new_ids():
    """邮箱框选择器必须覆盖新版 id 与旧版 name，缺一就会误判未登录页"""
    joined = " ".join(login.EMAIL_INPUT_SELECTORS)
    assert "#usernameEntry" in joined
    assert "loginfmt" in joined
    assert "type='email'" in joined


def test_password_tile_selectors_include_chinese_and_english():
    """凭据选择页上必须同时识别中英文"使用密码"，否则一种界面语言就会卡住"""
    joined = " ".join(login.PASSWORD_TILE_SELECTORS)
    assert "使用密码" in joined
    assert "Use your password" in joined


def test_alt_login_selectors_include_switch_ids():
    """除文案外还需保留 id 选择器，防止文案改版后彻底失效"""
    joined = " ".join(login.ALT_LOGIN_SELECTORS)
    assert "idA_PWD_SwitchToCredPicker" in joined
    assert "使用另一种方式登录" in joined


# ---------- _wait_for_email_input ----------


def test_wait_for_email_input_returns_locator_when_present():
    page = FakePage({"#usernameEntry": True})
    assert login._wait_for_email_input(page, timeout_ms=10) is not None


def test_wait_for_email_input_returns_none_when_absent():
    """登录页没渲染出邮箱框时应返回 None，而不是抛异常中断整个流程"""
    assert login._wait_for_email_input(FakePage({}), timeout_ms=10) is None


# ---------- _first_visible / _click_first ----------


def test_first_visible_skips_invisible_and_picks_visible():
    page = FakePage({"input[type='email']": True})
    assert login._first_visible(page, ("#usernameEntry", "input[type='email']")) is not None


def test_first_visible_returns_none_when_nothing_matches():
    assert login._first_visible(FakePage({}), ("#nope",)) is None


def test_click_first_returns_false_when_absent():
    assert login._click_first(FakePage({}), login.PASSWORD_TILE_SELECTORS) is False


def test_click_first_swallows_click_exception():
    """点击失败不能被当成已点击，否则后续会误判登录流程已推进"""

    class P(FakePage):
        def locator(self, sel):
            return FakeLocator(visible=True, click_ok=False)

    assert login._click_first(P({}), ("#x",)) is False


# ---------- do_login：凭据选择页 ----------


def test_do_login_succeeds_when_email_box_present(monkeypatch):
    """正常情况：邮箱框直接可见，不应去点任何切换登录方式的入口"""
    page = _make_email_flow_page({"#usernameEntry": True, "#idSIButton9": True})
    page.url = "https://login.live.com/"
    _install_tracker(page, monkeypatch)

    assert login.do_login(page, "u@outlook.com", "pw") is True
    assert page.clicked == [], "邮箱框已在时不应该点击切换密码的入口"


def test_do_login_clicks_password_tile_on_credential_picker(monkeypatch):
    """核心回归：设备上有 passkey 时登录页直接落在凭据选择页，
    必须先点"使用密码"，点击后邮箱框才会出现。"""
    page = _SwitchPage('[aria-label="使用密码"]')
    _install_tracker(page, monkeypatch)

    assert login.do_login(page, "u@outlook.com", "pw") is True
    assert page.clicked, "落在凭据选择页时必须点击'使用密码'才能继续"
    assert page.clicked[0] == '[aria-label="使用密码"]'
    assert page.switched, "点击后应已切到邮箱/密码输入阶段"


def test_do_login_falls_back_to_alt_login_entry(monkeypatch):
    """没有"使用密码"时，应退一步点"使用另一种方式登录"入口"""
    page = _SwitchPage("a:has-text('使用另一种方式登录')")
    _install_tracker(page, monkeypatch)

    assert login.do_login(page, "u@outlook.com", "pw") is True
    assert page.clicked
    assert page.clicked[0] == "a:has-text('使用另一种方式登录')"


def test_do_login_returns_false_when_no_email_box_and_no_tiles():
    """既没有邮箱框也没有任何切换入口时才失败，且必须返回 False"""
    page = FakePage({})
    page.url = "https://login.live.com/"

    assert login.do_login(page, "u@outlook.com", "pw") is False


def test_do_login_reports_current_url_when_email_box_missing(capsys):
    """失败时要输出当前 URL，方便排查到底停在哪一步"""
    page = FakePage({})
    page.url = "https://login.live.com/somewhere"
    login.do_login(page, "u@outlook.com", "pw")

    out = capsys.readouterr().out
    assert "未找到邮箱输入框" in out
    assert "login.live.com/somewhere" in out


# ---------- do_login：导航重试 ----------


def test_do_login_retries_navigation_on_http2_error(monkeypatch):
    """网络间歇性 ERR_HTTP2_PROTOCOL_ERROR 时应重试导航，而不是直接放弃"""
    page = _make_email_flow_page({"#usernameEntry": True, "#idSIButton9": True})
    page.goto_fail_times = 2
    page.url = "https://rewards.bing.com/"

    assert login.do_login(page, "u@outlook.com", "pw") is True
    assert len(page.goto_calls) == 3, f"应重试到第3次成功，实际={len(page.goto_calls)}"


def test_do_login_gives_up_after_navigation_retries():
    """连续失败时不能无限重试，必须收敛并返回 False"""
    page = FakePage({}, goto_fail_times=99)
    page.url = "https://rewards.bing.com/"

    assert login.do_login(page, "u@outlook.com", "pw") is False
    assert len(page.goto_calls) == 3, "重试次数应封顶为 3 次"


def test_do_login_skips_navigation_when_already_on_login_page():
    """已经在登录页时不应重复导航"""
    page = _make_email_flow_page({"#usernameEntry": True, "#idSIButton9": True})
    page.url = "https://login.live.com/login.srf"

    assert login.do_login(page, "u@outlook.com", "pw") is True
    assert page.goto_calls == [], "已在登录页时不应再 goto"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


class _IdentityPage:
    url = "https://rewards.bing.com/dashboard"

    def __init__(self, html):
        self.html = html

    def content(self):
        return self.html

    def evaluate(self, *_args, **_kwargs):
        return None


# 脱敏的 Rewards RSC 账号块：没有 signInName，只能从转义后的
# className="pii truncate" 与 children 结构中读取合成邮箱。
RSC_IDENTITY_HTML = (
    '<script>self.__next_f.push([1,"'
    '\\"className\\":\\"pii truncate\\",'
    '\\"children\\":\\"rewards.user@example.com\\"'
    '"])</script>'
)


def test_get_logged_in_identity_reads_sign_in_name():
    page = _IdentityPage('<script>{"signInName":"User.Name@example.com"}</script>')
    assert login.get_logged_in_identity(page) == "user.name@example.com"


def test_get_logged_in_identity_reads_escaped_rsc_account_block():
    """signInName 缺失时，应能从真实 RSC 转义账号块中提取合成邮箱。"""
    page = _IdentityPage(RSC_IDENTITY_HTML)
    assert login.get_logged_in_identity(page) == "rewards.user@example.com"


def test_wait_for_identity_parses_rsc_after_initial_empty_html():
    """RSC 账号块异步写入 DOM：首次 HTML 无身份，后续 HTML 应走真实解析路径。"""
    class AsyncRscPage(_IdentityPage):
        def __init__(self):
            super().__init__("<div>loading</div>")
            self.waits = []

        def wait_for_timeout(self, ms):
            self.waits.append(ms)
            self.html = RSC_IDENTITY_HTML

    page = AsyncRscPage()

    assert login._wait_for_logged_in_identity(page, timeout_ms=1000, interval_ms=100) == "rewards.user@example.com"
    assert page.waits == [100]


def test_wait_for_identity_returns_none_after_timeout(monkeypatch):
    """身份始终不出现时应有上限地收敛，不能无限轮询。"""
    class ReadableEmptyPage(_IdentityPage):
        def __init__(self):
            super().__init__("<div>loading</div>")
            self.waits = []
            self.content_calls = 0

        def content(self):
            self.content_calls += 1
            return self.html

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = ReadableEmptyPage()
    times = iter([0.0, 0.4, 0.9, 1.1])
    monkeypatch.setattr(login.time, "monotonic", lambda: next(times))

    assert login._wait_for_logged_in_identity(page, timeout_ms=1000, interval_ms=500) is None
    assert page.content_calls == 3
    assert page.waits == [500, 100]


def test_wait_for_identity_stops_immediately_when_page_closed():
    """页面已关闭时不应继续高频读取内容，应立即安全返回。"""
    class ClosedPage(_IdentityPage):
        def __init__(self):
            super().__init__("<div>loading</div>")
            self.waits = []
            self.content_calls = 0

        def is_closed(self):
            return True

        def content(self):
            self.content_calls += 1
            return self.html

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = ClosedPage()

    assert login._wait_for_logged_in_identity(page, timeout_ms=1000, interval_ms=100) is None
    assert page.content_calls == 0
    assert page.waits == []


def test_wait_for_identity_backs_off_and_caps_read_errors(monkeypatch):
    """连续读取异常应按退避间隔重试，并在有限次数后返回，不能耗尽整个超时窗口。"""
    class ReadErrorPage(_IdentityPage):
        def __init__(self):
            super().__init__("<div>loading</div>")
            self.waits = []
            self.content_calls = 0

        def is_closed(self):
            return False

        def content(self):
            self.content_calls += 1
            raise RuntimeError("page context unavailable")

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = ReadErrorPage()
    monkeypatch.setattr(login, "IDENTITY_MAX_CONSECUTIVE_READ_ERRORS", 3, raising=False)
    monkeypatch.setattr(login, "IDENTITY_READ_ERROR_BACKOFF_MS", 1000, raising=False)

    assert login._wait_for_logged_in_identity(page, timeout_ms=15000, interval_ms=500) is None
    assert page.content_calls == 3
    assert page.waits == [1000, 1000]


def test_wait_for_identity_resets_error_count_after_readable_empty(monkeypatch):
    """交替出现读取异常和可读空页面时，连续异常计数应重置，最终由总超时收敛。"""
    class AlternatingReadPage(_IdentityPage):
        def __init__(self):
            super().__init__("<div>loading</div>")
            self.waits = []
            self.content_calls = 0

        def is_closed(self):
            return False

        def content(self):
            self.content_calls += 1
            if self.content_calls % 2 == 1:
                raise RuntimeError("intermittent read failure")
            return self.html

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = AlternatingReadPage()
    times = iter([0.0, 0.6, 1.2, 1.8, 2.4, 3.1])
    monkeypatch.setattr(login, "IDENTITY_MAX_CONSECUTIVE_READ_ERRORS", 2, raising=False)
    monkeypatch.setattr(login, "IDENTITY_READ_ERROR_BACKOFF_MS", 1000, raising=False)
    monkeypatch.setattr(login.time, "monotonic", lambda: next(times))

    assert login._wait_for_logged_in_identity(page, timeout_ms=3000, interval_ms=500) is None
    assert page.content_calls == 5
    assert page.waits == [1000, 500, 1000, 500]


def test_ensure_account_logs_identity_failure_context(monkeypatch, capsys):
    """身份缺失时应输出 URL、标题和登录控件状态，便于区分时序与登录页。"""
    page = _IdentityPage("<div>no identity</div>")
    page.title = lambda: "首页 – Microsoft Rewards"
    monkeypatch.setattr(login, "_wait_for_logged_in_identity", lambda _page: None)
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not login")))

    assert login.ensure_account(page, "target@example.com", "pw") is False
    out = capsys.readouterr().out
    assert "无法读取当前账号身份" in out
    assert "rewards.bing.com/dashboard" in out
    assert "首页 – Microsoft Rewards" in out
    assert "登录控件=未出现" in out


def test_ensure_account_redacts_sensitive_url_parts_in_failure_context(monkeypatch, capsys):
    """失败诊断只保留域名和路径，不能把 OAuth 查询参数或片段写进日志。"""
    page = _IdentityPage("<div>no identity</div>")
    page.url = (
        "https://rewards.bing.com/dashboard?"
        "code=oauth-code-secret&state=state-secret&session=session-secret"
        "#fragment-secret"
    )
    monkeypatch.setattr(login, "_wait_for_logged_in_identity", lambda _page: None)
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not login")))

    assert login.ensure_account(page, "target@example.com", "pw") is False
    out = capsys.readouterr().out
    assert "https://rewards.bing.com/dashboard" in out
    for sensitive in ("oauth-code-secret", "state-secret", "session-secret", "fragment-secret"):
        assert sensitive not in out


@pytest.mark.parametrize(
    "diagnostic_url",
    (
        "/dashboard?code=relative-code-secret&state=relative-state-secret",
        "javascript:alert('session-secret')",
        "http://[broken?code=parse-error-secret",
    ),
)
def test_ensure_account_fails_closed_for_unsafe_diagnostic_urls(monkeypatch, capsys, diagnostic_url):
    """相对 URL、非 HTTP(S) 和解析异常 URL 应安全降级，不能中断失败处理。"""
    page = _IdentityPage("<div>no identity</div>")
    page.url = diagnostic_url
    monkeypatch.setattr(login, "_wait_for_logged_in_identity", lambda _page: None)
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not login")))

    assert login.ensure_account(page, "target@example.com", "pw") is False
    out = capsys.readouterr().out
    assert "无法读取当前账号身份" in out
    assert "URL=(无效)" in out
    assert "secret" not in out


def test_ensure_account_fails_closed_when_failure_context_raises(monkeypatch, capsys):
    """诊断上下文采集自身失败时，仍应安全返回 False，不中断账号流程。"""
    page = _IdentityPage("<div>no identity</div>")
    monkeypatch.setattr(login, "_wait_for_logged_in_identity", lambda _page: None)
    monkeypatch.setattr(
        login,
        "_identity_failure_context",
        lambda _page: (_ for _ in ()).throw(RuntimeError("context unavailable")),
    )
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not login")))

    assert login.ensure_account(page, "target@example.com", "pw") is False
    out = capsys.readouterr().out
    assert "无法读取当前账号身份" in out


def test_ensure_account_accepts_matching_identity(monkeypatch):
    page = _IdentityPage('<script>{"signInName":"User.Name@example.com"}</script>')
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not login")))
    assert login.ensure_account(page, "user.name@example.com", "pw") is True


def test_ensure_account_switches_mismatched_identity(monkeypatch):
    page = _IdentityPage('<script>{"signInName":"other@example.com"}</script>')
    visited = []
    def goto(url, **_):
        visited.append(url)
        page.url = "https://login.live.com/"
    monkeypatch.setattr(page, "goto", goto, raising=False)
    monkeypatch.setattr(page, "wait_for_timeout", lambda *_: None, raising=False)
    def fake_login(pg, u, p):
        pg.html = '<script>{"signInName":"target@example.com"}</script>'
        return True
    monkeypatch.setattr(login, "do_login", fake_login)
    assert login.ensure_account(page, "target@example.com", "pw") is True
    assert "logout.srf" in visited[0]


def test_ensure_account_fails_closed_when_identity_missing(monkeypatch):
    page = _IdentityPage("<div>no identity</div>")
    monkeypatch.setattr(login, "do_login", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not login")))
    assert login.ensure_account(page, "target@example.com", "pw") is False


def test_get_logged_in_identity_ignores_unrelated_email():
    page = _IdentityPage('<div>support@example.com</div>')
    assert login.get_logged_in_identity(page) is None
