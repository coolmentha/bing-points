"""浏览器会话通用操作：随机行为模拟、导航、批量搜索"""
import random
import time
from urllib.parse import quote_plus

from utils.config import BING_HOME


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
