"""第三方客户端任务：Bing Android 签到与 Rewards 桌面 WebView 检测。"""
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
    ENABLE_REWARDS_DESKTOP,
    REWARDS_DESKTOP_AUMID,
    REWARDS_DESKTOP_UIA_RESULT,
)

ADB_TIMEOUT = 15
UI_DUMP_PATH = "/sdcard/window_dump.xml"

# Rewards 桌面任务返回值：环境不可用与真实任务失败必须分开。
REWARDS_DESKTOP_ERROR = -1
REWARDS_DESKTOP_UIA_UNAVAILABLE = -2
REWARDS_DESKTOP_NO_TASK = 0
REWARDS_DESKTOP_TASK_GROWN = 1


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


def _rewards_desktop_hwnd() -> int:
    """返回 Rewards 桌面窗口句柄；AppX 进程名在子进程中可能不可见，句柄最可靠。"""
    if not ENABLE_REWARDS_DESKTOP:
        return 0
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        return int(user32.FindWindowW(None, "Microsoft Rewards") or 0)
    except Exception:
        return 0


def _last_output_value(output: str, key: str):
    prefix = key + "="
    value = None
    for line in output.splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip() or None
    return value


def _output_progress(value):
    if not value:
        return None
    match = re.search(r"(\d+)\s*/\s*(\d+)", value)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def rewards_desktop_available() -> bool:
    return bool(_rewards_desktop_hwnd())


def open_rewards_desktop() -> bool:
    """启动 Rewards 桌面应用；只负责打开，不在失败时伪造任务完成。"""
    try:
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + REWARDS_DESKTOP_AUMID])
    except OSError:
        return False
    for _ in range(6):
        time.sleep(2)
        if rewards_desktop_available():
            return True
    return False


def _rewards_uia(mode: str = "state") -> dict:
    script = Path(__file__).with_name("rewards_desktop_uia.ps1")
    hwnd = _rewards_desktop_hwnd()
    if not script.is_file() or not hwnd:
        return {
            "available": False, "task_progress": None, "success": False,
            "uia_ready": False, "output": "",
        }
    try:
        REWARDS_DESKTOP_UIA_RESULT.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", str(script), "-Mode", mode, "-Hwnd", str(hwnd),
                "-ResultPath", str(REWARDS_DESKTOP_UIA_RESULT),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "available": False, "task_progress": None, "success": False,
            "uia_ready": False, "output": "",
        }
    output = result.stdout
    try:
        if REWARDS_DESKTOP_UIA_RESULT.is_file():
            output = REWARDS_DESKTOP_UIA_RESULT.read_text(encoding="utf-8-sig")
    except OSError:
        pass
    task_progress = _output_progress(_last_output_value(output, "TASK_PROGRESS"))
    task_before = _output_progress(_last_output_value(output, "TASK_BEFORE"))
    task_after = _output_progress(_last_output_value(output, "TASK_AFTER"))
    progress = task_after or task_before or task_progress
    task_state = _last_output_value(output, "TASK_STATE")
    task_element_type = _last_output_value(output, "TASK_ELEMENT_TYPE")
    task_rect = _last_output_value(output, "TASK_RECT")
    task_invoke_mode = _last_output_value(output, "TASK_INVOKE_MODE")
    invoke_fallback = _last_output_value(output, "INVOKE_FALLBACK")
    ready_line = next((line for line in output.splitlines() if line.startswith("UIA_READY=")), "")
    doc_line = next((line for line in output.splitlines() if line.startswith("DOC=")), "")
    legacy_ready = bool(progress or (doc_line and doc_line != "DOC="))
    uia_ready = ready_line == "UIA_READY=1" or (not ready_line and legacy_ready)
    if task_state is None and uia_ready and progress:
        task_state = "task"
    error = "TASK_ERROR=" in output or "ERROR=" in output
    if task_state == "task" and task_element_type and task_element_type != "ControlType.Hyperlink":
        error = True
    if error and task_state not in ("no_task", "uia_unavailable"):
        task_state = "error"
    success = "SUCCESS=1" in output and not error
    if task_before and task_after:
        before_count, after_count = (int(part) for part in task_before.split("/"))
        current_after_count = int(task_after.split("/")[0])
        success = success and current_after_count > before_count
    return {
        "available": "AVAILABLE=1" in output,
        "task_progress": progress,
        "task_before": task_before,
        "task_after": task_after,
        "task_element_type": task_element_type,
        "task_rect": task_rect,
        "task_invoke_mode": task_invoke_mode,
        "invoke_fallback": invoke_fallback,
        "success": success,
        "uia_ready": uia_ready,
        "task_state": task_state,
        "error": error,
        "output": output,
    }


def rewards_desktop_state() -> dict:
    """读取 Rewards 桌面应用当前任务状态；无窗口时返回 available=False。"""
    return _rewards_uia("state")


def run_rewards_desktop_task() -> int:
    """推进一个桌面应用专属任务。

    返回 ``REWARDS_DESKTOP_TASK_GROWN``、``REWARDS_DESKTOP_NO_TASK``、
    ``REWARDS_DESKTOP_ERROR`` 或 ``REWARDS_DESKTOP_UIA_UNAVAILABLE``。
    最后一项表示当前进程无法访问桌面 UIA，不代表账号业务失败。
    """
    if not ENABLE_REWARDS_DESKTOP:
        return REWARDS_DESKTOP_NO_TASK
    if not rewards_desktop_available() and not open_rewards_desktop():
        print("  未检测到 Rewards 桌面应用，WebView UIA 不可用")
        return REWARDS_DESKTOP_UIA_UNAVAILABLE
    result = _rewards_uia("run")
    if result.get("available") and not result.get("uia_ready"):
        print("  WebView 完整 UIA 节点不可用；请通过 start.bat/计划任务以正常桌面权限运行")
        return REWARDS_DESKTOP_UIA_UNAVAILABLE
    if not result.get("available"):
        print("  Rewards 桌面应用 UIA 不可用")
        return REWARDS_DESKTOP_UIA_UNAVAILABLE
    if result.get("task_state") == "no_task" and not result.get("error"):
        print("  未找到已启用的 WebView 每周任务，跳过")
        return REWARDS_DESKTOP_NO_TASK
    if result.get("error"):
        print("  WebView 任务定位或点击失败，账号不报告成功")
        return REWARDS_DESKTOP_ERROR
    print(f"  WebView 任务进度: {result.get('task_progress') or '未读取到'}")
    if result.get("success"):
        print("  WebView 任务本次进度已增长")
        return REWARDS_DESKTOP_TASK_GROWN
    print("  本次没有可推进的 WebView 任务")
    return REWARDS_DESKTOP_NO_TASK
