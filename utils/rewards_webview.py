"""Microsoft Rewards 桌面 WebView 适配器。"""
from __future__ import annotations

import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from utils.config import (
    ENABLE_REWARDS_DESKTOP,
    REWARDS_DESKTOP_AUMID,
    REWARDS_DESKTOP_UIA_RESULT,
)


# Rewards 桌面任务返回值：环境不可用与真实任务失败必须分开。
REWARDS_DESKTOP_ERROR = -1
REWARDS_DESKTOP_UIA_UNAVAILABLE = -2
REWARDS_DESKTOP_NO_TASK = 0
REWARDS_DESKTOP_TASK_GROWN = 1
REWARDS_DESKTOP_IDENTITY_UNVERIFIED = -3
REWARDS_DESKTOP_IDENTITY_MISMATCH = -4
REWARDS_DESKTOP_TASK_ITEM_MISSING = -5


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


def _empty_rewards_result(**overrides) -> dict:
    result = {
        "available": False,
        "task_progress": None,
        "success": False,
        "uia_ready": False,
        "output": "",
        "task_state": None,
        "error": False,
        "protocol_error": False,
        "uia_unavailable": False,
        "identity_state": None,
        "desktop_identity": None,
        "run_id": None,
    }
    result.update(overrides)
    return result


def _is_explicit_uia_unavailable(output: str) -> bool:
    """只接受脚本明确报告的 UIA 环境不可用，不能用“缺失结果”推断。"""
    return (
        "AVAILABLE=0" in output
        or "UIA_READY=0" in output
        or "NO_PROCESS" in output
        or "NO_WINDOW" in output
        or "RootWebArea not found" in output
    )


def _parse_rewards_output(
    output: str,
    *,
    expected_email: str | None = None,
    run_id: str | None = None,
) -> dict:
    task_progress = _output_progress(_last_output_value(output, "TASK_PROGRESS"))
    task_before = _output_progress(_last_output_value(output, "TASK_BEFORE"))
    task_after = _output_progress(_last_output_value(output, "TASK_AFTER"))
    progress = task_after or task_before or task_progress
    task_state = _last_output_value(output, "TASK_STATE")
    task_error = _last_output_value(output, "TASK_ERROR")
    task_value = _last_output_value(output, "TASK")
    task_element_type = _last_output_value(output, "TASK_ELEMENT_TYPE")
    task_rect = _last_output_value(output, "TASK_RECT")
    task_invoke_mode = _last_output_value(output, "TASK_INVOKE_MODE")
    invoke_fallback = _last_output_value(output, "INVOKE_FALLBACK")
    desktop_identity = _last_output_value(output, "DESKTOP_IDENTITY")
    identity_state = _last_output_value(output, "IDENTITY_STATE")

    ready_line = next((line for line in output.splitlines() if line.startswith("UIA_READY=")), "")
    doc_line = next((line for line in output.splitlines() if line.startswith("DOC=")), "")
    legacy_ready = bool(progress or (doc_line and doc_line != "DOC="))
    uia_ready = ready_line == "UIA_READY=1" or (not ready_line and legacy_ready)
    if task_state is None and uia_ready and progress:
        task_state = "task"

    if expected_email:
        expected = expected_email.strip().lower()
        actual = (desktop_identity or "").strip().lower()
        if identity_state == "identity_mismatch" or (actual and actual != expected):
            identity_state = "identity_mismatch"
        elif not actual or identity_state in ("identity_unverified", "unverified"):
            identity_state = "identity_unverified"
        elif actual == expected:
            identity_state = "verified"
    elif identity_state is None:
        identity_state = "not_checked"

    if task_state == "task" and task_value == "0":
        task_state = "task_item_missing"
    if task_error == "task_item_missing":
        task_state = "task_item_missing"

    error = bool(task_error or _last_output_value(output, "ERROR"))
    if task_state == "task_item_missing":
        error = True
    if task_state == "task" and task_element_type and task_element_type != "ControlType.Hyperlink":
        error = True
    if error and task_state not in ("no_task", "uia_unavailable", "task_item_missing"):
        task_state = "error"

    success = "SUCCESS=1" in output and not error
    if expected_email:
        success = success and identity_state == "verified"
    if task_before and task_after:
        try:
            before_count = int(task_before.split("/", 1)[0])
            after_count = int(task_after.split("/", 1)[0])
            success = success and after_count > before_count
        except (TypeError, ValueError):
            success = False

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
        "protocol_error": False,
        "uia_unavailable": False,
        "identity_state": identity_state,
        "desktop_identity": desktop_identity,
        "run_id": run_id,
        "output": output,
    }


def _rewards_uia(mode: str = "state", expected_email: str | None = None) -> dict:
    script = Path(__file__).with_name("rewards_desktop_uia.ps1")
    hwnd = _rewards_desktop_hwnd()
    if not script.is_file() or not hwnd:
        return _empty_rewards_result()

    run_id = uuid.uuid4().hex
    try:
        result_root = REWARDS_DESKTOP_UIA_RESULT.parent
        result_root.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(prefix="bing_points_rewards_", dir=str(result_root))
    except OSError:
        return _empty_rewards_result(protocol_error=True, error=True, run_id=run_id)

    with temp_dir:
        result_path = Path(temp_dir.name) / f"{run_id}.txt"
        args = [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", str(script), "-Mode", mode, "-Hwnd", str(hwnd),
            "-RunId", run_id, "-ResultPath", str(result_path),
        ]
        if expected_email:
            args.extend(["-ExpectedEmail", expected_email])
        try:
            result = subprocess.run(
                args,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _empty_rewards_result(
                protocol_error=True,
                error=True,
                task_state="error",
                run_id=run_id,
                output=f"POWERSHELL_ERROR={type(exc).__name__}: {exc}",
            )

        raw_stdout = result.stdout or ""
        file_output = ""
        try:
            if result_path.is_file():
                file_output = result_path.read_text(encoding="utf-8-sig")
        except OSError:
            file_output = ""
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass

        combined_output = file_output or raw_stdout
        if result.returncode != 0:
            return _empty_rewards_result(
                available="AVAILABLE=1" in combined_output,
                uia_ready="UIA_READY=1" in combined_output,
                protocol_error=True,
                error=True,
                task_state="error",
                run_id=run_id,
                output=combined_output,
            )
        if not file_output:
            return _empty_rewards_result(
                available="AVAILABLE=1" in raw_stdout,
                uia_ready="UIA_READY=1" in raw_stdout,
                protocol_error=True,
                error=True,
                task_state="error",
                run_id=run_id,
                output=raw_stdout,
            )
        if _last_output_value(file_output, "RUN_ID") != run_id:
            return _empty_rewards_result(
                available="AVAILABLE=1" in file_output,
                uia_ready="UIA_READY=1" in file_output,
                protocol_error=True,
                error=True,
                task_state="error",
                run_id=run_id,
                output=file_output,
            )

        parsed = _parse_rewards_output(
            file_output,
            expected_email=expected_email,
            run_id=run_id,
        )
        parsed["uia_unavailable"] = _is_explicit_uia_unavailable(file_output)
        return parsed


def rewards_desktop_state(expected_email: str | None = None) -> dict:
    """读取 Rewards 桌面应用当前任务状态；无窗口时返回 available=False。"""
    return _rewards_uia("state", expected_email=expected_email)


def run_rewards_desktop_task(expected_email: str | None = None) -> int:
    """推进一个桌面应用专属任务。

    ``expected_email`` 提供时，PowerShell 必须在点击任何任务前读取并校验
    Rewards 桌面应用当前可见账号；无法读取或不一致时安全失败。
    """
    if not ENABLE_REWARDS_DESKTOP:
        return REWARDS_DESKTOP_NO_TASK
    if not rewards_desktop_available() and not open_rewards_desktop():
        print("  未检测到 Rewards 桌面应用，WebView UIA 不可用")
        return REWARDS_DESKTOP_UIA_UNAVAILABLE
    if expected_email:
        result = _rewards_uia("run", expected_email=expected_email)
    else:
        result = _rewards_uia("run")
        print("  WebView 未校验桌面应用账号身份（兼容模式）")

    if result.get("protocol_error"):
        print("  WebView 结果协议校验失败，账号不报告成功")
        return REWARDS_DESKTOP_ERROR
    task_state = result.get("task_state")
    identity_state = result.get("identity_state")
    if identity_state == "identity_mismatch":
        print("  Rewards 桌面应用当前账号与目标不一致，未执行任务")
        return REWARDS_DESKTOP_IDENTITY_MISMATCH
    if not result.get("uia_ready"):
        if result.get("uia_unavailable") or task_state == "uia_unavailable":
            print("  Rewards 桌面应用 UIA 不可用")
            return REWARDS_DESKTOP_UIA_UNAVAILABLE
        print("  WebView 未返回可验证的 UIA 状态，账号不报告成功")
        return REWARDS_DESKTOP_ERROR
    if not result.get("available"):
        print("  WebView 未返回可验证的 UIA 状态，账号不报告成功")
        return REWARDS_DESKTOP_ERROR

    if identity_state == "identity_unverified":
        print("  无法读取 Rewards 桌面应用当前账号，未执行任务")
        return REWARDS_DESKTOP_IDENTITY_UNVERIFIED

    if task_state == "task_item_missing":
        print("  已确认 WebView 每周任务页，但找不到可执行任务项，账号不报告成功")
        return REWARDS_DESKTOP_TASK_ITEM_MISSING
    if result.get("error"):
        print("  WebView 任务定位或点击失败，账号不报告成功")
        return REWARDS_DESKTOP_ERROR
    if task_state == "no_task":
        print("  未找到已启用的 WebView 每周任务，跳过")
        return REWARDS_DESKTOP_NO_TASK
    print(f"  WebView 任务进度: {result.get('task_progress') or '未读取到'}")
    if result.get("success"):
        print("  WebView 任务本次进度已增长")
        return REWARDS_DESKTOP_TASK_GROWN
    if task_state == "task":
        print("  WebView 任务已定位，但未能确认本次进度增长，账号不报告成功")
        return REWARDS_DESKTOP_ERROR
    print("  本次没有可推进的 WebView 任务")
    return REWARDS_DESKTOP_NO_TASK
