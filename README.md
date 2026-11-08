# Bing Points 自动化

> 更新日期：2026-11-06 · 执行者：Codex

## 功能简介
- 自动完成 Microsoft Rewards：每日任务卡片、每日拼图、桌面/移动搜索积分。
- 自动推进网页可完成的限时 Punch Card（每次最多一步），并领取额外积分。
- 遇到验证码、人机验证或异常流量提示时立即停止当前账号后续动作。
- 支持多账号轮询登录；如已处于正确登录态将直接复用，否则自动注销后登录。
- 搜索次数改为配置驱动：默认桌面 20 次、移动 0 次（新版 Rewards 移动端积分需手机 App，浏览器刷搜索不计分），可用环境变量覆盖。
- 兼容新版入口：Rewards 面板与 Bing 搜索页都会按候选地址自动回退，减少因域名/入口切换导致的失败。

## 浏览器与 Edge 浏览任务
- 默认强制使用本机 Edge（`msedge`），缺少 Edge 时直接退出，不再回退 Chromium/Chrome。
- Edge 30 分钟连续打卡默认使用本机正式 Edge User Data：
  ```powershell
  $env:BING_REWARDS_EDGE_USER_DATA_DIR="$env:LOCALAPPDATA\Microsoft\Edge\User Data"
  ```
  脚本会正常启动可见 Edge，再通过仅绑定 `127.0.0.1` 的临时 CDP 端口接入；不会用自动化实例抢占正式 profile。可用环境变量覆盖 profile 路径。
- Bing 手机应用签到通过已连接 Android 模拟器中的 Bing App 完成（`com.microsoft.bing`）；默认自动检测。无模拟器或客户端禁用时 App 签到明确跳过，不影响其他已完成任务；有设备但身份不一致或未回读到 `checked` 时仍不伪造签到，单账号会明确报告未确认。
- Rewards 桌面 WebView 的“桌面奖励应用每周专属任务”会自动继续；每次运行最多推进一个任务，并回读 `N/8` 进度确认增长。当前没有已启用任务时正常跳过。
- WebView 的返回状态有明确区分：进度增长或无启用任务都不会导致账号失败；`WebView UIA 不可用，跳过（不影响其他任务）` 表示当前 Codex/Python 子进程或计划任务环境无法读取桌面 UIA，仅跳过 WebView，不会覆盖 Edge、搜索等已完成任务的账号成功；`Rewards 桌面 WebView 任务执行失败` 才表示实际任务定位或点击失败，账号仍报告失败。
- 正式 Edge 与 WebView UIA 需要正常桌面权限。请用 `start.bat` 或计划任务运行，不要从受限/低完整性终端直接启动。
- **Edge 打卡前置条件**：网页 Rewards 已登录**不等于** Edge 浏览器 profile 已登录。请先手动打开 Edge，进入 `设置 → 个人资料`，登录目标 Microsoft 账号并保持同步，再运行脚本。脚本只读检查 `Local State` 与 `Default/Preferences`，不会启动或修改 profile；未检测到登录账号时会快速停止，不空跑 30 分钟。
  - 日志出现 `Edge 浏览器 profile 未登录`：按上述步骤登录 Edge。
  - 日志出现 `Edge 浏览器已登录，但已等待 30 分钟 Rewards 仍为 ...，未回写打卡进度`：说明浏览器侧已有登录态，但 Rewards 尚未回写，脚本不会伪造成功。

## 使用步骤
1. 安装依赖（Python 3.10+）：`python -m pip install playwright requests tenacity tqdm`
2. 在项目根目录创建 `accounts.txt`：
   ```
   user1@example.com:password1
   user2@example.com:password2;user3@example.com:password3
   # 可用逗号/分号/换行分隔，# 为注释
   ```
3. 无需 Edge WebDriver：脚本固定使用本机 Edge。正式 profile 通过正常 Edge 启动 + CDP 接入；缺少 Edge 会直接退出。
4. 可选：如需修改搜索次数，可设置：
   - `BING_REWARDS_PC_SEARCHES=37`
   - `BING_REWARDS_MOBILE_SEARCHES=0`（设为 0 跳过移动端搜索）
5. 运行脚本：`start.bat --task`（推荐，自动使用正常权限与正式 Edge profile）；直接 `python main.py` 仅用于调试。
   - 无头模式：`python main.py headless`（也支持 `--headless` / `-h`）
   - 其他参数会被忽略并提示（避免误传参数导致无头）

## 稳定性机制（已内置）
- 导航等待：关键页面访问使用“文档 readyState 等待”，降低页面未加载完成就找元素导致的失败。
- 纯 DOM 模式：不再尝试读取 `dashboard` 等前端内部变量，统一按页面真实可点击任务入口处理。
- 每日任务日期回退：若 Rewards 返回的日期键与本地格式不一致，会自动回退到最新可用日期键。
- 每日任务点击重试：优先按当前页面真实可点击链接定位，旧 XPath 作为兜底；打开失败会自动重试（最多 3 次）。
- 任务触发更新：统一按 DOM 扫描每日任务、额外任务、可领取任务入口并逐个触发。
- 额外积分领取：不再依赖页面内部 JSON 结构，而是直接扫描面板中的可领取按钮/链接。
- 搜索自愈：搜索中途异常时，会尝试回到 Bing 首页再继续下一次重试。
- 搜索框适配：除经典 `#sb_form_q` 外，也兼容新版 `name=q` / `textarea[name=q]` 搜索框。
- profile 占用自愈：项目隔离 profile 被残留进程占用时会定向清理后重试；正式 Edge User Data（通过 `BING_REWARDS_EDGE_USER_DATA_DIR` 指定）被占用时只提示，绝不自动结束用户的 Edge。
- 单实例保护：同一时间只允许一个实例运行（默认回环端口 `47821` 互斥），后启动的实例会提示后退出，避免两个实例互抢 profile；确需并行请设 `BING_REWARDS_ALLOW_MULTI=1` 并使用不同 profile。
- 启动失败提示清晰：当前固定 Edge-only，缺少 Edge 直接退出，不再回退 Chromium；profile 冲突会单独提示。
- 临时目录固定到项目内：Playwright 驱动只认启动时的 `TEMP`，系统 `%TEMP%` 不可写会抛 `EPERM: operation not permitted, mkdtemp '...\Temp\playwright-artifacts-XXXXXX'`。程序在 `sync_playwright()` 之前把 `TEMP/TMP/TMPDIR` 指向 `tmp_playwright_temp/` 并实测可写，同时清理上次遗留的临时目录。

## 登录逻辑要点
- 正式 Edge profile 可能已经登录其他账号；脚本会从 Rewards 页面读取当前邮箱，与 `accounts.txt` 目标账号比对，不一致时先退出再登录，避免操作错账号。
- 登录后从微软注入的 `signInName` 校验当前邮箱；与目标账号不一致时先退出再登录，读取不到身份时不擅自切换账号。
- 适配新版登录页：
  - 账号输入框优先 `id=usernameEntry`，回退 `name=loginfmt`。
  - “下一步”按钮优先 `data-testid=primaryButton`，回退 `idSIButton9`。
  - 若出现“使用密码”卡片，会自动点击后再填密码。

## 测试
- 单元测试命令：`python -m pytest -q`
- 若未安装：`python -m pip install pytest`

## 开发
- 建议使用虚拟环境：`python -m venv .venv`
  - Linux/macOS：`source .venv/bin/activate`
  - PowerShell：`.venv\\Scripts\\Activate`
- 安装运行依赖：`python -m pip install playwright requests tenacity tqdm`

## 失败推送（邮件）
无人值守运行时没人盯着，失败容易被忽略。配置后可让**失败**自动发邮件提醒。

**只需三步**：
1. 复制 `.env.example` 为 `.env`（同目录）
2. 填 `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_TO`
3. 验证：`python -m utils.notify`（会尝试发一封测试邮件）

```ini
SMTP_HOST=smtp.qq.com     # 163: smtp.163.com / Outlook: smtp.office365.com
SMTP_PORT=465             # 465=SSL(默认)，587=STARTTLS
SMTP_USER=you@qq.com
SMTP_PASSWORD=授权码        # 注意：多数邮箱要"授权码/应用专用密码"，不是登录密码
SMTP_TO=you@qq.com        # 多个用逗号分隔
```

行为约定：
- **只在失败时推送**（退出码非 0），成功不打扰。退出码含义见下方。
- 邮件含**退出码 + 中文原因 + 日志尾部 + 排查建议**，不用再去翻日志。
- 未配置 `SMTP_HOST`/`SMTP_TO` 时功能自动关闭，不报错。
- **推送失败绝不影响运行结果**：SMTP 挂了只记一行日志，退出码保持原样。
- `.env` 已在 `.gitignore` 中，凭据不会被提交。

## 其他说明
- 当前机制的整体重构设计见 [docs/bingpoints-mechanism-refactor-design.md](docs/bingpoints-mechanism-refactor-design.md)；该文档为待评审方案，不代表现有代码已实施。
- 热词接口结果会做缓存：成功结果按天复用；若接口失败会在一段时间内降频重试，避免频繁请求。
- 若热词接口不可用，会使用内置默认关键词列表。
- 移动端搜索仅在 `BING_REWARDS_MOBILE_SEARCHES > 0` 时执行。
- 所有日志输出为中文，便于在 CLI 中查看。
- 计划任务/无人值守运行时的输出会落盘到 `logs\run_<时间戳>.log`（`logs/` 已加入 `.gitignore`），不再因为看不到窗口而无法排查。

## 常见问题
- **定时任务“跑了但没效果”**：先看 `logs\run_<时间戳>.log`（任务没有可见窗口，所有输出都落盘在这里）。
  - **关键坑：任务要用 `Interactive` 登录类型，不要用 S4U。** S4U（"不管用户是否登录都要运行"）会把任务放进**会话 0（services，无窗口站）**，Edge 在那里经常连消息窗口都建不出来，于是启动瞬间 FATAL 退出：
    ```
    Failed to create a message-only window: 拒绝访问。 (0x5)
    FATAL: Check failed: Failed to create message-only Window: 拒绝访问。 (0x5)
    ```
    表现为任务"秒退、什么都没做"，而**双击 `start.bat` 却完全正常**（交互式会话有窗口站）。正确做法是让任务在已登录的交互式会话中运行：
    ```powershell
    $t = Get-ScheduledTask -TaskName "BingPoints Daily"
    $p = $t.Principal; $p.LogonType = 'Interactive'
    Set-ScheduledTask -TaskName "BingPoints Daily" -Principal $p
    ```
    用 `schtasks /query /tn "\BingPoints Daily" /v /fo list` 确认 `Logon Mode` 是 `Interactive only`（而不是 `Interactive/Background`）。
  - **注意：会话 0 的失败是"间歇性"的，别被单次成功骗了。** 实测同一份 S4U 配置有批次连续成功、也有批次连续失败；而 `Interactive` 稳定成功。所以**不要**因为"试了一次好像能跑"就继续用 S4U——它会在某天早上静默失败。判断依据看日志里的会话号：
    - `会话号=0(服务会话(无窗口站))` → 跑在会话 0，随时可能崩 `0x5`
    - `会话号=2(用户桌面会话)` → 正常运行
  - **不需要人工登录**：本机已开启自动登录（`AutoAdminLogon=1`），开机后 Windows 会自动进入 `b1184` 的桌面会话，任务随之在交互式会话里启动——所以 `Interactive` + 开机触发 = 开机自动跑，中途无需你输密码。
  - 另注：`headless` 在会话 0 里**能**启动浏览器（不需要窗口站），但登录页/搜索流程在会话 0 仍可能不稳定；`Interactive` 是唯一实测稳定的路线。
  - 注册动作带上 `--task`（即 `start.bat --task`）表示无人值守；这样即使运行在交互式会话里，失败时也不会等按键。`start.bat` 的等待是**有上限的**：只有确认是真人双击（有控制台且没传 `--task`／`BING_REWARDS_NONINTERACTIVE`）才等待，且最多 60 秒自动继续，绝不会永久挂住——因为任务策略是 `IgnoreNew`，一次挂住会连带吃掉之后所有天的运行机会。
  - `main.py` 的退出码语义：`0` 成功（含“已有另一实例在运行”的正常让位）、`2` 启动失败（profile/Temp/浏览器）、`3` 全部账号失败、`4` 读取账号失败、`5` 风控检测失败。
  - 任务卡在“正在运行”时，先 `schtasks /end /tn "\BingPoints Daily"` 结束它，再查看当次日志定位原因。
- **双击时弹出“打开文件 - 安全警告”（无法验证发布者）**：这是 Windows 的 Mark-of-the-Web（下载标记 `Zone.Identifier`）在起作用——文件是从网上/聊天工具/压缩包传进来的，系统不信任它。**双击的人可以点“运行”，但定时任务和开机自启没有窗口、没人能点，于是一直卡在弹窗上，表现就是“启动了但完全没反应”**。
  - 根治（两条同时做才彻底）：
    1. **给文件解除阻止**：`Get-ChildItem <项目> -Recurse -File -Include *.bat,*.vbs,*.py | Unblock-File`（也可以在资源管理器里右键文件 → 属性 → 勾选“解除锁定”）。用 `Get-Item start.bat -Stream *` 或 `dir /r start.bat` 可以看到 `Zone.Identifier` 数据流，解除后消失。
    2. **不要用 ShellExecute 直接打开 `.bat`**：双击 `.bat`、任务计划里 `Command` 直接写 `start.bat`、VBS 里 `shell.Run "start.bat"`，都会走安全警告。改为经 `cmd.exe` 包装即可（`cmd.exe` 是受信任的签名程序，不会弹窗）：
       - 计划任务：`Command = C:\Windows\System32\cmd.exe`，`Arguments = /c "D:\PycharmProjects\bing-points\start.bat" --task`
       - 自启 VBS：`shell.Run "cmd.exe /c """"<项目>\start.bat"""" --task", 0, False`（`install_autostart.bat` 已按此生成）
  - 排查用：确认当前有没有弹窗挡着，以及是谁弹的：
    ```powershell
    Get-Process | Where-Object { $_.MainWindowTitle -match '安全警告' } | Select-Object Id,ProcessName,MainWindowTitle
    ```
  - 注意：任何一次重新下载/解压/覆盖脚本都可能重新带上下载标记，改完脚本后留意是否又出现弹窗。
- 报错 `EPERM: operation not permitted, mkdtemp '...\Temp\playwright-artifacts-XXXXXX'`：Playwright 驱动在系统 `%TEMP%` 下建不了临时目录（权限受限或被安全软件拦截）。当前版本已自动把临时目录改到项目内 `tmp_playwright_temp/`；若仍报错，说明该目录同样不可写，请检查其权限，或把项目移到当前用户可写的盘符。
- 报错 `Failed to create a ProcessSingleton for your profile directory`：说明已有浏览器实例占用 `tmp_playwright_profile`，最常见的原因是**计划任务/开机自启的实例正在后台运行**（例如计划任务 `\BingPoints Daily` 以会话 0 运行 `start.bat`）。先排查占用：
  - `python -m utils.profile --check` 查看占用状态与持有进程
  - `python -m utils.profile --kill` 结束占用该 profile 的浏览器进程（只结束浏览器，且不会误杀自身）
  - 不需要后台运行时执行 `remove_autostart.bat`，或删除计划任务：`schtasks /delete /tn "BingPoints Daily" /f`
  - 设置 `BING_REWARDS_NO_PROFILE_KILL=1` 可关闭启动时的自动清理（只检查不清理）
- 页面结构变更导致元素找不到：先用有界面模式运行观察卡在哪里，再根据日志定位到具体步骤（登录/奖励面板/搜索/每日任务）。

## 本次排查结论（2026-11-06）
- 触发 `exit=3` 的直接原因不是验证码：日志显示 `风控=None`，流程完整跑完；失败点是 `Microsoft Edge 分钟: 0/30`，脚本按“未回读到 30/30 不宣布成功”的规则退出。
- 新版 Rewards 的 Edge 打卡入口不是普通链接：需先点击仪表板 `Microsoft Edge / 分钟` 卡片，再点击弹窗中的“使用 Edge”；脚本已按此路径打开官方活动页。
- 实测该活动页的 `reportActivity` 请求返回 200，但 Edge 分钟在 3–10 分钟观察窗口内仍保持 `0/30`。因此目前只能确认“入口已触发”，不能确认微软已记录分钟；脚本不会伪造完成。
- Android Bing App 签到已在模拟器中实测成功；模拟器断开时会明确跳过，不会把其他账号的 `checked` 误算给当前账号。
