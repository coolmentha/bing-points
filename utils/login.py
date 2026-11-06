"""Microsoft OAuth 登录流程"""


# 凭据选择页（"使用另一种方式登录"）上"使用密码"选项的候选定位器。
# 设备上存在 passkey/Windows Hello 时，login.live.com 会跳过邮箱输入，
# 直接落到该页，此时必须先点"使用密码"才能进入密码输入。
PASSWORD_TILE_SELECTORS = (
    '[aria-label="使用密码"]',
    '[aria-label="Use your password"]',
    'div[role="button"]:has-text("使用密码")',
    'div[role="button"]:has-text("Use your password")',
    'button:has-text("使用密码")',
    'button:has-text("Use your password")',
    "#idA_PWD_SwitchToPassword",
    "#idA_PWD_SwitchToCredPicker",
)

# "使用另一种方式登录"/"其他登录方式"的候选定位器。
ALT_LOGIN_SELECTORS = (
    "a:has-text('使用另一种方式登录')",
    "a:has-text('其他登录方式')",
    "a:has-text('Use another account')",
    "button:has-text('使用另一种方式登录')",
    "#idA_PWD_SwitchToCredPicker",
    "#otherOptions",
    "[role='link']:has-text('使用另一种方式登录')",
)


def _first_visible(page, selectors, timeout_ms: int = 0):
    """按顺序返回第一个可见可用的定位器，找不到返回 None"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if timeout_ms:
                loc.wait_for(state="visible", timeout=timeout_ms)
            if loc.count() > 0 and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def _click_first(page, selectors) -> bool:
    """点击第一个可见的候选元素，成功返回 True"""
    loc = _first_visible(page, selectors)
    if loc is None:
        return False
    try:
        loc.click()
        return True
    except Exception:
        return False


EMAIL_INPUT_SELECTORS = (
    "#usernameEntry",
    "input[name='loginfmt']",
    "input[type='email']",
)


def _wait_for_email_input(page, timeout_ms: int = 12000):
    """等邮箱框出现（登录页 JS 渲染需要时间，立即判断会误判为没有）"""
    for sel in EMAIL_INPUT_SELECTORS:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None


def do_login(page, username: str, password: str) -> bool:
    print(f"  登录账号: {username}")

    # 确保在登录页。部分网络环境下 Edge 对 login.live.com 的 HTTP/2 协商会
    # 间歇性失败（net::ERR_HTTP2_PROTOCOL_ERROR），所以这里重试而不是一次定生死。
    if "login.live.com" not in page.url.lower():
        for attempt in range(1, 4):
            try:
                page.goto("https://login.live.com/", timeout=30000, wait_until="domcontentloaded")
                if "chrome-error" not in page.url:
                    break
            except Exception as exc:
                print(f"  打开登录页失败(第{attempt}次): {str(exc).splitlines()[0][:80]}")
            page.wait_for_timeout(3000)
        page.wait_for_timeout(2000)

    email_input = _wait_for_email_input(page)

    # 邮箱框没出现：可能直接落在了凭据选择页（设备上有 passkey/Windows Hello）。
    # 此时点"使用密码"或"使用另一种方式登录"才能继续。
    if email_input is None:
        for attempt in range(1, 4):
            print(f"  未直接出现邮箱框，尝试切换到密码登录 (第{attempt}次)...")
            if _click_first(page, PASSWORD_TILE_SELECTORS) or _click_first(page, ALT_LOGIN_SELECTORS):
                page.wait_for_timeout(3000)
                email_input = _wait_for_email_input(page)
                if email_input is not None:
                    break
                # 可能出现"输入邮箱"选项
                if _click_first(page, ("a:has-text('使用其他帐户')", "a:has-text('Use another account')")):
                    page.wait_for_timeout(2000)
                    email_input = _wait_for_email_input(page)
                    if email_input is not None:
                        break
            else:
                page.wait_for_timeout(2000)

    if email_input is None:
        print("  ✗ 未找到邮箱输入框")
        print(f"    当前URL: {page.url[:120]}")
        return False

    email_input.click()
    email_input.fill("")
    email_input.type(username, delay=80)
    page.wait_for_timeout(300)

    # 等待"下一步"按钮可用
    page.wait_for_timeout(1500)
    for btn_sel in [
        "#idSIButton9",
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('下一步')",
        "input[value='下一步']",
        "a:has-text('下一步')",
        "[role='button']:has-text('下一步')",
    ]:
        next_btn = page.locator(btn_sel).first
        if next_btn.count() > 0 and next_btn.is_visible() and next_btn.is_enabled():
            next_btn.click()
            print("  已点击下一步")
            break
    else:
        # 回退：直接按回车
        print("  ⚠ 未找到下一步按钮，尝试回车")
        email_input.press("Enter")
    page.wait_for_timeout(3000)

    # 2. 等待密码页或安全验证页（循环处理）
    max_wait = 60
    waited = 0
    pwd_filled = False  # 防止死循环：密码只填一次
    while waited < max_wait:
        url = page.url.lower()

        # 已登录成功
        if "login.live.com" not in url and "login.microsoft" not in url:
            print("  登录完成")
            break

        # 密码输入框出现
        pwd = page.locator("input[name='passwd']").first
        if pwd.count() == 0 or not pwd.is_visible():
            pwd = page.locator("#i0118").first
        if pwd.count() == 0 or not pwd.is_visible():
            pwd = page.locator("input[type='password']").first
        if pwd.count() > 0 and pwd.is_visible():
            if pwd_filled:
                # 已填过密码，正在等待跳转，不再重复填写
                page.wait_for_timeout(2000)
                waited += 2
                continue
            pwd.click()
            pwd.fill("")
            pwd.type(password, delay=80)
            pwd_filled = True
            # 等待"登录"按钮可用
            page.wait_for_timeout(1500)

            for btn_sel in [
                "#idSIButton9",
                "input[type='submit']",
                "button[type='submit']",
                "button:has-text('登录')",
                "input[value='登录']",
                "button:has-text('下一步')",
                "[role='button']:has-text('登录')",
            ]:
                sign_btn = page.locator(btn_sel).first
                if sign_btn.count() > 0 and sign_btn.is_visible() and sign_btn.is_enabled():
                    sign_btn.click()
                    print("  已点击登录")
                    page.wait_for_timeout(3000)
                    # 等待跳转
                    try:
                        page.wait_for_url(lambda u: "login.live.com" not in u.lower() and "login.microsoft" not in u.lower(), timeout=25000)
                        break
                    except Exception:
                        pass
                    break
            else:
                # 回退：按回车
                print("  ⚠ 未找到登录按钮，尝试回车")
                pwd.press("Enter")
                page.wait_for_timeout(3000)
            continue

        # 切换登录方式（passkey失败后）
        switch_link = page.locator("#idA_PWD_SwitchToCredPicker").first
        if switch_link.count() > 0 and switch_link.is_visible():
            switch_link.click()
            page.wait_for_timeout(3000)
            continue

        alt_login = page.locator("a:has-text('使用另一种方式登录')").first
        if alt_login.count() > 0 and alt_login.is_visible():
            alt_login.click()
            page.wait_for_timeout(3000)
            continue

        # 选择"使用密码"tile
        use_pwd = page.locator('[aria-label="使用密码"]').first
        if use_pwd.count() > 0 and use_pwd.is_visible():
            use_pwd.click()
            page.wait_for_timeout(3000)
            continue

        # 取消按钮
        cancel_btn = page.locator("#idBtn_Back").first
        if cancel_btn.count() > 0 and cancel_btn.is_visible():
            cancel_btn.click()
            page.wait_for_timeout(2000)
            continue

        # 保持登录弹窗：点"否"
        for no_sel in [
            "input[value='否']",
            "#declineButton",
            "button:has-text('否')",
            "#idBtn_Back",
        ]:
            stay_no = page.locator(no_sel).first
            if stay_no.count() > 0 and stay_no.is_visible():
                stay_no.click()
                print("  已点击'否'（保持登录弹窗）")
                page.wait_for_timeout(2000)
                break

        page.wait_for_timeout(2000)
        waited += 2

        if waited >= 30:
            print(f"  等待登录超时({waited}s)，当前URL: {page.url[:100]}")

    # 3. 兜底处理保持登录弹窗
    page.wait_for_timeout(2000)
    for no_sel in [
        "input[value='否']",
        "#declineButton",
        "button:has-text('否')",
        "#idBtn_Back",
    ]:
        try:
            no_btn = page.locator(no_sel).first
            if no_btn.count() > 0 and no_btn.is_visible():
                no_btn.click()
                page.wait_for_timeout(2000)
                break
        except Exception:
            continue

    # 等待最终跳转
    try:
        page.wait_for_url(lambda u: "login.live.com" not in u.lower(), timeout=30000)
    except Exception:
        pass

    url_now = page.url.lower()
    print(f"  登录后URL: {url_now[:100]}")
    if "login.live.com" in url_now or "login.microsoft" in url_now:
        print("  ✗ 登录未成功，仍在登录页面（可能需要验证码/2FA/密码错误）")
        return False
    return True


LOGIN_URL = "https://login.live.com/"


def get_logged_in_identity(page) -> str | None:
    """读取当前 Rewards 账号邮箱（signInName 或账号块的 RSC 数据）。"""
    import re
    try:
        html = page.content()
    except Exception:
        return None

    # 旧版/部分页面直接输出 signInName。
    match = re.search(r'"signInName"\s*:\s*"([^"]+)"', html, re.I)
    if match and "@" in match.group(1):
        return match.group(1).strip().lower()

    # 新版 RSC 账号块里的 JSON 引号是转义形式：
    # \"className\":\"pii truncate\",\"children\":\"user@example.com\"
    match = re.search(
        r'\\"className\\":\\"pii truncate\\",\\"children\\":\\"'
        r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\\"',
        html,
        re.I,
    )
    if match:
        return match.group(1).strip().lower()

    # 已经渲染 MeControl 弹窗时，优先读明确的账号元素。
    try:
        identity = page.evaluate(r"""() => {
          const root = document.querySelector(
            '#mectrl_currentAccount_primary, [data-testid="account-email"]'
          );
          if (!root) return null;
          const text = root.textContent || root.getAttribute('aria-label') || '';
          const match = text.match(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
          return match ? match[0] : null;
        }""")
    except Exception:
        identity = None
    return identity.strip().lower() if isinstance(identity, str) and "@" in identity else None

def ensure_account(page, username: str, password: str) -> bool:
    """确认页面登录的是目标账号；无法读取身份时安全停止，不猜测。"""
    current = get_logged_in_identity(page)
    if current is None:
        print("  无法读取当前账号 signInName，安全停止，不擅自切换账号")
        return False
    if current == username.lower():
        return True
    print(f"  当前登录账号 {current} 与目标 {username} 不一致，切换账号")
    try:
        page.goto("https://login.live.com/logout.srf", timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception:
        pass
    if not do_login(page, username, password):
        return False
    verified = get_logged_in_identity(page)
    if verified != username.lower():
        print("  登录后仍无法确认目标账号，安全停止")
        return False
    return True
