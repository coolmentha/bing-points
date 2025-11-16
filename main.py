import os
import re
import sys
import time
import random
import zipfile
import plistlib
import tempfile
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException
from selenium_stealth import stealth
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm


DEFAULT_DRIVER_PATH = Path(__file__).with_name("msedgedriver.exe")
DEFAULT_KEYWORDS = ["微软奖励", "必应搜索", "信息流热点", "今天新闻", "最新科技", "体育资讯"]
REQUEST_TIMEOUT = 8
EDGE_RELEASE_URLS = [
    "https://msedgewebdriverstorage.blob.core.windows.net/edgewebdriver/LATEST_RELEASE",
    "https://msedgedriver.azureedge.net/LATEST_RELEASE",
]
EDGE_DOWNLOAD_BASES = [
    "https://msedgedriver.microsoft.com",
    "https://msedgedriver.azureedge.net",
]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _resolve_driver_path() -> str | None:
    driver_env = os.getenv("EDGEWEBDRIVER")
    if driver_env:
        resolved_path = Path(driver_env).expanduser()
        if resolved_path.exists():
            return str(resolved_path)
        raise FileNotFoundError(f"EDGEWEBDRIVER 指向的驱动不存在：{resolved_path}")

    resolved_path = DEFAULT_DRIVER_PATH
    if resolved_path.exists():
        return str(resolved_path)

    try:
        _download_driver(resolved_path)
    except Exception as exc:
        print(f"自动下载 Edge 驱动失败：{exc}")

    if resolved_path.exists():
        return str(resolved_path)

    existing = _find_driver_on_path()
    if existing:
        print(f"发现 PATH 中的驱动：{existing}")
        return existing

    print("未找到可用驱动，将交给 Selenium Manager 自动管理")
    return None


def _detect_driver_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system.startswith("win"):
        return "win64" if "64" in machine else "win32"
    if system == "darwin":
        return "mac64"
    return "linux64"


def _download_driver(target_path: Path):
    platform_token = _detect_driver_platform()
    latest_version = _resolve_release_version()
    download_url = None
    last_exc = None
    for base in EDGE_DOWNLOAD_BASES:
        candidate = f"{base}/{latest_version}/edgedriver_{platform_token}.zip"
        try:
            _download_zip(candidate, target_path)
            download_url = candidate
            break
        except Exception as exc:
            last_exc = exc
            continue
    if download_url is None:
        raise last_exc or RuntimeError("下载 EdgeDriver 失败")


def _download_zip(url: str, target_path: Path):
    tmp_file = None
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT * 3, stream=True, headers=HTTP_HEADERS)
        resp.raise_for_status()
        fd, tmp_file = tempfile.mkstemp(suffix=".zip")
        with os.fdopen(fd, "wb") as tmp_out:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    tmp_out.write(chunk)
        with zipfile.ZipFile(tmp_file) as archive:
            names = archive.namelist()
            driver_name = "msedgedriver.exe" if platform_token.startswith("win") else "msedgedriver"
            member = next((n for n in names if n.endswith(driver_name)), None)
            if not member:
                raise RuntimeError("压缩包中缺少驱动")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.chmod(target_path, 0o755)
        print(f"已自动下载 Edge 驱动到 {target_path}")
    finally:
        if tmp_file and os.path.exists(tmp_file):
            os.remove(tmp_file)


def _resolve_release_version() -> str:
    candidates = []
    local_version = _get_edge_version()
    if local_version:
        major = local_version.split(".")[0]
        for base in EDGE_RELEASE_URLS:
            candidates.append(f"{base}_{major}")
    candidates.extend(EDGE_RELEASE_URLS)

    last_exc = None
    for url in candidates:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
            resp.raise_for_status()
            version = resp.text.strip()
            if version:
                return version
        except Exception as exc:
            last_exc = exc
    raise RuntimeError("无法获取 EdgeDriver 版本") from last_exc


def _find_driver_on_path() -> str | None:
    driver_name = "msedgedriver.exe" if platform.system().lower().startswith("win") else "msedgedriver"
    found = shutil.which(driver_name)
    return found


def _get_edge_version() -> str | None:
    system = platform.system().lower()
    if system.startswith("win"):
        try:
            import winreg
        except ImportError:
            return None
        reg_paths = [
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\\Microsoft\\Edge\\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Microsoft\\Edge\\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\WOW6432Node\\Microsoft\\Edge\\BLBeacon"),
        ]
        for hive, path in reg_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                    if version:
                        return version
            except OSError:
                continue
        return None

    if system == "darwin":
        plist_paths = [
            Path("/Applications/Microsoft Edge.app/Contents/Info.plist"),
            Path.home() / "Applications/Microsoft Edge.app/Contents/Info.plist",
        ]
        for plist_path in plist_paths:
            if plist_path.exists():
                try:
                    with open(plist_path, "rb") as fp:
                        data = plistlib.load(fp)
                        version = data.get("CFBundleShortVersionString")
                        if version:
                            return version
                except Exception:
                    continue
        return None

    candidates = ["microsoft-edge", "microsoft-edge-stable", "msedge"]
    for cmd in candidates:
        try:
            result = subprocess.run([cmd, "--version"], capture_output=True, text=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
        output = (result.stdout or result.stderr).strip()
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
    return None


def _apply_stealth(driver):
    try:
        stealth(
            driver,
            languages=["zh-CN", "zh"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL",
            fix_hairline=True,
        )
    except ValueError as exc:
        print(f"Stealth 注入失败（{exc}），继续使用默认配置")


def init_browser(s):
    options = EdgeOptions()
    if s:
        options.add_argument("--headless")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.use_chromium = True
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    driver_path = _resolve_driver_path()
    if driver_path:
        service = EdgeService(executable_path=driver_path)
        driver = webdriver.Edge(service=service, options=options)
    else:
        driver = webdriver.Edge(options=options)
    _apply_stealth(driver)
    return driver


def init_mobile_edge_appium(s):
    mobile_emulation = {"deviceName": "iPhone X"}
    options = EdgeOptions()
    options.add_experimental_option("mobileEmulation", mobile_emulation)
    if s:
        options.add_argument("--headless")
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.use_chromium = True
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    driver_path = _resolve_driver_path()
    if driver_path:
        service = EdgeService(executable_path=driver_path)
        driver = webdriver.Edge(service=service, options=options)
    else:
        driver = webdriver.Edge(options=options)
    _apply_stealth(driver)
    return driver


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
def gohome(driver):
    try:
        driver.get("https://rewards.bing.com/?ref=rewardspanel")
    except Exception as e:
        print(f"跳转奖励面板失败：{e}")
        raise


def _random_sleep(short_range=(1, 3), long_prob=0.0, long_range=(6, 12)):
    if long_prob and random.random() < long_prob:
        time.sleep(random.uniform(*long_range))
        return
    time.sleep(random.uniform(*short_range))


def _type_keyword(element, keyword):
    if random.random() < 0.5:
        element.send_keys(keyword)
    else:
        for ch in keyword:
            element.send_keys(ch)
            time.sleep(random.uniform(0.1, 0.35))
            if random.random() < 0.08:
                typo = random.choice("abcdefghijklmnopqrstuvwxyz")
                element.send_keys(typo)
                time.sleep(random.uniform(0.1, 0.2))
                element.send_keys(Keys.BACKSPACE)
                time.sleep(random.uniform(0.05, 0.2))
        if random.random() < 0.3:
            time.sleep(random.uniform(0.5, 1.2))


def _maybe_select_suggestion(element):
    if random.random() > 0.35:
        return
    times = random.randint(1, 3)
    for _ in range(times):
        try:
            element.send_keys(Keys.ARROW_DOWN)
        except StaleElementReferenceException:
            return
        time.sleep(random.uniform(0.2, 0.5))
    if random.random() < 0.8:
        try:
            element.send_keys(Keys.ENTER)
        except StaleElementReferenceException:
            return


def _random_click_result(driver):
    try:
        if random.random() > 0.4:
            return
        results = WebDriverWait(driver, 5).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#b_results > li.b_algo h2 a"))
        )
        target = random.choice(results[: min(len(results), 5)])
        target.click()
        _random_sleep(short_range=(2, 4), long_prob=0.5, long_range=(5, 9))
        driver.back()
        _random_sleep(short_range=(1, 2))
    except StaleElementReferenceException:
        pass
    except Exception:
        pass


def _random_scroll_results(driver):
    try:
        if random.random() > 0.8:
            return
        scroll_distance = random.randint(300, 1200)
        driver.execute_script("window.scrollBy(0, arguments[0]);", scroll_distance)
        _random_sleep(short_range=(1, 2), long_prob=0.4, long_range=(4, 8))
        if random.random() < 0.5:
            driver.execute_script("window.scrollBy(0, arguments[0]);", -scroll_distance // 2)
    except Exception:
        pass


def _jiggle_mouse(driver):
    try:
        if random.random() > 0.6:
            return
        actions = ActionChains(driver)
        actions.move_by_offset(random.randint(-30, 30), random.randint(-30, 30)).pause(0.2)
        actions.move_by_offset(random.randint(-50, 50), random.randint(-50, 50)).perform()
    except Exception:
        pass


def _maybe_take_break(tag: str):
    if random.random() > 0.2:
        return
    duration = random.uniform(5, 15)
    print(f"{tag} 随机休息 {duration:.1f} 秒")
    time.sleep(duration)


def bing_search(driver, keyword):
    for attempt in range(3):
        try:
            element = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="sb_form_q"]'))
            )
            element.clear()
            _random_sleep(short_range=(2, 4), long_prob=0.4, long_range=(5, 10))
            _type_keyword(element, keyword)
            _maybe_select_suggestion(element)
            _random_sleep(short_range=(0.8, 2))
            if random.random() < 0.5:
                element.submit()
            else:
                element.send_keys(Keys.ENTER)
            _random_sleep(short_range=(3, 5), long_prob=0.3, long_range=(6, 12))
            _random_scroll_results(driver)
            _random_click_result(driver)
            _jiggle_mouse(driver)
            return
        except StaleElementReferenceException:
            print(f"搜索 {keyword} 时元素失效，重试 {attempt + 1}")
            _random_sleep(short_range=(1, 2))
        except Exception as e:
            print(f"搜索 {keyword} 失败：{e}")
            return


def daily_set(driver):
    gohome(driver)
    dashboard = getDashboardData(driver)
    data = dashboard.get("dailySetPromotions", {})
    todayDate = datetime.now().strftime("%m/%d/%Y")
    for i in data.get(todayDate, []):
        if i.get("attributes", {}).get("state") == "Complete":
            continue
        cardId = int(i.get("offerId", "0")[-1:])
        openDailySetActivity(driver, cardId)
        time.sleep(random.randint(3, 5))
    gohome(driver)
    print("完成每日任务")


def getDashboardData(driver) -> dict:
    try:
        data = driver.execute_script("return dashboard")
    except Exception as e:
        print(f"读取 dashboard 失败：{e}")
        return {}
    return data or {}


def openDailySetActivity(driver, cardId: int):
    try:
        element = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    f'//*[@id="daily-sets"]/mee-card-group[1]/div/mee-card[{cardId}]/div/card-content/mee-rewards-daily-set-item-content/div/a',
                )
            )
        )
        element.click()
        switchToNewTab(driver, 8)
        closeCurrentTab(driver)
    except Exception as e:
        print(f"打开每日任务 {cardId} 失败：{e}")


def switchToNewTab(driver, timeToWait: int = 0):
    handles = driver.window_handles
    if len(handles) < 2:
        return
    time.sleep(0.5)
    driver.switch_to.window(window_name=handles[1])
    if timeToWait > 0:
        time.sleep(timeToWait)


def closeCurrentTab(driver):
    handles = driver.window_handles
    if not handles:
        return
    driver.close()
    time.sleep(0.5)
    if driver.window_handles:
        driver.switch_to.window(window_name=driver.window_handles[0])
    time.sleep(0.5)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=4))
def goSearch(driver):
    try:
        driver.get("https://cn.bing.com/")
    except Exception as e:
        print(f"打开 Bing 失败：{e}")
        raise


def getBaiduTrends() -> list:
    words = []
    try:
        r = requests.get("https://v2.xxapi.cn/api/baiduhot", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"获取百度热榜失败：{e}")
        return words
    data = r.json().get("data", [])
    for trend in data:
        title = trend.get("title")
        if title:
            words.append(title)
    return words


def getZhihuTrends():
    words = []
    try:
        r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=zhihu", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"获取知乎热榜失败：{e}")
        return words
    data = r.json().get("data", [])
    for trend in data:
        title = trend.get("title")
        if title:
            words.append(title)
    return words


def getDouYinTrends():
    words = []
    try:
        r = requests.get("https://api.cenguigui.cn/api/juhe/hotlist.php?type=weibo", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"获取抖音/微博热榜失败：{e}")
        return words
    data = r.json().get("data", [])
    for trend in data:
        title = trend.get("title")
        if title:
            words.append(title)
    return words


def getRemainingSearches(driver):
    dashboard = getDashboardData(driver)
    if not dashboard:
        return 0, 0
    searchPoints = 1
    user_status = dashboard.get("userStatus", {})
    counters = user_status.get("counters", {})
    pcSearch = counters.get("pcSearch") or []

    if not pcSearch:
        return 0, 0

    progressDesktop = sum(item.get("pointProgress", 0) for item in pcSearch)
    targetDesktop = sum(item.get("pointProgressMax", 0) for item in pcSearch)

    if targetDesktop in [33, 102]:
        searchPoints = 3
    elif targetDesktop == 55 or targetDesktop >= 170:
        searchPoints = 5
    remainingDesktop = max(0, int((targetDesktop - progressDesktop) / searchPoints))

    remainingMobile = 0
    level_info = user_status.get("levelInfo", {})
    mobileSearch = counters.get("mobileSearch") or []
    if level_info.get("activeLevel") != "Level1" and mobileSearch:
        progressMobile = mobileSearch[0].get("pointProgress", 0)
        targetMobile = mobileSearch[0].get("pointProgressMax", 0)
        remainingMobile = max(0, int((targetMobile - progressMobile) / searchPoints))
    return remainingDesktop, remainingMobile


def _ensure_keywords(*keyword_lists):
    for words in keyword_lists:
        if words:
            return words
    print("热词接口不可用，使用默认关键词")
    return DEFAULT_KEYWORDS.copy()


def main():
    print("启动！")
    argv = sys.argv
    s = None
    if len(argv) == 2:
        s = argv[1]
    remainingSearches = 0
    remainingSearchesM = 0
    edge_driver = init_browser(s)
    try:
        time.sleep(random.randint(2, 4))
        daily_set(edge_driver)
        remainingSearches, remainingSearchesM = getRemainingSearches(edge_driver)
        goSearch(edge_driver)
        keyword_list = _ensure_keywords(
            getDouYinTrends(),
            getBaiduTrends(),
            getZhihuTrends(),
        )
        desk_time = 0
        if remainingSearches:
            desk_time = remainingSearches / 3 + 10
        for _ in tqdm(range(int(desk_time)), desc="bing searches", unit="search"):
            keyword = random.choice(keyword_list)
            if len(keyword_list) > 1:
                keyword_list.remove(keyword)
            bing_search(edge_driver, keyword)
            _maybe_take_break("PC")
    finally:
        edge_driver.quit()

    edge_driver = init_mobile_edge_appium(s)
    try:
        goSearch(edge_driver)
        keyword_list = _ensure_keywords(
            getBaiduTrends(),
            getZhihuTrends(),
            getDouYinTrends(),
        )
        mobile_time = 0
        if remainingSearchesM:
            mobile_time = remainingSearchesM / 3 + 5
        for _ in tqdm(range(int(mobile_time)), desc="bing searches", unit="search"):
            keyword = random.choice(keyword_list)
            if len(keyword_list) > 1:
                keyword_list.remove(keyword)
            bing_search(edge_driver, keyword)
            time.sleep(random.randint(2, 4))
            _maybe_take_break("Mobile")
    finally:
        edge_driver.quit()


if __name__ == "__main__":
    main()
