"""单账号任务流水线。

该模块只负责账号内的业务编排，不依赖 main.py；main 仅保留静态再导出，
使既有调用方和 CLI 生命周期逻辑保持兼容。
"""
import os

from playwright.sync_api import Error as PlaywrightError

from utils import profile
from utils.browser import (
    BrowserRiskControlError,
    SearchOutcome,
    SearchStatus,
    do_searches,
    launch_persistent,
    safe_goto,
)
from utils.clients import (
    list_android_devices,
    mobile_checkin,
    REWARDS_DESKTOP_NO_TASK,
    REWARDS_DESKTOP_IDENTITY_MISMATCH,
    REWARDS_DESKTOP_IDENTITY_UNVERIFIED,
    REWARDS_DESKTOP_TASK_ITEM_MISSING,
    REWARDS_DESKTOP_UIA_UNAVAILABLE,
    run_rewards_desktop_task,
)
from utils.config import (
    DEFAULT_MOBILE_SEARCHES,
    DEFAULT_PC_SEARCHES,
    EDGE_USER_DATA_DIR,
    ENABLE_MOBILE_CLIENT,
    MOBILE_PROFILE_DIR,
    MOBILE_USER_AGENT,
    REWARDS_DASHBOARD,
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
    do_point_punch_cards,
    read_streak_progress,
)
from utils.outcomes import AccountOutcome, TaskOutcome, TaskStatus
from utils.trends import (
    ensure_keywords,
    get_baidu_trends,
    get_zhihu_trends,
    get_weibo_trends,
)


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


def _run_account_outcome(
    p,
    context,
    username: str,
    password: str,
    headless: bool,
    total_accounts: int = 1,
) -> AccountOutcome:
    """执行单个账号流程并收集任务结果；控制流与日志语义保持原样。"""
    print(f"\n{'='*50}")
    print(f"处理账号: {username}")
    print(f"{'='*50}")

    page = context.new_page()
    outcomes: list[TaskOutcome] = []
    current_task = "login"
    required_by_task = {
        "login": True,
        "daily_set": True,
        "daily_activities": True,
        "pc_search": True,
        "edge": True,
        "android": False,
        "webview": True,
        "final_claim": True,
    }

    def _record(
        name: str,
        status: TaskStatus,
        *,
        required: bool | None = None,
        detail: str = "",
        before: object | None = None,
        after: object | None = None,
    ) -> None:
        task = TaskOutcome(
            name=name,
            status=status,
            required=required_by_task.get(name, True) if required is None else required,
            detail=detail,
            before=before,
            after=after,
        )
        for index, existing in enumerate(outcomes):
            if existing.name == name:
                outcomes[index] = task
                return
        outcomes.append(task)

    def _finish(
        final_claim_status: TaskStatus = TaskStatus.SKIPPED,
        error: str = "",
    ) -> AccountOutcome:
        required_names = frozenset(
            name
            for name, required in required_by_task.items()
            if required and name != "final_claim"
        )
        present = {task.name for task in outcomes}
        for name in required_names - present:
            outcomes.append(
                TaskOutcome(
                    name=name,
                    status=TaskStatus.BLOCKED,
                    required=True,
                    detail="前序任务失败，任务未执行",
                )
            )
        return AccountOutcome(
            account=username,
            tasks=tuple(outcomes),
            final_claim_status=final_claim_status,
            error=error,
            required_task_names=required_names,
        )

    def _failure_outcome(
        status: TaskStatus = TaskStatus.FAILED,
        detail: str = "",
    ) -> AccountOutcome:
        claimed = current_task == "final_claim" and any(
            task.name == "final_claim" and task.status is TaskStatus.SUCCESS
            for task in outcomes
        )
        _record(current_task, status, detail=detail)
        return _finish(
            TaskStatus.SUCCESS if claimed else (
                TaskStatus.FAILED if current_task == "final_claim" else TaskStatus.SKIPPED
            ),
            error=detail,
        )

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
                _record(
                    "login",
                    TaskStatus.IDENTITY_UNVERIFIED,
                    detail="账号身份校验失败",
                )
                return _finish()
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
                _record("login", TaskStatus.FAILED, detail="登录失败")
                return _finish()

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
                _record("login", TaskStatus.FAILED, detail="登录失败")
                return _finish()
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        risk_reason = detect_risk_control(page)
        if risk_reason:
            raise RewardsRiskControlError(risk_reason)

        # 最终确认登录状态与账号身份；所有登录分支都必须复核。
        url = page.url.lower()
        if "login" in url or "welcome" in url:
            print("  登录未成功，可能需人工验证（2FA/验证码/密码错误），跳过")
            _record("login", TaskStatus.FAILED, detail="登录未成功")
            return _finish()
        if not ensure_account(page, username, password):
            print("  无法确认当前登录的是目标账号，停止操作")
            _record(
                "login",
                TaskStatus.IDENTITY_UNVERIFIED,
                detail="无法确认当前登录的是目标账号",
            )
            return _finish()
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=4000)
        _record("login", TaskStatus.SUCCESS)

        # 3. 解析页面数据
        print("  解析页面数据...")
        data = parse_dashboard_data(page)
        print(f"    可领取积分: {data['claimable_points']}")
        print(f"    每日任务: {len(data['daily_set_items'])} 个, 已完成 {data.get('daily_activities_done', 0)}/{data.get('daily_activities_total', 3)}")
        print(f"    PC搜索: {data['pc_search_done']}/{data['pc_search_total']}")

        # 4. 先领取可领取积分
        print("\n  --- 领取积分 ---")
        current_task = "final_claim"
        claim_available_points(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 5. 每日任务(每日套餐: 测验/投票/探索)
        print("\n  --- 每日任务 ---")
        current_task = "daily_set"
        do_daily_set_tasks(page, data["daily_set_items"])

        # 6. 仪表板活动卡片(每日活动)
        print("\n  --- 每日活动 ---")
        current_task = "daily_activities"
        do_dashboard_activities(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 6.5 日常任务(/earn 页搜索与每日拼图)
        print("\n  --- 日常任务 ---")
        current_task = "daily_set"
        do_daily_offer_tasks(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 6.7 限时积分活动：每次最多推进一个网页可完成任务
        print("\n  --- 限时积分活动 ---")
        do_point_punch_cards(page)
        safe_goto(page, REWARDS_DASHBOARD)

        # 7. 领取完成后的积分
        current_task = "final_claim"
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
        _record(
            "daily_set",
            TaskStatus.SUCCESS if daily_tasks_ok else TaskStatus.FAILED,
            detail="" if daily_tasks_ok else f"仍有每日任务 {len(data2['daily_set_items'])} 个",
            after=len(data2["daily_set_items"]),
        )
        _record(
            "daily_activities",
            TaskStatus.SUCCESS if daily_activities_ok else TaskStatus.FAILED,
            detail="" if daily_activities_ok else f"每日活动未完成 {daily_done}/{daily_total}",
            after=(daily_done, daily_total),
        )
        if not daily_tasks_ok:
            print(f"    仍有每日任务: {len(data2['daily_set_items'])} 个")
        if not daily_activities_ok:
            print(f"    每日活动未完成: {daily_done}/{daily_total}")

        # 9. PC 搜索 — 面板无今日搜索计数（"搜索: 1/1"是打卡子任务），
        #    按每日上限次数执行（3分/次×20=60分），积分余额不增长则提前结束
        search_ok = True
        current_task = "pc_search"
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
            status = getattr(search_outcome, "status", None)
            completed = getattr(search_outcome, "completed", "unknown")
            if not search_ok:
                if status is SearchStatus.SCORE_STALLED:
                    detail = "积分连续无增长，无法确认搜索是否已封顶，按未完成处理"
                else:
                    detail = f"状态={status or 'unknown'}, 实际 {completed}/{DEFAULT_PC_SEARCHES} 次"
                print(
                    f"  PC 搜索未完成: 状态={status or 'unknown'}, "
                    f"实际 {completed}/{DEFAULT_PC_SEARCHES} 次"
                )
                _record(
                    "pc_search",
                    TaskStatus.FAILED,
                    required=True,
                    detail=detail,
                    after=completed,
                )
            else:
                _record("pc_search", TaskStatus.SUCCESS, after=search_outcome.completed)
        else:
            print("\n  PC 搜索已跳过")
            _record("pc_search", TaskStatus.SKIPPED, required=False)

        # 10. 搜索后再领取
        current_task = "final_claim"
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
        current_task = "edge"
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=3000)
        if EDGE_USER_DATA_DIR.resolve() != USER_DATA_DIR.resolve():
            edge_result = do_edge_browsing_streak_detail(
                page, expected_email=username
            )
            if edge_result.status is EdgeStreakStatus.IDENTITY_UNVERIFIED:
                print("  Edge 浏览器 profile 无法确认目标账号，账号不报告成功")
                _record(
                    "edge",
                    TaskStatus.IDENTITY_UNVERIFIED,
                    detail="Edge 浏览器 profile 无法确认目标账号",
                )
                return _finish()
            if edge_result.status is EdgeStreakStatus.IDENTITY_MISMATCH:
                print("  Edge 浏览器 profile 账号与目标账号不一致，账号不报告成功")
                _record(
                    "edge",
                    TaskStatus.IDENTITY_MISMATCH,
                    detail="Edge 浏览器 profile 账号与目标账号不一致",
                )
                return _finish()
            if edge_result.status is EdgeStreakStatus.INVALID_PROGRESS:
                print("  Edge 浏览进度异常，账号不报告成功")
                _record("edge", TaskStatus.FAILED, detail="Edge 浏览进度异常")
                return _finish()
            if edge_result.status is EdgeStreakStatus.COMPLETED:
                _record("edge", TaskStatus.SUCCESS, after=edge_result.progress)
            elif edge_result.status is EdgeStreakStatus.INCOMPLETE:
                current_edge = valid_edge_progress(
                    read_streak_progress(page, "edge")
                )
                if current_edge is None or current_edge[0] < current_edge[1]:
                    print("  Edge 浏览任务未完成，账号不报告成功")
                    _record(
                        "edge",
                        TaskStatus.FAILED,
                        detail="Edge 浏览任务未完成",
                        after=current_edge,
                    )
                    return _finish()
                _record("edge", TaskStatus.SUCCESS, after=current_edge)
            else:
                print(f"  Edge 浏览任务失败: {edge_result.status.value}")
                _record(
                    "edge",
                    TaskStatus.FAILED,
                    detail=f"Edge 浏览任务失败: {edge_result.status.value}",
                )
                return _finish()
        else:
            print("  当前为临时隔离 profile，跳过 Edge 30 分钟任务")
            _record("edge", TaskStatus.SKIPPED, required=False)

        print("\n  --- 必应应用签到（Android 模拟器） ---")
        current_task = "android"
        mobile_result = mobile_checkin(username)
        if mobile_result != 1:
            if not ENABLE_MOBILE_CLIENT:
                print("  必应移动客户端已禁用，跳过必应应用签到")
                _record("android", TaskStatus.SKIPPED, required=False)
            elif not list_android_devices():
                print("  未连接 Android 设备，跳过必应应用签到")
                _record("android", TaskStatus.SKIPPED, required=False)
            elif total_accounts == 1:
                print("  必应应用签到未确认，账号不报告成功")
                _record(
                    "android",
                    TaskStatus.FAILED,
                    required=True,
                    detail="必应应用签到未确认",
                )
                return _finish()
            else:
                print("  多账号下移动 App 未确认目标身份，本账号不报告 App 签到")
                _record("android", TaskStatus.SKIPPED, required=False)
        else:
            _record("android", TaskStatus.SUCCESS, required=False)

        # 12.5 无启用任务可正常跳过；UIA 环境不可用不覆盖其他任务成功。
        print("\n  --- Rewards 桌面 WebView ---")
        current_task = "webview"
        webview_status = run_rewards_desktop_task(username)
        if webview_status == REWARDS_DESKTOP_UIA_UNAVAILABLE:
            print("  WebView UIA 不可用，跳过（不影响其他任务）")
            _record("webview", TaskStatus.ENVIRONMENT_UNAVAILABLE, required=False)
        elif webview_status == REWARDS_DESKTOP_IDENTITY_UNVERIFIED:
            print("  WebView 无法确认桌面应用账号身份，账号不报告成功")
            _record(
                "webview",
                TaskStatus.IDENTITY_UNVERIFIED,
                detail="WebView 无法确认桌面应用账号身份",
            )
            return _finish()
        elif webview_status == REWARDS_DESKTOP_IDENTITY_MISMATCH:
            print("  WebView 桌面应用账号与目标账号不一致，账号不报告成功")
            _record(
                "webview",
                TaskStatus.IDENTITY_MISMATCH,
                detail="WebView 桌面应用账号与目标账号不一致",
            )
            return _finish()
        elif webview_status == REWARDS_DESKTOP_TASK_ITEM_MISSING:
            print("  WebView 已进入任务页但未找到可点击任务项，账号不报告成功")
            _record(
                "webview",
                TaskStatus.FAILED,
                detail="WebView 已进入任务页但未找到可点击任务项",
            )
            return _finish()
        elif webview_status < 0:
            print("  Rewards 桌面 WebView 任务执行失败，账号不报告成功")
            _record(
                "webview",
                TaskStatus.FAILED,
                detail="Rewards 桌面 WebView 任务执行失败",
            )
            return _finish()
        elif webview_status == REWARDS_DESKTOP_NO_TASK:
            _record(
            "webview",
            TaskStatus.SKIPPED,
            required=False,
            after=webview_status,
        )
        else:
            _record("webview", TaskStatus.SUCCESS, after=webview_status)

        # 13. 最终领取
        current_task = "final_claim"
        safe_goto(page, REWARDS_DASHBOARD)
        claim_available_points(page)
        _record("final_claim", TaskStatus.SUCCESS)

        if not daily_tasks_ok or not daily_activities_ok or not search_ok:
            print("  必需任务未全部完成，账号不报告成功")
            return _finish(TaskStatus.SUCCESS)

        print(f"\n  账号 {username} 处理完成")
        return _finish(TaskStatus.SUCCESS)

    except (RewardsRiskControlError, BrowserRiskControlError) as exc:
        print(f"  检测到微软风控/验证码，立即停止当前账号后续动作: {exc}")
        return _failure_outcome(detail=str(exc))
    except RiskDetectionError:
        # 交由 _run 统一分类为退出码 5；不能在这里降级成普通账号失败。
        raise
    except Exception as e:
        print(f"  处理账号出错: {e}")
        import traceback
        traceback.print_exc()
        return _failure_outcome(detail=str(e))
    finally:
        page.close()


def run_account(p, context, username: str, password: str, headless: bool, total_accounts: int = 1) -> bool:
    """处理单个账号的完整流程，返回是否成功（失败不应被计划任务误判为成功）"""
    return _run_account_outcome(
        p, context, username, password, headless, total_accounts
    ).success
