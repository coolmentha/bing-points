"""Edge 浏览连续打卡：入口、身份边界、激活页所有权与进度回读。"""
import json
import math
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from utils.browser import safe_goto
from utils.config import EDGE_USER_DATA_DIR, REWARDS_DASHBOARD


edge_user_data_dir = EDGE_USER_DATA_DIR


def _edge_user_data_dir():
    return edge_user_data_dir


def read_streak_progress(page, label: str):
    """读取首页卡片进度；Edge 与移动端共用同一解析规则。"""
    try:
        body_text = page.evaluate("() => document.body.innerText || ''")
    except Exception:
        return None
    patterns = {
        "edge": r"Microsoft Edge\s*\n\s*分钟:\s*(\d+)\s*/\s*(\d+)",
        "mobile": r"移动应用\s*\n\s*签到:\s*(\d+)\s*/\s*(\d+)",
    }
    match = re.search(patterns[label], body_text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


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
    requested = Path(profile_dir) if profile_dir is not None else Path(_edge_user_data_dir())
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
    activation_timeout_ms: int | None = None,
) -> EdgeStreakActivation:
    """点击一次激活链接；只有事件确认的本次新增 Bing 页才算激活成功。"""
    if activation_timeout_ms is None:
        activation_timeout_ms = EDGE_STREAK_ACTIVATION_TIMEOUT_MS
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


def _read_edge_progress(page):
    return read_streak_progress(page, "edge")


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


def _valid_edge_progress(progress):
    """内部别名；测试可直接替换该名称。"""
    return valid_edge_progress(progress)


def do_edge_browsing_streak_detail(
    page,
    minutes: int = 30,
    poll_seconds: int = 60,
    expected_email: str | None = None,
) -> EdgeStreakResult:
    """执行 Edge 浏览并返回不可被页面进度覆盖的明确状态。"""
    print("  检查 Edge 浏览连续打卡...")
    if expected_email is not None:
        actual = edge_profile_identity(_edge_user_data_dir())
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
        identity = edge_profile_identity(_edge_user_data_dir())
        print("  Edge 浏览器 profile 已登录；未校验目标账号（调用方未提供 expected_email）。")

    current = _valid_edge_progress(_read_edge_progress(page))
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
                now = _valid_edge_progress(_read_edge_progress(page))
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




