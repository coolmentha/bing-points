"""浏览器 profile 占用检测、清理与单实例保护

Playwright 的 persistent context 依赖 Chromium 的 ProcessSingleton 机制：
同一个 user-data-dir 只能被一个浏览器进程占用，否则启动时报
"Failed to create a ProcessSingleton for your profile directory"。

常见触发场景：
1. 开机自启（start.bat）的实例还在跑，同时又手动运行 main.py；
2. 上一次运行异常退出，残留的 msedge.exe / chrome.exe 仍持有 profile。

另外 Playwright 的 Node 驱动会在临时目录下 mkdtemp，系统 %TEMP% 不可写时
会抛 "EPERM: operation not permitted, mkdtemp ..."；本模块同时负责把临时
目录重定向到项目内并确保其可用。

本模块负责在启动前发现这类占用、定向结束占用进程，并阻止多实例并发。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Chromium 会以独占方式持有的关键文件，可用于判断 profile 是否被占用
_LOCK_FILES = ("lockfile", "Default/Network/Cookies")
# 可能占用 profile 的浏览器进程名
_BROWSER_EXE_NAMES = ("msedge.exe", "chrome.exe", "chromium.exe", "msedgewebview2.exe")
# 单实例保护默认端口（仅绑定本机回环地址）
SINGLE_INSTANCE_PORT = 47821

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5
_ERROR_SHARING_VIOLATION = 32


def prepare_temp_dir(temp_dir) -> Path:
    """把临时目录固定到项目内，并让 Playwright/Node 驱动也使用它

    驱动是独立进程，只认环境变量；因此这里同时改 os.environ 与 tempfile 的
    默认值，避免系统 %TEMP% 不可写时启动阶段直接 EPERM。
    """
    path = Path(temp_dir)
    path.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = resolved
    tempfile.tempdir = resolved
    return path


def warmup_temp_dir(temp_dir) -> bool:
    """用真实 mkdtemp + 写入验证临时目录可用，失败返回 False"""
    try:
        probe = tempfile.mkdtemp(prefix="probe-", dir=str(temp_dir))
    except OSError:
        return False
    try:
        target = Path(probe) / "probe.txt"
        target.write_text("ok", encoding="utf-8")
        return target.read_text(encoding="utf-8") == "ok"
    except OSError:
        return False
    finally:
        try:
            for child in Path(probe).iterdir():
                child.unlink()
            Path(probe).rmdir()
        except OSError:
            pass


def cleanup_stale_artifacts(temp_dir, max_age_hours: float = 24.0) -> int:
    """清掉上次运行遗留的 playwright-artifacts-* 目录，返回清理数量

    只删除超过 max_age_hours 的目录，避免误删并发实例正在用的临时目录。
    """
    import shutil

    base = Path(temp_dir)
    if not base.is_dir():
        return 0
    deadline = time.time() - max_age_hours * 3600
    removed = 0
    try:
        entries = list(base.glob("playwright-artifacts-*"))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir() and entry.stat().st_mtime < deadline:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed



def edge_candidates() -> list[Path]:
    """本机 Edge 可执行文件的候选路径（按优先级）"""
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA")
    candidates = [
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    if local_appdata:
        candidates.append(Path(local_appdata) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return candidates


def find_edge_executable() -> Path | None:
    """返回本机 Edge 可执行文件路径，未安装则返回 None"""
    for path in edge_candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _probe_exclusive(path: Path) -> str:
    """尝试以独占方式打开文件，返回 missing / free / locked / unknown"""
    try:
        if not path.exists():
            return "missing"
    except OSError:
        return "unknown"

    if os.name != "nt":
        # 非 Windows 平台不依赖共享锁语义，交给调用方的重试逻辑处理
        return "free"

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    open_existing = 3
    handle = create_file(str(path), generic_read, 0, None, open_existing, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        if err == _ERROR_FILE_NOT_FOUND:
            return "missing"
        if err in (_ERROR_ACCESS_DENIED, _ERROR_SHARING_VIOLATION):
            return "locked"
        return "unknown"
    close_handle(handle)
    return "free"


def is_profile_in_use(user_data_dir) -> bool:
    """profile 目录是否被浏览器进程占用"""
    base = Path(user_data_dir)
    return any(_probe_exclusive(base / name) == "locked" for name in _LOCK_FILES)


def _parse_holder_lines(output: str) -> list[tuple[int, str]]:
    """解析 "pid<TAB>命令行" 形式的查询输出"""
    holders: list[tuple[int, str]] = []
    for line in output.splitlines():
        pid_text, _, command = line.strip().partition("\t")
        if pid_text.isdigit():
            holders.append((int(pid_text), command.strip()))
    return holders


def _powershell_holders(user_data_dir: Path) -> list[tuple[int, str]] | None:
    """Windows：用 CIM 查询命令行包含该 profile 的浏览器进程，查询失败返回 None"""
    names = ",".join(f"'{name}'" for name in dict.fromkeys(_BROWSER_EXE_NAMES))
    pattern = os.fspath(user_data_dir).replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"Get-CimInstance Win32_Process | Where-Object {{ $_.Name -in @({names}) "
        f"-and $_.CommandLine -like '*{pattern}*' }} | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _parse_holder_lines(result.stdout)


def _posix_holders(user_data_dir) -> list[tuple[int, str]] | None:
    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    pattern = os.fspath(user_data_dir)
    holders = []
    for line in result.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or pattern not in command:
            continue
        if any(name in command for name in ("chrome", "chromium", "msedge")):
            holders.append((int(pid_text), command.strip()))
    return holders


def _process_table() -> dict[int, tuple[int, str, int]] | None:
    """一次性取进程表 {pid: (父pid, 名称, 会话号)}；跨会话进程的命令行/路径读不到，
    但名称与父子关系可以读到，用于兜底定位占用 profile 的浏览器进程。
    """
    if os.name != "nt":
        return None
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | "
        'ForEach-Object { "$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.Name)`t$($_.SessionId)" }'
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    table: dict[int, tuple[int, str, int]] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        ppid = int(parts[1]) if parts[1].isdigit() else 0
        session = int(parts[3]) if parts[3].isdigit() else -1
        table[int(parts[0])] = (ppid, parts[2].strip(), session)
    return table


def _ancestor_names(pid: int, table: dict[int, tuple[int, str, int]], limit: int = 12) -> list[str]:
    names: list[str] = []
    seen = {pid}
    current = table.get(pid, (0, "", -1))[0]
    while current and current not in seen and len(names) < limit:
        seen.add(current)
        entry = table.get(current)
        if entry is None:
            break
        names.append(entry[1].lower())
        current = entry[0]
    return names


def _self_and_ancestors(table: dict[int, tuple[int, str, int]]) -> set[int]:
    """自身与祖先进程 pid：任何情况下都不能被清理"""
    protected = {os.getpid()}
    current = os.getpid()
    for _ in range(12):
        entry = table.get(current)
        if entry is None or not entry[0]:
            break
        current = entry[0]
        protected.add(current)
    return protected


def _browser_process_names() -> set[str]:
    return {name.lower() for name in _BROWSER_EXE_NAMES}


def _tree_holders(table: dict[int, tuple[int, str, int]]) -> list[int]:
    """兜底：找出由 python.exe 拉起、且父进程不是浏览器的浏览器主进程"""
    browsers = _browser_process_names()
    holders: list[int] = []
    for pid, (ppid, name, _session) in table.items():
        if name.lower() not in browsers:
            continue
        parent_name = table.get(ppid, (0, "", -1))[1].lower()
        if parent_name in browsers:
            continue  # 只取浏览器主进程，子进程交给 taskkill /T
        if any("python" in name for name in _ancestor_names(pid, table)):
            holders.append(pid)
    return holders


def find_profile_holders(user_data_dir) -> list[tuple[int, str]] | None:
    """列出占用指定 profile 的浏览器进程，返回 [(pid, 描述)]；查询不可用返回 None

    依次尝试：Restart Manager 反查句柄 → 命令行匹配 → 进程树兜底。
    计划任务在会话 0 启动的实例读不到命令行，只能靠前两者或进程树识别。
    """
    base = Path(user_data_dir)
    if os.name != "nt":
        return _posix_holders(base)

    table = _process_table()

    def describe(pid: int) -> str:
        if table and pid in table:
            _ppid, name, session = table[pid]
            return f"{name}（会话 {session}）"
        names = _process_names([pid])
        return names.get(pid, "浏览器进程")

    lock_files = [base / name for name in _LOCK_FILES]
    pids = restart_manager_pids(lock_files)
    if pids:
        return [(pid, describe(pid)) for pid in pids]

    matched = _powershell_holders(base)
    if matched:
        return matched
    if pids is None and matched is None:
        return None

    # 兜底：profile 已被占用但查不到持有者时，用进程树找出 python 拉起的浏览器
    if is_profile_in_use(base) and table:
        return [(pid, describe(pid)) for pid in _tree_holders(table)]
    return matched or []


def _lock_file_paths(user_data_dir) -> list[Path]:
    base = Path(user_data_dir)
    return [base / name for name in _LOCK_FILES if (base / name).exists()]


def restart_manager_pids(paths) -> list[int] | None:
    """用 Windows Restart Manager 反查持有这些文件的进程；不可用返回 None"""
    if os.name != "nt":
        return None
    targets = [str(path) for path in paths]
    if not targets:
        return []

    import ctypes
    from ctypes import wintypes

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("Process", UniqueProcess),
            ("strAppName", wintypes.WCHAR * 256),
            ("strServiceShortName", wintypes.WCHAR * 64),
            ("ApplicationType", ctypes.c_uint),
            ("AppStatus", ctypes.c_uint),
            ("TSSessionId", ctypes.c_uint),
            ("bRestartable", wintypes.BOOL),
        ]

    try:
        rstrtmgr = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    except OSError:
        return None

    start_session = rstrtmgr.RmStartSession
    start_session.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    start_session.restype = wintypes.DWORD
    register = rstrtmgr.RmRegisterResources
    register.argtypes = [
        wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT, ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
    ]
    register.restype = wintypes.DWORD
    get_list = rstrtmgr.RmGetList
    get_list.argtypes = [
        wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(ProcessInfo), ctypes.POINTER(wintypes.DWORD),
    ]
    get_list.restype = wintypes.DWORD
    end_session = rstrtmgr.RmEndSession
    end_session.argtypes = [wintypes.DWORD]
    end_session.restype = wintypes.DWORD

    session = wintypes.DWORD(0)
    # 会话 key 需由调用方填入一个 GUID 字符串（实测传空缓冲区会返回 ERROR_WRITE_FAULT）
    key = ctypes.create_unicode_buffer(str(uuid.uuid4()))
    if start_session(ctypes.byref(session), 0, key) != 0:
        return None

    try:
        file_array = (wintypes.LPCWSTR * len(targets))(*targets)
        if register(session, len(targets), file_array, 0, None, 0, None) != 0:
            return None

        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        result = get_list(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        if result == 0:  # ERROR_SUCCESS：没有进程持有
            return []
        if result != 234:  # ERROR_MORE_DATA
            return None

        count = wintypes.UINT(needed.value)
        buffer = (ProcessInfo * needed.value)()
        result = get_list(session, ctypes.byref(needed), ctypes.byref(count), buffer, ctypes.byref(reasons))
        if result != 0:
            return None
        return [int(buffer[i].Process.dwProcessId) for i in range(count.value)]
    finally:
        end_session(session)


def _process_names(pids) -> dict[int, str]:
    """批量取进程名：{pid: 名称}"""
    wanted = {int(pid) for pid in pids}
    if not wanted:
        return {}
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    import csv
    import io

    names: dict[int, str] = {}
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) < 2 or not row[1].strip().isdigit():
            continue
        pid = int(row[1])
        if pid in wanted:
            names[pid] = row[0].strip()
    return names


def _kill_pid(pid: int) -> bool:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, errors="replace", timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0
    try:
        os.kill(pid, 9)
    except OSError:
        return False
    return True


def kill_profile_holders(user_data_dir, quiet: bool = False) -> list[int]:
    """结束占用指定 profile 的浏览器进程，返回成功结束的 pid 列表

    只会结束浏览器进程，且永不触碰自身及其祖先进程。
    """
    holders = find_profile_holders(user_data_dir) or []
    browsers = _browser_process_names()
    table = _process_table()
    protected = _self_and_ancestors(table) if table else {os.getpid()}

    killed: list[int] = []
    for pid, description in holders:
        if pid in protected:
            continue
        if table and pid in table and table[pid][1].lower() not in browsers:
            continue
        if _kill_pid(pid):
            killed.append(pid)
            if not quiet:
                print(f"  已结束占用 profile 的进程: pid={pid} {description[:120]}")
    return killed


def ensure_profile_available(user_data_dir, auto_kill: bool = True, retries: int = 3, delay: float = 2.0) -> bool:
    """确保 profile 可用：被占用时尝试结束残留进程，成功返回 True"""
    for attempt in range(retries + 1):
        if not is_profile_in_use(user_data_dir):
            return True
        if not auto_kill:
            return False

        holders = find_profile_holders(user_data_dir)
        if holders:
            kill_profile_holders(user_data_dir)
        if attempt < retries:
            time.sleep(delay)

    return not is_profile_in_use(user_data_dir)


def acquire_single_instance(port: int = SINGLE_INSTANCE_PORT, host: str = "127.0.0.1"):
    """获取单实例锁：成功返回持有锁的 socket，已被占用返回 None

    用回环端口做互斥，进程退出（含崩溃）后由系统自动释放，不会留下死锁文件。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def main(argv=None) -> int:
    """命令行入口：python -m utils.profile [--check|--kill]"""
    import argparse

    from utils.config import MOBILE_PROFILE_DIR, USER_DATA_DIR

    parser = argparse.ArgumentParser(description="检查/清理 bing-points 使用的浏览器 profile 占用")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="只检查占用情况（默认）")
    group.add_argument("--kill", action="store_true", help="结束占用这些 profile 的浏览器进程")
    args = parser.parse_args(argv)

    exit_code = 0
    for label, directory in (("桌面", USER_DATA_DIR), ("移动端", MOBILE_PROFILE_DIR)):
        path = Path(directory)
        if not path.exists():
            print(f"[{label}] profile 目录不存在（未使用过）: {path}")
            continue
        holders = find_profile_holders(path)
        in_use = is_profile_in_use(path)
        print(f"[{label}] {path}")
        print(f"  占用状态: {'被占用' if in_use else '空闲'}")
        if holders is None:
            print("  进程查询不可用（可能是权限限制），无法定位占用进程")
        elif not holders:
            print("  未找到命令行匹配该 profile 的浏览器进程")
        else:
            for pid, command in holders:
                print(f"  pid={pid} {command[:160]}")

        if args.kill and in_use:
            killed = kill_profile_holders(path)
            print(f"  已结束 {len(killed)} 个进程")
            if is_profile_in_use(path):
                print("  警告: profile 仍被占用，可能有其它进程（如杀毒软件）持有文件")
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
