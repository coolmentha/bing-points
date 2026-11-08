"""第三方客户端任务：Bing Android 签到与 Rewards WebView 静态兼容导出。"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from utils.config import (
    ADB_PATH,
    BING_ANDROID_ACTIVITY,
    BING_ANDROID_PACKAGE,
    ENABLE_MOBILE_CLIENT,
)
from utils.rewards_webview import (  # noqa: F401
    ENABLE_REWARDS_DESKTOP,
    REWARDS_DESKTOP_AUMID,
    REWARDS_DESKTOP_ERROR,
    REWARDS_DESKTOP_IDENTITY_MISMATCH,
    REWARDS_DESKTOP_IDENTITY_UNVERIFIED,
    REWARDS_DESKTOP_NO_TASK,
    REWARDS_DESKTOP_TASK_GROWN,
    REWARDS_DESKTOP_TASK_ITEM_MISSING,
    REWARDS_DESKTOP_UIA_RESULT,
    REWARDS_DESKTOP_UIA_UNAVAILABLE,
    _empty_rewards_result,
    _is_explicit_uia_unavailable,
    _last_output_value,
    _output_progress,
    _parse_rewards_output,
    _rewards_desktop_hwnd,
    _rewards_uia,
    open_rewards_desktop,
    rewards_desktop_available,
    rewards_desktop_state,
    run_rewards_desktop_task,
)

ADB_TIMEOUT = 15
UI_DUMP_PATH = "/sdcard/window_dump.xml"


def _adb(*args, timeout: int = ADB_TIMEOUT):
    adb = Path(ADB_PATH)
    if not adb.is_file():
        return None
    try:
        return subprocess.run(
            [str(adb), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _adb_path(*args, timeout: int = ADB_TIMEOUT):
    adb = Path(ADB_PATH)
    if not adb.is_file():
        return None
    try:
        return subprocess.run(
            [str(adb), *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def list_android_devices():
    proc = _adb("devices", "-l")
    if proc is None or proc.returncode != 0:
        return []
    return [
        line.split()[0]
        for line in proc.stdout.splitlines()[1:]
        if line.strip() and line.split()[1:2] == ["device"]
    ]


def _uia_nodes(serial: str):
    dump = _adb("-s", serial, "shell", "uiautomator", "dump", UI_DUMP_PATH)
    if dump is None or dump.returncode != 0:
        return []
    raw = _adb_path("-s", serial, "exec-out", "cat", UI_DUMP_PATH)
    if raw is None or raw.returncode != 0:
        return []
    try:
        root = ET.fromstring(raw.stdout)
    except ET.ParseError:
        return []
    return list(root.iter("node"))


def _bounds(node):
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _tap_node(serial: str, node) -> bool:
    point = _bounds(node)
    if not point:
        return False
    proc = _adb("-s", serial, "shell", "input", "tap", str(point[0]), str(point[1]))
    return proc is not None and proc.returncode == 0


def _tap_bounds(serial: str, bounds: str) -> bool:
    """点击可点击父容器的中心，处理文字本身 clickable=false 的情况。"""
    match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return False
    x1, y1, x2, y2 = map(int, match.groups())
    proc = _adb("-s", serial, "shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
    return proc is not None and proc.returncode == 0


def _checkin_action_node(nodes):
    """找到签到文字上方/包裹它的可点击控件；文字节点本身通常不可点击。"""
    label = next(
        (node for node in nodes
         if (node.get("text") or "").strip().lower() == "check in"),
        None,
    )
    if label is None:
        return None, None
    bounds = label.get("bounds") or ""
    label_match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not label_match:
        return label, None
    lx1, y1, lx2, y2 = map(int, label_match.groups())
    candidates = []
    for node in nodes:
        if node.get("clickable") != "true":
            continue
        parent_match = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
        if not parent_match:
            continue
        px1, py1, px2, py2 = map(int, parent_match.groups())
        if px1 <= lx1 and px2 >= lx2 and py1 <= y1 and py2 >= y2:
            candidates.append((px2 - px1, py2 - py1, node))
    if candidates:
        candidates.sort(key=lambda item: (item[0] * item[1], item[0] + item[1]))
        return label, candidates[0][2]
    return label, None


def android_profile_email(serial: str = "") -> str:
    """读取 Bing App 内当前登录邮箱；无法读取时返回空字符串，不猜测身份。"""
    if not serial:
        devices = list_android_devices()
        if not devices:
            return ""
        serial = devices[0]
    nodes = _uia_nodes(serial)
    profile_button = next(
        (node for node in nodes
         if (node.get("resource-id") or "").endswith("sa_profile_button")),
        None,
    )
    if profile_button is not None:
        _tap_node(serial, profile_button)
        time.sleep(2)
        nodes = _uia_nodes(serial)
    for node in nodes:
        resource = (node.get("resource-id") or "").lower()
        value = (node.get("text") or node.get("content-desc") or "").strip()
        if value and ("account_hint" in resource or ("@" in value and " " not in value)):
            return value
    return ""


def mobile_checkin(expected_email: str = "") -> int:
    """在已连接 Android 模拟器中打开 Bing App 并完成每日签到。"""
    if not ENABLE_MOBILE_CLIENT:
        return 0
    devices = list_android_devices()
    if not devices:
        print("  未找到已连接 Android 设备，跳过必应应用签到")
        return 0
    serial = devices[0]
    print(f"  使用 Android 设备: {serial}")
    if expected_email:
        actual_email = android_profile_email()
        if not actual_email:
            print("  无法读取 Android Bing App 登录账号，拒绝用无法核验的状态签到")
            return 0
        if actual_email.lower() != expected_email.lower():
            print(f"  Android Bing App 当前账号为 {actual_email}，与目标不一致，跳过")
            return 0
    _adb("-s", serial, "shell", "am", "force-stop", BING_ANDROID_PACKAGE)
    _adb("-s", serial, "shell", "am", "start", "-n", f"{BING_ANDROID_PACKAGE}/{BING_ANDROID_ACTIVITY}")
    nodes = []
    for _ in range(8):
        time.sleep(2)
        nodes = _uia_nodes(serial)
        if nodes and any(n.get("package") == BING_ANDROID_PACKAGE for n in nodes):
            break

    # 首次启动的评分弹窗可能遮住首页入口。
    for node in nodes:
        if (node.get("content-desc") or "") == "Close" and "do_you_like" in (node.get("resource-id") or ""):
            _tap_node(serial, node)
            time.sleep(2)
            nodes = _uia_nodes(serial)
            break
    # 如果启动后已经落在签到页，直接读取状态，避免无谓跳转。
    page_text = " ".join(
        ((n.get("text") or "") + " " + (n.get("content-desc") or "")).strip().lower()
        for n in nodes
    )
    if "check in" in page_text or "streaks" in page_text:
        if "checked" in page_text:
            if expected_email and android_profile_email().lower() != expected_email.lower():
                print("  Android Bing App 登录账号与目标不一致，跳过签到")
                return 0
            print("  必应应用今日签到已完成")
            return 1
        _, check_target = _checkin_action_node(nodes)
        if check_target is not None and _tap_node(serial, check_target):
            time.sleep(4)
            nodes = _uia_nodes(serial)
            if any((n.get("text") or "").strip().lower() == "checked" for n in nodes):
                print("  必应应用签到成功")
                return 1
        print("  未能确认必应应用签到状态")
        return 0

    # 先处理评分弹窗等遮挡层。
    for node in nodes:
        if (node.get("content-desc") or "") == "Close" and "do_you_like" in (node.get("resource-id") or ""):
            _tap_node(serial, node)
            time.sleep(1)
            nodes = _uia_nodes(serial)
            break

    # Bing App 首页 -> Apps -> Rewards（当前版本 Rewards 位于九宫格）。
    rewards_entry = next(
        (n for n in nodes if n.get("resource-id", "").endswith("app_item_normal_container")
         and "Rewards" in (n.get("content-desc") or "")),
        None,
    )
    if rewards_entry is None:
        apps = next(
            (n for n in nodes if (n.get("content-desc") or "").strip().lower() == "apps"),
            None,
        )
        if apps is not None:
            _tap_node(serial, apps)
            time.sleep(2)
            nodes = _uia_nodes(serial)
            rewards_entry = next(
                (n for n in nodes if n.get("resource-id", "").endswith("app_item_normal_container")
                 and "Rewards" in (n.get("content-desc") or "")),
                None,
            )
    if rewards_entry is None:
        print("  未找到 Bing 应用 Rewards 入口")
        return 0
    _tap_node(serial, rewards_entry)

    # Rewards 页面是 WebView，加载和账号选择都需要轮询等待。
    nodes = []
    for _ in range(12):
        time.sleep(2)
        nodes = _uia_nodes(serial)
        texts = " ".join(
            ((n.get("text") or "") + " " + (n.get("content-desc") or "")).lower()
            for n in nodes
        )
        if "check in" in texts or "streaks" in texts or "sign in as" in texts:
            break

    # 登录弹窗：已登录账号直接选择，否则交给人工登录。
    profile = next(
        (n for n in nodes if n.get("resource-id", "").endswith("profile_display_email")),
        None,
    )
    if profile is not None and _tap_node(serial, profile):
        for _ in range(6):
            time.sleep(2)
            nodes = _uia_nodes(serial)
            texts = " ".join(
                ((n.get("text") or "") + " " + (n.get("content-desc") or "")).lower()
                for n in nodes
            )
            if "check in" in texts or "streaks" in texts:
                break

    checked = any(
        (n.get("text") or "").strip().lower() == "checked"
        or (n.get("content-desc") or "").strip().lower() == "checked"
        for n in nodes
    )
    if checked:
        if expected_email and android_profile_email().lower() != expected_email.lower():
            print("  Android Bing App 登录账号与目标不一致，跳过签到")
            return 0
        print("  必应应用今日签到已完成")
        return 1
    _, check_target = _checkin_action_node(nodes)
    if check_target is not None and _tap_node(serial, check_target):
        time.sleep(4)
        nodes = _uia_nodes(serial)
        if any((n.get("text") or "").strip().lower() == "checked" for n in nodes):
            print("  必应应用签到成功")
            return 1
    print("  未能确认必应应用签到状态")
    return 0
