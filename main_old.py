import os
import re
import sys
import time
import random
import math
from urllib.parse import quote_plus
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
from selenium.common.exceptions import StaleElementReferenceException, InvalidElementStateException
from selenium_stealth import stealth
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm


DEFAULT_DRIVER_PATH = Path(__file__).with_name("msedgedriver.exe")
DEFAULT_KEYWORDS = ["微软奖励", "必应搜索", "信息流热点", "今天新闻", "最新科技", "体育资讯"]
REQUEST_TIMEOUT = 8
TRENDS_CACHE_FAILURE_TTL = 600
NAVIGATION_TIMEOUT = 20
ACCOUNTS_PATH = Path(__file__).with_name("accounts.txt")
DEFAULT_PC_SEARCHES = int(os.getenv("BING_REWARDS_PC_SEARCHES", "30"))
DEFAULT_MOBILE_SEARCHES = int(os.getenv("BING_REWARDS_MOBILE_SEARCHES", "20"))
REWARDS_HOME_URLS = [
    url
    for url in (
        os.getenv("REWARDS_HOME_URL"),
        "https://rewards.bing.com/dashboard",
        "https://rewards.bing.com/?ref=rewardspanel",
        "https://rewards.bing.com/",
    )
    if url
]
CLAIM_PANEL_TRIGGER_XPATH = '//*[@id="react-aria6979061832-_r_g6_"]/div/div'
CLAIM_PANEL_BUTTON_XPATH = '//*[@id="react-aria6979061832-_r_ju_"]'
DAILY_SECTION_SELECTOR = "#dailyset"
CLAIM_CARD_XPATHS = [
    CLAIM_PANEL_TRIGGER_XPATH,
    "//*[@id='shell']//main//button[.//p[normalize-space()='可领取']]",
    "//*[@id='shell']//main//button[contains(normalize-space(.), '可领取') and contains(normalize-space(.), '领取')]",
]
CLAIM_SUBMIT_XPATHS = [
    CLAIM_PANEL_BUTTON_XPATH,
    "//*[@role='dialog']//button[normalize-space()='领取积分']",
    "//*[@role='dialog']//button[contains(normalize-space(.), '领取积分')]",
]
BING_HOME_URLS = [
    url
    for url in (
        os.getenv("BING_SEARCH_URL"),
        "https://www.bing.com/",
        "https://cn.bing.com/",
    )
    if url
]
SEARCH_BOX_LOCATORS = [
    (By.ID, "sb_form_q"),
    (By.NAME, "q"),
    (By.CSS_SELECTOR, "textarea[name='q']"),
    (By.CSS_SELECTOR, "input[type='search']"),
]
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

_TRENDS_CACHE: dict[str, dict] = {}


def _parse_accounts(raw: str) -> list[tuple[str, str]]:
    """解析账号串，支持逗号、分号或换行分隔的 user:password 形式，忽略空行与注释。"""
    accounts: list[tuple[str, str]] = []
    if not raw:
        return accounts
    for part in re.split(r"[;,\n]+", raw):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, pwd = part.split(":", 1)
        user = user.strip()
        if not user or not pwd:
            continue
        accounts.append((user, pwd))
    return accounts


def _load_accounts(path: Path | None = None) -> list[tuple[str, str]]:
    """从配置文件读取账号列表，文件中每行 user:password，支持逗号/分号/换行分隔。"""
    cfg = path or ACCOUNTS_PATH
    if not cfg.exists():
        raise RuntimeError(f"未找到账号配置文件：{cfg}，请创建并写入 user:password，每行或以逗号/分号分隔")
    raw = cfg.read_text(encoding="utf-8")
    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    accounts = _parse_accounts(raw)
    if not accounts:
        raise RuntimeError(f"账号配置为空，请在 {cfg} 中填写 user:password")
    return accounts


def _extract_major_version(version: str | None) -> str | None:
    if not version:
        return None
    match = re.search(r"(\d+)", version)
    return match.group(1) if match else None


def _get_driver_version(driver_path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(driver_path), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=REQUEST_TIMEOUT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", output)
    if match:
        return match.group(1)
    return None


def _is_driver_compatible(driver_path: Path, edge_major: str | None) -> bool:
    if not edge_major:
        return True
    driver_version = _get_driver_version(driver_path)
    driver_major = _extract_major_version(driver_version)
    if not driver_major:
        print(f"无法读取驱动版本，继续尝试使用：{driver_path}")
        return True
    if driver_major != edge_major:
        print(f"驱动主版本({driver_major})与浏览器主版本({edge_major})不匹配：{driver_path}")
        return False
    return True


def _resolve_driver_path() -> str | None:
    edge_major = _extract_major_version(_get_edge_version())
    driver_env = os.getenv("EDGEWEBDRIVER")
    if driver_env:
        resolved_path = Path(driver_env).expanduser()
        if not resolved_path.exists():
            raise FileNotFoundError(f"EDGEWEBDRIVER 指向的驱动不存在：{resolved_path}")
        if _is_driver_compatible(resolved_path, edge_major):
            return str(resolved_path)
        print("EDGEWEBDRIVER 指向的驱动版本不匹配，已忽略该配置")

    resolved_path = DEFAULT_DRIVER_PATH
    if resolved_path.exists():
        if _is_driver_compatible(resolved_path, edge_major):
            return str(resolved_path)
        print(f"本地驱动版本不匹配，尝试自动更新：{resolved_path}")

    try:
        _download_driver(resolved_path)
    except Exception as exc:
        print(f"自动下载 Edge 驱动失败：{exc}")

    if resolved_path.exists() and _is_driver_compatible(resolved_path, edge_major):
        return str(resolved_path)

    existing = _find_driver_on_path()
    if existing:
        existing_path = Path(existing)
        if _is_driver_compatible(existing_path, edge_major):
            print(f"发现 PATH 中的驱动：{existing}")
            return existing
        print(f"PATH 中驱动版本不匹配，已忽略：{existing}")

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
    # 重新检测平台标识，便于选择正确的驱动文件名
    platform_token = _detect_driver_platform()
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


def _detect_logged_in_email(driver) -> str | None:
    """进入个人中心尝试读取当前登录邮箱，失败则返回 None。"""
    try:
        driver.get("https://account.microsoft.com/")
        wait = WebDriverWait(driver, 15)
        email_pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
        # 优先从页面配置 JSON 读取 signInName
        try:
            feedback_root = wait.until(EC.presence_of_element_located((By.ID, "feedback-root")))
            config_raw = feedback_root.get_attribute("data-area-config") or ""
            if config_raw:
                try:
                    import json

                    config = json.loads(config_raw)
                    sign_in_name = config.get("signInName")
                    if sign_in_name and email_pattern.search(sign_in_name):
                        return sign_in_name.lower()
                except Exception:
                    pass
        except Exception:
            pass

        selectors = [
            # (By.CSS_SELECTOR, "#mectrl_currentAccount_secondary"),
            # (By.CSS_SELECTOR, "#mectrl_currentAccount_primary"),
            # (By.CSS_SELECTOR, ".mectrlaccounttext"),
            # (By.CSS_SELECTOR, "div[data-bi-name='profileAccount'] span"),
            # 个人中心新版 UI：FUI 文本组件
            (By.CSS_SELECTOR, "span.fui-Text"),
        ]
        for by, sel in selectors:
            try:
                elem = wait.until(EC.presence_of_element_located((by, sel)))
                text = (elem.text or "").strip()
                if not text or "@" not in text:
                    continue
                match = email_pattern.search(text)
                if match:
                    return match.group(0).lower()
            except Exception:
                continue
        # 回退：遍历页面文本查找邮箱模式
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "span, div, p, a")
            for el in elements:
                txt = (el.text or "").strip()
                if not txt or "@" not in txt:
                    continue
                match = email_pattern.search(txt)
                if match:
                    return match.group(0).lower()
        except Exception:
            pass
    except Exception:
        return None
    return None


def _detect_bing_logged_in_email(driver) -> str | None:
    """在 Bing 首页尝试读取当前登录邮箱，失败则返回 None。"""
    try:
        _safe_get(driver, "https://www.bing.com/")
        wait = WebDriverWait(driver, 10)
        email_pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")

        # 尝试打开右上角账号菜单
        for by, sel in [
            (By.ID, "mectrl_main_trigger"),
            (By.ID, "id_l"),
            (By.CSS_SELECTOR, "#mectrl_main_trigger"),
        ]:
            try:
                elem = wait.until(EC.element_to_be_clickable((by, sel)))
                elem.click()
                break
            except Exception:
                continue

        selectors = [
            (By.CSS_SELECTOR, "#mectrl_currentAccount_secondary"),
            (By.CSS_SELECTOR, "#mectrl_currentAccount_primary"),
            (By.CSS_SELECTOR, ".mectrlaccounttext"),
        ]
        for by, sel in selectors:
            try:
                elem = wait.until(EC.presence_of_element_located((by, sel)))
                text = (elem.text or "").strip()
                match = email_pattern.search(text)
                if match:
                    return match.group(0).lower()
            except Exception:
                continue

        # 回退：全页扫描邮箱
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "span, div, p, a")
            for el in elements:
                txt = (el.text or "").strip()
                if not txt or "@" not in txt:
                    continue
                match = email_pattern.search(txt)
                if match:
                    return match.group(0).lower()
        except Exception:
            pass
    except Exception:
        return None
    return None


def _bing_sign_out(driver):
    try:
        _safe_get(driver, "https://www.bing.com/fd/auth/signout?return_url=https%3A%2F%2Fwww.bing.com%2F")
        time.sleep(1)
    except Exception:
        pass


def _bing_sign_in(driver):
    # 使用交互式登录入口，让 Bing 侧与当前 MSA 登录态对齐
    try:
        _safe_get(
            driver,
            "https://www.bing.com/fd/auth/signin?action=interactive&provider=windows_live_id&return_url=https%3A%2F%2Fwww.bing.com%2F",
        )
        time.sleep(1)
    except Exception:
        pass


def _ensure_bing_account(driver, username: str, tag: str = ""):
    prefix = f"{tag} " if tag else ""
    current = _detect_bing_logged_in_email(driver)
    if current and current == username.lower():
        print(f"{prefix}Bing 账号已对齐：{current}")
        return
    if current and current != username.lower():
        print(f"{prefix}Bing 当前登录为 {current}，与目标 {username} 不一致，尝试重新对齐")
    else:
        print(f"{prefix}未能读取 Bing 当前邮箱，尝试重新对齐")

    _bing_sign_out(driver)
    _bing_sign_in(driver)
    current2 = _detect_bing_logged_in_email(driver)
    if current2 and current2 == username.lower():
        print(f"{prefix}Bing 账号对齐完成：{current2}")
        return
    if current2:
        print(f"{prefix}Bing 账号仍不一致：当前 {current2}，目标 {username}（可能被风控/需手动确认）")
    else:
        print(f"{prefix}Bing 账号对齐后仍无法读取邮箱（可能需要手动打开账号菜单确认）")


def _sign_out(driver):
    try:
        driver.get("https://login.live.com/logout.srf")
        time.sleep(2)
    except Exception:
        pass


def login(driver, username: str, password: str):
    """执行 Microsoft 账号登录，如已登录其他账号则先注销。"""
    print(f"登录账号：{username}")
    driver.get("https://login.live.com/")
    wait = WebDriverWait(driver, 20)
    try:
        # 登录页可能直接展示选择账号，如果已记住账号则需要点击“使用其他账号”
        try:
            use_other = wait.until(EC.presence_of_element_located((By.ID, "otherTile")))
            use_other.click()
        except Exception:
            pass

        # 新版 UI 使用 id=usernameEntry；旧版仍为 name=loginfmt
        email_locators = [
            (By.ID, "usernameEntry"),
            (By.NAME, "loginfmt"),
        ]
        email_input = None
        for by, locator in email_locators:
            try:
                email_input = wait.until(EC.presence_of_element_located((by, locator)))
                break
            except Exception:
                continue
        if email_input is None:
            raise RuntimeError("未找到账号输入框，登录页面结构可能已变")
        email_input.clear()
        email_input.send_keys(username)
        next_button_locators = [
            (By.CSS_SELECTOR, "button[data-testid='primaryButton']"),
            (By.ID, "idSIButton9"),
        ]
        next_btn = None
        for by, locator in next_button_locators:
            try:
                next_btn = wait.until(EC.element_to_be_clickable((by, locator)))
                break
            except Exception:
                continue
        if next_btn is None:
            raise RuntimeError("未找到下一步按钮，登录页面结构可能已变")
        next_btn.click()

        # 某些账号会出现“使用密码”卡片，需要先点击才能出现密码输入框
        try:
            pwd_tile = wait.until(
                EC.element_to_be_clickable(
                    (
                        By.CSS_SELECTOR,
                        'div[data-testid="tile"][aria-label="使用密码"], div[role="group"][aria-label="使用密码"]',
                    )
                )
            )
            pwd_tile.click()
        except Exception:
            pass

        pwd_input = wait.until(EC.presence_of_element_located((By.NAME, "passwd")))
        pwd_input.clear()
        pwd_input.send_keys(password)
        pwd_next_locators = [
            (By.CSS_SELECTOR, "button[data-testid='primaryButton']"),
            (By.ID, "idSIButton9"),
        ]
        sign_btn = None
        for by, locator in pwd_next_locators:
            try:
                sign_btn = wait.until(EC.element_to_be_clickable((by, locator)))
                break
            except Exception:
                continue
        if sign_btn is None:
            raise RuntimeError("未找到密码提交按钮，登录页面结构可能已变")
        sign_btn.click()

        try:
            # 新版 UI 的“否”常为 secondaryButton，旧版为 idBtn_Back
            if not _try_click(driver, By.CSS_SELECTOR, "button[data-testid='secondaryButton']"):
                no_btn = wait.until(EC.element_to_be_clickable((By.ID, "idBtn_Back")))
                no_btn.click()
        except Exception:
            pass

        _wait_login_complete(driver, timeout=60)
        print("登录完成")
    except Exception as exc:
        print(f"登录失败：{exc}")
        raise


def _wait_document_ready(driver, timeout: int = NAVIGATION_TIMEOUT) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") in ("interactive", "complete")
        )
        return True
    except Exception:
        return False


def _safe_get(driver, url: str, timeout: int = NAVIGATION_TIMEOUT):
    driver.get(url)
    _wait_document_ready(driver, timeout=timeout)


def _open_first_available(driver, urls: list[str], timeout: int = NAVIGATION_TIMEOUT):
    last_exc = None
    for url in urls:
        try:
            _safe_get(driver, url, timeout=timeout)
            return url
        except Exception as exc:
            last_exc = exc
    raise last_exc or RuntimeError("没有可用的目标地址")


def _try_click(driver, by, locator) -> bool:
    try:
        elements = driver.find_elements(by, locator)
    except Exception:
        return False
    for el in elements:
        try:
            el.click()
            return True
        except Exception:
            continue
    return False


def _wait_login_complete(driver, timeout: int = 60):
    """等待登录流程完成或识别阻塞页面，避免直接 TimeoutException 难以排查。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = (getattr(driver, "current_url", "") or "").lower()
        if "rewards" in url or "login.live.com" not in url:
            return

        try:
            err_elems = driver.find_elements(By.ID, "passwordError")
            for el in err_elems:
                txt = (el.text or "").strip()
                if txt:
                    raise RuntimeError(f"账号或密码可能错误：{txt}")
        except RuntimeError:
            raise
        except Exception:
            pass

        try:
            src = getattr(driver, "page_source", "") or ""
            blockers = [
                ("帮助我们保护你的帐户", "账号触发安全验证（需要手动验证），无法自动完成登录"),
                ("验证你的身份", "账号需要验证身份（可能为二次验证/短信/邮箱验证）"),
                ("更多信息", "账号需要补充安全信息（More information required）"),
                ("two-step verification", "账号开启了两步验证，需要手动处理"),
                ("approve sign in request", "需要在手机/Authenticator 上确认本次登录"),
                ("验证", "登录页面要求额外验证，建议改用有界面模式观察阻塞点"),
            ]
            src_lower = src.lower()
            for needle, message in blockers:
                if needle.lower() in src_lower:
                    raise RuntimeError(message)
        except RuntimeError:
            raise
        except Exception:
            pass

        _try_click(driver, By.CSS_SELECTOR, "button[data-testid='secondaryButton']")
        _try_click(driver, By.ID, "idBtn_Back")
        _try_click(driver, By.CSS_SELECTOR, "button[data-testid='primaryButton']")
        time.sleep(1)

    raise RuntimeError(f"登录后页面未跳转，当前 URL：{getattr(driver, 'current_url', '')}")


def _ensure_logged_in(driver, username: str, password: str, tag: str = ""):
    """确保当前 driver 登录为指定账号，tag 用于区分 PC/移动端日志。"""
    prefix = f"{tag} " if tag else ""
    current_email = _detect_logged_in_email(driver)
    if current_email:
        if current_email == username.lower():
            print(f"{prefix}检测到已有登录态，直接复用")
            return
        print(f"{prefix}当前登录为 {current_email}，与目标 {username} 不一致，执行注销后重新登录")
        _sign_out(driver)
        login(driver, username, password)
        return

    print(f"{prefix}未能读取当前邮箱，默认注销后登录以确保账号匹配")
    _sign_out(driver)
    login(driver, username, password)


def init_browser(s):
    return _init_edge(headless_flag=s, mobile_emulation=None)


def init_mobile_edge_appium(s):
    return _init_edge(headless_flag=s, mobile_emulation={"deviceName": "iPhone X"})


def _init_edge(headless_flag: str | None, mobile_emulation: dict | None):
    options = EdgeOptions()
    if mobile_emulation:
        options.add_experimental_option("mobileEmulation", mobile_emulation)
    if headless_flag:
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
        _open_first_available(driver, REWARDS_HOME_URLS)
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


def _locate_search_box(driver, timeout: int = 10):
    for by, locator in SEARCH_BOX_LOCATORS:
        try:
            element = WebDriverWait(driver, timeout).until(EC.presence_of_element_located((by, locator)))
            if element is not None:
                return element
        except Exception:
            continue
    return None


def _reset_search_box(driver, element):
    try:
        driver.execute_script("arguments[0].focus();", element)
    except Exception:
        pass
    try:
        element.click()
    except Exception:
        pass
    try:
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
        return
    except (StaleElementReferenceException, InvalidElementStateException):
        raise
    except Exception:
        pass
    try:
        driver.execute_script(
            """
            arguments[0].value = '';
            arguments[0].dispatchEvent(new Event('input', {bubbles: true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles: true}));
            """,
            element,
        )
    except Exception:
        pass


def _search_via_direct_url(driver, keyword: str):
    search_url = f"https://www.bing.com/search?q={quote_plus(keyword)}"
    _safe_get(driver, search_url)
    _random_sleep(short_range=(3, 5), long_prob=0.2, long_range=(6, 10))
    _random_scroll_results(driver)
    _random_click_result(driver)
    _jiggle_mouse(driver)


def bing_search(driver, keyword):
    for attempt in range(3):
        try:
            if attempt > 0:
                goSearch(driver)
            element = _locate_search_box(driver, timeout=10)
            if element is None:
                if attempt >= 1:
                    _search_via_direct_url(driver, keyword)
                    return
                raise RuntimeError("未找到 Bing 搜索框")
            _reset_search_box(driver, element)
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
        except (StaleElementReferenceException, InvalidElementStateException):
            print(f"搜索 {keyword} 时元素失效，重试 {attempt + 1}")
            _random_sleep(short_range=(1, 2))
        except Exception as e:
            if attempt < 2:
                print(f"搜索 {keyword} 失败：{e}，重试 {attempt + 1}")
                _random_sleep(short_range=(1, 2))
                try:
                    goSearch(driver)
                except Exception:
                    pass
                continue
            print(f"搜索 {keyword} 失败：{e}")
            return


def _pick_daily_set_key(data: dict) -> str | None:
    if not data:
        return None
    today_key = datetime.now().strftime("%m/%d/%Y")
    if today_key in data:
        return today_key
    keys = [k for k, v in data.items() if isinstance(v, list) and v]
    if not keys:
        return None
    parsed = []
    for key in keys:
        try:
            parsed.append((datetime.strptime(key, "%m/%d/%Y"), key))
        except Exception:
            continue
    if parsed:
        parsed.sort(reverse=True)
        return parsed[0][1]
    return keys[-1]


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _promotion_title(item: dict) -> str:
    return (item.get("title") or item.get("name") or item.get("offerId") or "未命名任务").strip()


def _normalize_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _contains_any_keyword(text: str | None, keywords: list[str] | tuple[str, ...]) -> bool:
    normalized = _normalize_text(text)
    return any(keyword.lower() in normalized for keyword in keywords)


def _is_generic_nav_entry(text: str | None, context: str | None = None, href: str | None = None) -> bool:
    combined = " ".join(
        part for part in (_normalize_text(text), _normalize_text(context), _normalize_text(href)) if part
    )
    blocked_keywords = (
        "首页",
        "home",
        "homepage",
        "earn more",
        "赚取更多",
        "learn more",
        "了解更多",
        "discover more",
        "更多奖励",
        "rewards dashboard",
        "rewardspanel",
    )
    return any(keyword in combined for keyword in blocked_keywords)


def _promotion_offer_id(item: dict) -> str:
    return (item.get("offerId") or item.get("offerid") or item.get("name") or "").strip()


def _promotion_target_url(item: dict) -> str:
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    for candidate in (
        item.get("destinationUrl"),
        item.get("destination"),
        attributes.get("destination"),
        attributes.get("destinationUrl"),
        item.get("deeplink"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _promotion_points_total(item: dict) -> int:
    return max(
        _safe_int(item.get("pointProgressMax")),
        _safe_int(item.get("activityProgressMax")),
        _safe_int(item.get("max")),
        _safe_int((item.get("attributes") or {}).get("max") if isinstance(item.get("attributes"), dict) else 0),
    )


def _promotion_progress(item: dict) -> int:
    return max(
        _safe_int(item.get("pointProgress")),
        _safe_int(item.get("activityProgress")),
        _safe_int(item.get("progress")),
        _safe_int((item.get("attributes") or {}).get("progress") if isinstance(item.get("attributes"), dict) else 0),
    )


def _promotion_is_complete(item: dict) -> bool:
    if item.get("complete") is True:
        return True
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    state = str(attributes.get("state") or item.get("state") or "").strip().lower()
    if state == "complete":
        return True
    total = _promotion_points_total(item)
    progress = _promotion_progress(item)
    return bool(total > 0 and progress >= total)


def _promotion_is_locked(item: dict) -> bool:
    if item.get("isHidden") is True:
        return True
    locked_status = str(item.get("exclusiveLockedFeatureStatus") or "").strip().lower()
    return locked_status == "locked"


def _promotion_can_trigger(item: dict) -> bool:
    if not isinstance(item, dict) or _promotion_is_complete(item) or _promotion_is_locked(item):
        return False
    if _promotion_target_url(item):
        return True
    return _promotion_points_total(item) > 0


def _dedupe_promotions(items: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        signature = (
            _promotion_offer_id(item),
            _promotion_target_url(item),
            _promotion_title(item),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(item)
    return deduped


def _find_promotion_link(driver, promotion: dict):
    offer_id = _promotion_offer_id(promotion)
    target_url = _promotion_target_url(promotion)
    title = _promotion_title(promotion)

    selectors = []
    if offer_id:
        selectors.extend(
            [
                (By.CSS_SELECTOR, f'[data-offer-id="{offer_id}"] a[href]'),
                (By.CSS_SELECTOR, f'[data-offer-id="{offer_id}"]'),
                (By.CSS_SELECTOR, f'a[href*="{offer_id}"]'),
            ]
        )
    if target_url:
        selectors.append((By.CSS_SELECTOR, f'a[href="{target_url}"]'))

    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed():
                    return element
            except Exception:
                continue

    if title:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, "a, button, [role='button']")
        except Exception:
            elements = []
        normalized = re.sub(r"\s+", "", title)
        for element in elements:
            try:
                text = re.sub(r"\s+", "", (element.text or "").strip())
            except StaleElementReferenceException:
                continue
            if not text:
                continue
            if text == normalized or normalized in text or text in normalized:
                return element
    return None


def _open_promotion_url(driver, url: str, wait_seconds: int = 8) -> bool:
    previous_handles = set(driver.window_handles)
    previous_url = (getattr(driver, "current_url", "") or "").strip()
    try:
        driver.execute_script("window.open(arguments[0], '_blank');", url)
        switchToNewTab(driver, timeToWait=0, previous_handles=previous_handles)
        _wait_document_ready(driver, timeout=10)
        time.sleep(wait_seconds)
        closeCurrentTab(driver)
        return True
    except Exception:
        try:
            _safe_get(driver, url)
            time.sleep(wait_seconds)
            driver.back()
            _wait_document_ready(driver, timeout=10)
            return True
        except Exception:
            try:
                if getattr(driver, "current_url", "") != previous_url:
                    driver.get(previous_url)
            except Exception:
                pass
            return False


def _trigger_promotion(driver, promotion: dict, wait_seconds: int = 8, tag: str = "") -> bool:
    title = _promotion_title(promotion)
    prefix = f"{tag} " if tag else ""
    target_url = _promotion_target_url(promotion)
    if target_url and _open_promotion_url(driver, target_url, wait_seconds=wait_seconds):
        print(f"{prefix}已触发任务：{title}")
        return True

    try:
        element = _find_promotion_link(driver, promotion)
        if element is None:
            print(f"{prefix}未找到任务入口：{title}")
            return False
        previous_handles = set(driver.window_handles)
        previous_url = (getattr(driver, "current_url", "") or "").strip()
        _click_element(driver, element)
        open_state = _wait_for_activity_open(driver, previous_handles, previous_url, timeout=10)
        if open_state == "new_tab":
            switchToNewTab(driver, timeToWait=wait_seconds, previous_handles=previous_handles)
            closeCurrentTab(driver)
        elif open_state == "same_tab":
            time.sleep(wait_seconds)
            driver.back()
            _wait_document_ready(driver, timeout=10)
        else:
            time.sleep(wait_seconds)
        print(f"{prefix}已触发任务：{title}")
        return True
    except Exception as exc:
        print(f"{prefix}触发任务失败：{title}，原因：{exc}")
        return False


def daily_set(driver):
    gohome(driver)
    completed = _run_daily_set_dom_fallback(driver)
    if completed <= 0:
        print("未找到每日任务入口，跳过每日任务")
        return
    gohome(driver)
    print("完成每日任务")


def _collect_daily_set_links(driver):
    selectors = [
        "#daily-sets a[href]",
        "mee-card-group a[href]",
        "mee-card a[href]",
        "a[href*='rewards.bing.com']",
    ]
    links = []
    seen = set()
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                href = (element.get_attribute("href") or "").strip()
                text = (element.text or "").strip()
            except StaleElementReferenceException:
                continue
            if not href:
                continue
            signature = (href, text)
            if signature in seen:
                continue
            seen.add(signature)
            links.append(element)
    return links


def _collect_dom_reward_entries(
    driver,
    section_keywords: list[str] | tuple[str, ...],
    action_keywords: list[str] | tuple[str, ...] | None = None,
):
    try:
        entries = driver.execute_script(
            """
            const sectionKeywords = (arguments[0] || []).map(x => String(x).toLowerCase());
            const actionKeywords = (arguments[1] || []).map(x => String(x).toLowerCase());
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const getContext = (el) => {
                const parts = [];
                let node = el;
                for (let i = 0; i < 6 && node; i += 1) {
                    parts.push(
                        normalize(node.id),
                        normalize(node.className),
                        normalize((node.getAttribute && node.getAttribute('aria-label')) || ''),
                        normalize((node.innerText || node.textContent || '').slice(0, 180))
                    );
                    node = node.parentElement;
                }
                return parts.join(' ');
            };

            const results = [];
            let index = 0;
            for (const el of document.querySelectorAll("a[href], button, [role='button']")) {
                if (!visible(el)) {
                    continue;
                }
                const text = normalize(el.innerText || el.textContent || '');
                const href = normalize(el.getAttribute && el.getAttribute('href'));
                const context = getContext(el);
                if (!text && (!href || href === '/' || href === '#')) {
                    continue;
                }
                if (href === '/' || href === '#' || href.startsWith('javascript:')) {
                    continue;
                }
                if (!sectionKeywords.some(keyword => context.includes(keyword))) {
                    continue;
                }
                if (actionKeywords.length && !actionKeywords.some(keyword => text.includes(keyword) || context.includes(keyword))) {
                    continue;
                }
                index += 1;
                const taskId = `codex-task-${Date.now()}-${index}`;
                el.setAttribute('data-codex-task-id', taskId);
                results.push({
                    taskId,
                    text,
                    href,
                    context,
                });
            }
            return results;
            """,
            list(section_keywords),
            list(action_keywords or []),
        )
    except Exception:
        return []

    deduped = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        signature = (
            entry.get("href", ""),
            entry.get("text", ""),
            entry.get("context", "")[:120],
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(entry)
    return deduped


def _collect_daily_set_dom_entries(driver):
    cards = _collect_daily_task_cards(driver)
    if not cards:
        return []

    results = []
    for entry in cards:
        task_id = (entry.get("taskId") or "").strip()
        if not task_id:
            continue
        results.append(
            {
                "taskId": task_id,
                "text": entry.get("text", ""),
                "href": entry.get("href", ""),
                "context": f"dailyset {entry.get('title', '')} {entry.get('points', '')}",
            }
        )
    return results


def _collect_daily_task_cards(driver):
    try:
        WebDriverWait(driver, 12).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, DAILY_SECTION_SELECTOR)
            or d.find_elements(By.XPATH, "//section[@id='dailyset']")
            or d.find_elements(By.XPATH, "//section[.//h2[normalize-space()='每日活动']]")
            or d.find_elements(By.XPATH, "//h2[normalize-space()='每日活动']")
        )
    except Exception:
        pass

    try:
        entries = driver.execute_script(
            """
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const pointPattern = /(?:\\+?\\d+)\\s*(?:points?|积分)/i;
            const dailyUrlPattern = /(form=ml2g76|form=dsetqu|reward?sdo|dailyset|gamification_dailyset)/i;
            const dailySection =
                document.querySelector(arguments[0]) ||
                document.querySelector("section#dailyset") ||
                Array.from(document.querySelectorAll("section")).find((el) =>
                    /每日活动/i.test(normalize(el.innerText || el.textContent || ''))
                );
            const candidates = [];
            let index = 0;
            const elements = dailySection
                ? (dailySection.querySelectorAll("a[href]") || [])
                : document.querySelectorAll("a[href]");
            for (const el of elements) {
                if (!visible(el)) {
                    continue;
                }
                const text = normalize(el.innerText || el.textContent || '');
                const href = normalize((el.getAttribute && el.getAttribute('href')) || '');
                if (!text || !pointPattern.test(text)) {
                    continue;
                }
                if (text.includes('每日活动') || text.includes('赚取更多') || text.includes('了解更多')) {
                    continue;
                }
                if (!dailySection && !dailyUrlPattern.test(href)) {
                    continue;
                }
                const match = text.match(pointPattern);
                const points = match ? match[0] : '';
                const title = text.replace(pointPattern, '').trim().split('\\n')[0].trim() || text;
                if (!title || title === '领取' || title === '可领取') {
                    continue;
                }
                index += 1;
                const taskId = `codex-daily-${Date.now()}-${index}`;
                el.setAttribute('data-codex-daily-id', taskId);
                candidates.push({taskId, title, text, href, points});
            }
            return candidates;
            """,
            DAILY_SECTION_SELECTOR,
        )
    except Exception:
        entries = []

    deduped = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        signature = (
            entry.get("title", ""),
            entry.get("points", ""),
            entry.get("href", ""),
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(entry)
    if deduped:
        return deduped

    try:
        containers = driver.find_elements(By.XPATH, "//section[@id='dailyset']") or driver.find_elements(
            By.XPATH, "//section[.//h2[normalize-space()='每日活动']]"
        )
    except Exception:
        containers = []
    fallback_entries = []
    seen = set()
    elements = []
    if containers:
        section = containers[0]
        try:
            elements = section.find_elements(By.XPATH, ".//a[@href]")
            if not elements:
                elements = section.find_elements(By.XPATH, ".//button[.//*[contains(normalize-space(.), '+')] or contains(normalize-space(.), '积分')]")
        except Exception:
            elements = []
    if not elements:
        try:
            elements = driver.find_elements(
                By.XPATH,
                "//a[contains(@href,'form=ML2G76') or contains(@href,'form=dsetqu') or contains(@href,'RewardsDO') or contains(@href,'DailySet')]",
            )
        except Exception:
            elements = []
    for index, element in enumerate(elements, 1):
        try:
            text = re.sub(r"\s+", " ", (element.text or "").strip())
            href = (element.get_attribute("href") or "").strip()
        except Exception:
            continue
        if not text:
            continue
        if ("+" not in text and "积分" not in text) or "每日活动" in text or "赚取更多" in text or "了解更多" in text:
            continue
        points_match = re.search(r"(\+\d+\s*(?:积分|points?)?|\d+\s*积分)", text, re.I)
        if not points_match:
            continue
        points = points_match.group(1).strip()
        title = re.sub(r"\s+", " ", text.replace(points, "")).strip().split("\n")[0].strip()
        if not title:
            continue
        signature = (title, points, href)
        if signature in seen:
            continue
        seen.add(signature)
        task_id = f"codex-daily-fallback-{int(time.time() * 1000)}-{index}"
        try:
            driver.execute_script("arguments[0].setAttribute('data-codex-daily-id', arguments[1]);", element, task_id)
        except Exception:
            continue
        fallback_entries.append({"taskId": task_id, "title": title, "text": text, "href": href, "points": points})
    return fallback_entries


def _resolve_daily_task_entry(driver, entry: dict) -> dict | None:
    task_id = (entry.get("taskId") or "").strip()
    if task_id:
        try:
            driver.find_element(By.CSS_SELECTOR, f'[data-codex-daily-id="{task_id}"]')
            return entry
        except Exception:
            pass

    target_href = (entry.get("href") or "").strip()
    target_title = _normalize_text(entry.get("title") or entry.get("text") or "")
    target_points = _normalize_text(entry.get("points") or "")

    fresh_cards = _collect_daily_task_cards(driver)
    if not fresh_cards:
        return None

    if target_href:
        for candidate in fresh_cards:
            if (candidate.get("href") or "").strip() == target_href:
                return candidate

    for candidate in fresh_cards:
        candidate_title = _normalize_text(candidate.get("title") or candidate.get("text") or "")
        candidate_points = _normalize_text(candidate.get("points") or "")
        if candidate_title == target_title and (not target_points or candidate_points == target_points):
            return candidate

    for candidate in fresh_cards:
        candidate_text = _normalize_text(candidate.get("text") or "")
        if target_title and (target_title in candidate_text or candidate_text in target_title):
            return candidate
    return None


def _click_daily_task_card(driver, entry: dict) -> bool:
    entry = _resolve_daily_task_entry(driver, entry) or entry
    task_id = entry.get("taskId")
    title = (entry.get("title") or entry.get("text") or "未命名日常任务").strip()
    points = (entry.get("points") or "").strip()
    if not task_id:
        return False
    try:
        element = driver.find_element(By.CSS_SELECTOR, f'[data-codex-daily-id="{task_id}"]')
    except Exception:
        return False

    previous_handles = set(driver.window_handles)
    previous_url = (getattr(driver, "current_url", "") or "").strip()
    try:
        _click_element(driver, element)
        open_state = _wait_for_activity_open(driver, previous_handles, previous_url, timeout=10)
        if open_state == "new_tab":
            switchToNewTab(driver, timeToWait=random.randint(6, 10), previous_handles=previous_handles)
            closeCurrentTab(driver)
        elif open_state == "same_tab":
            time.sleep(random.randint(6, 10))
            driver.back()
            _wait_document_ready(driver, timeout=10)
        else:
            href = (entry.get("href") or "").strip()
            if href:
                return _open_promotion_url(driver, href, wait_seconds=random.randint(6, 10))
        print(f"日常任务 触发任务：{title} {points}".strip())
        return True
    except Exception:
        href = (entry.get("href") or "").strip()
        if href:
            ok = _open_promotion_url(driver, href, wait_seconds=random.randint(6, 10))
            if ok:
                print(f"日常任务 触发任务：{title} {points}".strip())
            return ok
        return False


def _click_reward_entry(driver, entry: dict, wait_seconds: int = 8, tag: str = "") -> bool:
    task_id = entry.get("taskId")
    title = (entry.get("text") or entry.get("context") or entry.get("href") or "未命名任务").strip()
    if not task_id:
        return False
    try:
        element = driver.find_element(By.CSS_SELECTOR, f'[data-codex-task-id="{task_id}"]')
    except Exception:
        return False

    previous_handles = set(driver.window_handles)
    previous_url = (getattr(driver, "current_url", "") or "").strip()
    try:
        _click_element(driver, element)
        open_state = _wait_for_activity_open(driver, previous_handles, previous_url, timeout=10)
        if open_state == "new_tab":
            switchToNewTab(driver, timeToWait=wait_seconds, previous_handles=previous_handles)
            closeCurrentTab(driver)
        elif open_state == "same_tab":
            time.sleep(wait_seconds)
            driver.back()
            _wait_document_ready(driver, timeout=10)
        else:
            href = entry.get("href", "")
            if href:
                return _open_promotion_url(driver, href, wait_seconds=wait_seconds)
            time.sleep(wait_seconds)
        if tag:
            print(f"{tag} DOM 触发任务：{title}")
        return True
    except Exception:
        href = entry.get("href", "")
        if href:
            ok = _open_promotion_url(driver, href, wait_seconds=wait_seconds)
            if ok and tag:
                print(f"{tag} DOM 触发任务：{title}")
            return ok
        return False


def _run_daily_set_dom_fallback(driver) -> int:
    for attempt in range(2):
        daily_cards = _collect_daily_task_cards(driver)
        if daily_cards:
            completed = 0
            for entry in daily_cards[:3]:
                if _click_daily_task_card(driver, entry):
                    completed += 1
                    try:
                        gohome(driver)
                    except Exception:
                        pass
            print(f"日常任务触发 {completed}/{min(len(daily_cards), 3)} 个")
            return completed

        entries = _collect_daily_set_dom_entries(driver)
        if entries:
            completed = 0
            for entry in entries[:3]:
                if _click_reward_entry(driver, entry, wait_seconds=random.randint(6, 10), tag="每日任务"):
                    completed += 1
                    try:
                        gohome(driver)
                    except Exception:
                        pass
            print(f"DOM 回退触发每日任务 {completed}/{min(len(entries), 3)} 个")
            return completed

        if attempt == 0:
            try:
                _random_sleep(short_range=(2, 3))
                gohome(driver)
                continue
            except Exception:
                pass

    print("DOM 回退也未找到每日任务入口")
    return 0


def _collect_claimable_dom_entries(driver):
    section_keywords = (
        "more activities",
        "more promotions",
        "punch",
        "offer",
        "promotion",
        "activity",
        "奖励",
        "积分",
        "活动",
        "任务",
    )
    action_keywords = (
        "claim",
        "open",
        "earn",
        "start",
        "play",
        "join",
        "get",
        "领取",
        "打开",
        "开始",
        "赚取",
        "获取",
    )
    entries = _collect_dom_reward_entries(driver, section_keywords, action_keywords=action_keywords)
    blocked_keywords = ("complete", "completed", "done", "已完成")
    results = []
    for entry in entries:
        text = entry.get("text", "")
        context = entry.get("context", "")
        href = entry.get("href", "")
        if any(keyword in text or keyword in context for keyword in blocked_keywords):
            continue
        if _is_generic_nav_entry(text, context, href):
            continue
        if "bing.com/search" in href or "bing.com/?" in href:
            continue
        if "可领取" in text and "待领取" not in text:
            continue
        if not (
            _contains_any_keyword(text, ("claim", "领取", "open", "earn", "start", "join", "play", "get", "赚取", "获取"))
            or _contains_any_keyword(context, ("points", "积分", "rewards", "奖励", "activity", "活动", "offer", "promotion"))
        ):
            continue
        if not (
            _contains_any_keyword(text, ("待领取", "claim", "领取", "获取"))
            or _contains_any_keyword(context, ("待领取", "claim", "领取", "获取"))
        ):
            continue
        results.append(entry)
    return results


def _collect_claim_button_entries(driver):
    claim_keywords = (
        "claim",
        "claim now",
        "claim points",
        "get reward",
        "领取",
        "立即领取",
        "领取积分",
        "获取积分",
    )
    blocked_keywords = (
        "redeem",
        "gift card",
        "donate",
        "兑换",
        "捐赠",
    )
    try:
        entries = driver.execute_script(
            """
            const claimKeywords = (arguments[0] || []).map(x => String(x).toLowerCase());
            const blockedKeywords = (arguments[1] || []).map(x => String(x).toLowerCase());
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
            const results = [];
            let index = 0;
            for (const el of document.querySelectorAll("button, a[href], [role='button']")) {
                if (!visible(el)) {
                    continue;
                }
                const text = normalize(el.innerText || el.textContent || '');
                const aria = normalize((el.getAttribute && el.getAttribute('aria-label')) || '');
                const href = normalize((el.getAttribute && el.getAttribute('href')) || '');
                const context = `${text} ${aria} ${href}`;
                if (!claimKeywords.some(keyword => context.includes(keyword))) {
                    continue;
                }
                if (blockedKeywords.some(keyword => context.includes(keyword))) {
                    continue;
                }
                if (href.includes('redeem') || href.includes('giftcards')) {
                    continue;
                }
                if (context.includes('可领取') && !context.includes('领取积分') && !context.includes('claim points')) {
                    continue;
                }
                index += 1;
                const taskId = `codex-claim-${Date.now()}-${index}`;
                el.setAttribute('data-codex-claim-id', taskId);
                results.push({taskId, text, href, context});
            }
            return results;
            """,
            list(claim_keywords),
            list(blocked_keywords),
        )
    except Exception:
        return []

    deduped = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        signature = (entry.get("text", ""), entry.get("href", ""))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(entry)
    return deduped


def _claim_points_from_dashboard_sidebar(driver, tag: str = "积分任务") -> int:
    claimed = 0
    try:
        _safe_get(driver, "https://rewards.bing.com/dashboard")
        WebDriverWait(driver, 10).until(
            lambda d: d.find_elements(By.ID, "shell") or d.find_elements(By.XPATH, "//*[@id='shell']//main")
        )
    except Exception:
        return 0

    for _ in range(3):
        try:
            dialog_open = None
            try:
                dialog_open = driver.find_element(By.XPATH, "//*[@role='dialog']")
            except Exception:
                dialog_open = None

            if dialog_open is None:
                trigger = None
                for xpath in CLAIM_CARD_XPATHS:
                    try:
                        trigger = WebDriverWait(driver, 4).until(
                            EC.element_to_be_clickable((By.XPATH, xpath))
                        )
                        if trigger is not None:
                            break
                    except Exception:
                        continue
                if trigger is None:
                    break
                _click_element(driver, trigger)
                WebDriverWait(driver, 6).until(
                    EC.presence_of_element_located((By.XPATH, "//*[@role='dialog']"))
                )
            claim_button = None
            for xpath in CLAIM_SUBMIT_XPATHS:
                try:
                    claim_button = WebDriverWait(driver, 4).until(
                        EC.element_to_be_clickable((By.XPATH, xpath))
                    )
                    if claim_button is not None:
                        break
                except Exception:
                    continue
            if claim_button is None:
                break
            _click_element(driver, claim_button)
            claimed += 1
            print(f"{tag} 侧边栏领取成功")
            try:
                WebDriverWait(driver, 8).until_not(
                    EC.presence_of_element_located((By.XPATH, "//*[@role='dialog']"))
                )
            except Exception:
                pass
            _random_sleep(short_range=(1, 2))
            _safe_get(driver, "https://rewards.bing.com/dashboard")
        except Exception:
            break
    return claimed


def _click_claim_entry(driver, entry: dict, tag: str = "") -> bool:
    claim_id = entry.get("taskId")
    title = (entry.get("text") or entry.get("context") or "claim").strip()
    if not claim_id:
        return False
    try:
        element = driver.find_element(By.CSS_SELECTOR, f'[data-codex-claim-id="{claim_id}"]')
    except Exception:
        return False

    previous_handles = set(driver.window_handles)
    previous_url = (getattr(driver, "current_url", "") or "").strip()
    try:
        _click_element(driver, element)
        open_state = _wait_for_activity_open(driver, previous_handles, previous_url, timeout=6)
        if open_state == "new_tab":
            switchToNewTab(driver, timeToWait=3, previous_handles=previous_handles)
            closeCurrentTab(driver)
        elif open_state == "same_tab":
            time.sleep(3)
            if "rewards" not in (getattr(driver, "current_url", "") or "").lower():
                driver.back()
                _wait_document_ready(driver, timeout=10)
        else:
            time.sleep(2)
        if tag:
            print(f"{tag} 点击领取：{title}")
        return True
    except Exception:
        return False


def _claim_available_points(driver, tag: str = "积分任务") -> int:
    claimed = _claim_points_from_dashboard_sidebar(driver, tag=tag)
    for _ in range(3):
        entries = _collect_claim_button_entries(driver)
        if not entries:
            break
        clicked = False
        for entry in entries:
            if _click_claim_entry(driver, entry, tag=tag):
                claimed += 1
                clicked = True
                _random_sleep(short_range=(1, 2))
                try:
                    gohome(driver)
                except Exception:
                    pass
                break
        if not clicked:
            break
    if claimed:
        print(f"{tag} 已领取 {claimed} 次")
    return claimed


def _run_dom_reward_fallback(driver, tag: str) -> int:
    entries = _collect_claimable_dom_entries(driver)
    if not entries:
        print(f"{tag} DOM 回退未找到可领取任务")
        return 0
    completed = 0
    for entry in entries[:8]:
        if _click_reward_entry(driver, entry, wait_seconds=random.randint(6, 10), tag=tag):
            completed += 1
            try:
                gohome(driver)
            except Exception:
                pass
    print(f"{tag} DOM 回退已触发 {completed}/{min(len(entries), 8)} 个任务")
    return completed


def _click_element(driver, element):
    try:
        element.click()
        return
    except Exception:
        pass
    driver.execute_script("arguments[0].click();", element)


def _wait_for_activity_open(driver, previous_handles: set[str], previous_url: str, timeout: int = 10) -> str | None:
    def _state(d):
        handles = set(d.window_handles)
        if len(handles) > len(previous_handles):
            return "new_tab"
        current_url = (getattr(d, "current_url", "") or "").strip()
        if current_url and current_url != previous_url:
            return "same_tab"
        return None

    try:
        return WebDriverWait(driver, timeout).until(lambda d: _state(d))
    except Exception:
        return None


def openDailySetActivity(driver, cardId: int):
    for attempt in range(3):
        try:
            previous_handles = set(driver.window_handles)
            previous_url = (getattr(driver, "current_url", "") or "").strip()
            links = WebDriverWait(driver, 10).until(lambda d: _collect_daily_set_links(d) or None)
            if len(links) >= cardId:
                element = links[cardId - 1]
            else:
                element = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (
                            By.XPATH,
                            f'//*[@id="daily-sets"]/mee-card-group[1]/div/mee-card[{cardId}]/div/card-content/mee-rewards-daily-set-item-content/div/a',
                        )
                    )
                )
            _click_element(driver, element)
            open_state = _wait_for_activity_open(driver, previous_handles, previous_url, timeout=10)
            if open_state == "new_tab":
                switchToNewTab(driver, 8, previous_handles=previous_handles)
                closeCurrentTab(driver)
                return
            if open_state == "same_tab":
                time.sleep(8)
                driver.back()
                _wait_document_ready(driver, timeout=10)
                return
            raise RuntimeError("点击每日任务后未检测到新标签页或页面跳转")
            return
        except Exception as e:
            if attempt < 2:
                print(f"打开每日任务 {cardId} 失败：{e}，重试 {attempt + 1}")
                time.sleep(1.0)
                continue
            print(f"打开每日任务 {cardId} 失败：{e}")
            return


def switchToNewTab(driver, timeToWait: int = 0, previous_handles: set[str] | None = None):
    handles = driver.window_handles
    if previous_handles is None:
        if len(handles) < 2:
            return
        time.sleep(0.5)
        driver.switch_to.window(window_name=handles[1])
        if timeToWait > 0:
            time.sleep(timeToWait)
        return

    prev = set(previous_handles)
    try:
        WebDriverWait(driver, 10).until(lambda d: len(d.window_handles) > len(prev))
    except Exception:
        pass
    handles = driver.window_handles
    target = next((h for h in handles if h not in prev), None)
    if not target:
        if len(handles) < 2:
            return
        target = handles[-1]
    driver.switch_to.window(window_name=target)
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
        _open_first_available(driver, BING_HOME_URLS)
    except Exception as e:
        print(f"打开 Bing 失败：{e}")
        raise


def _get_cached_trends(cache_key: str, fetcher) -> list:
    now = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    cached = _TRENDS_CACHE.get(cache_key)

    if cached:
        if cached.get("date") == today and cached.get("ok") and isinstance(cached.get("words"), list):
            return cached["words"].copy()
        if (
            not cached.get("ok")
            and isinstance(cached.get("ts"), (int, float))
            and now - cached["ts"] < TRENDS_CACHE_FAILURE_TTL
        ):
            return []

    words = fetcher() or []
    ok = bool(words)
    _TRENDS_CACHE[cache_key] = {"date": today, "ts": now, "ok": ok, "words": words.copy()}
    return words


def getBaiduTrends() -> list:
    def _fetch():
        words = []
        try:
            r = requests.get(
                "https://v2.xxapi.cn/api/baiduhot",
                timeout=REQUEST_TIMEOUT,
                headers=HTTP_HEADERS,
            )
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

    return _get_cached_trends("baidu", _fetch)


def getZhihuTrends():
    def _fetch():
        words = []
        try:
            r = requests.get(
                "https://v2.xxapi.cn/api/douyinhot",
                timeout=REQUEST_TIMEOUT,
                headers=HTTP_HEADERS,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"获取知乎热榜失败：{e}")
            return words
        data = r.json().get("data", [])
        for trend in data:
            title = trend.get("word")
            if title:
                words.append(title)
        return words

    return _get_cached_trends("zhihu", _fetch)


def getDouYinTrends():
    def _fetch():
        words = []
        try:
            r = requests.get(
                "https://api.cenguigui.cn/api/juhe/hotlist.php?type=weibo",
                timeout=REQUEST_TIMEOUT,
                headers=HTTP_HEADERS,
            )
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

    return _get_cached_trends("douyin", _fetch)


def _infer_search_points(target_total: int) -> int:
    if target_total in (33, 102):
        return 3
    if target_total == 55 or target_total >= 170:
        return 5
    return 1


def _remaining_searches_from_counter(counter_items: list[dict] | None) -> int:
    if not counter_items:
        return 0

    remaining = 0
    for item in counter_items:
        if not isinstance(item, dict):
            continue
        try:
            progress = int(item.get("pointProgress", 0) or 0)
            target = int(item.get("pointProgressMax", 0) or 0)
        except (TypeError, ValueError):
            continue
        if target <= 0:
            continue
        search_points = max(1, _infer_search_points(target))
        remaining_points = max(0, target - progress)
        remaining += math.ceil(remaining_points / search_points)
    return remaining


def getRemainingSearches(driver):
    del driver
    return DEFAULT_PC_SEARCHES, DEFAULT_MOBILE_SEARCHES


def _ensure_keywords(*keyword_lists):
    for words in keyword_lists:
        if words:
            return words
    print("热词接口不可用，使用默认关键词")
    return DEFAULT_KEYWORDS.copy()


def _keyword_cycle(words: list[str]):
    if not words:
        return
    last = None
    while True:
        pool = words.copy()
        random.shuffle(pool)
        if last is not None and len(pool) > 1 and pool[0] == last:
            pool[0], pool[1] = pool[1], pool[0]
        for item in pool:
            yield item
            last = item


def _search_loop(driver, keyword_list, searches: int, tag: str, extra_sleep: bool = False):
    if searches <= 0:
        print(f"{tag} 已无剩余搜索")
        return
    if not keyword_list:
        print(f"{tag} 未获取到关键词，跳过搜索")
        return
    cycle = _keyword_cycle(keyword_list)
    for _ in tqdm(range(searches), desc=f"{tag} bing searches", unit="search"):
        keyword = next(cycle)
        bing_search(driver, keyword)
        if extra_sleep:
            time.sleep(random.randint(2, 4))
        _maybe_take_break(tag)


def _run_reward_promotions(driver, promotions: list[dict], tag: str):
    pending = [item for item in promotions if _promotion_can_trigger(item)]
    if not pending:
        print(f"{tag} 无可触发任务")
        return 0
    completed = 0
    for item in pending:
        if _trigger_promotion(driver, item, wait_seconds=random.randint(6, 10), tag=tag):
            completed += 1
            _random_sleep(short_range=(1, 3))
            try:
                gohome(driver)
            except Exception:
                pass
    print(f"{tag} 已触发 {completed}/{len(pending)} 个任务")
    return completed


def receive_points(driver):
    gohome(driver)
    claimed_before = _claim_available_points(driver, tag="积分任务")
    triggered = _run_dom_reward_fallback(driver, "积分任务")
    gohome(driver)
    claimed_after = _claim_available_points(driver, tag="积分任务")
    total = claimed_before + triggered + claimed_after
    print(f"本轮额外领取/触发任务共 {total} 个（领取 {claimed_before + claimed_after}，触发 {triggered}）")




def _run_desktop_flow(username: str, password: str, headless_flag: str | None) -> int:
    driver = init_browser(headless_flag)
    try:
        _ensure_logged_in(driver, username, password, tag="PC")
        gohome(driver)
        time.sleep(random.randint(2, 4))
        daily_set(driver)
        remaining_desktop, remaining_mobile = getRemainingSearches(driver)
        print(f"本轮计划搜索次数：PC={remaining_desktop}，移动端={remaining_mobile}")
        if remaining_desktop > 0:
            goSearch(driver)
            _ensure_bing_account(driver, username, tag="PC")
            keyword_list = _ensure_keywords(
                getBaiduTrends(),
                getZhihuTrends(),
            )
            _search_loop(driver, keyword_list, remaining_desktop, "PC")
        else:
            print("PC 无剩余搜索次数，跳过 PC 搜索")
        receive_points(driver)
    finally:
        driver.quit()
    return remaining_mobile


def _run_mobile_flow(username: str, password: str, headless_flag: str | None, remaining_mobile: int):
    if not _should_run_mobile(remaining_mobile):
        print("移动端无剩余搜索次数，跳过移动端流程")
        return
    driver = init_mobile_edge_appium(headless_flag)
    try:
        _ensure_logged_in(driver, username, password, tag="移动端")
        gohome(driver)
        goSearch(driver)
        _ensure_bing_account(driver, username, tag="移动端")
        keyword_list = _ensure_keywords(
            getBaiduTrends(),
            getZhihuTrends()
        )
        _search_loop(driver, keyword_list, remaining_mobile, "Mobile", extra_sleep=True)
    finally:
        driver.quit()


def _should_run_mobile(remaining_mobile: int) -> bool:
    return remaining_mobile > 0


def _parse_headless_flag(argv: list[str]) -> str | None:
    if len(argv) < 2:
        return None
    flag = (argv[1] or "").strip().lower()
    if flag in ("headless", "--headless", "-h"):
        return argv[1]
    print(f"未识别参数：{argv[1]}，将以有界面模式运行（如需无头请传 headless）")
    return None


def main():
    print("启动！")
    argv = sys.argv
    s = _parse_headless_flag(argv)
    accounts = _load_accounts()
    for index, (username, password) in enumerate(accounts, 1):
        print(f"开始处理账号 {index}/{len(accounts)}：{username}")
        remaining_mobile = _run_desktop_flow(username, password, s)
        if _should_run_mobile(remaining_mobile):
            _run_mobile_flow(username, password, s, remaining_mobile)
        else:
            print("移动端无剩余搜索次数，跳过移动端搜索")
        #     领取积分



    print("所有账号处理完毕")


if __name__ == "__main__":
    main()
