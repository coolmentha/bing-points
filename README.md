# Bing Points 自动化

> 更新日期：2025-12-23 · 执行者：Codex

## 功能简介
- 自动完成 Microsoft Rewards：每日任务卡片、桌面/移动搜索积分。
- 支持多账号轮询登录；如已处于正确登录态将直接复用，否则自动注销后登录。
- 自动下载/定位 Edge WebDriver，默认优先本地 `msedgedriver.exe`。
- 搜索次数按 Rewards 面板剩余次数精确执行：PC/移动端无剩余会自动跳过对应流程。

## 使用步骤
1. 安装依赖（Python 3.10+）：`python -m pip install selenium requests selenium-stealth tenacity tqdm`
2. 在项目根目录创建 `accounts.txt`：
   ```
   user1@example.com:password1
   user2@example.com:password2;user3@example.com:password3
   # 可用逗号/分号/换行分隔，# 为注释
   ```
3. 可选：将 Edge 驱动放在项目根目录命名为 `msedgedriver.exe`，或设置环境变量 `EDGEWEBDRIVER` 指向驱动路径。
4. 运行脚本：`python main.py`（Linux/macOS 建议用 `python3 main.py`）
   - 无头模式：`python main.py headless`（也支持 `--headless` / `-h`）
   - 其他参数会被忽略并提示（避免误传参数导致无头）

## 稳定性机制（已内置）
- 导航等待：关键页面访问使用“文档 readyState 等待”，降低页面未加载完成就找元素导致的失败。
- Dashboard 等待：读取 `dashboard` 变量时会轮询等待其注入完成；失败会打印原因并回退空字典。
- 每日任务日期回退：若 Rewards 返回的日期键与本地格式不一致，会自动回退到最新可用日期键。
- 每日任务点击重试：打开每日任务卡片失败会自动重试（最多 3 次）。
- 搜索自愈：搜索中途异常时，会尝试回到 Bing 首页再继续下一次重试。

## 登录逻辑要点
- 登录前尝试从个人中心 `#feedback-root` 的配置 JSON 读取 `signInName` 确认当前账号；读取不到或与目标不符时，会访问注销链接并重新登录。
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
- 安装运行依赖：`python -m pip install selenium requests selenium-stealth tenacity tqdm`

## 其他说明
- 热词接口结果会做缓存：成功结果按天复用；若接口失败会在一段时间内降频重试，避免频繁请求。
- 若热词接口不可用，会使用内置默认关键词列表。
- 移动端仅在检测到“移动端剩余搜索次数 > 0”时才会启动移动端浏览器流程。
- 所有日志输出为中文，便于在 CLI 中查看。

## 常见问题
- 找不到驱动/版本不匹配：优先更新/替换根目录 `msedgedriver.exe` 以匹配本机 Edge 版本；或设置 `EDGEWEBDRIVER=/path/to/msedgedriver`。
- 自动下载驱动失败：网络受限时会失败，脚本会打印错误并尝试使用 PATH 或 Selenium Manager；建议手动放置驱动文件。
- 页面结构变更导致元素找不到：先用有界面模式运行观察卡在哪里，再根据日志定位到具体步骤（登录/奖励面板/搜索/每日任务）。
