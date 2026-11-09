"""全局配置常量"""
import os
import re
from pathlib import Path
from typing import Mapping

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 供其他模块定位项目内文件（.env、logs/ 等）
PROJECT_ROOT = _PROJECT_ROOT
LOG_DIR = _PROJECT_ROOT / "logs"
ENV_PATH = _PROJECT_ROOT / ".env"


_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}|%([^%]+)%")


def resolve_env_path(value: str, env: Mapping[str, str] | None = None) -> Path:
    """展开路径中的环境变量并返回绝对路径。

    使用自定义展开而非 ``os.path.expandvars``，因为后者在 Windows 上不识别
    ``%VAR%`` 语法。未知变量和相对路径必须 fail-fast，避免静默指向错误目录。
    """
    source = os.environ if env is None else env
    raw = str(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return source.get(name, match.group(0))

    expanded = _ENV_VAR_PATTERN.sub(replace, raw)
    unresolved = _ENV_VAR_PATTERN.search(expanded)
    if unresolved:
        raise ValueError(
            f"路径包含未展开的环境变量 {unresolved.group(0)}: {raw!r}"
        )
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        raise ValueError(f"路径必须是绝对路径: {raw!r}")
    return path


def resolve_edge_user_data_dir(
    value: str | None,
    default: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    """解析 Edge profile 路径；未配置时使用项目内专用 profile。"""
    return resolve_env_path(str(default) if value is None else value, env)


def load_env(env_path: str | Path | None = None) -> None:
    """加载 .env 并验证 Edge profile 路径；系统环境变量始终优先。"""
    path = Path(env_path) if env_path is not None else ENV_PATH
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        try:
            load_dotenv(path, override=False, interpolate=False)
        except TypeError:
            # 旧版 python-dotenv 没有 interpolate 参数；下方仍会验证结果。
            load_dotenv(path, override=False)

    value = os.environ.get("BING_REWARDS_EDGE_USER_DATA_DIR")
    if value is not None:
        resolve_env_path(value)


load_env()

# 浏览器引擎：默认固定 Edge，不再自动回退到 Chromium/Chrome。
BROWSER_ENGINE = os.getenv("BING_REWARDS_BROWSER", "edge").strip().lower()
USER_DATA_DIR = _PROJECT_ROOT / "tmp_playwright_profile"
# 日常 Edge 的正式 User Data：仅作占用提示/身份判断基准，默认不再直接复用。
DEFAULT_EDGE_USER_DATA_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(_PROJECT_ROOT)))
    / "Microsoft"
    / "Edge"
    / "User Data"
)
# 无人值守专用 Edge profile：与用户正在使用的 Edge 隔离，首次手动登录后长期复用。
DEDICATED_EDGE_USER_DATA_DIR = _PROJECT_ROOT / "tmp_playwright_profile_edge"
EDGE_USER_DATA_DIR = resolve_edge_user_data_dir(
    os.getenv("BING_REWARDS_EDGE_USER_DATA_DIR"),
    DEDICATED_EDGE_USER_DATA_DIR,
)
# 移动端搜索用独立 profile（Android UA 需要单独登录一次）
MOBILE_PROFILE_DIR = _PROJECT_ROOT / "tmp_playwright_profile_mobile"
# Playwright/Chromium 临时目录：固定在项目内，避免系统 %TEMP% 不可写时
# 驱动抛 "EPERM: operation not permitted, mkdtemp '...\Temp\playwright-artifacts-XXXXXX'"
TEMP_DIR = _PROJECT_ROOT / "tmp_playwright_temp"
ACCOUNTS_PATH = _PROJECT_ROOT / "accounts.txt"

REQUEST_TIMEOUT = 8
TRENDS_CACHE_FAILURE_TTL = 600

# PC 搜索：每次 3 分，60 分/天封顶 ≈ 20 次（面板无今日计数，按计划次数执行 + 积分校验兜底）
DEFAULT_PC_SEARCHES = int(os.getenv("BING_REWARDS_PC_SEARCHES", "20"))

# ======================== 移动客户端（Android 模拟器） ========================
# 设为 1 后，扫描已有 com.microsoft.bing 客户端并执行每日签到。
ENABLE_MOBILE_CLIENT = os.getenv("BING_REWARDS_MOBILE_CLIENT", "1").strip() == "1"
ADB_PATH = os.getenv(
    "BING_REWARDS_ADB",
    r"C:\Android\sdk\platform-tools\adb.exe",
)
BING_ANDROID_PACKAGE = os.getenv("BING_REWARDS_BING_PACKAGE", "com.microsoft.bing")
BING_ANDROID_ACTIVITY = os.getenv(
    "BING_REWARDS_BING_ACTIVITY",
    "com.microsoft.sapphire.app.main.SapphireMainActivity",
)
# ======================== Rewards 桌面 WebView ========================
ENABLE_REWARDS_DESKTOP = os.getenv("BING_REWARDS_DESKTOP_APP", "1").strip() == "1"
REWARDS_DESKTOP_UIA_RESULT = _PROJECT_ROOT / "tmp_rewards_desktop_result.txt"
REWARDS_DESKTOP_AUMID = "Microsoft.24183918CCDA_8wekyb3d8bbwe!MicrosoftRewards"

# 移动端搜索：实测 CN 个人账号不计数（8次零入账），默认关闭；env 可开启试验
DEFAULT_MOBILE_SEARCHES = int(os.getenv("BING_REWARDS_MOBILE_SEARCHES", "0"))

# 移动端 UA（Edge Android），配合独立移动 profile 使用
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36 EdgA/124.0.0.0"
)

REWARDS_DASHBOARD = "https://rewards.bing.com/dashboard"
REWARDS_EARN_PAGE = "https://rewards.bing.com/earn"
BING_HOME = "https://www.bing.com/"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0 Safari/537.36"
}

# 热词默认关键词
FALLBACK_KEYWORDS = ["微软奖励", "必应搜索", "信息流热点", "今天新闻", "最新科技", "体育资讯", "娱乐八卦", "财经新闻", "人工智能", "量子计算"]

# 搜索框选择器
SEARCH_BOX_SELECTORS = [
    "#sb_form_q",
    "input[name='q']",
    "textarea[name='q']",
    "input[type='search']",
]
