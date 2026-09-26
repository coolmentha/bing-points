"""诊断: 导出 Rewards 面板真实文本与解析结果，对比两种搜索方式的计分

用法: .venv\\Scripts\\python.exe debug_dashboard.py
输出: debug_dashboard_out.txt + debug_baseline.png
"""
import json
import time

from playwright.sync_api import sync_playwright

from utils.accounts import load_accounts
from utils.browser import do_bing_search, safe_goto
from utils.config import BING_HOME, REWARDS_DASHBOARD, USER_DATA_DIR
from utils.dashboard import parse_dashboard_data
from utils.login import do_login

OUT = "debug_dashboard_out.txt"

SEARCH_TERMS = {
    "direct": "人工智能 最新进展 诊断A",
    "box": "量子计算 科普 诊断B",
}


def dump(page, lines, tag):
    safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)
    data = parse_dashboard_data(page)
    text = page.evaluate("() => document.body.innerText || ''")
    rel = [l.strip() for l in text.splitlines() if l.strip() and ("搜索" in l or "积分" in l or "/" in l or "活动" in l)]
    lines.append(f"\n===== {tag} =====")
    lines.append("parsed_search: " + json.dumps(
        {k: v for k, v in data.items() if "search" in k or k == "daily_activities_done"},
        ensure_ascii=False))
    lines.append("FULL_TEXT_BEGIN")
    lines.append(text)
    lines.append("FULL_TEXT_END")
    if tag == "baseline":
        page.screenshot(path="debug_baseline.png")


def box_search(page, keyword):
    """首页搜索框输入并回车（模拟真实搜索）"""
    try:
        safe_goto(page, BING_HOME)
        box = page.locator("#sb_form_q").first
        if box.count() > 0 and box.is_visible():
            box.fill(keyword)
            box.press("Enter")
            time.sleep(8)
            return True
    except Exception as e:
        print(f"box_search 异常: {e}")
    return False


def main():
    lines = []
    accounts = load_accounts()
    username, password = accounts[0]

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
        """)
        page = context.new_page()

        safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)
        if "login" in page.url.lower() or "welcome" in page.url.lower():
            lines.append("!! 未登录，先执行登录")
            if not do_login(page, username, password):
                lines.append("!! 登录失败，诊断终止")
                context.close()
                return
            safe_goto(page, REWARDS_DASHBOARD, wait_extra=5000)

        dump(page, lines, "baseline")
        url0 = page.url.lower()
        lines.append(f"当前URL: {url0[:80]}")

        # 方式A: 直接URL搜索（当前 do_bing_search 的主路径）
        do_bing_search(page, SEARCH_TERMS["direct"])
        time.sleep(25)
        dump(page, lines, "after_direct_url_search")

        context.close()

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"诊断完成，结果写入 {OUT}")


if __name__ == "__main__":
    main()
