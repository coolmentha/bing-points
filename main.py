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
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from utils import edge_streak, profile
from utils.account_pipeline import (
    make_pc_score,
    run_account,
    run_mobile_searches,
)
from utils.accounts import load_accounts
from utils.browser import launch_persistent
from utils.config import (
    BING_HOME,
    BROWSER_ENGINE,
    DEDICATED_EDGE_USER_DATA_DIR,
    DEFAULT_EDGE_USER_DATA_DIR,
    EDGE_USER_DATA_DIR,
    PROJECT_ROOT,
    TEMP_DIR,
    USER_DATA_DIR,
)
from utils.outcomes import ExitCode, RunSnapshot, exit_code_reason
from utils.tasks import RiskDetectionError

# 兼容旧调用方：这些符号历史上由 main.py 模块级导入。
from utils.clients import (  # noqa: E402,F401
    list_android_devices,
    mobile_checkin,
    REWARDS_DESKTOP_IDENTITY_MISMATCH,
    REWARDS_DESKTOP_IDENTITY_UNVERIFIED,
    REWARDS_DESKTOP_TASK_ITEM_MISSING,
    REWARDS_DESKTOP_UIA_UNAVAILABLE,
    run_rewards_desktop_task,
)
from utils.browser import (  # noqa: E402,F401
    BrowserRiskControlError,
    SearchOutcome,
    SearchStatus,
    do_searches,
    safe_goto,
)
from utils.config import (  # noqa: E402,F401
    DEFAULT_MOBILE_SEARCHES,
    DEFAULT_PC_SEARCHES,
    ENABLE_MOBILE_CLIENT,
    MOBILE_PROFILE_DIR,
    MOBILE_USER_AGENT,
    REWARDS_DASHBOARD,
)
from utils.dashboard import parse_dashboard_data  # noqa: E402,F401
from utils.login import do_login, ensure_account  # noqa: E402,F401
from utils.tasks import (  # noqa: E402,F401
    RewardsRiskControlError,
    claim_available_points,
    detect_risk_control,
    do_daily_offer_tasks,
    do_daily_set_tasks,
    do_dashboard_activities,
    do_edge_browsing_streak_detail,
    EdgeStreakStatus,
    valid_edge_progress,
    do_point_punch_cards,
    read_streak_progress,
)
from utils.trends import (  # noqa: E402,F401
    ensure_keywords,
    get_baidu_trends,
    get_zhihu_trends,
    get_weibo_trends,
)


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


def _profile_busy_help(
    user_data_dir,
    shared_edge_profile: bool = False,
    project_owned: bool = False,
) -> None:
    print(f"!! 浏览器 profile 被其它实例占用: {user_data_dir}")
    if shared_edge_profile:
        print("   这是日常 Edge 的正式 User Data：脚本不会自动结束 Edge。")
        print("   请关闭所有日常 Edge 窗口后重试，或取消 BING_REWARDS_EDGE_USER_DATA_DIR 覆盖以使用专用 profile。")
        return
    if not project_owned:
        print("   这是外部 Edge profile：脚本不会自动结束其进程。")
        print("   请关闭使用该 profile 的 Edge 窗口，或取消 BING_REWARDS_EDGE_USER_DATA_DIR 覆盖。")
        print("   如仍无法确认占用来源，可用任务管理器检查对应 msedge.exe 进程。")
        return
    print("   常见原因：开机自启的实例（start.bat）仍在运行，或上次运行残留了 Edge 进程。")
    print("   排查/清理：python -m utils.profile --check")
    print("              python -m utils.profile --kill")
    print("   若确认不需要开机自启，可运行 remove_autostart.bat 后重试。")


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


def _launch_visible_edge_for_login(user_data_dir) -> None:
    """启动一次可见 Edge 供专用 profile 首次登录，不连接 CDP。"""
    edge_path = profile.find_edge_executable()
    if edge_path is None:
        print("!! 未找到本机 Edge (msedge)")
        raise SystemExit(2)
    user_data_dir = Path(user_data_dir)
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
        (user_data_dir / "Default").mkdir(exist_ok=True)
    except OSError as exc:
        print(f"!! 无法创建专用 Edge profile 目录: {exc}")
        raise SystemExit(2) from exc

    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(user_data_dir), buffer, len(buffer)):
        print("!! 无法取得 Edge profile 的短路径")
        raise SystemExit(2)
    args = [
        str(edge_path),
        f"--user-data-dir={buffer.value}",
        "--no-first-run",
        "--disable-session-crashed-bubble",
        BING_HOME,
    ]
    try:
        subprocess.Popen(
            args,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"!! 无法启动 Edge: {exc}")
        raise SystemExit(2) from exc
    print("   已打开专用 Edge profile，请完成 Microsoft 登录。")
    print("   确认登录完成并关闭该 Edge 窗口后，再次运行本程序。")


def _launch_real_edge_cdp(p, user_data_dir, headless: bool):
    """正常启动可持久化 Edge，再通过本地 CDP 连接，避免 ProcessSingleton 冲突。"""
    edge_path = profile.find_edge_executable()
    if edge_path is None:
        print("!! 未找到本机 Edge (msedge)")
        raise SystemExit(2)
    user_data_dir = Path(user_data_dir)
    if user_data_dir.resolve() == DEDICATED_EDGE_USER_DATA_DIR.resolve():
        try:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            (user_data_dir / "Default").mkdir(exist_ok=True)
        except OSError as exc:
            print(f"!! 无法创建专用 Edge profile 目录: {exc}")
            raise SystemExit(2) from exc
    port = _free_loopback_port()
    endpoint = f"http://127.0.0.1:{port}"
    # Edge 的 user-data-dir 参数不能含空格，否则它会把路径截断。
    # 使用 Windows 8.3 短路径可保留同一个正式 User Data，同时让 Edge 正常解析。
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(user_data_dir), buffer, len(buffer)):
        print("!! 无法取得 Edge profile 的短路径")
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
        print(f"!! 无法启动 Edge: {exc}")
        raise SystemExit(2) from exc
    if not _wait_cdp(endpoint):
        print("!! Edge 已启动但本地调试端口不可用")
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
        print("  Edge profile 已通过本地 CDP 接入")
        return _EdgeCDPContext(browser, contexts[0], process)
    except Exception as exc:
        print(f"!! 连接 Edge 调试端口失败: {str(exc).splitlines()[0]}")
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
        print("  !! 当前是临时隔离 profile；Edge 30 分钟任务无法读取登录态")
        print("     请取消 BING_REWARDS_EDGE_USER_DATA_DIR 覆盖以使用专用 Edge profile")
    elif re.match(r"^(default|profile \d+)$", user_data_dir.name, re.I):
        print("  !! Edge profile 配置应指向 User Data 根目录，而不是其中的 Default/Profile N 子目录")
        raise SystemExit(2)
    dedicated_profile = user_data_dir.resolve() == DEDICATED_EDGE_USER_DATA_DIR.resolve()
    if dedicated_profile:
        print("  当前使用专用 Edge profile；首次运行请在打开的窗口完成 Microsoft 登录")

    shared_edge_profile = user_data_dir.resolve() == DEFAULT_EDGE_USER_DATA_DIR.resolve()
    legacy_profile = user_data_dir.resolve() == USER_DATA_DIR.resolve()
    project_owned_profile = dedicated_profile or legacy_profile
    auto_kill = project_owned_profile and not os.getenv("BING_REWARDS_NO_PROFILE_KILL")
    if not profile.ensure_profile_available(user_data_dir, auto_kill=auto_kill):
        _profile_busy_help(
            user_data_dir,
            shared_edge_profile=shared_edge_profile,
            project_owned=project_owned_profile,
        )
        raise SystemExit(2)

    if dedicated_profile and not edge_streak.edge_browser_profile_logged_in(user_data_dir):
        print("  !! 专用 Edge profile 尚未登录，进入首次登录流程")
        _launch_visible_edge_for_login(user_data_dir)
        print("  首次登录流程已启动；完成 Microsoft 登录并关闭 Edge 后再次运行。")
        raise SystemExit(2)

    # 默认专用 profile 和任何外部 profile 都通过正常 Edge + CDP 启动，
    # 以保留登录态；只有旧版通用临时 profile 走 Playwright 独占启动。
    if user_data_dir.resolve() != USER_DATA_DIR.resolve():
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
                _profile_busy_help(
                    user_data_dir,
                    shared_edge_profile=shared_edge_profile,
                    project_owned=project_owned_profile,
                )
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

def _resolve_log_path():
    """返回 start.bat 传入的本次日志路径。

    不能回退到 logs/ 下最新文件：旧实例发通知时，新实例可能已经创建了
    更新的日志，猜测会把新任务的日志误附到旧通知中。未显式传入时返回
    None，由通知正文明确标注“未知”。
    """
    explicit = os.getenv("BING_REWARDS_LOG_FILE")
    return explicit if explicit else None


def _run(run_info: dict | None = None) -> int:
    """执行一次运行：清理 -> 结束标记 -> 快照 -> 释放锁 -> 通知。"""
    started_at = datetime.now()
    inherited = run_info or {}
    log_path = inherited.get("log_path") or _resolve_log_path() or "未知"

    def log(message: str) -> None:
        # 无人值守运行时没有可见窗口，stdout 由 start.bat 重定向到 logs/run_*.log
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)

    guard = None

    def execute() -> int:
        nonlocal guard
        log("启动 Bing Rewards 自动化 (Playwright)!")
        log(f"pid={os.getpid()} cwd={os.getcwd()} argv={sys.argv[1:]}")
        log(_log_session())
        if log_path != "未知":
            log(f"本次运行日志: {log_path}")

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
                return int(ExitCode.ALREADY_RUNNING)

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
        # 完成日志落盘后再生成不可变快照；通知必须在释放单实例锁之后。
        finished_at = datetime.now()
        marker = (
            f"[{finished_at:%Y-%m-%d %H:%M:%S}] "
            f"===== BingPoints run end exit={exit_code} "
            f"reason={exit_code_reason(exit_code)} ====="
        )
        if log_path and log_path != "未知":
            try:
                from pathlib import Path

                with Path(log_path).open("a", encoding="utf-8") as log_file:
                    log_file.write(marker + chr(10))
            except Exception:
                try:
                    log(marker)
                except Exception:
                    pass
        else:
            try:
                log(marker)
            except Exception:
                pass

        try:
            stable_exit_code = ExitCode(int(exit_code))
        except (TypeError, ValueError):
            stable_exit_code = ExitCode.INTERNAL_ERROR
            exit_code = int(stable_exit_code)
        snapshot = RunSnapshot(
            pid=os.getpid(),
            log_path=log_path,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=stable_exit_code,
            log_text=_read_snapshot_log_tail(log_path),
        )
        if guard is not None:
            try:
                guard.close()
            except Exception as exc:
                log(f"!! 释放单实例锁失败（已忽略，保持退出码 {exit_code}）: {exc}")
        _notify_failure(snapshot)
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


def _read_snapshot_log_tail(log_path: str) -> str:
    if not log_path or log_path == "未知":
        return ""
    try:
        from utils.notify import read_log_tail

        return read_log_tail(log_path)
    except Exception:
        return ""


def _notify_failure(snapshot: RunSnapshot) -> None:
    """释放单实例锁后尽最大努力发送失败通知。"""
    if snapshot.exit_code in (ExitCode.SUCCESS, ExitCode.ALREADY_RUNNING):
        return
    try:
        from utils.notify import notify_failure

        notify_failure(snapshot)
    except Exception:
        # 通知是尽最大努力：绝不允许它改变运行结果或吞掉原退出码
        try:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [推送] 推送流程异常（已忽略）")
            import traceback

            traceback.print_exc()
        except Exception:
            pass


def main() -> int:
    """入口：运行快照、锁释放和失败通知由 _run 统一编排。"""
    return _run(_capture_run_info(datetime.now()))


if __name__ == "__main__":
    sys.exit(main())
