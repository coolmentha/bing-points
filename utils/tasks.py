"""Rewards 任务执行：领积分 / 每日任务 / 每日活动 / 日常任务"""
import heapq
import html as html_lib
import json
import math
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from utils.browser import _random_scroll, random_sleep, safe_goto
from utils.config import EDGE_USER_DATA_DIR, REWARDS_DASHBOARD, REWARDS_EARN_PAGE


EDGE_STREAK_LAUNCH_URL = "https://www.microsoft.com/edge/launch/rewards-browsing-streak1"
EDGE_STREAK_REDIRECT_TIMEOUT_MS = 15000
EDGE_STREAK_POLL_INTERVAL_MS = 500
EDGE_STREAK_ACTIVATION_TEXT = "开始在 Edge 上浏览"
EDGE_STREAK_ACTIVATION_POLL_MS = 500
EDGE_STREAK_ACTIVATION_TIMEOUT_MS = 10000


@dataclass(frozen=True)
class EdgeStreakEntry:
    """Edge 活动入口详情；只允许清理本次新建且仍可追踪的页面。"""

    activity_page: object | None
    created_pages: tuple
    can_cleanup: bool


@dataclass(frozen=True)
class EdgeStreakActivation:
    """点击激活链接后的明确结果；未确认新增 Bing 页时不回退旧页面。"""

    browsing_page: object | None
    created_pages: tuple
    activated: bool


class EdgeStreakStatus(str, Enum):
    """Edge 浏览任务的明确结果；身份未知不能与进度已完成混用。"""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    IDENTITY_MISMATCH = "identity_mismatch"
    IDENTITY_UNVERIFIED = "identity_unverified"
    ENTRY_FAILED = "entry_failed"
    ACTIVATION_FAILED = "activation_failed"
    INVALID_PROGRESS = "invalid_progress"
    ERROR = "error"


@dataclass(frozen=True)
class EdgeStreakResult:
    status: EdgeStreakStatus
    progress: tuple[int, int] | None = None
    identity: str | None = None

    @property
    def success(self) -> bool:
        return self.status is EdgeStreakStatus.COMPLETED


MICROSOFT_HOSTS = (
    "bing.com", "microsoft.com", "live.com", "microsoftonline.com",
)

# 微软验证页的精确端点；不使用正文关键词或通用 captcha DOM，避免误报。
RISK_CONTROL_PATHS = (
    "/challenge", "/captcha", "/auth/challenge", "/login/challenge",
)


class RewardsRiskControlError(RuntimeError):
    """页面出现微软验证码或限制页时立即停止当前账号。"""


class RiskDetectionError(RuntimeError):
    """风控检测过程中页面不可访问；仅供底层判断是否继续，不能直接判风控。"""


def detect_risk_control(page) -> str | None:
    return detect_microsoft_risk(page)


def _is_microsoft_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return False
    return any(host == item or host.endswith("." + item) for item in MICROSOFT_HOSTS)


def _url_is_verification(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url or "")
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower().rstrip("/")
    except Exception:
        return False
    if not any(host == item or host.endswith("." + item) for item in MICROSOFT_HOSTS):
        return False
    return path in RISK_CONTROL_PATHS


def _page_risk_evidence(page):
    """读取页面标题仅用于日志；读取失败时返回空字符串，不应升级成风控。"""
    try:
        value = page.evaluate("() => document.title || ''")
        return value if isinstance(value, str) else ""
    except Exception:
        return ""


def detect_microsoft_risk(page) -> str | None:
    """只识别微软验证网页；普通搜索、第三方 captcha 和正文关键词不触发。"""
    try:
        url = page.url or ""
    except Exception:
        return None
    if not _is_microsoft_url(url) or not _url_is_verification(url):
        return None
    title = (_page_risk_evidence(page) or "").strip()[:120]
    return f"微软验证页面; url={url[:200]}; title={title}"

def _ensure_no_risk_control(page) -> None:
    reason = detect_risk_control(page)
    if reason:
        raise RewardsRiskControlError(reason)


def claim_available_points(page) -> int:
    """点击"可领取→领取"按钮（两段式：打开弹窗 → 确认领取）"""
    _ensure_no_risk_control(page)
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
        except (RewardsRiskControlError, RiskDetectionError):
            raise
        except Exception:
            continue
        finally:
            _ensure_no_risk_control(page)

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
            _ensure_no_risk_control(page)
            print("  已点击弹窗内领取积分")
            page.wait_for_timeout(3000)
            # 等待弹窗关闭
            try:
                page.wait_for_selector("[role='dialog']", state="detached", timeout=10000)
            except Exception:
                pass
            return 1

        print("  弹窗内未找到领取按钮")
    except (RewardsRiskControlError, RiskDetectionError):
        raise
    except Exception as e:
        print(f"  处理弹窗失败: {e}")

    return 0


def _handle_activity_page(page) -> bool:
    """处理测验/投票/探索类活动页面 — 返回是否成功交互"""
    _ensure_no_risk_control(page)
    page.wait_for_timeout(3000)
    _ensure_no_risk_control(page)

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
                            _ensure_no_risk_control(page)
                            answered = True
                            interacted = True
                            break
                    if answered:
                        break
            except (RewardsRiskControlError, RiskDetectionError):
                raise
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
                        _ensure_no_risk_control(page)
                        interacted = True
            except (RewardsRiskControlError, RiskDetectionError):
                raise
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
                    _ensure_no_risk_control(page)
                    interacted = True
                    break
            except (RewardsRiskControlError, RiskDetectionError):
                raise
            except Exception:
                continue

        # 没有可交互的元素了，可能已结束
        if not answered:
            break

    # 尝试点"完成"/"关闭"/"Done"；拼图类活动无需作答，点"跳过"即可拿分
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
        "button:has-text('跳过')",
        "a:has-text('跳过')",
        "[aria-label='跳过']",
        "[role='button']:has-text('跳过')",
    ]:
        try:
            done_btn = page.locator(done_sel).first
            if done_btn.count() > 0 and done_btn.is_visible():
                done_btn.click()
                page.wait_for_timeout(2000)
                _ensure_no_risk_control(page)
                interacted = True
                if "跳过" in done_sel:
                    # 点跳过后可能弹确认框
                    for confirm_sel in [
                        "[role='dialog'] button:has-text('跳过')",
                        "button:has-text('确认跳过')",
                        "[role='dialog'] button:has-text('确认')",
                        "[role='dialog'] button:has-text('确定')",
                    ]:
                        try:
                            confirm_btn = page.locator(confirm_sel).first
                            if confirm_btn.count() > 0 and confirm_btn.is_visible():
                                confirm_btn.click()
                                page.wait_for_timeout(2000)
                                _ensure_no_risk_control(page)
                                break
                        except (RewardsRiskControlError, RiskDetectionError):
                            raise
                        except Exception:
                            continue
                break
        except (RewardsRiskControlError, RiskDetectionError):
            raise
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
        except (RewardsRiskControlError, RiskDetectionError):
            raise
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
                except (RewardsRiskControlError, RiskDetectionError):
                    raise
                except Exception as e:
                    print(f"    活动处理异常: {e}")

                safe_goto(page, REWARDS_DASHBOARD, wait_extra=2000)
                page.wait_for_timeout(1500)
        except (RewardsRiskControlError, RiskDetectionError):
            raise
        except Exception:
            continue

    if completed == 0:
        print("  未找到未完成的仪表板活动卡片")
    else:
        print(f"  完成 {completed} 个仪表板活动")
    return completed


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def extract_daily_offer_tasks(panel_html: str) -> list[dict]:
    """提取 /earn「日常任务」中网页可完成的搜索与每日拼图。"""
    start = panel_html.find('aria-label="日常任务"')
    if start < 0:
        return []
    panel_start = panel_html.find("react-aria-DisclosurePanel", start)
    if panel_start < 0:
        return []
    end = panel_html.find('slot="trigger">', panel_start + 100)
    panel = panel_html[panel_start:end if end > 0 else panel_start + 40000]

    items: list[dict] = []
    seen: set[str] = set()
    pattern = r'<(?:a|span)\s[^>]*href="([^"]+)"[^>]*target="_blank"[^>]*>(.*?)</(?:a|span)>'
    for match in re.finditer(pattern, panel, re.S):
        href = html_lib.unescape(match.group(1))
        block = match.group(2)
        if href in seen:
            continue
        if "已完成" in block or "Completed" in block:
            continue
        if "积分商城" in block or "仅限" in block:
            continue
        if "bing.com/search" in href:
            kind = "search"
        elif "bing.com/spotlight/imagepuzzle" in href:
            kind = "puzzle"
        else:
            continue
        title_match = re.search(r'<img alt="([^"]+)"', block)
        title = html_lib.unescape(title_match.group(1)) if title_match else ""
        if not title:
            title = _strip_html(block)[:80]
        seen.add(href)
        items.append({"title": title, "url": href, "kind": kind})
    return items


def extract_daily_earn_links(panel_html: str) -> list[dict]:
    """兼容旧接口：只返回可点击的追踪搜索任务。"""
    return [
        {"title": item["title"], "url": item["url"]}
        for item in extract_daily_offer_tasks(panel_html)
        if item["kind"] == "search"
    ]


def extract_point_punch_cards(page_html: str) -> list[dict]:
    """提取明确奖励积分的网页 Punch Card，跳过 App/Xbox/购买类活动。"""
    cards: list[dict] = []
    seen: set[str] = set()
    pattern = r'<a\b([^>]*\bhref="([^"]*_punchcard[^"]*)"[^>]*)>(.*?)</a>'
    for match in re.finditer(pattern, page_html, re.S):
        href = html_lib.unescape(match.group(2)).split("?", 1)[0]
        if href.startswith("/"):
            href = "https://rewards.bing.com" + href
        if href in seen:
            continue
        block = match.group(3)
        card_text = _strip_html(block)
        if "奖励应用" in card_text or "积分商城" in card_text:
            continue
        points_match = re.search(r"\+\s*([\d,]+)", card_text)
        if not points_match:
            continue
        title_match = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        if not title_match:
            title_match = re.search(r'<img[^>]+alt="([^"]+)"', block, re.S)
        title = _strip_html(title_match.group(1)) if title_match else card_text[:80]
        if not title:
            continue
        seen.add(href)
        cards.append({
            "title": title,
            "url": href,
            "points": int(points_match.group(1).replace(",", "")),
            "text": card_text,
        })
    return cards


def _puzzle_goal(size: int = 9) -> tuple[int, ...]:
    return tuple(range(size - 1)) + (-1,)


def _puzzle_is_solved(state, size: int = 9) -> bool:
    return tuple(state) == _puzzle_goal(size)


def _puzzle_solvable(state, rows: int = 3, cols: int = 3) -> bool:
    values = [v for v in state if v != -1]
    inversions = sum(
        1
        for i, left in enumerate(values)
        for right in values[i + 1:]
        if left > right
    )
    if cols % 2:
        return inversions % 2 == 0
    blank_index = state.index(-1)
    blank_row_from_bottom = rows - blank_index // cols
    return (inversions + blank_row_from_bottom) % 2 == 1


def solve_sliding_puzzle(state, rows: int = 3, cols: int = 3):
    """用 A* 求 8 数码最短移动序列；返回待点击坐标，不可解返回 None。"""
    size = rows * cols
    start = tuple(state)
    goal = _puzzle_goal(size)
    if len(start) != size or set(start) != set(range(-1, size - 1)):
        return None
    if start == goal:
        return []
    if not _puzzle_solvable(start, rows, cols):
        return None

    def heuristic(board):
        distance = 0
        for index, value in enumerate(board):
            if value < 0:
                continue
            goal_y, goal_x = divmod(value, cols)
            y, x = divmod(index, cols)
            distance += abs(y - goal_y) + abs(x - goal_x)
        return distance

    frontier = [(heuristic(start), 0, start, ())]
    best = {start: 0}
    visited = 0
    while frontier and visited < 200000:
        _estimate, cost, board, moves = heapq.heappop(frontier)
        if cost != best.get(board):
            continue
        visited += 1
        if board == goal:
            return list(moves)
        blank = board.index(-1)
        y, x = divmod(blank, cols)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = y + dy, x + dx
            if not (0 <= ny < rows and 0 <= nx < cols):
                continue
            neighbor = ny * cols + nx
            candidate = list(board)
            candidate[blank], candidate[neighbor] = candidate[neighbor], candidate[blank]
            candidate_state = tuple(candidate)
            next_cost = cost + 1
            if next_cost >= best.get(candidate_state, 10**9):
                continue
            best[candidate_state] = next_cost
            heapq.heappush(
                frontier,
                (next_cost + heuristic(candidate_state), next_cost,
                 candidate_state, moves + ((ny, nx),)),
            )
    return None


def _read_puzzle_state(page):
    """按棋盘坐标读取 8 数码状态；空位记为 -1。"""
    try:
        slots = page.evaluate(r"""() => Array.from(document.querySelectorAll('#tiles > div.tile[x][y]'))
          .map((slot) => {
            const nested = Array.from(slot.querySelectorAll('*'))
              .find((el) => /^img\d+$/.test(el.id));
            return {
              x: Number(slot.getAttribute('x')),
              y: Number(slot.getAttribute('y')),
              image: nested ? Number(nested.id.slice(3)) : -1,
            };
          })""")
    except Exception:
        return None
    if not isinstance(slots, list) or len(slots) != 9:
        return None
    try:
        # Bing 的 x 是视觉行、y 是视觉列（tile.id = x + y）。
        by_position = {(int(item["x"]), int(item["y"])): int(item["image"]) for item in slots}
        state = [by_position[(row, col)] for row in range(3) for col in range(3)]
    except (KeyError, TypeError, ValueError):
        return None
    if sorted(state) != list(range(-1, 8)):
        return None
    return state


def _puzzle_completed(page) -> bool:
    try:
        congrats = page.locator("#congrats").first
        if congrats.count() > 0 and congrats.is_visible():
            return True
    except Exception:
        pass
    # 完成后页面会在 3 秒后自动跳转到 Spotlight，跳转成功同样视为完成。
    try:
        return "spotlight/imagepuzzle" not in (page.url or "").lower() and "spotlight" in (page.url or "").lower()
    except Exception:
        return False


def complete_image_puzzle(page) -> bool:
    """按真实点击完成 3x3 拼图；不牺牲步数去点“跳过拼图”。"""
    if _puzzle_completed(page):
        print("    拼图已经完成")
        return True

    for attempt in range(2):
        _ensure_no_risk_control(page)
        state = _read_puzzle_state(page)
        if state is None:
            print("    无法读取拼图棋盘")
            return False
        if _puzzle_is_solved(state):
            page.wait_for_timeout(2000)
            return _puzzle_completed(page)

        moves = solve_sliding_puzzle(state)
        if moves is None:
            print("    拼图状态不可解，放弃本次")
            return False
        print(f"    拼图求解: {len(moves)} 步（第 {attempt + 1} 次）")
        page.wait_for_timeout(random.randint(700, 1200))

        for index, (x, y) in enumerate(moves):
            if index % 8 == 0:
                _ensure_no_risk_control(page)
            tile = page.locator(f'#tiles > div.tile[x="{x}"][y="{y}"]').first
            if tile.count() == 0 or not tile.is_visible():
                print("    拼图棋盘发生变化，重新读取")
                break
            try:
                tile.hover()
                page.wait_for_timeout(random.randint(30, 90))
                tile.click(delay=random.randint(70, 160))
            except Exception as exc:
                print(f"    拼图点击失败: {str(exc).splitlines()[0][:80]}")
                break
            page.wait_for_timeout(random.randint(120, 240))

        page.wait_for_timeout(2200)
        if _puzzle_completed(page):
            print("    拼图完成")
            return True

    print("    拼图未完成，未使用跳过按钮冒充完成")
    return False


def read_streak_progress(page, label: str):
    """读取首页活动卡片的进度，如 Edge 分钟: 0/30、移动应用 签到: 0/1。"""
    try:
        text = page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return None
    patterns = {
        "edge": r"Microsoft Edge\s*\n\s*分钟:\s*(\d+)\s*/\s*(\d+)",
        "mobile": r"移动应用\s*\n\s*签到:\s*(\d+)\s*/\s*(\d+)",
    }
    match = re.search(patterns[label], text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _load_json_object(path: Path) -> dict | None:
    """只读解析 JSON 对象；缺失、锁定或损坏时返回 None，不触碰 profile。"""
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _has_account_value(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return bool(value)


def _edge_profile_directories(profile_dir: Path) -> list[Path]:
    """定位 Default/Profile N；也兼容直接传入单个 profile 目录。"""
    directories = []
    if re.match(r"^(default|profile \d+)$", profile_dir.name, re.I):
        directories.append(profile_dir)
    try:
        children = list(profile_dir.iterdir())
    except OSError:
        children = []
    for child in children:
        try:
            if child.is_dir() and re.match(r"^(default|profile \d+)$", child.name, re.I):
                directories.append(child)
        except OSError:
            continue
    return directories


def _normalize_email(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _selected_edge_profile(profile_dir=None) -> tuple[Path, Path | None, dict | None]:
    """返回 (User Data 根目录, 当前 profile 目录, Local State)。

    多 profile 且没有 last_used 时 profile 目录为 None，调用方必须安全失败，
    不能遍历所有 profile 猜测当前账号。
    """
    requested = Path(profile_dir) if profile_dir is not None else Path(EDGE_USER_DATA_DIR)
    root = requested
    if re.match(r"^(default|profile \d+)$", requested.name, re.I):
        root = requested.parent
        local_state_path = root / "Local State"
        if not local_state_path.is_file():
            local_state_path = requested / "Local State"
        local_state = _load_json_object(local_state_path)
        return root, requested, local_state

    local_state_path = root / "Local State"
    local_state = _load_json_object(local_state_path)
    profile_dirs = {
        directory.name.lower(): directory
        for directory in _edge_profile_directories(root)
    }
    if not profile_dirs:
        return root, None, local_state

    last_used = None
    if local_state:
        profile_state = local_state.get("profile", {})
        if isinstance(profile_state, dict):
            value = profile_state.get("last_used")
            if isinstance(value, str):
                last_used = value.strip()
    if last_used:
        selected = profile_dirs.get(last_used.lower())
        return root, selected, local_state

    info_cache = {}
    if local_state and isinstance(local_state.get("profile"), dict):
        cached = local_state["profile"].get("info_cache", {})
        if isinstance(cached, dict):
            info_cache = cached
    if len(info_cache) == 1:
        name = next(iter(info_cache))
        selected = profile_dirs.get(str(name).lower())
        if selected is not None:
            return root, selected, local_state

    if len(profile_dirs) == 1:
        return root, next(iter(profile_dirs.values())), local_state
    return root, None, local_state


def _preferences_account_emails(preferences: dict | None) -> list[str]:
    if not preferences:
        return []
    account_info = preferences.get("account_info")
    if not isinstance(account_info, list):
        return []
    emails = []
    for entry in account_info:
        if not isinstance(entry, dict):
            continue
        email = _normalize_email(entry.get("email"))
        if email and email not in emails:
            emails.append(email)
    return emails


def edge_profile_identity(profile_dir=None) -> str | None:
    """只读返回当前 Edge profile 的登录邮箱，无法唯一确定时返回 None。"""
    _root, selected, local_state = _selected_edge_profile(profile_dir)
    if selected is None:
        return None

    if local_state:
        profile_state = local_state.get("profile", {})
        info_cache = (
            profile_state.get("info_cache", {})
            if isinstance(profile_state, dict)
            else {}
        )
        if isinstance(info_cache, dict):
            cached = info_cache.get(selected.name)
            if not isinstance(cached, dict):
                for name, value in info_cache.items():
                    if str(name).lower() == selected.name.lower():
                        cached = value
                        break
            if isinstance(cached, dict):
                user_name = _normalize_email(cached.get("user_name"))
                if user_name:
                    return user_name

    preferences = _load_json_object(selected / "Preferences")
    emails = _preferences_account_emails(preferences)
    return emails[0] if len(emails) == 1 else None


def edge_browser_profile_matches(profile_dir, expected_email) -> bool:
    """目标邮箱必须与当前 profile 身份规范化后精确一致，未知一律不匹配。"""
    expected = _normalize_email(expected_email)
    if expected is None:
        return False
    actual = edge_profile_identity(profile_dir)
    return actual is not None and actual == expected


def edge_browser_profile_logged_in(profile_dir=None) -> bool:
    """只读判断当前 Edge profile 是否有登录证据，不遍历猜测其他 profile。"""
    if edge_profile_identity(profile_dir) is not None:
        return True

    # 兼容旧版 profile：Preferences 可能只有 account_name，没有 email。
    # 仅当当前 profile 已唯一确定时才接受该证据。
    _root, selected, _local_state = _selected_edge_profile(profile_dir)
    if selected is None:
        return False
    preferences = _load_json_object(selected / "Preferences")
    return bool(preferences and _has_account_value(preferences.get("account_info")))


def _is_edge_streak_url(url: str) -> bool:
    try:
        parsed = urlsplit(url or "")
        return (
            parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower() == "rewards.bing.com"
            and parsed.port in (None, 443)
            and parsed.path == "/promo/edge-browsing-streak"
        )
    except (TypeError, ValueError):
        return False


def _page_is_closed(page) -> bool:
    try:
        is_closed = getattr(page, "is_closed", None)
        return bool(is_closed()) if callable(is_closed) else False
    except Exception:
        return True


def _edge_streak_targets(page, existing_pages=None) -> list:
    """返回当前页和本次点击新增的标签页，绝不扫描历史标签页。"""
    targets = [page]
    try:
        pages = list(page.context.pages)
    except Exception:
        return targets
    for item in pages:
        if item is page:
            continue
        if existing_pages is not None and item in existing_pages:
            continue
        if item not in targets:
            targets.append(item)
    return targets


def _wait_for_edge_streak_target(
    page,
    targets,
    timeout_ms: int,
    existing_pages=None,
    include_new_pages: bool = True,
    additional_targets=None,
) -> object | None:
    """等待当前页或事件确认的新页跳转到活动页；坏页不能中断轮询。"""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    known_targets = list(targets)
    event_targets = additional_targets if additional_targets is not None else ()
    while True:
        for target in list(event_targets):
            if target not in known_targets:
                known_targets.append(target)
        for target in known_targets:
            if _page_is_closed(target):
                continue
            try:
                url = target.url
            except Exception:
                continue
            if _is_edge_streak_url(url):
                return target
        if include_new_pages:
            for target in _edge_streak_targets(page, existing_pages):
                if target not in known_targets:
                    known_targets.append(target)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        delay_ms = max(1, min(EDGE_STREAK_POLL_INTERVAL_MS, math.ceil(remaining * 1000)))
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            time.sleep(delay_ms / 1000)


def _is_edge_activation_url(url: str) -> bool:
    """仅接受 Bing 首页激活跳转，避免把任意页面当作激活页。"""
    try:
        parsed = urlsplit(url or "")
        return (
            parsed.scheme.lower() == "https"
            and (parsed.hostname or "").lower() in ("bing.com", "www.bing.com", "cn.bing.com")
            and parsed.port in (None, 443)
            and parsed.path in ("", "/")
        )
    except (TypeError, ValueError):
        return False


def _expect_page_from_click(page, click) -> object | None:
    """执行点击并只返回与这次点击绑定的 popup/expect_page 页面。

    优先使用 page.expect_popup()，它只绑定当前页面产生的弹窗；并发用户
    新开标签页不会进入所有权。旧客户端不支持时才回退 context.expect_page。
    """
    expect_popup = getattr(page, "expect_popup", None)
    expect_page = getattr(page.context, "expect_page", None)
    if callable(expect_popup):
        expectation_factory = expect_popup
    elif callable(expect_page):
        expectation_factory = expect_page
    else:
        # 无事件上下文能力的旧客户端仍可点击，但不会因此获得页面所有权。
        try:
            click()
        except Exception:
            pass
        return None
    try:
        expectation = expectation_factory(timeout=10000)
    except Exception:
        return None

    try:
        with expectation as info:
            try:
                click()
            except Exception:
                pass
    except Exception:
        # 点击或等待超时都不影响已发生的 expect_page 结果读取。
        pass

    try:
        if isinstance(info, dict):
            captured = info.get("page")
        else:
            captured = getattr(info, "value", info)
            if hasattr(captured, "value"):
                captured = captured.value
    except Exception:
        return None
    if captured is page or captured is None:
        return None
    return captured


def _wait_for_activation_page(
    anchor_page,
    owned_pages,
    timeout_ms: int,
) -> object | None:
    """等待事件确认的 Bing 激活页；历史标签页永远不参与。"""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    while True:
        for target in owned_pages:
            if _page_is_closed(target):
                continue
            try:
                if _is_edge_activation_url(target.url):
                    return target
            except Exception:
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        delay_ms = max(1, min(EDGE_STREAK_ACTIVATION_POLL_MS, math.ceil(remaining * 1000)))
        try:
            anchor_page.wait_for_timeout(delay_ms)
        except Exception:
            time.sleep(delay_ms / 1000)


def _activate_edge_streak(
    activity_page,
    fallback_page=None,
    timeout_ms: int = 5000,
    activation_timeout_ms: int = EDGE_STREAK_ACTIVATION_TIMEOUT_MS,
) -> EdgeStreakActivation:
    """点击一次激活链接；只有事件确认的本次新增 Bing 页才算激活成功。"""
    candidates = [activity_page]
    if fallback_page is not None and fallback_page is not activity_page:
        candidates.append(fallback_page)
    deadline = time.monotonic() + max(activation_timeout_ms, 0) / 1000
    activation_link = None
    while activation_link is None:
        for candidate in candidates:
            try:
                link = candidate.locator(
                    f"a:has-text('{EDGE_STREAK_ACTIVATION_TEXT}'):visible"
                ).first
                if link.count() > 0 and link.is_visible():
                    activation_link = link
                    break
            except Exception:
                continue
        if activation_link is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return EdgeStreakActivation(None, (), False)
            delay_ms = max(
                1,
                min(EDGE_STREAK_ACTIVATION_POLL_MS, math.ceil(remaining * 1000)),
            )
            try:
                activity_page.wait_for_timeout(delay_ms)
            except Exception:
                time.sleep(delay_ms / 1000)

    # 点击异常也可能发生在新页打开之后；只接受 expect_page 返回的专属页。
    captured = _expect_page_from_click(activity_page, activation_link.click)
    if captured is None:
        return EdgeStreakActivation(None, (), False)
    created = (captured,)
    activation_page = _wait_for_activation_page(
        activity_page, created, timeout_ms
    )
    if activation_page is None:
        return EdgeStreakActivation(None, created, False)
    return EdgeStreakActivation(activation_page, created, True)


def _prepare_edge_streak(page) -> EdgeStreakEntry:
    """打开 Edge 活动并返回入口页与事件确认的本次新建页。"""
    try:
        current_url = page.url or ""
    except Exception:
        return EdgeStreakEntry(None, (), False)
    if _is_edge_streak_url(current_url):
        return EdgeStreakEntry(page, (), False)

    # 新版卡片是 button，点击后才展开带“使用 Edge”的 dialog。
    try:
        card = page.locator('button:has-text("Microsoft Edge"):has-text("分钟")').first
        if card.count() > 0 and card.is_visible():
            card.click()
            try:
                card.wait_for(state="hidden", timeout=5000)
            except Exception:
                page.wait_for_timeout(500)
    except Exception:
        pass

    try:
        link = page.locator("[role='dialog'] a:has-text('使用 Edge')").first
        if link.count() > 0 and link.is_visible():
            try:
                list(page.context.pages)
            except Exception:
                link = None
            if link is not None:
                captured = _expect_page_from_click(page, link.click)
                created = (captured,) if captured is not None else ()
                target = None
                if captured is not None:
                    target = _wait_for_edge_streak_target(
                        page,
                        [captured],
                        EDGE_STREAK_REDIRECT_TIMEOUT_MS,
                        include_new_pages=False,
                    )
                if target is not None:
                    return EdgeStreakEntry(target, created, bool(created))
                return EdgeStreakEntry(None, created, bool(created))
    except Exception:
        pass

    try:
        if _is_edge_streak_url(page.url or ""):
            return EdgeStreakEntry(page, (), False)
        safe_goto(page, EDGE_STREAK_LAUNCH_URL, timeout=30000, wait_extra=0)
        target = _wait_for_edge_streak_target(
            page,
            [page],
            EDGE_STREAK_REDIRECT_TIMEOUT_MS,
            include_new_pages=False,
        )
        if target is not None:
            return EdgeStreakEntry(target, (), False)
    except Exception:
        pass
    return EdgeStreakEntry(None, (), False)


def _close_edge_streak_pages(page, pages, protected_pages=None) -> None:
    """逐个关闭本次新建页；传入页和受保护历史页永不关闭。"""
    protected = set(protected_pages or ())
    protected.add(page)
    for target in pages or ():
        if target is None or target in protected or target is page:
            continue
        try:
            if not _page_is_closed(target):
                target.close()
        except Exception:
            continue


def _open_edge_streak(page) -> bool:
    """兼容入口：仅表示已确认进入官方活动页。"""
    return _prepare_edge_streak(page).activity_page is not None


def _open_mobile_app(page) -> bool:
    for text in ("立即签入", "下载应用", "打开应用"):
        try:
            link = page.locator(f"a:has-text('{text}')").first
            if link.count() > 0 and link.is_visible():
                link.click()
                page.wait_for_timeout(2500)
                return True
        except Exception:
            continue
    return False


def valid_edge_progress(progress) -> tuple[int, int] | None:
    """返回合法 Edge 进度；完成判定必须同时满足 total>0 和 0<=done<=total。"""
    try:
        done, total = progress
        done = int(done)
        total = int(total)
    except (TypeError, ValueError):
        return None
    if total <= 0 or done < 0 or done > total:
        return None
    return done, total


# 兼容已有内部调用/测试；新代码应使用公开的 valid_edge_progress。
_valid_edge_progress = valid_edge_progress


def do_edge_browsing_streak_detail(
    page,
    minutes: int = 30,
    poll_seconds: int = 60,
    expected_email: str | None = None,
) -> EdgeStreakResult:
    """执行 Edge 浏览并返回不可被页面进度覆盖的明确状态。"""
    print("  检查 Edge 浏览连续打卡...")
    if expected_email is not None:
        actual = edge_profile_identity(EDGE_USER_DATA_DIR)
        expected = _normalize_email(expected_email)
        if actual is None:
            print("  Edge 浏览器 profile 无法确认目标账号，停止且不打开活动。")
            return EdgeStreakResult(EdgeStreakStatus.IDENTITY_UNVERIFIED)
        if expected is None or actual != expected:
            print("  Edge 浏览器 profile 目标账号不匹配，停止且不打开活动。")
            print("  请确认 Edge 当前实际使用的 profile 已登录网页账号的目标 Microsoft 账号。")
            return EdgeStreakResult(
                EdgeStreakStatus.IDENTITY_MISMATCH, identity=actual
            )
        identity = actual
    elif not edge_browser_profile_logged_in():
        print("  Edge 浏览器 profile 未登录，无法完成 Edge 浏览打卡。")
        print("  网页 Rewards 登录不等于 Edge 浏览器登录；请先手动打开 Edge，")
        print("  进入 设置 → 个人资料 → 登录目标 Microsoft 账号，并保持同步后重新运行。")
        return EdgeStreakResult(EdgeStreakStatus.IDENTITY_UNVERIFIED)
    else:
        identity = edge_profile_identity(EDGE_USER_DATA_DIR)
        print("  Edge 浏览器 profile 已登录；未校验目标账号（调用方未提供 expected_email）。")

    current = _valid_edge_progress(read_streak_progress(page, "edge"))
    if current is None:
        print("  未读取到有效的 Edge 浏览进度，停止且不伪造成功")
        return EdgeStreakResult(EdgeStreakStatus.INVALID_PROGRESS)
    if current[0] >= current[1]:
        print(f"  Edge 浏览已完成: {current[0]}/{current[1]}")
        return EdgeStreakResult(
            EdgeStreakStatus.COMPLETED, progress=current, identity=identity
        )

    try:
        poll_seconds = max(1, int(poll_seconds))
    except (TypeError, ValueError):
        print("  Edge 浏览轮询周期无效，停止")
        return EdgeStreakResult(
            EdgeStreakStatus.INCOMPLETE, progress=current, identity=identity
        )
    owned_pages = ()
    owned_ids = set()
    latest = current
    try:
        entry = _prepare_edge_streak(page)
        if entry.can_cleanup:
            owned_pages = tuple(entry.created_pages)
            owned_ids = {id(item) for item in owned_pages}
        if entry.activity_page is None:
            if entry.can_cleanup:
                print("  页面已打开但未确认进入 Edge 活动，停止并清理")
            else:
                print("  未找到 Edge 浏览入口")
            return EdgeStreakResult(
                EdgeStreakStatus.ENTRY_FAILED, progress=latest, identity=identity
            )

        activation = _activate_edge_streak(
            entry.activity_page,
            page,
            activation_timeout_ms=EDGE_STREAK_ACTIVATION_TIMEOUT_MS,
        )
        if activation.created_pages:
            for owned in activation.created_pages:
                if id(owned) not in owned_ids:
                    owned_pages = (*owned_pages, owned)
                    owned_ids.add(id(owned))
        if not activation.activated or activation.browsing_page is None:
            print("  未打开本次新增的 Bing 激活页，停止 Edge 浏览")
            return EdgeStreakResult(
                EdgeStreakStatus.ACTIVATION_FAILED,
                progress=latest,
                identity=identity,
            )
        browsing_page = activation.browsing_page
        print("  已在新标签页激活 Edge 浏览")

        target = max(1, min(int(minutes), current[1]))
        elapsed = 0
        report_interval_seconds = min(300, target * 60)
        content_urls = (
            "https://www.bing.com/search?q=%E4%BB%8A%E6%97%A5%E6%96%B0%E9%97%BB&form=QBRE",
            "https://www.bing.com/search?q=%E7%A7%91%E6%8A%80%E8%B5%84%E8%AE%AF&form=QBRE",
            "https://www.bing.com/search?q=%E7%94%9F%E6%B4%BB%E8%B4%A2%E7%BB%8F&form=QBRE",
        )
        while elapsed < target * 60:
            if _page_is_closed(browsing_page):
                print("  Edge 激活页已关闭，停止")
                return EdgeStreakResult(
                    EdgeStreakStatus.INCOMPLETE, progress=latest, identity=identity
                )
            # 始终复用首次激活页；进度只回读原 dashboard，不重开活动页。
            content_url = content_urls[(elapsed // 300) % len(content_urls)]
            safe_goto(browsing_page, content_url, wait_extra=1500)
            for _ in range(10):
                browsing_page.mouse.wheel(0, random.randint(350, 850))
                browsing_page.wait_for_timeout(random.randint(800, 1500))
            browsing_page.wait_for_timeout(max(1000, poll_seconds * 1000 - 12000))
            elapsed += poll_seconds
            if elapsed % report_interval_seconds == 0 or elapsed >= target * 60:
                safe_goto(page, REWARDS_DASHBOARD, wait_extra=2000)
                now = _valid_edge_progress(read_streak_progress(page, "edge"))
                if now:
                    latest = now
                    done, total = now
                    print(f"    Edge 浏览: {done}/{total}（已等待 {min(elapsed, target * 60) // 60} 分钟）")
                    if done >= total:
                        return EdgeStreakResult(
                            EdgeStreakStatus.COMPLETED,
                            progress=now,
                            identity=identity,
                        )
        print(
            f"  Edge 浏览器已登录，但已等待 {target} 分钟 Rewards 仍为 "
            f"{latest[0]}/{latest[1]}，未回写打卡进度，停止且不伪造成功。"
        )
    except Exception as exc:
        detail = str(exc).splitlines()
        print(f"  Edge 浏览异常: {(detail[0] if detail else type(exc).__name__)[:100]}")
        return EdgeStreakResult(
            EdgeStreakStatus.ERROR, progress=latest, identity=identity
        )
    finally:
        _close_edge_streak_pages(page, owned_pages)
    return EdgeStreakResult(
        EdgeStreakStatus.INCOMPLETE, progress=latest, identity=identity
    )


def do_edge_browsing_streak(
    page,
    minutes: int = 30,
    poll_seconds: int = 60,
    expected_email: str | None = None,
) -> int:
    """兼容入口：只有明确 COMPLETED 才返回 1。"""
    result = do_edge_browsing_streak_detail(
        page,
        minutes=minutes,
        poll_seconds=poll_seconds,
        expected_email=expected_email,
    )
    return 1 if result.success else 0


def do_mobile_app_checkin(page) -> int:
    """打开 Bing 手机 App 签到入口；桌面无等价签到时不伪造成功。"""
    print("  检查必应应用签到...")
    current = read_streak_progress(page, "mobile")
    if current and current[0] >= current[1]:
        print(f"  必应应用签到已完成: {current[0]}/{current[1]}")
        return 1
    if not current:
        print("  未读取到必应应用签到进度")
        return 0

    try:
        # 卡片按钮会跳转到 bingapp.microsoft.com；没有手机 App/ADB 时无法完成签到。
        if not _open_mobile_app(page):
            print("  未找到必应应用签到入口")
            return 0
        if "bingapp.microsoft.com" in page.url.lower():
            print("  必应应用签到需在 Bing 手机 App 内完成（当前只有下载页），跳过")
            return 0
        page.wait_for_timeout(3000)
        now = read_streak_progress(page, "mobile")
        if now and now[0] >= now[1]:
            print("  必应应用签到完成")
            return 1
        print("  未确认必应应用签到完成")
    except Exception as exc:
        print(f"  必应应用签到异常: {str(exc).splitlines()[0][:100]}")
    return 0


def _first_quest_action(page):
    """返回限时任务中当前第一个可点击的网页任务入口。"""
    try:
        headings = page.locator("main h3")
        for index in range(headings.count()):
            heading = headings.nth(index)
            if not heading.is_visible():
                continue
            card = heading.locator("xpath=ancestor::div[2]")
            if card.count() == 0:
                continue
            links = card.locator('a[target="_blank"]')
            if links.count() == 0:
                continue
            link = links.first
            if not link.is_visible():
                continue
            if (link.get_attribute("aria-disabled") or "").lower() == "true":
                continue
            href = link.get_attribute("href") or ""
            if href.startswith("http"):
                return link
    except Exception:
        pass
    return None


def _claim_quest_points(page) -> bool:
    for selector in (
        "main button:has-text('领取积分')",
        "main button:has-text('领取奖励')",
        "main a:has-text('领取积分')",
        "main a:has-text('领取奖励')",
    ):
        try:
            button = page.locator(selector).first
            if button.count() > 0 and button.is_visible() and button.is_enabled():
                button.click()
                page.wait_for_timeout(2000)
                _ensure_no_risk_control(page)
                return True
        except (RewardsRiskControlError, RiskDetectionError):
            raise
        except Exception:
            continue
    return False


def do_point_punch_cards(page) -> int:
    """每次运行最多推进一步明确奖励积分的 Punch Card。"""
    print("  扫描限时积分活动...")
    safe_goto(page, REWARDS_EARN_PAGE, wait_extra=2500)
    _ensure_no_risk_control(page)
    cards = extract_point_punch_cards(page.content())
    if not cards:
        print("  未找到可自动化的限时积分活动")
        return 0

    print(f"  找到 {len(cards)} 个限时积分活动")
    for card in cards:
        print(f"  检查: {card['title']} (+{card['points']})")
        safe_goto(page, card["url"], wait_extra=2500)
        _ensure_no_risk_control(page)
        text = page.evaluate("() => document.body.innerText || ''")
        progress = re.search(r"(?:状态:\s*)?(\d+)\s*/\s*(\d+)\s*个任务", text)
        if progress and progress.group(1) == progress.group(2):
            if _claim_quest_points(page):
                _ensure_no_risk_control(page)
                print("    已领取活动积分")
                return 1
            print("    活动已完成，无待领取按钮")
            continue

        action = _first_quest_action(page)
        if action is None:
            print("    当前没有可点击的网页任务，等待下次运行")
            continue

        try:
            action_text = (action.inner_text() or "").strip()[:60]
            print(f"    点击任务: {action_text}")
            new_page = None
            try:
                with page.context.expect_page(timeout=10000):
                    action.click(delay=random.randint(70, 180))
                new_page = page.context.pages[-1]
            except Exception:
                new_page = None
            target = new_page if new_page is not None and new_page != page else page
            try:
                target.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            target.wait_for_timeout(random.randint(3500, 5500))
            _ensure_no_risk_control(target)
            _random_scroll(target)
            target.wait_for_timeout(random.randint(1500, 3000))
            if new_page is not None:
                new_page.close()
        except (RewardsRiskControlError, RiskDetectionError):
            raise
        except Exception as exc:
            print(f"    任务执行异常: {str(exc).splitlines()[0][:100]}")

        safe_goto(page, card["url"], wait_extra=2000)
        _ensure_no_risk_control(page)
        updated = page.evaluate("() => document.body.innerText || ''")
        updated_match = re.search(r"(?:状态:\s*)?(\d+)\s*/\s*(\d+)\s*个任务", updated)
        if updated_match:
            done = int(updated_match.group(1))
            total = int(updated_match.group(2))
            print(f"    当前进度: {done}/{total}")
            if progress:
                before = int(progress.group(1))
                if done <= before:
                    print("    进度未变化，本次不计为完成")
                    continue
            return 1
        print("    未读到任务进度，保守起见不计为完成")
        continue

    return 0


def _open_task_card(page, title: str):
    card = page.locator(f'[target="_blank"]:has(img[alt="{title}"])').first
    if card.count() == 0 or not card.is_visible():
        return None
    try:
        with page.context.expect_page(timeout=10000):
            card.click(delay=random.randint(60, 160))
        return page.context.pages[-1]
    except Exception:
        return None


def do_daily_offer_tasks(page) -> int:
    """完成 /earn 日常搜索与每日拼图。"""
    print("  打开积分赚取页...")
    safe_goto(page, REWARDS_EARN_PAGE, wait_extra=3000)
    _ensure_no_risk_control(page)
    items = extract_daily_offer_tasks(page.content())
    if not items:
        print("  未找到待完成的日常任务")
        return 0

    print(f"  待完成日常任务: {len(items)} 个")
    completed = 0
    for index, item in enumerate(items):
        title = item.get("title") or f"任务{index + 1}"
        print(f"  [{index + 1}/{len(items)}] {title} ({item['kind']})")
        try:
            safe_goto(page, REWARDS_EARN_PAGE, wait_extra=2000)
            new_page = _open_task_card(page, title)
            if new_page is None:
                print("    未找到任务卡片，跳过")
                continue
            try:
                new_page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            _ensure_no_risk_control(new_page)

            if item["kind"] == "puzzle":
                ok = complete_image_puzzle(new_page)
            elif _is_app_only_page(new_page):
                print("    仅限积分商城应用完成，跳过")
                ok = False
            else:
                new_page.wait_for_timeout(random.randint(3500, 5500))
                _random_scroll(new_page)
                ok = True

            if new_page != page:
                new_page.close()
            if ok:
                completed += 1
                print("    完成")
        except (RewardsRiskControlError, RiskDetectionError):
            raise
        except Exception as exc:
            print(f"    失败: {str(exc).splitlines()[0][:100]}")
        random_sleep(short_range=(1, 3))

    print(f"  完成 {completed} 个日常任务")
    return completed


def do_daily_earn_tasks(page) -> int:
    """兼容入口：处理日常任务面板中的搜索和拼图。"""
    return do_daily_offer_tasks(page)
