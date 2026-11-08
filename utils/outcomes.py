"""运行结果的领域模型。

阶段 0 只定义纯数据结构与聚合规则，不连接浏览器、UIA、通知或主流程。
现有生产行为保持不变；后续迁移时由流水线逐步改用这些类型。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    """单个任务的终态。"""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    IDENTITY_UNVERIFIED = "identity_unverified"
    IDENTITY_MISMATCH = "identity_mismatch"
    BLOCKED = "blocked"


class RunStatus(str, Enum):
    """一次完整运行的终态。"""

    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    ALL_FAILED = "all_failed"
    STARTUP_FAILED = "startup_failed"
    ACCOUNT_CONFIG_FAILED = "account_config_failed"
    RISK_CONTROL = "risk_control"
    ALREADY_RUNNING = "already_running"
    INTERNAL_ERROR = "internal_error"


class ExitCode(int, Enum):
    """稳定的进程退出码。"""

    SUCCESS = 0
    INTERNAL_ERROR = 1
    STARTUP_FAILED = 2
    ALL_ACCOUNTS_FAILED = 3
    ACCOUNT_CONFIG_FAILED = 4
    RISK_CONTROL = 5
    PARTIAL_FAILURE = 6
    ALREADY_RUNNING = 7


@dataclass(frozen=True)
class TaskOutcome:
    """一个任务的不可变结果。"""

    name: str
    status: TaskStatus
    required: bool
    detail: str = ""
    before: object | None = None
    after: object | None = None

    @property
    def is_success(self) -> bool:
        return self.status is TaskStatus.SUCCESS


@dataclass(frozen=True)
class AccountOutcome:
    """一个账号的任务结果汇总。"""

    account: str
    tasks: tuple[TaskOutcome, ...] = ()
    final_claim_status: TaskStatus = TaskStatus.SUCCESS
    error: str = ""

    @property
    def success(self) -> bool:
        """仅当所有必需任务和最终领取均成功时，账号才算成功。"""
        required_tasks_succeeded = all(
            task.status is TaskStatus.SUCCESS
            for task in self.tasks
            if task.required
        )
        return required_tasks_succeeded and self.final_claim_status is TaskStatus.SUCCESS

    @property
    def failed_tasks(self) -> tuple[TaskOutcome, ...]:
        return tuple(
            task for task in self.tasks
            if task.required and task.status is not TaskStatus.SUCCESS
        )


@dataclass(frozen=True)
class RunResult:
    """一次完整运行的不可变快照。"""

    status: RunStatus
    exit_code: ExitCode
    accounts: tuple[AccountOutcome, ...] = ()
    pid: int | None = None
    log_path: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""

    @property
    def success(self) -> bool:
        return self.status is RunStatus.SUCCESS and self.exit_code is ExitCode.SUCCESS


def summarize_accounts(accounts: tuple[AccountOutcome, ...]) -> tuple[RunStatus, ExitCode]:
    """根据账号结果得出运行状态与退出码。"""
    if not accounts:
        return RunStatus.ALL_FAILED, ExitCode.ALL_ACCOUNTS_FAILED

    succeeded = sum(1 for account in accounts if account.success)
    if succeeded == len(accounts):
        return RunStatus.SUCCESS, ExitCode.SUCCESS
    if succeeded == 0:
        return RunStatus.ALL_FAILED, ExitCode.ALL_ACCOUNTS_FAILED
    return RunStatus.PARTIAL_FAILURE, ExitCode.PARTIAL_FAILURE


def task_result(
    name: str,
    status: TaskStatus | bool,
    *,
    required: bool,
    detail: str = "",
    before: object | None = None,
    after: object | None = None,
) -> TaskOutcome:
    """兼容旧布尔返回值，逐步迁移时使用。"""
    if isinstance(status, bool):
        normalized = TaskStatus.SUCCESS if status else TaskStatus.FAILED
    else:
        normalized = status
    return TaskOutcome(
        name=name,
        status=normalized,
        required=required,
        detail=detail,
        before=before,
        after=after,
    )
