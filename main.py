"""
Bing Rewards 自动化 — Playwright 版
基于 2026-05 Rewards 新版 dashboard (Next.js RSC) 重写

编排入口：浏览器启动 + 多账号循环，具体功能见 utils/ 各模块
"""
import sys

from playwright.sync_api import sync_playwright

from utils.accounts import load_accounts
from utils.browser import do_searches, safe_goto
from utils.config import BING_HOME, DEFAULT_MOBILE_SEARCHES, REWARDS_DASHBOARD, USER_DATA_DIR
from utils.dashboard import parse_dashboard_data
from utils.login import do_login
from utils.tasks import (
    claim_available_points,
    do_daily_earn_tasks,
    do_daily_set_tasks,
    do_dashboard_activities,
)
from utils.trends import ensure_keywords, get_baidu_trends, get_zhihu_trends, get_weibo_trends


# ======================== 主流程 ========================

def run_account(context, username: str, password: str, headless: bool):
    """处理单个账号的完整流程"""
    print(f"\n{'='*50}")
    print(f"处理账号: {username}")
    print(f"{'='*50}")

    page = context.new_page()

    try:
        # 1. 检查登录态
        page.goto(REWARDS_DASHBOARD, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

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
                print("  登录失败，跳过此账号")
                return

        # 2. 打开 dashboard
        print("  打开 Rewards 面板...")
        safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        # 再次确认不在登录页
        url = page.url.lower()
        if "login" in url or "welcome" in url:
            print("  仍未登录，再次尝试...")
            if not do_login(page, username, password):
                print("  登录失败，跳过")
                return
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        # 最终确认登录状态
        url = page.url.lower()
        if "login" in url or "welcome" in url:
            print("  登录未成功，可能需人工验证（2FA/验证码/密码错误），跳过")
            return

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

        # 6.5 日常任务(/earn 页带追踪参数的搜索任务)
        print("\n  --- 日常任务 ---")
        do_daily_earn_tasks(page)
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
        if data2["daily_set_items"]:
            print(f"    剩余每日任务: {len(data2['daily_set_items'])} 个")

        # 9. PC 搜索
        pc_needed = max(0, data["pc_search_total"] - data["pc_search_done"])
        if pc_needed > 0:
            print(f"\n  --- PC 搜索 ({pc_needed}次) ---")
            keywords = ensure_keywords(get_baidu_trends(), get_zhihu_trends(), get_weibo_trends())
            do_searches(page, pc_needed, keywords, "PC")
        else:
            print("\n  PC 搜索已完成，跳过")

        # 10. 搜索后再领取
        safe_goto(page, REWARDS_DASHBOARD)
        claim_available_points(page)

        # 11. 移动端搜索
        mobile_needed = max(0, data.get("mobile_search_total", DEFAULT_MOBILE_SEARCHES) - data.get("mobile_search_done", 0))
        if mobile_needed > 0:
            print(f"\n  --- 移动端搜索 ({mobile_needed}次) ---")
            # 用 mobile viewport
            mobile_page = context.new_page()
            mobile_page.set_viewport_size({"width": 390, "height": 844})
            try:
                safe_goto(mobile_page, REWARDS_DASHBOARD)
                safe_goto(mobile_page, BING_HOME)
                keywords = ensure_keywords(get_baidu_trends(), get_zhihu_trends())
                do_searches(mobile_page, mobile_needed, keywords, "Mobile")
            finally:
                mobile_page.close()

        # 12. 最终领取
        safe_goto(page, REWARDS_DASHBOARD)
        claim_available_points(page)

        print(f"\n  账号 {username} 处理完成")

    except Exception as e:
        print(f"  处理账号出错: {e}")
        import traceback
        traceback.print_exc()
    finally:
        page.close()


def main():
    print("启动 Bing Rewards 自动化 (Playwright)!")

    headless = False
    if len(sys.argv) > 1:
        flag = sys.argv[1].strip().lower()
        if flag in ("headless", "--headless", "-h"):
            headless = True

    accounts = load_accounts()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        # 注入反检测脚本
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        """)

        try:
            for idx, (username, password) in enumerate(accounts, 1):
                print(f"\n账号 {idx}/{len(accounts)}")
                run_account(context, username, password, headless)
        finally:
            context.close()

    print("\n所有账号处理完毕")


if __name__ == "__main__":
    main()
