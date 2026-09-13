"""Microsoft OAuth 登录流程"""


def do_login(page, username: str, password: str) -> bool:
    print(f"  登录账号: {username}")

    # 确保在登录页
    if "login.live.com" not in page.url.lower():
        page.goto("https://login.live.com/", timeout=30000)
        page.wait_for_timeout(2000)

    # 1. 输入邮箱
    email_input = page.locator("#usernameEntry").first
    if email_input.count() == 0 or not email_input.is_visible():
        email_input = page.locator("input[name='loginfmt']").first
    if email_input.count() == 0 or not email_input.is_visible():
        email_input = page.locator("input[type='email']").first

    if email_input.count() == 0:
        print("  ✗ 未找到邮箱输入框")
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
