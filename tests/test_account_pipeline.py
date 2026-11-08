"""单账号流水线的 AccountOutcome 契约测试。"""
import pytest

from utils import account_pipeline, browser, clients, config, tasks
from utils.edge_streak import EdgeStreakResult, EdgeStreakStatus
from utils.outcomes import AccountOutcome, TaskOutcome, TaskStatus


class _Page:
    url = "https://rewards.bing.com/dashboard"

    def goto(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, *_args):
        pass

    def close(self):
        pass


class _Context:
    def new_page(self):
        return _Page()


def _install_successful_account(
    monkeypatch,
    *,
    mobile_result=1,
    devices=None,
    webview_result=clients.REWARDS_DESKTOP_TASK_GROWN,
):
    monkeypatch.setattr(account_pipeline, "ensure_account", lambda *_a, **_k: True)
    monkeypatch.setattr(account_pipeline, "do_login", lambda *_a, **_k: True)
    monkeypatch.setattr(account_pipeline, "safe_goto", lambda *_a, **_k: None)
    monkeypatch.setattr(account_pipeline, "detect_risk_control", lambda _page: None)
    monkeypatch.setattr(
        account_pipeline,
        "parse_dashboard_data",
        lambda _page: {
            "claimable_points": 0,
            "daily_set_items": [],
            "daily_activities_done": 3,
            "daily_activities_total": 3,
            "pc_search_done": 0,
            "pc_search_total": 0,
        },
    )
    monkeypatch.setattr(account_pipeline, "claim_available_points", lambda *_a, **_k: 0)
    monkeypatch.setattr(account_pipeline, "do_daily_set_tasks", lambda *_a, **_k: 0)
    monkeypatch.setattr(account_pipeline, "do_dashboard_activities", lambda *_a, **_k: 0)
    monkeypatch.setattr(account_pipeline, "do_daily_offer_tasks", lambda *_a, **_k: 0)
    monkeypatch.setattr(account_pipeline, "do_point_punch_cards", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        account_pipeline,
        "do_edge_browsing_streak_detail",
        lambda *_a, **_k: EdgeStreakResult(EdgeStreakStatus.COMPLETED, progress=(30, 30)),
    )
    monkeypatch.setattr(
        account_pipeline,
        "mobile_checkin",
        lambda *_a, **_k: mobile_result,
    )
    monkeypatch.setattr(
        account_pipeline,
        "list_android_devices",
        lambda: [] if devices is None else devices,
    )
    monkeypatch.setattr(
        account_pipeline,
        "run_rewards_desktop_task",
        lambda *_a, **_k: webview_result,
    )
    monkeypatch.setattr(
        account_pipeline,
        "EDGE_USER_DATA_DIR",
        config.USER_DATA_DIR.parent / "real-edge-profile",
    )
    monkeypatch.setattr(account_pipeline, "DEFAULT_PC_SEARCHES", 3)
    monkeypatch.setattr(
        account_pipeline,
        "do_searches",
        lambda *_a, **_k: browser.SearchOutcome(
            status=browser.SearchStatus.COMPLETED,
            requested=3,
            completed=3,
        ),
    )
    monkeypatch.setattr(account_pipeline, "DEFAULT_MOBILE_SEARCHES", 0)


def _task_by_name(outcome, name):
    matches = [task for task in outcome.tasks if task.name == name]
    assert len(matches) == 1
    return matches[0]


def test_run_account_outcome_success_records_all_required_tasks(monkeypatch):
    _install_successful_account(monkeypatch, mobile_result=0, devices=[])

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    assert isinstance(outcome, AccountOutcome)
    assert outcome.success is True
    assert outcome.final_claim_status is TaskStatus.SUCCESS
    statuses = {task.name: task.status for task in outcome.tasks}
    assert statuses == {
        "login": TaskStatus.SUCCESS,
        "daily_set": TaskStatus.SUCCESS,
        "daily_activities": TaskStatus.SUCCESS,
        "pc_search": TaskStatus.SUCCESS,
        "edge": TaskStatus.SUCCESS,
        "android": TaskStatus.SKIPPED,
        "webview": TaskStatus.SUCCESS,
        "final_claim": TaskStatus.SUCCESS,
    }
    assert _task_by_name(outcome, "android").required is False


def test_run_account_outcome_daily_set_failure_does_not_overwrite_as_success(monkeypatch):
    _install_successful_account(monkeypatch)
    dashboards = iter(
        [
            {
                "claimable_points": 0,
                "daily_set_items": [],
                "daily_activities_done": 3,
                "daily_activities_total": 3,
                "pc_search_done": 0,
                "pc_search_total": 0,
            },
            {
                "claimable_points": 0,
                "daily_set_items": [{"title": "remaining"}],
                "daily_activities_done": 3,
                "daily_activities_total": 3,
                "pc_search_done": 0,
                "pc_search_total": 0,
            },
        ]
    )
    monkeypatch.setattr(account_pipeline, "parse_dashboard_data", lambda _page: next(dashboards))

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    assert outcome.success is False
    assert _task_by_name(outcome, "daily_set").status is TaskStatus.FAILED


def test_run_account_bool_is_success_of_outcome(monkeypatch):
    failed = AccountOutcome(
        account="user@example.invalid",
        tasks=(
            TaskOutcome(
                name="login",
                status=TaskStatus.FAILED,
                required=True,
            ),
        ),
        final_claim_status=TaskStatus.SKIPPED,
        required_task_names=frozenset({"login"}),
    )
    monkeypatch.setattr(account_pipeline, "_run_account_outcome", lambda *_a, **_k: failed)

    assert account_pipeline.run_account(
        None, _Context(), "user@example.invalid", "pw", True
    ) is False


def test_run_account_outcome_daily_activities_failure_is_failed(monkeypatch):
    _install_successful_account(monkeypatch)
    dashboards = iter(
        [
            {
                "claimable_points": 0,
                "daily_set_items": [],
                "daily_activities_done": 3,
                "daily_activities_total": 3,
                "pc_search_done": 0,
                "pc_search_total": 0,
            },
            {
                "claimable_points": 0,
                "daily_set_items": [],
                "daily_activities_done": 2,
                "daily_activities_total": 3,
                "pc_search_done": 0,
                "pc_search_total": 0,
            },
        ]
    )
    monkeypatch.setattr(account_pipeline, "parse_dashboard_data", lambda _page: next(dashboards))

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    assert outcome.success is False
    assert _task_by_name(outcome, "daily_activities").status is TaskStatus.FAILED
    assert _task_by_name(outcome, "pc_search").status is TaskStatus.SUCCESS


def test_run_account_outcome_pc_search_failure_is_failed(monkeypatch):
    _install_successful_account(monkeypatch)
    monkeypatch.setattr(account_pipeline, "DEFAULT_PC_SEARCHES", 3)
    monkeypatch.setattr(
        account_pipeline,
        "do_searches",
        lambda *_a, **_k: browser.SearchOutcome(
            status=browser.SearchStatus.SCORE_STALLED,
            requested=3,
            completed=1,
        ),
    )

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    assert outcome.success is False
    assert _task_by_name(outcome, "pc_search").status is TaskStatus.FAILED


def test_run_account_outcome_android_absent_is_optional_skip(monkeypatch):
    _install_successful_account(monkeypatch, mobile_result=0, devices=[])

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    android = _task_by_name(outcome, "android")
    assert android.status is TaskStatus.SKIPPED
    assert android.required is False
    assert outcome.success is True


def test_run_account_outcome_android_present_unconfirmed_short_circuits(monkeypatch):
    _install_successful_account(
        monkeypatch,
        mobile_result=0,
        devices=["emulator-5554"],
    )
    webview_calls = []
    monkeypatch.setattr(
        account_pipeline,
        "run_rewards_desktop_task",
        lambda *_a, **_k: webview_calls.append("webview") or 0,
    )

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True, total_accounts=1
    )

    android = _task_by_name(outcome, "android")
    assert outcome.success is False
    assert android.status is TaskStatus.FAILED
    assert android.required is True
    assert webview_calls == []
    assert all(
        task.status is not TaskStatus.SUCCESS
        for task in outcome.tasks
        if task.name in {"webview", "final_claim"}
    )


def test_run_account_outcome_login_failure_does_not_mark_later_tasks_success(monkeypatch):
    _install_successful_account(monkeypatch)
    monkeypatch.setattr(account_pipeline, "ensure_account", lambda *_a, **_k: False)

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    assert outcome.success is False
    assert _task_by_name(outcome, "login").status is TaskStatus.IDENTITY_UNVERIFIED
    assert outcome.final_claim_status is TaskStatus.SKIPPED
    by_name = {task.name: task for task in outcome.tasks}
    assert by_name["login"].status is TaskStatus.IDENTITY_UNVERIFIED
    for name in ("daily_set", "daily_activities", "pc_search", "edge", "webview"):
        assert by_name[name].status is TaskStatus.BLOCKED
    assert "android" not in by_name or by_name["android"].status is not TaskStatus.SUCCESS
    assert outcome.final_claim_status is TaskStatus.SKIPPED


def test_run_account_outcome_webview_no_task_is_optional_skip(monkeypatch):
    _install_successful_account(
        monkeypatch,
        webview_result=clients.REWARDS_DESKTOP_NO_TASK,
    )

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    webview = _task_by_name(outcome, "webview")
    assert webview.status is TaskStatus.SKIPPED
    assert webview.required is False
    assert outcome.success is True


def test_run_account_outcome_webview_uia_unavailable_is_optional_skip(monkeypatch):
    _install_successful_account(
        monkeypatch,
        webview_result=clients.REWARDS_DESKTOP_UIA_UNAVAILABLE,
    )

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    webview = _task_by_name(outcome, "webview")
    assert webview.status is TaskStatus.ENVIRONMENT_UNAVAILABLE
    assert webview.required is False
    assert outcome.success is True


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (clients.REWARDS_DESKTOP_IDENTITY_UNVERIFIED, TaskStatus.IDENTITY_UNVERIFIED),
        (clients.REWARDS_DESKTOP_IDENTITY_MISMATCH, TaskStatus.IDENTITY_MISMATCH),
        (clients.REWARDS_DESKTOP_TASK_ITEM_MISSING, TaskStatus.FAILED),
    ],
)
def test_run_account_outcome_webview_failure_short_circuits(monkeypatch, status, expected):
    _install_successful_account(monkeypatch, webview_result=status)
    claim_calls = []
    monkeypatch.setattr(
        account_pipeline,
        "claim_available_points",
        lambda *_a, **_k: claim_calls.append("claim") or 0,
    )

    outcome = account_pipeline._run_account_outcome(
        None, _Context(), "user@example.invalid", "pw", True
    )

    webview = _task_by_name(outcome, "webview")
    assert outcome.success is False
    assert webview.status is expected
    assert webview.required is True
    assert outcome.final_claim_status is TaskStatus.SKIPPED
    assert len(claim_calls) == 3
