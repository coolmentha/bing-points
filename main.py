"""
Bing Rewards 自动化 — Playwright 版
基于 2026-05 Rewards 新版 dashboard (Next.js RSC) 重写

编排入口：浏览器启动 + 多账号循环，具体功能见 utils/ 各模块
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from utils import profile
from utils.accounts import load_accounts
from utils.clients import (
    list_android_devices,
    mobile_checkin,
    REWARDS_DESKTOP_IDENTITY_MISMATCH,
    REWARDS_DESKTOP_IDENTITY_UNVERIFIED,
    REWARDS_DESKTOP_TASK_ITEM_MISSING,
    REWARDS_DESKTOP_UIA_UNAVAILABLE,
    rewards_desktop_state,
    run_rewards_desktop_task,
)
from utils.browser import (
    BrowserRiskControlError,
    SearchOutcome,
    SearchStatus,
    do_searches,
    safe_goto,
)
from utils.config import (
    BING_HOME,
    BROWSER_ENGINE,
    DEFAULT_MOBILE_SEARCHES,
    DEFAULT_PC_SEARCHES,
    EDGE_USER_DATA_DIR,
    ENABLE_MOBILE_CLIENT,
    MOBILE_PROFILE_DIR,
    MOBILE_USER_AGENT,
    PROJECT_ROOT,
    REWARDS_DASHBOARD,
    TEMP_DIR,
    USER_DATA_DIR,
)
from utils.dashboard import parse_dashboard_data
from utils.login import do_login, ensure_account
from utils.tasks import (
    RewardsRiskControlError,
    RiskDetectionError,
    claim_available_points,
    detect_risk_control,
    do_daily_offer_tasks,
    do_daily_set_tasks,
    do_dashboard_activities,
    do_edge_browsing_streak_detail,
    EdgeStreakStatus,
    valid_edge_progress,
    do_mobile_app_checkin,
    do_point_punch_cards,
    read_streak_progress,
)
from utils.trends import ensure_keywords, get_baidu_trends, get_zhihu_trends, get_weibo_trends


# ======================== 浏览器启动 ========================

PROFILE_BUSY_HINTS = (
    "ProcessSingleton",
    "already in use",
    "profile is already in use",
    "being used by another",
)
EDGE_MISSING_HINTS = (
    "Executable doesn't exist",
    "is not found at",
    "Failed to launch",
)
TEMP_DIR_ERROR_HINTS = (
    "mkdtemp",
    "EPERM",
    "playwright-artifacts",
)
# 会话0（计划任务"不管用户是否登录都运行"）里浏览器偶发拿不到窗口站，
# 启动瞬间就 FATAL 退出。同一配置重试通常即可成功，属于抖动而非配置错误。
MESSAGE_WINDOW_HINTS = (
    "message-only window",
    "Failed to create message-only Window",
)


def _matches(message: str, hints) -> bool:
    return any(hint.lower() in message.lower() for hint in hints)


def _profile_busy_help(user_data_dir, real_profile: bool = False) -> None:
    print(f"!! 浏览器 profile 被其它实例占用: {user_data_dir}")
    if real_profile:
        print("   这是正式 Edge User Data：脚本不会自动结束 Edge，请手动关闭所有 Edge 窗口后重试。")
        return
    print("   常见原因：开机自启的实例（start.bat）仍在运行，或上次运行残留了 Edge 进程。")
    print("   排查/清理：python -m utils.profile --check")
    print("              python -m utils.profile --kill")
    print("   若确认不需要开机自启，可运行 remove_autostart.bat 后重试。")


def launch_persistent(p, user_data_dir, headless: bool, use_edge: bool, **extra):
    """启动持久化上下文；use_edge=False 被禁止，防止静默启动 Chromium。"""
    if not use_edge:
        raise SystemExit("仅允许 Edge 启动，已禁止 Chromium 回退")
    kwargs = dict(
        user_data_dir=str(user_data_dir),
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        locale="zh-CN",
    )
    kwargs["channel"] = "msedge"
    kwargs.update(extra)
    return p.chromium.launch_persistent_context(**kwargs)


def _log_session() -> str:
    """记录当前会话号：0=服务会话(无窗口站)，非0=用户桌面会话。

    计划任务「不管用户是否登录都运行」(S4U) 会落到会话0，浏览器可能因拿不到
    窗口站而启动失败；把会话号写进日志，排查时一眼就能看出跑在哪种会话里。
    """
    session_id = None
    session_name = os.environ.get("SESSIONNAME") or "(空)"
    try:
        import ctypes

        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            session_id = sid.value
    except Exception:
        pass
    kind = "服务会话(无窗口站)" if session_id == 0 else "用户桌面会话"
    return f"会话号={session_id}({kind}) SESSIONNAME={session_name}"


class _EdgeCDPContext:
    """把正常启动的 Edge 会话包装成现有流程需要的 BrowserContext。"""

    def __init__(self, browser, context, process):
        self._browser = browser
        self._context = context
        self._process = process

    def __getattr__(self, name):
        return getattr(self._context, name)

    def close(self):
        # 只关闭本次由脚本启动的浏览器，保持真实窗口态。
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            # CDP 断开不保证 Edge 主进程退出；只终止本次 Popen 启动的进程。
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._process.kill()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_cdp(endpoint: str, timeout: float = 30.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(endpoint + "/json/version", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _launch_real_edge_cdp(p, user_data_dir, headless: bool):
    """正常启动正式 Edge，再通过本地 CDP 连接，避免 ProcessSingleton 冲突。"""
    edge_path = profile.find_edge_executable()
    if edge_path is None:
        print("!! 未找到本机 Edge (msedge)")
        raise SystemExit(2)
    port = _free_loopback_port()
    endpoint = f"http://127.0.0.1:{port}"
    # Edge 的 user-data-dir 参数不能含空格，否则它会把路径截断。
    # 使用 Windows 8.3 短路径可保留同一个正式 User Data，同时让 Edge 正常解析。
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(user_data_dir), buffer, len(buffer)):
        print("!! 无法取得正式 Edge User Data 的短路径")
        raise SystemExit(2)
    profile_arg = buffer.value
    args = [
        str(edge_path),
        f"--user-data-dir={profile_arg}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--disable-session-crashed-bubble",
    ]
    if headless:
        args.append("--headless=new")
    args.append(BING_HOME)
    try:
        process = subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"!! 无法启动正式 Edge: {exc}")
        raise SystemExit(2) from exc
    if not _wait_cdp(endpoint):
        print("!! 正式 Edge 已启动但本地调试端口不可用")
        # 只清理本次 Popen 启动、且仍在运行的 Edge；不碰用户已有实例。
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        raise SystemExit(2)
    try:
        browser = p.chromium.connect_over_cdp(endpoint, timeout=30000)
        contexts = browser.contexts
        if not contexts:
            browser.close()
            raise RuntimeError("Edge 未返回可用的浏览器上下文")
        print("  Edge 正式 profile 已通过本地 CDP 接入")
        return _EdgeCDPContext(browser, contexts[0], process)
    except Exception as exc:
        print(f"!! 连接正式 Edge 调试端口失败: {str(exc).splitlines()[0]}")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        raise SystemExit(2) from exc


def create_context(p, headless: bool):
    """启动 Edge 上下文；正式 profile 走正常启动 + CDP 接入。"""
    edge_path = profile.find_edge_executable()
    if BROWSER_ENGINE != "edge":
        print(f"!! 不支持的浏览器配置: {BROWSER_ENGINE}（只允许 edge）")
        raise SystemExit(2)
    if edge_path is None:
        print("!! 未找到本机 Edge (msedge)；已禁用 Chromium/Chrome 回退")
        raise SystemExit(2)
    user_data_dir = EDGE_USER_DATA_DIR
    print(f"  浏览器: Edge ({edge_path})")
    print(f"  Edge profile: {user_data_dir}")
    if user_data_dir == USER_DATA_DIR:
        print("  !! 当前是隔离 profile；Edge 30 分钟任务通常还需要正式 Edge 登录态")
        print("     可设置 BING_REWARDS_EDGE_USER_DATA_DIR 指向正式 Edge User Data")
    elif re.match(r"^(default|profile \d+)$", user_data_dir.name, re.I):
        print("  !! Edge profile 配置应指向 User Data 根目录，而不是其中的 Default/Profile N 子目录")
        raise SystemExit(2)

    is_real_edge_profile = user_data_dir.resolve() != USER_DATA_DIR.resolve()
    auto_kill = False if is_real_edge_profile else not os.getenv("BING_REWARDS_NO_PROFILE_KILL")
    if not profile.ensure_profile_available(user_data_dir, auto_kill=auto_kill):
        _profile_busy_help(user_data_dir, real_profile=is_real_edge_profile)
        raise SystemExit(2)

    if is_real_edge_profile:
        return _launch_real_edge_cdp(p, user_data_dir, headless)

    profile_retries = 1
    window_retries = 3  # 会话0抖动：同一命令重试几次通常能拿到窗口站
    while True:
        try:
            return launch_persistent(
                p, user_data_dir, headless, True,
                executable_path=str(edge_path),
                viewport={"width": 1280, "height": 800},
            )
        except PlaywrightError as exc:
            message = str(exc)
            if _matches(message, TEMP_DIR_ERROR_HINTS):
                print(f"!! Playwright 无法在临时目录创建临时文件夹: {TEMP_DIR}")
                print(f"   原始错误: {message.splitlines()[0]}")
                print("   请在 main.py 开头的 prepare_temp_dir 之前检查该目录可写性/安全软件拦截。")
                raise SystemExit(2) from exc
            if _matches(message, PROFILE_BUSY_HINTS) and profile_retries > 0:
                profile_retries -= 1
                if profile.ensure_profile_available(user_data_dir, auto_kill=auto_kill):
                    print("   profile 启动冲突，已清理残留浏览器进程，重试启动...")
                    continue
                _profile_busy_help(user_data_dir)
                print(f"   原始错误: {message.splitlines()[0]}")
                raise SystemExit(2) from exc
            if _matches(message, MESSAGE_WINDOW_HINTS) and window_retries > 0:
                window_retries -= 1
                print(f"  浏览器未拿到窗口站（会话0抖动），重试启动（剩余 {window_retries} 次）...")
                # 上一次失败可能留下半启动的浏览器进程，先清掉再重试
                profile.ensure_profile_available(user_data_dir, auto_kill=auto_kill)
                time.sleep(3)
                continue
            if _matches(message, EDGE_MISSING_HINTS):
                print("!! 无法启动本机 Edge；已禁用 Chromium 回退")
                raise SystemExit(2) from exc
            if window_retries == 0 and _matches(message, MESSAGE_WINDOW_HINTS):
                print("!! 多次重试后仍无法在会话0创建窗口，请改用'只在用户登录时运行'(Interactive)，")
                print("   或确认任务操作为 headless。原始错误:")
                print(f"   {message.splitlines()[0]}")
                raise SystemExit(2) from exc
            raise


# ======================== 主流程 ========================

def make_pc_score(page):
    """读取积分余额的回调，供 do_searches 校验搜索是否继续计分"""
    def _score():
        try:
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=2000)
            return parse_dashboard_data(page).get("total_points")
        except Exception:
            return None
    return _score


def run_mobile_searches(p, context, count: int, headless: bool):
    """独立移动上下文跑移动端搜索：复用桌面 cookie 免二次登录（Bing 会话不绑 UA）"""
    if not profile.ensure_profile_available(
        MOBILE_PROFILE_DIR, auto_kill=not os.getenv("BING_REWARDS_NO_PROFILE_KILL")
    ):
        print("  移动端 profile 被其它实例占用，跳过移动搜索")
        return
    edge_path = profile.find_edge_executable()
    if edge_path is None:
        print("  未找到 Edge，移动端搜索跳过（已禁用 Chromium 回退）")
        return
    try:
        mctx = launch_persistent(
            p, MOBILE_PROFILE_DIR, headless, True,
            executable_path=str(edge_path),
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            user_agent=MOBILE_USER_AGENT,
        )
    except PlaywrightError as exc:
        # 移动端搜索是附加项：启动失败不应把整个账号判为失败
        print(f"  移动端上下文启动失败，跳过移动搜索: {str(exc).splitlines()[0]}")
        return
    try:
        # 移动端 MS 登录页 DOM 不同且易触发风控，不走二次登录，直接注入桌面登录态
        mctx.add_cookies(context.cookies())
        page = mctx.pages[0] if mctx.pages else mctx.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        """)
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)
        if "login" in page.url.lower() or "welcome" in page.url.lower():
            print("  移动端登录态无效，跳过移动搜索")
            return
        keywords = ensure_keywords(get_baidu_trends(), get_zhihu_trends(), get_weibo_trends())
        do_searches(page, count, keywords, "Mobile", verify=make_pc_score(page))
    finally:
        mctx.close()


def run_account(p, context, username: str, password: str, headless: bool, total_accounts: int = 1) -> bool:
    """处理单个账号的完整流程，返回是否成功（失败不应被计划任务误判为成功）"""
    print(f"\n{'='*50}")
    print(f"处理账号: {username}")
    print(f"{'='*50}")

    page = context.new_page()

    try:
        # 1. 检查登录态
        page.goto(REWARDS_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        risk_reason = detect_risk_control(page)
        if risk_reason:
            raise RewardsRiskControlError(risk_reason)

        # 处理cookie弹窗
        try:
            accept = page.locator("button:has-text('接受')").first
            if accept.count() > 0 and accept.is_visible():
                accept.click()
                page.wait_for_timeout(1000)
        except Exception:
            pass

        url = page.url.lower()
        needs_login = "login" in url or "welcome" in url

        if not needs_login:
            # 正式 Edge profile 往往已登录；必须确认是目标账号，不能直接操作。
            if not ensure_account(page, username, password):
                print("  账号身份校验失败，跳过")
                return False
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=4000)

        if needs_login:
            # 如果是welcome页，点击登录
            if "welcome" in url:
                try:
                    login_link = page.locator("a:has-text('登录'):not(:has-text('创建'))").first
                    if login_link.count() > 0:
                        login_link.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

            if not do_login(page, username, password):
                risk_reason = detect_risk_control(page)
                if risk_reason:
                    raise RewardsRiskControlError(risk_reason)
                print("  登录失败，跳过此账号")
                return False

        # 2. 打开 dashboard
        print("  打开 Rewards 面板...")
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        # 再次确认不在登录页
        url = page.url.lower()
        if "login" in url or "welcome" in url:
            print("  仍未登录，再次尝试...")
            if not do_login(page, username, password):
                risk_reason = detect_risk_control(page)
                if risk_reason:
                    raise RewardsRiskControlError(risk_reason)
                print("  登录失败，跳过")
                return False
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        risk_reason = detect_risk_control(page)
        if risk_reason:
            raise RewardsRiskControlError(risk_reason)

        # 最终确认登录状态与账号身份；所有登录分支都必须复核。
        url = page.url.lower()
        if "login" in url or "welcome" in url:
            print("  登录未成功，可能需人工验证（2FA/验证码/密码错误），跳过")
            return False
        if not ensure_account(page, username, password):
            print("  无法确认当前登录的是目标账号，停止操作")
            return False
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=4000)

        # 3. 解析页面数据
        print("  解析页面数据...")
        data = parse_dashboard_data(page)
        print(f"    可领取积分: {data['claimable_points']}")
        print(f"    每日任务: {len(data['daily_set_items'])} 个, 已完成 {data.get('daily_activities_done', 0)}/{data.get('daily_activities_total', 3)}")
        print(f"    PC搜索: {data['pc_search_done']}/{data['pc_search_total']}")

        # 4. 先领取可领取积分
        print("\n  --- 领取积分 ---")
        claim_available_points(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 5. 每日任务(每日套餐: 测验/投票/探索)
        print("\n  --- 每日任务 ---")
        do_daily_set_tasks(page, data["daily_set_items"])

        # 6. 仪表板活动卡片(每日活动)
        print("\n  --- 每日活动 ---")
        do_dashboard_activities(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 6.5 日常任务(/earn 页搜索与每日拼图)
        print("\n  --- 日常任务 ---")
        do_daily_offer_tasks(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 6.7 限时积分活动：每次最多推进一个网页可完成任务
        print("\n  --- 限时积分活动 ---")
        do_point_punch_cards(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 7. 领取完成后的积分
        claim_available_points(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 8. 验证活动完成情况
        print("  验证完成情况...")
        data2 = parse_dashboard_data(page)
        daily_done = data2.get("daily_activities_done", 0)
        daily_total = data2.get("daily_activities_total", 3)
        print(f"    活动进度: {daily_done}/{daily_total}")
        daily_tasks_ok = not data2["daily_set_items"]
        daily_activities_ok = daily_done >= daily_total
        if not daily_tasks_ok:
            print(f"    仍有每日任务: {len(data2['daily_set_items'])} 个")
        if not daily_activities_ok:
            print(f"    每日活动未完成: {daily_done}/{daily_total}")

        # 9. PC 搜索 — 面板无今日搜索计数（"搜索: 1/1"是打卡子任务），
        #    按每日上限次数执行（3分/次×20=60分），积分余额不增长则提前结束
        search_ok = True
        if DEFAULT_PC_SEARCHES > 0:
            print(f"\n  --- PC 搜索 (计划{DEFAULT_PC_SEARCHES}次) ---")
            keywords = ensure_keywords(get_baidu_trends(), get_zhihu_trends(), get_weibo_trends())
            search_outcome = do_searches(
                page, DEFAULT_PC_SEARCHES, keywords, "PC", verify=make_pc_score(page)
            )
            search_ok = (
                isinstance(search_outcome, SearchOutcome)
                and search_outcome.status is SearchStatus.COMPLETED
                and search_outcome.completed >= DEFAULT_PC_SEARCHES
            )
            if not search_ok:
                status = getattr(search_outcome, "status", "unknown")
                completed = getattr(search_outcome, "completed", "unknown")
                print(
                    f"  PC 搜索未完成: 状态={status}, "
                    f"实际 {completed}/{DEFAULT_PC_SEARCHES} 次"
                )
        else:
            print("\n  PC 搜索已跳过")

        # 10. 搜索后再领取
        safe_goto(page, REWARDS_DASHBOARD)
        claim_available_points(page)

        # 11. 移动端搜索 — Bing 按 UA 判定设备，独立移动 profile + Android UA
        mobile_needed = DEFAULT_MOBILE_SEARCHES
        if mobile_needed > 0:
            print(f"\n  --- 移动端搜索 (计划{mobile_needed}次) ---")
            run_mobile_searches(p, context, mobile_needed, headless)
        else:
            print("\n  移动端搜索已跳过")

        # 12. Edge 浏览与必应应用签到
        print("\n  --- Edge 浏览连续打卡 ---")
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=3000)
        if EDGE_USER_DATA_DIR.resolve() != USER_DATA_DIR.resolve():
            edge_result = do_edge_browsing_streak_detail(
                page, expected_email=username
            )
            if edge_result.status is EdgeStreakStatus.IDENTITY_UNVERIFIED:
                print("  Edge 浏览器 profile 无法确认目标账号，账号不报告成功")
                return False
            if edge_result.status is EdgeStreakStatus.IDENTITY_MISMATCH:
                print("  Edge 浏览器 profile 账号与目标账号不一致，账号不报告成功")
                return False
            if edge_result.status is EdgeStreakStatus.INVALID_PROGRESS:
                print("  Edge 浏览进度异常，账号不报告成功")
                return False
            if edge_result.status is EdgeStreakStatus.COMPLETED:
                pass
            elif edge_result.status is EdgeStreakStatus.INCOMPLETE:
                current_edge = valid_edge_progress(
                    read_streak_progress(page, "edge")
                )
                if current_edge is None or current_edge[0] < current_edge[1]:
                    print("  Edge 浏览任务未完成，账号不报告成功")
                    return False
            else:
                print(f"  Edge 浏览任务失败: {edge_result.status.value}")
                return False
        else:
            print("  当前为隔离 profile，跳过 Edge 30 分钟任务（需正式 Edge profile）")

        print("\n  --- 必应应用签到（Android 模拟器） ---")
        mobile_result = mobile_checkin(username)
        if mobile_result != 1:
            if not ENABLE_MOBILE_CLIENT:
                print("  必应移动客户端已禁用，跳过必应应用签到")
            elif not list_android_devices():
                print("  未连接 Android 设备，跳过必应应用签到")
            elif total_accounts == 1:
                print("  必应应用签到未确认，账号不报告成功")
                return False
            else:
                print("  多账号下移动 App 未确认目标身份，本账号不报告 App 签到")

        # 12.5 无启用任务可正常跳过；UIA 环境不可用不覆盖其他任务成功。
        print("\n  --- Rewards 桌面 WebView ---")
        webview_status = run_rewards_desktop_task(username)
        if webview_status == REWARDS_DESKTOP_UIA_UNAVAILABLE:
            print("  WebView UIA 不可用，跳过（不影响其他任务）")
        elif webview_status == REWARDS_DESKTOP_IDENTITY_UNVERIFIED:
            print("  WebView 无法确认桌面应用账号身份，账号不报告成功")
            return False
        elif webview_status == REWARDS_DESKTOP_IDENTITY_MISMATCH:
            print("  WebView 桌面应用账号与目标账号不一致，账号不报告成功")
            return False
        elif webview_status == REWARDS_DESKTOP_TASK_ITEM_MISSING:
            print("  WebView 已进入任务页但未找到可点击任务项，账号不报告成功")
            return False
        elif webview_status < 0:
            print("  Rewards 桌面 WebView 任务执行失败，账号不报告成功")
            return False

        # 13. 最终领取
        safe_goto(page, REWARDS_DASHBOARD)
        claim_available_points(page)

        if not daily_tasks_ok or not daily_activities_ok or not search_ok:
            print("  必需任务未全部完成，账号不报告成功")
            return False

        print(f"\n  账号 {username} 处理完成")
        return True

    except (RewardsRiskControlError, BrowserRiskControlError) as exc:
        print(f"  检测到微软风控/验证码，立即停止当前账号后续动作: {exc}")
        return False
    except RiskDetectionError:
        # 交由 _run 统一分类为退出码 5；不能在这里降级成普通账号失败。
        raise
    except Exception as e:
        print(f"  处理账号出错: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        page.close()


def _resolve_log_path():
    """返回 start.bat 传入的本次日志路径。

    不能回退到 logs/ 下最新文件：旧实例发通知时，新实例可能已经创建了
    更新的日志，猜测会把新任务的日志误附到旧通知中。未显式传入时返回
    None，由通知正文明确标注“未知”。
    """
    explicit = os.getenv("BING_REWARDS_LOG_FILE")
    return explicit if explicit else None


def _run(run_info: dict | None = None) -> int:
    """执行一次运行；失败通知完成前始终持有单实例锁。"""
    run_info = run_info or _capture_run_info(datetime.now())

    def log(message: str) -> None:
        # 无人值守运行时没有可见窗口，stdout 由 start.bat 重定向到 logs/run_*.log
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)

    guard = None

    def execute() -> int:
        nonlocal guard
        log("启动 Bing Rewards 自动化 (Playwright)!")
        log(f"pid={os.getpid()} cwd={os.getcwd()} argv={sys.argv[1:]}")
        log(_log_session())
        if run_info.get("log_path"):
            log(f"本次运行日志: {run_info['log_path']}")

        headless = False
        # --task 由 start.bat 传入，仅用于标记“无人值守启动”，此处忽略
        flags = [arg.strip().lower() for arg in sys.argv[1:] if arg.strip().lower() != "--task"]
        if flags and flags[0] in ("headless", "--headless", "-h"):
            headless = True

        try:
            accounts = load_accounts()
        except Exception as exc:
            log(f"!! 读取账号失败: {exc}")
            return 4
        log(f"账号数量: {len(accounts)}")

        # 临时目录必须在 sync_playwright() 之前固定：驱动进程在启动时继承 TEMP，
        # 之后再改环境变量对它无效（系统 %TEMP% 不可写会抛 mkdtemp EPERM）
        profile.prepare_temp_dir(TEMP_DIR)
        if not profile.warmup_temp_dir(TEMP_DIR):
            log(f"!! 临时目录不可写: {TEMP_DIR}")
            log("   Playwright 需要在该目录创建临时文件夹，请检查目录权限或安全软件拦截。")
            return 2
        stale = profile.cleanup_stale_artifacts(TEMP_DIR)
        if stale:
            log(f"已清理 {stale} 个遗留的 Playwright 临时目录")

        # 单实例保护：两个实例共用同一 profile 只会互相抢占，第二个直接退出
        if not os.getenv("BING_REWARDS_ALLOW_MULTI"):
            guard = profile.acquire_single_instance()
            if guard is None:
                log("!! 已有另一个 bing-points 实例在运行（共用同一浏览器 profile）")
                log("   并发运行会互相抢占 profile 导致启动失败，本次退出。")
                log("   如确实需要并行，请设置环境变量 BING_REWARDS_ALLOW_MULTI=1（并使用不同 profile）")
                return 0

        results: list[bool] = []
        try:
            with sync_playwright() as p:
                log("启动浏览器上下文...")
                context = create_context(p, headless)
                log("浏览器已启动，进入账号循环")
                # 注入反检测脚本
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
                """)

                try:
                    for idx, (username, password) in enumerate(accounts, 1):
                        log(f"账号 {idx}/{len(accounts)} 开始")
                        results.append(run_account(p, context, username, password, headless, len(accounts)))
                        log(f"账号 {idx}/{len(accounts)} 结束")
                finally:
                    context.close()
                    log("浏览器上下文已关闭")
        except RiskDetectionError as exc:
            log(f"!! 风控检测失败，停止运行: {exc}")
            return 5
        except SystemExit as exc:
            log(f"!! 启动阶段终止，退出码 {exc.code}")
            return int(exc.code or 1)

        failed = results.count(False)
        if results and failed == len(results):
            log(f"!! 全部 {failed} 个账号均处理失败")
            return 3
        if failed:
            log(f"!! 部分账号处理失败（成功 {len(results) - failed}/{len(results)}）")
            return 6

        log(f"所有账号处理完毕（成功 {len(results) - failed}/{len(results)}）")
        return 0

    exit_code = 1
    try:
        try:
            exit_code = execute()
        except RiskDetectionError as exc:
            log(f"!! 风控检测失败，停止运行: {exc}")
            exit_code = 5
        except Exception:
            import traceback

            traceback.print_exc()
            exit_code = 1
    finally:
        # 通知必须在 guard.close() 之前完成，避免旧通知发送期间新实例启动。
        _notify_failure(exit_code, run_info)
        if guard is not None:
            guard.close()
    return exit_code


def _log_mtime(log_path: str) -> str:
    try:
        from pathlib import Path

        path = Path(log_path)
        if path.exists():
            return datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
    except Exception:
        pass
    return "未知"


def _capture_run_info(started_at: datetime) -> dict:
    """采集本次运行的稳定身份，供失败通知定位日志。"""
    log_path = _resolve_log_path()
    info = {
        "pid": os.getpid(),
        "log_path": log_path or "未知",
        "started_at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
        "log_mtime": "未知",
    }
    if log_path:
        info["log_mtime"] = _log_mtime(log_path)
    return info


def _notify_failure(exit_code: int, run_info: dict) -> None:
    """在单实例锁释放前尽最大努力发送失败通知。"""
    if exit_code == 0:
        return
    try:
        from utils.notify import RunLog, notify_failure, read_log_tail

        log_path = run_info.get("log_path")
        if log_path and log_path != "未知":
            run_info["log_mtime"] = _log_mtime(log_path)
            log_text = read_log_tail(log_path)
        else:
            log_text = ""
        notify_failure(exit_code, RunLog(log_text, run_info=run_info))
    except Exception:
        # 通知是尽最大努力：绝不允许它改变运行结果或吞掉原退出码
        try:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [推送] 推送流程异常（已忽略）")
            import traceback

            traceback.print_exc()
        except Exception:
            pass


def main() -> int:
    """入口：通知生命周期由 _run 与单实例锁共同覆盖。"""
    return _run(_capture_run_info(datetime.now()))


if __name__ == "__main__":
    sys.exit(main())
