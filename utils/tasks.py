"""Rewards 任务执行：领积分 / 每日任务 / 每日活动 / 日常任务"""
import random
import re

from utils.browser import _random_scroll, random_sleep, safe_goto
from utils.config import REWARDS_DASHBOARD, REWARDS_EARN_PAGE


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
            if not claim_btn.is_enabled():
                # 没有可领的积分（按钮禁用），直接关闭弹窗
                print("  无可领取积分，跳过")
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
                return 0
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


def extract_daily_earn_links(panel_html: str) -> list[dict]:
    """从 /earn 页「日常任务」面板 HTML 中提取追踪搜索链接"""
    start = panel_html.find('aria-label="日常任务"')
    if start < 0:
        return []
    # 触发按钮之后紧跟的 DisclosurePanel 才是任务列表（下一个触发按钮之前）
    panel_start = panel_html.find("react-aria-DisclosurePanel", start)
    if panel_start < 0:
        return []
    end = panel_html.find('slot="trigger">', panel_start + 100)
    panel = panel_html[panel_start:end if end > 0 else panel_start + 40000]

    items: list[dict] = []
    seen: set[str] = set()
    # 逐个卡片块匹配（部分卡片用 <span href> 而非 <a>），便于结合卡片内文本判断是否已完成
    for m in re.finditer(r'<(?:a|span)\s[^>]*href="([^"]+)"[^>]*target="_blank"[^>]*>(.*?)</(?:a|span)>', panel, re.S):
        href = m.group(1).replace("&amp;", "&")
        block = m.group(2)
        if href in seen:
            continue
        if "已完成" in block or "Completed" in block:
            continue
        # 只做搜索类任务：带追踪参数的 bing 搜索链接
        if "bing.com/search" not in href:
            continue
        title_m = re.search(r'<img alt="([^"]+)"', block)
        title = title_m.group(1) if title_m else ""
        seen.add(href)
        items.append({"title": title, "url": href})
    return items


def do_daily_earn_tasks(page) -> int:
    """完成 /earn 页「日常任务」— 点击任务卡片（必须真实点击，直接访问URL不计分）"""
    print("  打开积分赚取页...")
    safe_goto(page, REWARDS_EARN_PAGE, wait_extra=3000)

    items = extract_daily_earn_links(page.content())
    if not items:
        print("  未找到待完成的日常任务")
        return 0

    print(f"  待完成日常任务: {len(items)} 个")
    completed = 0
    for idx, item in enumerate(items):
        title = item.get("title") or f"任务{idx+1}"
        print(f"  [{idx+1}/{len(items)}] {title}")
        try:
            # 每个任务前回到赚取页并点击对应卡片
            safe_goto(page, REWARDS_EARN_PAGE, wait_extra=2000)
            card = page.locator(f'[target="_blank"]:has(img[alt="{title}"])').first
            if card.count() == 0 or not card.is_visible():
                print(f"    未找到任务卡片，跳过")
                continue

            new_page = None
            try:
                with page.context.expect_page(timeout=10000):
                    card.click(delay=random.randint(50, 200))
                new_page = page.context.pages[-1]
            except Exception:
                new_page = None

            if new_page is not None and new_page != page:
                new_page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(random.randint(3000, 5000))
                _random_scroll(new_page)
                new_page.close()
            else:
                # 未开新标签：可能同页跳转或弹窗，稍作停留
                page.wait_for_timeout(random.randint(3000, 5000))
                _random_scroll(page)
            completed += 1
            print(f"    完成")
        except Exception as e:
            print(f"    失败: {e}")
        random_sleep(short_range=(1, 3))

    print(f"  完成 {completed} 个日常任务")
    return completed
