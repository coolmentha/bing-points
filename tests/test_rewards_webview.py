"""Rewards 桌面 WebView 适配器的独立契约测试。"""
import subprocess
import sys
from pathlib import Path


WEBVIEW_EXPORTS = (
    "ENABLE_REWARDS_DESKTOP",
    "REWARDS_DESKTOP_AUMID",
    "REWARDS_DESKTOP_UIA_RESULT",
    "REWARDS_DESKTOP_ERROR",
    "REWARDS_DESKTOP_UIA_UNAVAILABLE",
    "REWARDS_DESKTOP_NO_TASK",
    "REWARDS_DESKTOP_TASK_GROWN",
    "REWARDS_DESKTOP_IDENTITY_UNVERIFIED",
    "REWARDS_DESKTOP_IDENTITY_MISMATCH",
    "REWARDS_DESKTOP_TASK_ITEM_MISSING",
    "_rewards_desktop_hwnd",
    "_last_output_value",
    "_output_progress",
    "rewards_desktop_available",
    "open_rewards_desktop",
    "_empty_rewards_result",
    "_is_explicit_uia_unavailable",
    "_parse_rewards_output",
    "_rewards_uia",
    "rewards_desktop_state",
    "run_rewards_desktop_task",
)


def test_rewards_webview_exports_adapter_api_directly():
    from utils import rewards_webview

    for name in WEBVIEW_EXPORTS:
        assert hasattr(rewards_webview, name), name


def test_clients_statically_reexports_rewards_webview_symbols():
    from utils import clients, rewards_webview

    for name in WEBVIEW_EXPORTS:
        assert getattr(clients, name) is getattr(rewards_webview, name), name


def test_rewards_webview_fresh_import_has_no_cycle():
    script = (
        "import utils.rewards_webview;"
        "import utils.clients;"
        "import utils.account_pipeline;"
        "import main;"
        "assert utils.clients.run_rewards_desktop_task is "
        "utils.rewards_webview.run_rewards_desktop_task"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_task_page_without_item_is_explicit_failure():
    from utils import rewards_webview

    result = rewards_webview._parse_rewards_output(
        "AVAILABLE=1\n"
        "UIA_READY=1\n"
        "TASK_STATE=task\n"
        "TASK=0\n"
        "SUCCESS=0\n"
    )

    assert result["task_state"] == "task_item_missing"
    assert result["error"] is True
    assert result["success"] is False


def test_run_task_returns_task_item_missing_status(monkeypatch):
    from utils import rewards_webview

    monkeypatch.setattr(rewards_webview, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(rewards_webview, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(
        rewards_webview,
        "_rewards_uia",
        lambda mode="run", expected_email=None: {
            "available": True,
            "uia_ready": True,
            "task_state": "task_item_missing",
            "error": True,
            "success": False,
        },
    )

    assert rewards_webview.run_rewards_desktop_task() == (
        rewards_webview.REWARDS_DESKTOP_TASK_ITEM_MISSING
    )


def test_run_task_returns_uia_unavailable_status(monkeypatch):
    from utils import rewards_webview

    monkeypatch.setattr(rewards_webview, "ENABLE_REWARDS_DESKTOP", True)
    monkeypatch.setattr(rewards_webview, "rewards_desktop_available", lambda: True)
    monkeypatch.setattr(
        rewards_webview,
        "_rewards_uia",
        lambda mode="run", expected_email=None: {
            "available": True,
            "uia_ready": False,
            "uia_unavailable": True,
        },
    )

    assert rewards_webview.run_rewards_desktop_task() == (
        rewards_webview.REWARDS_DESKTOP_UIA_UNAVAILABLE
    )
