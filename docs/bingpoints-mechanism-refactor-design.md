# BingPoints 机制重构设计

- 文档状态：待评审，未实施
- 适用范围：当前 `hotfix/rewards-identity-wait` 工作区相对 `master` 的未提交变更
- 目标读者：项目维护者、后续实现 Agent、Reviewer
- 编写依据：2026-11-08 两次只读整体审查、真实生产运行日志、当前代码
- 重要约束：本文只定义目标机制与迁移方案，不代表现有代码已经满足

## 1. 背景与结论

当前代码已能完成一次完整生产运行，但整体机制存在结构性风险。测试全绿（`272 passed`）证明的是既有 mock、页面夹具和文本协议解析通过，不能证明真实浏览器、正式 Edge profile、Windows UIA 和账号边界安全。

本次审查确认的核心结论：

1. 账号身份边界没有统一约束。网页 Rewards、Edge profile、Android Bing App、Rewards 桌面 WebView 各自独立判断，部分只判断“某个账号存在”，没有确认是当前目标账号。
2. 页面所有权没有可靠模型。Edge Streak 用页面集合差推断“本次新建页”，可能关闭用户并发打开的标签页。
3. 任务结果没有显式模型。大量函数返回 `bool`、`0/1/-1/-2` 或只打印日志，导致假成功、静默失败、环境问题与业务失败混在一起。
4. 主流程是长过程 + 布尔短路。`run_account()` 负责登录、每日任务、活动、搜索、Edge、Android、WebView、最终领取和异常处理，无法清楚表达哪些任务是必需、可选、跳过或被阻塞。
5. 外部客户端协议脆弱。WebView 使用固定结果文件和 `KEY=VALUE` 文本协议，可能读取旧结果；UIA 的“任务页存在但任务项找不到”可能被上层解释为无任务。
6. 通知生命周期虽然已避免“旧日志误附”，但当前把 SMTP 网络 I/O 放在单实例锁生命周期内，仍可能阻塞下一次运行。

结论：当前分支不适合直接合入。应先把安全边界和结果语义做成不可绕过的基础设施，再考虑继续增加选择器、兜底和测试。

## 2. 审查中已确认的真实问题

以下问题具有当前代码路径或只读复现依据，优先级高于一般重构建议。

### 2.1 P1：必需任务未完成仍可能返回账号成功

位置：`main.py:476-511`、`utils/browser.py:109-143`

- 每日任务、活动、日常任务、限时活动的返回值被忽略。
- 搜索在关键词为空、积分不增长、页面异常时可能提前结束，但账号仍可能返回 `True`。
- 最终成功没有基于“重新解析页面后的必需任务状态”做确认。

影响：无人值守模式下会报告成功，但实际任务未完成。

### 2.2 P1：多账号部分失败仍按完全成功处理

位置：`main.py:675-681`

当前仅当 `failed == len(results)` 时返回退出码 3。两个账号一成功一失败时返回 0，也不发送失败通知。

影响：部分账号长期失败会被静默吞掉。

### 2.3 P1：WebView 没有绑定目标账号

位置：`main.py:555-560`、`utils/clients.py:430-460`、`utils/rewards_desktop_uia.ps1`

`run_rewards_desktop_task()` 没有接收目标邮箱。桌面应用登录其他账号时，仍可能点击其任务，再把结果归到当前网页账号。

影响：跨账号操作风险，结果不可归属。

### 2.4 P1：Edge profile 只验证“任意账号已登录”

位置：`utils/tasks.py:837-864`、`utils/tasks.py:1156`

`edge_browser_profile_logged_in()` 只判断任意 profile 的 `user_name` 或 `account_info` 非空，不接受目标邮箱，也不确认当前 CDP 使用的 profile。

影响：目标网页账号与 Edge profile 账号不一致时仍会进入 30 分钟流程，可能空跑或触发错误账号活动。

### 2.5 P1：页面集合差可能关闭用户并发打开的标签页

位置：`utils/tasks.py:953-962`、`utils/tasks.py:1046-1049`、`utils/tasks.py:1089-1093`、`utils/tasks.py:1114-1125`

当前 `_new_pages()` 使用：

```python
item for item in context.pages
if item is not page and item not in existing_pages
```

正式 Edge profile 下，用户自己新开的标签页也会进入该差集，并可能被 `_close_edge_streak_pages()` 关闭。

影响：破坏用户浏览器状态。

### 2.6 P1：WebView 固定结果文件可能被旧结果污染

位置：`utils/clients.py:351-403`

当前使用固定 `tmp_rewards_desktop_result.txt`，删除失败只忽略 `OSError`，且未校验 PowerShell 返回码或结果归属。

影响：上一轮残留的 `SUCCESS=1` 可能导致本轮假成功。

### 2.7 P2：WebView“任务页存在但任务项缺失”可能被当成无任务

位置：`utils/rewards_desktop_uia.ps1:434-484`、`utils/clients.py:430-460`

PowerShell 可能输出：

```text
TASK_STATE=task
TASK=0
SUCCESS=0
```

但客户端没有把它映射为错误，上层可能按“无任务”跳过。

影响：任务实际存在但定位失败时，账号可能被判定成功。

### 2.8 P2：Edge 30 分钟按逻辑分钟计时

位置：`utils/tasks.py:1206-1226`

`elapsed += poll_seconds` 不按真实墙钟推进。导航、加载、滚动和等待自身耗时会导致实际运行明显超过 30 分钟。

影响：长时间占用正式 profile，放大锁冲突与用户操作干扰。

### 2.9 P2：Android 环境不可用仍可能判账号失败

位置：`main.py:540-549`、`utils/clients.py:164-300`

当前只区分“无设备”。设备存在但 App 未安装、UIA dump 失败、入口找不到、身份无法读取时，`mobile_checkin()` 都返回 `0`，单账号下会判失败。

影响：可选客户端的环境问题覆盖其他已完成任务。

### 2.10 P2：Edge 异常进度可被判完成

位置：`utils/tasks.py:1149-1152`

当前判断：

```python
if current and current[0] >= current[1]:
    return 1
```

`0/0`、`1/0` 会被当作完成。应要求 `total > 0` 且 `0 <= done <= total`。

### 2.11 P3：通知锁覆盖网络 I/O

位置：`main.py:638-644`、`main.py:696-699`

通知在 `guard.close()` 前发送，SMTP 网络调用期间持有单实例锁。新实例可能被静默让位并返回 0。

## 3. 目标原则

重构后应满足以下不可绕过的原则。

### 3.1 账号一致性优先于任务推进

任何外部客户端在操作前必须确认其当前账号等于本次目标账号。

无法确认身份时：

- 不点击任务
- 不切换账号
- 不把客户端结果计入当前账号
- 返回 `identity_unverified` 或 `identity_mismatch`

不能以“通常是同一账号”为假设继续。

### 3.2 成功必须来自可验证状态

账号成功只能来自页面或客户端回读状态，而不能来自：

- 某个函数被调用
- 某个按钮曾点击
- 某个导航发生
- 某个页面集合发生变化
- 某个可选客户端跳过

### 3.3 环境不可用、跳过、失败必须分开

每个任务至少需要以下状态：

```text
success
failed
skipped
environment_unavailable
identity_unverified
blocked
```

`skipped` 代表业务上允许不执行；`environment_unavailable` 代表运行环境不满足；二者都不能自动等价为业务成功或业务失败，必须由任务策略决定。

### 3.4 页面所有权必须由事件确定

只能清理“本次点击事件明确创建的页面”。集合差、URL 相同、页面标题相同都不能作为所有权依据。

无法确认归属时，保留页面，不关闭。

### 3.5 主流程只编排，不实现细节

`main.py` 不应继续承载 Edge 入口、UIA 字符串解析、通知格式、账户任务判定等细节。主流程只负责：

- 获取运行配置与账号列表
- 执行每个账号的流水线
- 汇总 `AccountOutcome`
- 决定退出码与通知
- 管理资源生命周期

### 3.6 测试应针对状态转换和真实协议

优先为纯状态机、结果映射、页面所有权和协议 fixture 写测试，而不是继续增加大型 fake Page/Locator 类。

## 4. 目标领域模型

建议新增 `utils/outcomes.py`（或 `domain/outcomes.py`，但遵循项目现有 `utils/` 结构）。

### 4.1 任务状态

```python
from dataclasses import dataclass
from enum import Enum

class TaskStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    IDENTITY_UNVERIFIED = "identity_unverified"
    IDENTITY_MISMATCH = "identity_mismatch"
    BLOCKED = "blocked"

@dataclass(frozen=True)
class TaskOutcome:
    name: str
    status: TaskStatus
    required: bool
    detail: str = ""
    before: object | None = None
    after: object | None = None
```

### 4.2 账号结果

```python
@dataclass(frozen=True)
class AccountOutcome:
    account: str
    tasks: tuple[TaskOutcome, ...]
    final_claim_status: TaskStatus
    error: str = ""

    @property
    def success(self) -> bool:
        return all(
            task.status == TaskStatus.SUCCESS
            for task in self.tasks
            if task.required
        ) and self.final_claim_status == TaskStatus.SUCCESS
```

### 4.3 运行结果

```python
class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL_FAILURE = "partial_failure"
    ALL_FAILED = "all_failed"
    STARTUP_FAILED = "startup_failed"
    ACCOUNT_CONFIG_FAILED = "account_config_failed"
    RISK_CONTROL = "risk_control"
    ALREADY_RUNNING = "already_running"
    INTERNAL_ERROR = "internal_error"
```

`RunResult` 应包含：

- `status`
- `exit_code`
- `accounts: tuple[AccountOutcome, ...]`
- `log_path`
- `started_at`
- `finished_at`
- `pid`

## 5. 任务策略

必须显式定义每项任务是否必需，而不是继续靠 `if not x: return False` 隐式表达。

建议初始策略如下，最终需产品确认：

| 任务 | 是否必需 | 环境不可用 | 身份不一致 | 未完成 |
|---|---|---|---|---|
| 网页登录/Rewards 身份 | 是 | 失败 | 失败 | 失败 |
| 每日任务 | 是 | 不适用 | 失败 | 失败 |
| 每日活动 | 是 | 不适用 | 失败 | 失败 |
| PC 搜索 | 是 | 不适用 | 失败 | 若积分未增长，需明确策略 |
| Edge 30 分钟打卡 | 是（正式 profile） | 失败 | 失败 | 失败 |
| Android App 签到 | 否 | 跳过 | 跳过 | 单账号失败/多账号记录 |
| WebView 每周任务 | 是（若检测到任务） | 跳过 | 失败 | 失败 |
| 最终领取 | 是 | 失败 | 失败 | 失败 |

对“可选任务失败是否导致账号失败”必须集中配置，不能在 `main.py` 里散落条件。

## 6. 目标模块拆分

建议目录：

```text
main.py
utils/
  outcomes.py
  run_context.py
  account_pipeline.py
  edge_streak.py
  rewards_webview.py
  mobile_client.py
  android_client.py
  notify.py
  profile.py
  login.py
  tasks.py
  browser.py
```

### 6.1 `utils/outcomes.py`

职责：

- 定义 `TaskStatus`、`TaskOutcome`、`AccountOutcome`、`RunStatus`、`RunResult`
- 提供任务策略到账号结果的聚合函数
- 不含页面、浏览器、邮件或 UIA 逻辑

### 6.2 `utils/run_context.py`

职责：

- 封装一次运行的不可变身份：
  - `pid`
  - `started_at`
  - `log_path`
  - `accounts`
  - `headless`
  - `edge_profile`
- 提供日志前缀和通知快照所需的运行元数据
- 不负责实际发邮件

### 6.3 `utils/edge_streak.py`

建议将当前 `utils/tasks.py` 中以下内容迁出：

- `edge_browser_profile_logged_in`
- `_is_edge_streak_url`
- `_edge_streak_targets`
- `_wait_for_edge_streak_target`
- `_activate_edge_streak`
- `_prepare_edge_streak`
- `_close_edge_streak_pages`
- `do_edge_browsing_streak`

建议 API：

```python
class EdgeStreakSession:
    def __init__(self, dashboard_page, expected_email, edge_profile_dir): ...
    def open(self) -> TaskOutcome: ...
    def activate(self) -> TaskOutcome: ...
    def browse_until_complete(self) -> TaskOutcome: ...
    def close(self) -> None: ...
```

关键约束：

- `open` 和 `activate` 使用明确页面事件确认所有权。
- `browse_until_complete` 使用 `time.monotonic()` 截止时间，不按逻辑分钟。
- 只关闭 `owned_pages`。
- `owned_pages` 只来自 click 事件或 Playwright `expect_page()`。
- 无法确认所有权的页面不关闭。

### 6.4 `utils/rewards_webview.py`

职责：

- 打开 Rewards 桌面应用
- 读取当前账号身份
- 校验目标账号
- 读取任务状态
- 点击一个任务
- 回读进度
- 返回 `TaskOutcome`

PowerShell 输出建议改为 JSON，而不是文本键值：

```json
{
  "run_id": "uuid",
  "available": true,
  "uia_ready": true,
  "identity": "user@example.com",
  "task_state": "task_found",
  "task_before": "2/8",
  "task_after": "3/8",
  "invoke_mode": "uia",
  "error": null
}
```

Python 侧使用 `RewardsWebViewResult` dataclass 解析。每次调用使用唯一结果文件和 `run_id`，必须校验：

- PowerShell 返回码
- 结果文件属于本次 run
- JSON 完整可解析
- 账号身份等于目标账号
- 进度确实增长

### 6.5 `utils/mobile_client.py` / `utils/android_client.py`

把当前 `mobile_checkin()` 的返回值改为 `TaskOutcome` 或明确的枚举：

```text
success
no_device
app_missing
uia_unavailable
identity_unverified
identity_mismatch
task_not_found
click_failed
```

上层不再用 `0` 同时表示“无设备、无任务、失败”。

### 6.6 `utils/account_pipeline.py`

职责：

- 按固定顺序执行任务
- 采集 `TaskOutcome`
- 根据任务策略判断是否阻止后续任务
- 最终领取放在独立 finalizer
- 返回 `AccountOutcome`

建议伪代码：

```python
def run_account_pipeline(page, account, policy) -> AccountOutcome:
    outcomes = []
    for task in ordered_tasks:
        outcome = task.run(page, account)
        outcomes.append(outcome)
        if policy.should_block(outcome):
            break
    claim = claim_final(page)
    return AccountOutcome(account.email, tuple(outcomes), claim)
```

### 6.7 `main.py`

只负责：

- 解析参数
- 初始化日志与单实例
- 创建浏览器上下文
- 调用 `account_pipeline`
- 汇总 `RunResult`
- 决定退出码
- 生成通知快照

不再直接判断：

- Edge 入口
- UIA 文本
- 移动端返回码
- 每日任务剩余数

## 7. 账号身份校验设计

### 7.1 Edge profile

新增 API：

```python
def edge_profile_identity(profile_dir) -> str | None
```

只读取当前实际使用 profile 的身份，返回邮箱或 `None`。

`EdgeStreakSession` 在打开活动前必须：

```python
actual = edge_profile_identity(edge_profile_dir)
if actual is None:
    return TaskOutcome(..., IDENTITY_UNVERIFIED)
if actual.lower() != expected_email.lower():
    return TaskOutcome(..., IDENTITY_MISMATCH)
```

### 7.2 WebView

PowerShell 返回桌面应用当前账号身份。Python 侧比较目标账号：

```python
if not result.identity:
    return TaskOutcome(..., IDENTITY_UNVERIFIED)
if result.identity.lower() != expected_email.lower():
    return TaskOutcome(..., IDENTITY_MISMATCH)
```

身份不一致时不得点击任何任务。

### 7.3 Android

保留并强化现有 `android_profile_email()`：

- 无法读取身份 -> `identity_unverified`
- 身份不一致 -> `identity_mismatch`
- 只有确认目标身份后才允许点击签到

## 8. 页面所有权设计

禁止使用全量页面集合差推断所有权。

正确做法：

```python
with dashboard.context.expect_page(timeout=15000) as page_info:
    use_edge_link.click()
opened_page = page_info.value
```

对同页跳转：

- 记录当前页 URL
- 使用导航或 URL 条件等待
- 只有在当前页 URL 变为官方活动页后才认为成功

对点击未抛出但新页延迟出现的场景：

- 只在明确 `page` 事件中获得页面
- 超时后无法确认归属时，不进入 `owned_pages`
- 宁可残留，不关闭用户页面

`_close_edge_streak_pages()` 只接受 `owned_pages`，不接受任意页面列表。

## 9. 时间与轮询设计

Edge 30 分钟任务改为真实截止时间：

```python
deadline = time.monotonic() + target_minutes * 60
next_progress_check = time.monotonic() + 300

while time.monotonic() < deadline:
    perform_browse_step()
    if time.monotonic() >= next_progress_check:
        outcome = read_progress()
        next_progress_check += 300
```

所有等待使用 `max(0, min(wait_seconds, deadline - time.monotonic()))`。

测试使用 fake clock，而不是直接 no-op `wait_for_timeout()`。

## 10. WebView 协议设计

### 10.1 唯一结果文件

每次调用创建唯一路径：

```text
tmp/rewards_webview/<run_id>.json
```

输出中必须包含同一个 `run_id`。

### 10.2 状态机

```text
UIA_UNAVAILABLE
IDENTITY_UNVERIFIED
IDENTITY_MISMATCH
NO_TASK
TASK_FOUND
TASK_ITEM_MISSING
TASK_INVOKE_FAILED
TASK_PROGRESS_NOT_GROWN
TASK_GROWN
```

映射：

| 状态 | TaskOutcome |
|---|---|
| UIA_UNAVAILABLE | ENVIRONMENT_UNAVAILABLE |
| IDENTITY_UNVERIFIED | IDENTITY_UNVERIFIED |
| IDENTITY_MISMATCH | IDENTITY_MISMATCH |
| NO_TASK | SKIPPED |
| TASK_FOUND + 进度增长 | SUCCESS |
| TASK_ITEM_MISSING | FAILED |
| TASK_INVOKE_FAILED | FAILED |
| TASK_PROGRESS_NOT_GROWN | FAILED |

不能把 `TASK_ITEM_MISSING` 映射成 `SKIPPED`。

## 11. 通知与锁设计

### 11.1 运行生命周期

```text
获取锁
→ 执行账号流水线
→ 清理浏览器和客户端
→ 生成不可变 RunSnapshot
→ 写最终运行结束标记
→ 释放锁
→ 异步/独立发送通知
→ 进程退出
```

注意：`start.bat` 当前在 Python 退出后写 `run end`。如果通知必须在结束标记之后，需要把最终日志写入职责移到 Python，或由启动脚本通过单独 finalizer 完成。

### 11.2 通知内容

通知必须包含：

- `run_id`
- `pid`
- `started_at`
- `finished_at`
- `log_path`
- 账号成功/失败摘要
- 每个账号的任务结果摘要
- 本次日志快照

不得从“最新日志”猜测。

### 11.3 通知失败

通知失败不得：

- 改变退出码
- 阻塞账号流水线
- 阻塞锁释放超过明确上限

## 12. 退出码设计

建议集中到 `utils/outcomes.py` 或 `utils/exit_codes.py`：

```text
0  SUCCESS
1  INTERNAL_ERROR
2  STARTUP_FAILED
3  ALL_ACCOUNTS_FAILED
4  ACCOUNT_CONFIG_FAILED
5  RISK_CONTROL
6  PARTIAL_FAILURE
7  ALREADY_RUNNING
```

`ALREADY_RUNNING` 不应与成功共用 0；否则监控无法区分“完成”与“让位”。

`notify.py` 不应单独维护一份退出码文案，应由 `ExitCode` 枚举提供统一原因。

## 13. 迁移阶段

### 阶段 0：只补契约和测试，不改行为

产出：

- `TaskOutcome`、`AccountOutcome`、`RunResult` 数据模型
- 纯状态映射测试
- 当前行为基线

不接主流程，避免一次性大改。

### 阶段 1：修安全边界

优先级最高：

1. WebView 绑定目标账号。
2. Edge profile 绑定目标账号。
3. 页面所有权改为事件驱动。
4. WebView 唯一结果文件 + run_id。

这一阶段完成后，必须增加对应真实调用链测试。

### 阶段 2：修结果判定

1. 每日任务、活动、搜索纳入必需任务结果。
2. 多账号部分失败返回 `PARTIAL_FAILURE`。
3. Android 三态化。
4. Edge 进度合法性校验。
5. WebView 状态机化。

### 阶段 3：拆主流程

1. 提取 `utils/account_pipeline.py`。
2. 提取 `utils/edge_streak.py`。
3. 提取 `utils/rewards_webview.py`。
4. 把 `main.py` 缩减为编排入口。

### 阶段 4：通知与退出码解耦

1. 引入 `RunSnapshot`。
2. 引入统一 `ExitCode`。
3. 通知在锁释放后独立执行。
4. 增加通知生命周期测试。

## 14. 验收标准

### 14.1 安全

- [ ] 目标账号与 Edge profile 不一致时，不打开活动页、不等待 30 分钟。
- [ ] 目标账号与 WebView 不一致时，不点击任务。
- [ ] 无法读取任何客户端身份时，不操作该客户端。
- [ ] 用户并发打开的标签页永远不会被脚本关闭。
- [ ] 通知正文不包含密码、cookie、token、SMTP 密钥。

### 14.2 正确性

- [ ] 每日任务有剩余时，账号不能返回成功。
- [ ] 活动未完成时，账号不能返回成功。
- [ ] 搜索计划执行但积分未增长时，有明确失败或跳过策略。
- [ ] Edge `0/0`、`1/0` 不会被判完成。
- [ ] WebView `TASK_ITEM_MISSING`、`TASK_INVOKE_FAILED` 不会被判无任务。
- [ ] 多账号部分失败会返回非零状态并通知。

### 14.3 生命周期

- [ ] 失败通知只发送一次。
- [ ] 通知使用本次 `run_id`、PID、日志路径。
- [ ] 通知异常不改变业务退出码。
- [ ] SMTP 超时不阻塞下一次运行的锁获取。

### 14.4 测试

- [ ] 每个 `TaskStatus` 至少有一个状态转换测试。
- [ ] WebView 协议覆盖所有状态，不依赖真实点击。
- [ ] 页面所有权测试覆盖“用户并发打开无关标签页”。
- [ ] Edge 30 分钟使用 fake clock 验证真实截止时间。
- [ ] 多账号部分失败有退出码和通知测试。

## 15. 明确暂不做的事情

在阶段 1 之前，不建议继续做以下事情：

- 继续增加 Edge 选择器兜底。
- 继续扩大 UIA 文本解析。
- 继续堆叠单测 mock。
- 引入新的自动化框架。
- 修改正式 Edge profile 或浏览器配置。
- 自动完成 Microsoft 账号登录或验证码。

## 16. 推荐第一步

建议先实现阶段 0 和阶段 1，不直接重写整个项目：

1. 新增 `utils/outcomes.py`，只定义数据模型和状态聚合。
2. 新增 `utils/rewards_webview.py`，把 WebView 结果转为结构化状态。
3. 新增 `utils/edge_streak.py`，先只迁移身份校验和页面所有权。
4. 给 `run_account()` 接入 `TaskOutcome`，保留现有日志格式。
5. 每迁移一个任务，删除对应旧分支，不在两处同时判断。
