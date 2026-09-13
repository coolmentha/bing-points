"""全局配置常量"""
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

USER_DATA_DIR = _PROJECT_ROOT / "tmp_playwright_profile"
ACCOUNTS_PATH = _PROJECT_ROOT / "accounts.txt"

REQUEST_TIMEOUT = 8
TRENDS_CACHE_FAILURE_TTL = 600

DEFAULT_PC_SEARCHES = int(os.getenv("BING_REWARDS_PC_SEARCHES", "37"))
DEFAULT_MOBILE_SEARCHES = int(os.getenv("BING_REWARDS_MOBILE_SEARCHES", "0"))

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
