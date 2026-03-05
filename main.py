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
TRENDS_CACHE_FAILURE_TTL = 600
NAVIGATION_TIMEOUT = 20
DASHBOARD_WAIT_TIMEOUT = 15
ACCOUNTS_PATH = Path(__file__).with_name("accounts.txt")
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
        _safe_get(driver, "https://rewards.bing.com/?ref=rewardspanel")
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


def daily_set(driver):
    gohome(driver)
    dashboard = getDashboardData(driver)
    data = dashboard.get("dailySetPromotions", {})
    key = _pick_daily_set_key(data)
    if not key:
        print("未获取到每日任务列表，跳过每日任务")
        return
    if key != datetime.now().strftime("%m/%d/%Y"):
        print(f"未找到今日任务键，回退使用 {key}")
    for index, item in enumerate(data.get(key, []), 1):
        if item.get("attributes", {}).get("state") == "Complete":
            continue
        openDailySetActivity(driver, index)
        time.sleep(random.randint(3, 5))
    gohome(driver)
    print("完成每日任务")


def getDashboardData(driver) -> dict:
    def _read_dashboard(d):
        try:
            return d.execute_script("return (typeof dashboard !== 'undefined') ? dashboard : null")
        except Exception:
            return None

    try:
        data = WebDriverWait(driver, DASHBOARD_WAIT_TIMEOUT).until(lambda d: _read_dashboard(d))
    except Exception as e:
        print(f"读取 dashboard 失败：{e}")
        return {}
    return data or {}


def openDailySetActivity(driver, cardId: int):
    for attempt in range(3):
        try:
            previous_handles = set(driver.window_handles)
            element = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (
                        By.XPATH,
                        f'//*[@id="daily-sets"]/mee-card-group[1]/div/mee-card[{cardId}]/div/card-content/mee-rewards-daily-set-item-content/div/a',
                    )
                )
            )
            element.click()
            switchToNewTab(driver, 8, previous_handles=previous_handles)
            closeCurrentTab(driver)
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
        _safe_get(driver, "https://cn.bing.com/")
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


def getRemainingSearches(driver):
    # dashboard = getDashboardData(driver)
    # if not dashboard:
    #     return 0, 0
    # user_status = dashboard.get("userStatus", {})
    # counters = user_status.get("counters", {})
    # pcSearch = counters.get("pcSearch") or []
    # remainingDesktop = 0
    # if pcSearch:
    #     progressDesktop = sum(item.get("pointProgress", 0) for item in pcSearch)
    #     targetDesktop = sum(item.get("pointProgressMax", 0) for item in pcSearch)
    #     search_points = _infer_search_points(targetDesktop)
    #     remainingDesktop = max(0, int((targetDesktop - progressDesktop) / search_points))
    #
    # remainingMobile = 0
    # level_info = user_status.get("levelInfo", {})
    # mobileSearch = counters.get("mobileSearch") or []
    # if level_info.get("activeLevel") != "Level1" and mobileSearch:
    #     progressMobile = sum(item.get("pointProgress", 0) for item in mobileSearch)
    #     targetMobile = sum(item.get("pointProgressMax", 0) for item in mobileSearch)
    #     search_points = _infer_search_points(targetMobile)
    #     remainingMobile = max(0, int((targetMobile - progressMobile) / search_points))
    return random.randint(30,40), random.randint(30,40)


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
    for _ in tqdm(range(int(searches/3)), desc=f"{tag} bing searches", unit="search"):
        keyword = next(cycle)
        bing_search(driver, keyword)
        if extra_sleep:
            time.sleep(random.randint(2, 4))
        _maybe_take_break(tag)


def receive_points(driver):
    gohome(driver)




def _run_desktop_flow(username: str, password: str, headless_flag: str | None) -> int:
    driver = init_browser(headless_flag)
    try:
        _ensure_logged_in(driver, username, password, tag="PC")
        gohome(driver)
        time.sleep(random.randint(2, 4))
        daily_set(driver)
        remaining_desktop, remaining_mobile = getRemainingSearches(driver)
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
