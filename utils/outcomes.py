"""运行结果的领域模型。"""
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


_EXIT_CODE_REASONS = {
    ExitCode.SUCCESS: "运行成功",
    ExitCode.INTERNAL_ERROR: "内部错误（未预期异常）",
    ExitCode.STARTUP_FAILED: "启动失败（浏览器 profile 被占用 / 临时目录不可写 / 会话0拿不到窗口站）",
    ExitCode.ALL_ACCOUNTS_FAILED: "全部账号处理失败（通常是登录态失效或网络问题）",
    ExitCode.ACCOUNT_CONFIG_FAILED: "读取账号配置失败（accounts.txt 缺失或格式错误）",
    ExitCode.RISK_CONTROL: "检测到微软验证页面或验证码，已停止运行",
    ExitCode.PARTIAL_FAILURE: "部分账号处理失败（至少一个账号成功、至少一个失败）",
    ExitCode.ALREADY_RUNNING: "已有另一个实例在运行，本次已安全让位",
}


def exit_code_reason(code: int | ExitCode) -> str:
    """返回稳定的中文退出码说明，供日志和通知共用。"""
    try:
        normalized = ExitCode(int(code))
    except (TypeError, ValueError):
        return f"未知错误（退出码 {code}）"
    return _EXIT_CODE_REASONS[normalized]


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
    required_task_names: frozenset[str] = frozenset()

    @property
    def success(self) -> bool:
        """仅当完整必需任务集合和最终领取均成功时，账号才算成功。"""
        recorded = {task.name for task in self.tasks}
        if not recorded or not self.required_task_names:
            return False
        if not self.required_task_names.issubset(recorded):
            return False
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


@dataclass(frozen=True)
class RunSnapshot:
    """一次运行完成后的不可变通知快照。"""

    pid: int | None
    log_path: str
    started_at: datetime
    finished_at: datetime
    exit_code: ExitCode
    log_text: str = ""
