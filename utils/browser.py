"""浏览器会话通用操作：随机行为模拟、导航、批量搜索"""
import random
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, quote_plus, urlsplit

from utils.config import BING_HOME


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


class BrowserRiskControlError(RuntimeError):
    """搜索中出现微软验证码或限制页时停止当前账号。"""


class SearchStatus(str, Enum):
    """批量搜索的明确终态，不能再用 None 混淆成功与未执行。"""

    COMPLETED = "completed"
    NO_KEYWORDS = "no_keywords"
    SCORE_STALLED = "score_stalled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class SearchOutcome:
    status: SearchStatus
    requested: int
    completed: int

    @property
    def success(self) -> bool:
        return (
            self.status is SearchStatus.COMPLETED
            and self.requested > 0
            and self.completed >= self.requested
        )


def _detect_search_risk(page):
    """只认微软/Bing 的验证组件或明确验证页，忽略普通正文中的“验证码”。"""
    from utils.tasks import RiskDetectionError, detect_risk_control
    try:
        return detect_risk_control(page)
    except RiskDetectionError:
        # 检测失败单独分类，由 main 返回退出码 5，不能伪装成真实风控。
        raise


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


def _is_confirmed_bing_search(page, keyword: str) -> bool:
    """确认当前页面确实是本次关键词的 Bing 搜索结果页且 DOM 可读。"""
    try:
        parsed = urlsplit(str(page.url))
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if host != "bing.com" and not host.endswith(".bing.com"):
        return False
    if parsed.path.rstrip("/") != "/search":
        return False

    try:
        query = parse_qs(parsed.query).get("q", [""])[0].strip()
    except Exception:
        return False
    if query != keyword.strip():
        return False

    try:
        state = page.evaluate(
            """() => ({
                readyState: document.readyState,
                hasBody: Boolean(document.body)
            })"""
        )
    except Exception:
        return False
    return (
        isinstance(state, dict)
        and state.get("readyState") in ("interactive", "complete")
        and state.get("hasBody") is True
    )


def _navigate_confirmed(page, url: str) -> bool:
    """执行一次真实导航并返回是否发生成功导航。

    不能只检查最终 URL/DOM：旧页面可能已经停留在同一个目标 URL，
    此时被吞掉的导航失败会造成假成功。
    """
    before_url = ""
    try:
        before_url = str(page.url or "")
    except Exception:
        pass
    try:
        response = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        if response is None or (hasattr(response, "ok") and not response.ok):
            return False
    except Exception:
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(3000)
    except Exception:
        pass
    try:
        after_url = str(page.url or "")
    except Exception:
        return False
    return bool(after_url)


def do_bing_search(page, keyword: str) -> bool:
    """执行一次 Bing 搜索，只有本次导航成功且到达对应结果页才返回 True。"""
    from utils.tasks import RiskDetectionError

    search_url = f"https://www.bing.com/search?q={quote_plus(keyword)}&form=QBRE"
    try:
        if not _navigate_confirmed(page, search_url):
            print("    搜索导航失败，尝试首页回退")
        elif not _is_confirmed_bing_search(page, keyword):
            print("    搜索导航未到达目标 Bing 结果页，尝试首页回退")
        else:
            risk = _detect_search_risk(page)
            if risk:
                raise BrowserRiskControlError(risk)
            random_sleep(short_range=(4, 7))
            _random_scroll(page)
            _random_click_result(page)
            risk = _detect_search_risk(page)
            if risk:
                raise BrowserRiskControlError(risk)
            return True
    except (BrowserRiskControlError, RiskDetectionError):
        raise
    except Exception as exc:
        print(f"    搜索异常: {exc}")

    # 最终回退：Bing 首页手动搜索；safe_goto 会吞导航异常，因此必须再次验证 URL/DOM。
    try:
        if not _navigate_confirmed(page, BING_HOME):
            return False
        search_box = page.locator("#sb_form_q").first
        if search_box.count() > 0 and search_box.is_visible():
            search_box.fill(keyword)
            search_box.press("Enter")
            random_sleep(short_range=(3, 6))
            return _is_confirmed_bing_search(page, keyword)
    except (BrowserRiskControlError, RiskDetectionError):
        raise
    except Exception as exc:
        print(f"    搜索回退失败: {exc}")
    return False


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


def do_searches(page, count: int, keywords: list, tag: str, verify=None):
    """执行批量搜索并返回明确终态。

    verify: 可选回调，返回当前积分余额。每4次搜索核对一次，
    连续2次无增长则提前结束。由于当前无法可靠区分积分封顶与未计分，
    调用方必须把 SCORE_STALLED 视为未完成。
    """
    from utils.tasks import RiskDetectionError
    if count <= 0:
        print(f"  {tag} 无需搜索")
        return SearchOutcome(SearchStatus.COMPLETED, count, 0)

    if not keywords:
        print(f"  {tag} 无关键词")
        return SearchOutcome(SearchStatus.NO_KEYWORDS, count, 0)
    print(f"  {tag} 搜索: {count} 次")
    cycle = _keyword_cycle(keywords)

    prev_score = verify() if verify else None
    if prev_score:
        print(f"    当前积分: {prev_score}")
    stable_checks = 0

    completed = 0
    try:
        for i in range(count):
            keyword = next(cycle)
            if not do_bing_search(page, keyword):
                print(f"  {tag} 搜索导航失败，停止本任务（已完成 {completed}/{count} 次）")
                return SearchOutcome(SearchStatus.INTERRUPTED, count, completed)
            completed = i + 1
            risk = _detect_search_risk(page)
            if risk:
                raise BrowserRiskControlError(risk)
            if verify and (i + 1) % 4 == 0:
                score = verify()
                if score and prev_score and score == prev_score:
                    stable_checks += 1
                    print(f"    积分无增长({score})，连续 {stable_checks} 次")
                    if stable_checks >= 2:
                        # 最终确认一次，避免计分延迟导致误停
                        time.sleep(30)
                        confirm = verify()
                        if confirm and confirm == score:
                            print(f"  {tag} 积分已达上限，提前结束（实际执行 {i + 1} 次）")
                            return SearchOutcome(SearchStatus.SCORE_STALLED, count, completed)
                        stable_checks = 0
                        if confirm:
                            prev_score = confirm
                else:
                    stable_checks = 0
                if score:
                    prev_score = score
            if i < count - 1:
                delay = random.randint(15, 60)
                if i % 5 == 0 and i > 0:
                    delay = random.randint(30, 90)
                print(f"    进度 {i+1}/{count}, 等待{delay}s...")
                time.sleep(delay)
    except (BrowserRiskControlError, RiskDetectionError):
        raise
    except Exception as exc:
        print(f"  {tag} 搜索中断: {exc}")
        return SearchOutcome(SearchStatus.INTERRUPTED, count, completed)

    return SearchOutcome(SearchStatus.COMPLETED, count, completed)


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
