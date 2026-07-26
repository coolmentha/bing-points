"""
Bing Rewards 自动化 — Playwright 版
基于 2026-05 Rewards 新版 dashboard (Next.js RSC) 重写
"""
import json
import os
import re
import sys
import time
import random
import math
from pathlib import Path
from datetime import datetime
from urllib.parse import quote_plus

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ======================== 配置常量 ========================

USER_DATA_DIR = Path(__file__).with_name("tmp_playwright_profile")
ACCOUNTS_PATH = Path(__file__).with_name("accounts.txt")
REQUEST_TIMEOUT = 8
TRENDS_CACHE_FAILURE_TTL = 600

DEFAULT_PC_SEARCHES = int(os.getenv("BING_REWARDS_PC_SEARCHES", "37"))
DEFAULT_MOBILE_SEARCHES = int(os.getenv("BING_REWARDS_MOBILE_SEARCHES", "20"))

REWARDS_DASHBOARD = "https://rewards.bing.com/dashboard"
BING_HOME = "https://www.bing.com/"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0 Safari/537.36"
}

# 热词默认关键词
FALLBACK_KEYWORDS = ["微软奖励", "必应搜索", "信息流热点", "今天新闻", "最新科技", "体育资讯", "娱乐八卦", "财经新闻", "人工智能", "量子计算"]

# 搜索框选择器
SEARCH_BOX_SELECTORS = [
    "#sb_form_q",
    "input[name='q']",
    "textarea[name='q']",
    "input[type='search']",
]

_TRENDS_CACHE: dict = {}


# ======================== 账号管理 ========================

def load_accounts(path: Path | None = None) -> list[tuple[str, str]]:
    cfg = path or ACCOUNTS_PATH
    if not cfg.exists():
        raise RuntimeError(f"未找到账号配置文件：{cfg}，请创建并写入 user:password")
    raw = cfg.read_text(encoding="utf-8")
    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    accounts = []
    for part in re.split(r"[;,\n]+", raw):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, pwd = part.split(":", 1)
        user, pwd = user.strip(), pwd.strip()
        if user and pwd:
            accounts.append((user, pwd))
    if not accounts:
        raise RuntimeError(f"账号配置为空，请在 {cfg} 中填写 user:password")
    return accounts


# ======================== 热词 ========================

def _get_cached_trends(cache_key: str, fetcher) -> list:
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    cached = _TRENDS_CACHE.get(cache_key)
    if cached:
        if cached.get("date") == today and cached.get("ok") and isinstance(cached.get("words"), list):
            return cached["words"].copy()
        if not cached.get("ok") and isinstance(cached.get("ts"), (int, float)) and now - cached["ts"] < TRENDS_CACHE_FAILURE_TTL:
            return []
    words = fetcher() or []
    ok = bool(words)
    _TRENDS_CACHE[cache_key] = {"date": today, "ts": now, "ok": ok, "words": words.copy()}
    return words


def get_baidu_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://v2.xxapi.cn/api/baiduhot", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("title") for t in r.json().get("data", []) if t.get("title")]
        except Exception:
            return []
    return _get_cached_trends("baidu", _fetch)


def get_zhihu_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://v2.xxapi.cn/api/douyinhot", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("word") for t in r.json().get("data", []) if t.get("word")]
        except Exception:
            return []
    return _get_cached_trends("zhihu", _fetch)


def get_weibo_trends() -> list:
    def _fetch():
        try:
            r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=weibo", timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            r.raise_for_status()
            return [t.get("title") for t in r.json().get("data", []) if t.get("title")]
        except Exception:
            return []
    return _get_cached_trends("weibo", _fetch)


def ensure_keywords(*lists) -> list:
    for words in lists:
        if words:
            return words
    return FALLBACK_KEYWORDS.copy()


# ======================== Playwright 登录 ========================

def do_login(page, username: str, password: str) -> bool:
    """Microsoft OAuth 登录流程"""
    print(f"  登录账号: {username}")

    # 确保在登录页
    if "login.live.com" not in page.url.lower():
        page.goto("https://login.live.com/", timeout=30000)
        page.wait_for_timeout(2000)

    # 1. 输入邮箱
    email_input = page.locator("#usernameEntry").first
    if email_input.count() == 0 or not email_input.is_visible():
        email_input = page.locator("input[name='loginfmt']").first
    if email_input.count() == 0 or not email_input.is_visible():
        email_input = page.locator("input[type='email']").first

    if email_input.count() == 0:
        print("  ✗ 未找到邮箱输入框")
        return False

    email_input.click()
    email_input.fill("")
    email_input.type(username, delay=80)
    page.wait_for_timeout(300)

    # 等待"下一步"按钮可用
    page.wait_for_timeout(1500)
    for btn_sel in [
        "#idSIButton9",
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('下一步')",
        "input[value='下一步']",
        "a:has-text('下一步')",
        "[role='button']:has-text('下一步')",
    ]:
        next_btn = page.locator(btn_sel).first
        if next_btn.count() > 0 and next_btn.is_visible() and next_btn.is_enabled():
            next_btn.click()
            print("  已点击下一步")
            break
    else:
        # 回退：直接按回车
        print("  ⚠ 未找到下一步按钮，尝试回车")
        email_input.press("Enter")
    page.wait_for_timeout(3000)

    # 2. 等待密码页或安全验证页（循环处理）
    max_wait = 60
    waited = 0
    pwd_filled = False  # 防止死循环：密码只填一次
    while waited < max_wait:
        url = page.url.lower()

        # 已登录成功
        if "login.live.com" not in url and "login.microsoft" not in url:
            print("  登录完成")
            break

        # 密码输入框出现
        pwd = page.locator("input[name='passwd']").first
        if pwd.count() == 0 or not pwd.is_visible():
            pwd = page.locator("#i0118").first
        if pwd.count() == 0 or not pwd.is_visible():
            pwd = page.locator("input[type='password']").first
        if pwd.count() > 0 and pwd.is_visible():
            if pwd_filled:
                # 已填过密码，正在等待跳转，不再重复填写
                page.wait_for_timeout(2000)
                waited += 2
                continue
            pwd.click()
            pwd.fill("")
            pwd.type(password, delay=80)
            pwd_filled = True
            # 等待"登录"按钮可用
            page.wait_for_timeout(1500)

            for btn_sel in [
                "#idSIButton9",
                "input[type='submit']",
                "button[type='submit']",
                "button:has-text('登录')",
                "input[value='登录']",
                "button:has-text('下一步')",
                "[role='button']:has-text('登录')",
            ]:
                sign_btn = page.locator(btn_sel).first
                if sign_btn.count() > 0 and sign_btn.is_visible() and sign_btn.is_enabled():
                    sign_btn.click()
                    print("  已点击登录")
                    page.wait_for_timeout(3000)
                    # 等待跳转
                    try:
                        page.wait_for_url(lambda u: "login.live.com" not in u.lower() and "login.microsoft" not in u.lower(), timeout=25000)
                        break
                    except Exception:
                        pass
                    break
            else:
                # 回退：按回车
                print("  ⚠ 未找到登录按钮，尝试回车")
                pwd.press("Enter")
                page.wait_for_timeout(3000)
            continue

        # 切换登录方式（passkey失败后）
        switch_link = page.locator("#idA_PWD_SwitchToCredPicker").first
        if switch_link.count() > 0 and switch_link.is_visible():
            switch_link.click()
            page.wait_for_timeout(3000)
            continue

        alt_login = page.locator("a:has-text('使用另一种方式登录')").first
        if alt_login.count() > 0 and alt_login.is_visible():
            alt_login.click()
            page.wait_for_timeout(3000)
            continue

        # 选择"使用密码"tile
        use_pwd = page.locator('[aria-label="使用密码"]').first
        if use_pwd.count() > 0 and use_pwd.is_visible():
            use_pwd.click()
            page.wait_for_timeout(3000)
            continue

        # 取消按钮
        cancel_btn = page.locator("#idBtn_Back").first
        if cancel_btn.count() > 0 and cancel_btn.is_visible():
            cancel_btn.click()
            page.wait_for_timeout(2000)
            continue

        # 保持登录弹窗：点"否"
        for no_sel in [
            "input[value='否']",
            "#declineButton",
            "button:has-text('否')",
            "#idBtn_Back",
        ]:
            stay_no = page.locator(no_sel).first
            if stay_no.count() > 0 and stay_no.is_visible():
                stay_no.click()
                print("  已点击'否'（保持登录弹窗）")
                page.wait_for_timeout(2000)
                break

        page.wait_for_timeout(2000)
        waited += 2

        if waited >= 30:
            print(f"  等待登录超时({waited}s)，当前URL: {page.url[:100]}")

    # 3. 兜底处理保持登录弹窗
    page.wait_for_timeout(2000)
    for no_sel in [
        "input[value='否']",
        "#declineButton",
        "button:has-text('否')",
        "#idBtn_Back",
    ]:
        try:
            no_btn = page.locator(no_sel).first
            if no_btn.count() > 0 and no_btn.is_visible():
                no_btn.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            continue

    # 等待最终跳转
    try:
        page.wait_for_url(lambda u: "login.live.com" not in u.lower(), timeout=30000)
    except Exception:
        pass

    url_now = page.url.lower()
    print(f"  登录后URL: {url_now[:100]}")
    if "login.live.com" in url_now or "login.microsoft" in url_now:
        print("  ✗ 登录未成功，仍在登录页面（可能需要验证码/2FA/密码错误）")
        return False
    return True


# ======================== Dashboard 数据解析 ========================

def _extract_json_array(html: str, key: str) -> str | None:
    """从 HTML 中用括号计数提取 JSON 数组"""
    idx = html.find(key)
    if idx < 0:
        return None
    # 跳过 key 和冒号
    start = html.find("[", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return None


def _extract_json_object(html: str, key: str) -> str | None:
    """从 HTML 中用括号计数提取 JSON 对象"""
    idx = html.find(key)
    if idx < 0:
        return None
    start = html.find("{", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(html)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return None


def parse_dashboard_data(page) -> dict:
    """从页面提取任务数据"""
    result = {
        "claimable_points": 0,
        "claim_entries": [],
        "daily_set_items": [],
        "pc_search_total": DEFAULT_PC_SEARCHES,
        "pc_search_done": 0,
        "mobile_search_total": DEFAULT_MOBILE_SEARCHES,
        "mobile_search_done": 0,
    }

    html = page.content()

    # 提取 dailySetItems
    arr_text = _extract_json_array(html, "dailySetItems")
    if arr_text:
        try:
            items = json.loads(arr_text)
        except json.JSONDecodeError:
            try:
                fixed = arr_text.replace('\\"', '"').replace("\\\\", "\\")
                items = json.loads(fixed)
            except json.JSONDecodeError:
                items = []

        today_str = datetime.now().strftime("%m/%d/%Y")
        for item in items:
            item_date = item.get("date", "")
            if not item_date or item.get("isCompleted") or item_date != today_str:
                continue
            result["daily_set_items"].append({
                "title": item.get("title", ""),
                "points": item.get("points", 0),
                "destination": item.get("destination", ""),
                "offerId": item.get("offerId", ""),
                "date": item_date,
            })

        if not result["daily_set_items"]:
            # 回退：用最新日期
            dates = [item.get("date", "") for item in items if item.get("date") and not item.get("isCompleted")]
            if dates:
                latest = sorted(dates)[-1]
                for item in items:
                    if item.get("date") == latest and not item.get("isCompleted"):
                        result["daily_set_items"].append({
                            "title": item.get("title", ""),
                            "points": item.get("points", 0),
                            "destination": item.get("destination", ""),
                            "offerId": item.get("offerId", ""),
                            "date": latest,
                        })

    # 提取 pointClaim
    obj_text = _extract_json_object(html, "pointClaim")
    if obj_text:
        try:
            claim_data = json.loads(obj_text)
            result["claimable_points"] = claim_data.get("points", 0)
            result["claim_entries"] = claim_data.get("entries", [])
        except json.JSONDecodeError:
            try:
                fixed = obj_text.replace('\\"', '"').replace("\\\\", "\\")
                claim_data = json.loads(fixed)
                result["claimable_points"] = claim_data.get("points", 0)
            except json.JSONDecodeError:
                pass

    # 从页面文本提取进度
    body_text = page.evaluate("() => document.body.innerText || ''")

    for pattern in [
        r'必应搜索[:\s]*(\d+)\s*/\s*(\d+)',
        r'搜索[:\s]*(\d+)\s*/\s*(\d+)',
    ]:
        m = re.search(pattern, body_text)
        if m:
            result["pc_search_done"] = int(m.group(1))
            result["pc_search_total"] = int(m.group(2))
            break

    mobile_match = re.search(r'移动搜索[:\s]*(\d+)\s*/\s*(\d+)', body_text)
    if mobile_match:
        result["mobile_search_done"] = int(mobile_match.group(1))
        result["mobile_search_total"] = int(mobile_match.group(2))

    daily_match = re.search(r'活动[:\s]*(\d+)\s*/\s*(\d+)', body_text)
    if daily_match:
        result["daily_activities_done"] = int(daily_match.group(1))
        result["daily_activities_total"] = int(daily_match.group(2))

    return result


# ======================== 核心操作 ========================

def random_sleep(short_range=(0.5, 2.0), long_prob=0.1, long_range=(3, 8)):
    if long_prob and random.random() < long_prob:
        time.sleep(random.uniform(*long_range))
        return
    time.sleep(random.uniform(*short_range))


def safe_goto(page, url: str, timeout: int = 30000, wait_extra: int = 3000):
    try:
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
    except Exception:
        pass
    # 等待React渲染完成
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(wait_extra)


def claim_available_points(page) -> int:
    """点击"可领取→领取"按钮（两段式：打开弹窗 → 确认领取）"""
    print("  检查可领取积分...")

    # 步骤1: 点击"可领取"卡片打开弹窗
    opened_dialog = False
    for selector in [
        "button:has-text('可领取')",
        "[role='button']:has-text('可领取')",
    ]:
        try:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                page.wait_for_timeout(2000)
                opened_dialog = True
                break
        except Exception:
            continue

    if not opened_dialog:
        print("  未找到可领取按钮")
        return 0

    # 步骤2: 弹窗内点击"领取积分"
    try:
        dialog = page.locator("[role='dialog']").first
        dialog.wait_for(state="visible", timeout=10000)
        page.wait_for_timeout(1000)

        # 找弹窗内的"领取积分"按钮
        claim_btn = dialog.locator("button:has-text('领取积分')").first
        if claim_btn.count() == 0:
            claim_btn = dialog.locator("button:has-text('领取')").last  # 弹窗内第二个"领取"按钮
        if claim_btn.count() > 0 and claim_btn.is_visible():
            claim_btn.click()
            print("  已点击弹窗内领取积分")
            page.wait_for_timeout(3000)
            # 等待弹窗关闭
            try:
                page.wait_for_selector("[role='dialog']", state="detached", timeout=10000)
            except Exception:
                pass
            return 1

        print("  弹窗内未找到领取按钮")
    except Exception as e:
        print(f"  处理弹窗失败: {e}")

    return 0


def _handle_activity_page(page) -> bool:
    """处理测验/投票/探索类活动页面 — 返回是否成功交互"""
    page.wait_for_timeout(3000)

    interacted = False

    # 测验类: 循环答题(最多12题)
    for q_idx in range(12):
        answered = False

        # 尝试点击答案选项
        for ans_sel in [
            ".rq_answer",
            "[data-isOption='true']",
            ".btOption",
            ".quiz-option",
            ".answer-option",
            "button.answer-btn",
            "div.rq_answers > div",
            "[role='radio']",
            "label[role='radio']",
            ".option-card",
            ".choice-option",
            "input[type='radio']",
            ".poll-option",
            "button:has-text('投票')",
        ]:
            try:
                options = page.locator(ans_sel)
                count = options.count()
                if count > 0:
                    # 优先点第一个可见的
                    for i in range(count):
                        opt = options.nth(i)
                        if opt.is_visible():
                            opt.click(delay=random.randint(100, 300))
                            page.wait_for_timeout(random.randint(1200, 2200))
                            answered = True
                            interacted = True
                            break
                    if answered:
                        break
            except Exception:
                continue

        if not answered:
            # 尝试点击任意可点击的答案区域
            try:
                answer_zone = page.locator(".rq_answers, .bt_poll, .quiz-content, [data-testid='quiz-body']")
                if answer_zone.count() > 0:
                    clickable = answer_zone.first.locator("a, button, [role='button'], [tabindex]")
                    if clickable.count() > 0:
                        clickable.first.click()
                        page.wait_for_timeout(random.randint(1500, 2500))
                        interacted = True
            except Exception:
                pass

        # 尝试点"下一题"/"Next"按钮
        for next_sel in [
            ".rq_nextBtn",
            "#nextQuestionbtn",
            "button:has-text('Next')",
            "button:has-text('下一题')",
            "a:has-text('Next')",
            "a:has-text('下一题')",
            ".nextQuestion",
            "[data-testid='next-button']",
        ]:
            try:
                next_btn = page.locator(next_sel).first
                if next_btn.count() > 0 and next_btn.is_visible():
                    next_btn.click()
                    page.wait_for_timeout(random.randint(1500, 3000))
                    interacted = True
                    break
            except Exception:
                continue

        # 没有可交互的元素了，可能已结束
        if not answered:
            break

    # 尝试点"完成"/"关闭"/"Done"
    for done_sel in [
        "button:has-text('Done')",
        "button:has-text('完成')",
        "button:has-text('关闭')",
        ".rq_closeBtn",
        ".rq_doneBtn",
        "[aria-label='Close']",
        "[aria-label='关闭']",
        ".close-button",
        "#closeButton",
        "a:has-text('完成')",
    ]:
        try:
            done_btn = page.locator(done_sel).first
            if done_btn.count() > 0 and done_btn.is_visible():
                done_btn.click()
                page.wait_for_timeout(2000)
                interacted = True
                break
        except Exception:
            continue

    page.wait_for_timeout(2000)
    return interacted


def _find_and_click_daily_card(page, item: dict) -> bool:
    """在仪表板上查找并点击每日套餐活动卡片，返回是否成功点击"""
    title = item.get("title", "")
    offer_id = item.get("offerId", "")
    destination = item.get("destination", "")

    # 策略1: 通过标题文本匹配卡片
    if title:
        for t in [title, title[:15], title.split(" ")[0]]:
            try:
                el = page.locator(f"text='{t}'").first
                if el.count() > 0 and el.is_visible():
                    # 往上找可点击的父元素
                    clickable = el.locator("xpath=ancestor::a | ancestor::button | ancestor::*[@role='button']").first
                    if clickable.count() > 0:
                        clickable.click(delay=random.randint(50, 200))
                        return True
            except Exception:
                continue

    # 策略2: 通过 offerId 匹配
    if offer_id:
        for sel in [
            f"[data-offer-id='{offer_id}']",
            f"[data-offerid='{offer_id}']",
        ]:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    el.click(delay=random.randint(50, 200))
                    return True
            except Exception:
                continue

    # 策略3: 通过 destination URL 匹配
    if destination:
        url_part = destination.strip("/").split("/")[-1][:30]
        if url_part:
            try:
                el = page.locator(f"a[href*='{url_part}']").first
                if el.count() > 0 and el.is_visible():
                    el.click(delay=random.randint(50, 200))
                    return True
            except Exception:
                pass

    return False


def do_daily_set_tasks(page, daily_items: list[dict]) -> int:
    """执行每日任务 — 点击仪表板卡片"""
    if not daily_items:
        print("  未找到每日任务")
        return 0

    print(f"  待完成每日任务: {len(daily_items)} 个")
    completed = 0

    for idx, item in enumerate(daily_items):
        title = item.get("title", f"任务{idx+1}")
        destination = item.get("destination", "")
        points = item.get("points", 0)

        print(f"  [{idx+1}/{len(daily_items)}] {title} (+{points})")

        if not destination:
            print(f"    无目标URL，跳过")
            continue

        url_before = page.url

        if not _find_and_click_daily_card(page, item):
            print(f"    未找到对应卡片")
            continue

        page.wait_for_timeout(2500)
        try:
            if len(page.context.pages) > 1:
                new_page = page.context.pages[-1]
                if new_page != page:
                    new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                    _handle_activity_page(new_page)
                    new_page.close()
                    completed += 1
                    print(f"    完成 (新标签)")
            elif page.url != url_before and "rewards.bing.com/dashboard" not in page.url:
                _handle_activity_page(page)
                completed += 1
                print(f"    完成 (导航)")
            else:
                _handle_activity_page(page)
                completed += 1
                print(f"    完成 (弹窗)")
        except Exception as e:
            print(f"    处理失败: {e}")

        safe_goto(page, REWARDS_DASHBOARD, wait_extra=2000)
        random_sleep(short_range=(1, 3))

    return completed


def do_dashboard_activities(page) -> int:
    """处理仪表板上的每日活动卡片（需点击卡片才能完成）"""
    print("  扫描仪表板活动卡片...")
    completed = 0
    clicked_urls: set[str] = set()

    # 按优先级排列: 越具体的放前面
    activity_selectors = [
        "a[href*='/rewards/daily']",
        "[data-testid='activity-card'] a, [data-testid='activity-card'] button",
        "a[href*='/rewards/quiz']",
        "a[href*='/rewards/poll']",
        "a[href*='/rewards/explore']",
        "a[href*='/rewards/earn']",
        "button:has-text('赚取积分')",
        "a:has-text('赚取积分')",
        "a:has-text('了解详情')",
        "[aria-label*='积分']:not([aria-label*='搜索'])",
    ]

    # 先排除已完成/已领取的元素
    completed_indicators = [
        "已完成", "已领取", "Completed",
        "[aria-label*='done']", "[aria-label*='complete']",
        "svg[class*='check']", ".completed",
    ]

    def _looks_completed(el) -> bool:
        try:
            parent_html = el.locator("xpath=ancestor::*[1]").inner_html()
            for indicator in completed_indicators:
                if indicator.lower() in parent_html.lower():
                    return True
        except Exception:
            pass
        return False

    for sel in activity_selectors:
        try:
            entries = page.locator(sel)
            count = entries.count()
            for i in range(count):
                entry = entries.nth(i)
                if not entry.is_visible():
                    continue
                if _looks_completed(entry):
                    continue

                href = ""
                try:
                    href = entry.get_attribute("href") or ""
                except Exception:
                    pass
                if href and href in clicked_urls:
                    continue
                if href:
                    clicked_urls.add(href)

                url_before = page.url
                try:
                    entry.click(delay=random.randint(50, 200))
                    page.wait_for_timeout(random.randint(2500, 4000))
                except Exception:
                    continue

                # 处理点击后的结果
                try:
                    if len(page.context.pages) > 1:
                        new_page = page.context.pages[-1]
                        if new_page != page:
                            new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                            _handle_activity_page(new_page)
                            new_page.close()
                            completed += 1
                            print(f"    活动完成 (新标签)")
                    elif page.url != url_before and "rewards.bing.com/dashboard" not in page.url:
                        _handle_activity_page(page)
                        completed += 1
                        print(f"    活动完成: {page.url[:80]}")
                    else:
                        _handle_activity_page(page)
                        completed += 1
                        print(f"    活动完成 (弹窗)")
                except Exception as e:
                    print(f"    活动处理异常: {e}")

                safe_goto(page, REWARDS_DASHBOARD, wait_extra=2000)
                page.wait_for_timeout(1500)
        except Exception:
            continue

    if completed == 0:
        print("  未找到未完成的仪表板活动卡片")
    else:
        print(f"  完成 {completed} 个仪表板活动")
    return completed


def do_bing_search(page, keyword: str):
    """执行一次Bing搜索 — 优先用直接URL避免搜索框问题"""
    try:
        # 直接用搜索URL，最可靠
        search_url = f"https://www.bing.com/search?q={quote_plus(keyword)}&form=QBRE"
        safe_goto(page, search_url)
        random_sleep(short_range=(4, 7))
        _random_scroll(page)
        _random_click_result(page)
    except Exception as e:
        print(f"    搜索异常: {e}")
        # 最终回退：Bing首页手动搜索
        try:
            safe_goto(page, BING_HOME)
            search_box = page.locator("#sb_form_q").first
            if search_box.count() > 0 and search_box.is_visible():
                search_box.fill(keyword)
                search_box.press("Enter")
                random_sleep(short_range=(3, 6))
        except Exception:
            pass


def _random_scroll(page):
    try:
        if random.random() > 0.7:
            return
        scroll_y = random.randint(200, 800)
        page.evaluate(f"window.scrollBy(0, {scroll_y})")
        time.sleep(random.uniform(0.5, 2))
        if random.random() < 0.4:
            page.evaluate(f"window.scrollBy(0, {-scroll_y//2})")
    except Exception:
        pass


def _random_click_result(page):
    try:
        if random.random() > 0.5:
            return
        results = page.locator("#b_results > li.b_algo h2 a")
        count = results.count()
        if count > 0:
            target = results.nth(random.randint(0, min(count, 4) - 1))
            target.click()
            random_sleep(short_range=(2, 4))
            page.go_back()
    except Exception:
        pass


def do_searches(page, count: int, keywords: list, tag: str):
    """执行批量搜索"""
    if count <= 0:
        print(f"  {tag} 无需搜索")
        return

    if not keywords:
        print(f"  {tag} 无关键词")
        return

    print(f"  {tag} 搜索: {count} 次")
    cycle = _keyword_cycle(keywords)

    for i in range(count):
        keyword = next(cycle)
        do_bing_search(page, keyword)
        if i < count - 1:
            delay = random.randint(15, 60)
            if i % 5 == 0 and i > 0:
                delay = random.randint(30, 90)
            print(f"    进度 {i+1}/{count}, 等待{delay}s...")
            time.sleep(delay)


def _keyword_cycle(words: list):
    if not words:
        return
    last = None
    while True:
        pool = words.copy()
        random.shuffle(pool)
        if last is not None and len(pool) > 1 and pool[0] == last:
            pool[0], pool[1] = pool[1], pool[0]
        for item in pool:
            yield item
            last = item


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
