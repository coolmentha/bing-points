"""阶段 0：运行结果领域模型的纯状态测试。"""
from datetime import datetime

import pytest

from utils.outcomes import (
    AccountOutcome,
    ExitCode,
    RunResult,
    RunStatus,
    TaskOutcome,
    TaskStatus,
    summarize_accounts,
    task_result,
)


def _task(name: str, status: TaskStatus, required: bool = True) -> TaskOutcome:
    return TaskOutcome(name=name, status=status, required=required)


def test_task_outcome_is_success_only_for_success_status():
    assert _task("daily", TaskStatus.SUCCESS).is_success is True
    assert _task("daily", TaskStatus.SKIPPED).is_success is False
    assert _task("daily", TaskStatus.FAILED).is_success is False


def test_account_success_requires_every_required_task_to_succeed():
    account = AccountOutcome(
        account="user@example.invalid",
        tasks=(
            _task("daily", TaskStatus.SUCCESS),
            _task("edge", TaskStatus.FAILED),
        ),
        required_task_names=frozenset({"daily", "edge"}),
    )

    assert account.success is False
    assert [item.name for item in account.failed_tasks] == ["edge"]


def test_optional_task_failure_does_not_fail_account():
    account = AccountOutcome(
        account="user@example.invalid",
        tasks=(
            _task("daily", TaskStatus.SUCCESS),
            _task("android", TaskStatus.ENVIRONMENT_UNAVAILABLE, required=False),
            _task("webview", TaskStatus.SKIPPED, required=False),
        ),
        required_task_names=frozenset({"daily"}),
    )

    assert account.success is True
    assert account.failed_tasks == ()


def test_account_success_requires_final_claim_success():
    account = AccountOutcome(
        account="user@example.invalid",
        tasks=(_task("daily", TaskStatus.SUCCESS),),
        final_claim_status=TaskStatus.FAILED,
        required_task_names=frozenset({"daily"}),
    )

    assert account.success is False


def test_account_success_requires_complete_required_task_set():
    only_login = AccountOutcome(
        account="user@example.invalid",
        tasks=(_task("login", TaskStatus.SUCCESS),),
        final_claim_status=TaskStatus.SUCCESS,
        required_task_names=frozenset({"login", "daily_set", "final_claim"}),
    )
    assert only_login.success is False

    empty = AccountOutcome(account="user@example.invalid")
    assert empty.success is False


def test_identity_statuses_are_not_treated_as_success():
    account = AccountOutcome(
        account="target@example.invalid",
        tasks=(
            _task("edge", TaskStatus.IDENTITY_MISMATCH),
            _task("webview", TaskStatus.IDENTITY_UNVERIFIED),
        ),
        required_task_names=frozenset({"edge", "webview"}),
    )

    assert account.success is False
    assert {item.status for item in account.failed_tasks} == {
        TaskStatus.IDENTITY_MISMATCH,
        TaskStatus.IDENTITY_UNVERIFIED,
    }


def test_empty_accounts_is_all_failed():
    assert summarize_accounts(()) == (
        RunStatus.ALL_FAILED,
        ExitCode.ALL_ACCOUNTS_FAILED,
    )


def test_all_accounts_success():
    account = AccountOutcome(
        account="user@example.invalid",
        tasks=(_task("daily", TaskStatus.SUCCESS),),
        required_task_names=frozenset({"daily"}),
    )

    assert summarize_accounts((account,)) == (RunStatus.SUCCESS, ExitCode.SUCCESS)


def test_partial_failure_is_not_reported_as_success():
    ok = AccountOutcome(
        account="ok@example.invalid",
        tasks=(_task("daily", TaskStatus.SUCCESS),),
        required_task_names=frozenset({"daily"}),
    )
    failed = AccountOutcome(
        account="failed@example.invalid",
        tasks=(_task("daily", TaskStatus.FAILED),),
        required_task_names=frozenset({"daily"}),
    )

    assert summarize_accounts((ok, failed)) == (
        RunStatus.PARTIAL_FAILURE,
        ExitCode.PARTIAL_FAILURE,
    )


def test_all_accounts_failed():
    failed = AccountOutcome(
        account="failed@example.invalid",
        tasks=(_task("daily", TaskStatus.FAILED),),
        required_task_names=frozenset({"daily"}),
    )

    assert summarize_accounts((failed,)) == (
        RunStatus.ALL_FAILED,
        ExitCode.ALL_ACCOUNTS_FAILED,
    )


def test_run_result_success_requires_matching_status_and_exit_code():
    result = RunResult(
        status=RunStatus.SUCCESS,
        exit_code=ExitCode.SUCCESS,
        accounts=(),
        pid=123,
        log_path="logs/run_test.log",
        started_at=datetime(2026, 11, 8, 20, 0, 0),
        finished_at=datetime(2026, 11, 8, 20, 30, 0),
    )

    assert result.success is True

    partial = RunResult(
        status=RunStatus.PARTIAL_FAILURE,
        exit_code=ExitCode.PARTIAL_FAILURE,
    )
    assert partial.success is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (True, TaskStatus.SUCCESS),
        (False, TaskStatus.FAILED),
        (TaskStatus.SKIPPED, TaskStatus.SKIPPED),
        (TaskStatus.ENVIRONMENT_UNAVAILABLE, TaskStatus.ENVIRONMENT_UNAVAILABLE),
    ],
)
def test_task_result_normalizes_legacy_boolean_and_explicit_status(status, expected):
    outcome = task_result("legacy", status, required=False, detail="legacy adapter")

    assert outcome.status is expected
    assert outcome.required is False
    assert outcome.detail == "legacy adapter"


def test_exit_code_values_are_stable():
    assert ExitCode.SUCCESS == 0
    assert ExitCode.INTERNAL_ERROR == 1
    assert ExitCode.STARTUP_FAILED == 2
    assert ExitCode.ALL_ACCOUNTS_FAILED == 3
    assert ExitCode.ACCOUNT_CONFIG_FAILED == 4
    assert ExitCode.RISK_CONTROL == 5
    assert ExitCode.PARTIAL_FAILURE == 6
    assert ExitCode.ALREADY_RUNNING == 7
