"""失败推送：把无人值守运行的结果通过邮件发出去。

设计要点：
- 只在**失败**时推送（成功打扰没有意义）。
- 推送本身绝不能影响主流程：任何异常都被吞掉并写日志，
  否则"通知失败"会把一次本来成功的运行变成失败。
- 未配置 SMTP 时静默跳过，视为功能未启用。
"""
import os
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from utils.config import ENV_PATH

# main.py 返回的退出码 → 人类可读的失败原因
EXIT_CODE_REASONS = {
    2: "启动失败（浏览器 profile 被占用 / 临时目录不可写 / 会话0拿不到窗口站）",
    3: "全部账号处理失败（通常是登录态失效或网络问题）",
    4: "读取账号配置失败（accounts.txt 缺失或格式错误）",
    5: "检测到微软验证页面或验证码，已停止运行",
}

# 环境变量名，集中在这里便于文档与测试引用
ENV_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_TO",
    "SMTP_FROM",
    "SMTP_SSL",
)


def _load_env() -> None:
    """把项目根目录的 .env 读进环境变量（缺失或没装 dotenv 都跳过）"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except Exception:
        # 没装 python-dotenv 时退化为只读真实环境变量
        pass


def load_smtp_config(env: dict | None = None) -> dict | None:
    """读取 SMTP 配置；未配置（缺 host 或 to）返回 None 表示不推送"""
    if env is None:
        _load_env()
        env = os.environ

    host = (env.get("SMTP_HOST") or "").strip()
    to = (env.get("SMTP_TO") or "").strip()
    if not host or not to:
        return None

    user = (env.get("SMTP_USER") or "").strip()
    return {
        "host": host,
        "port": int((env.get("SMTP_PORT") or "465").strip() or 465),
        "user": user,
        "password": env.get("SMTP_PASSWORD") or "",
        # 收件人支持用逗号/分号分隔多个
        "to": [addr.strip() for addr in to.replace(";", ",").split(",") if addr.strip()],
        "sender": (env.get("SMTP_FROM") or user or to).strip(),
        # 465 是隐式 SSL；587 走 STARTTLS。默认 1，可用 SMTP_SSL=0 关闭
        "use_ssl": (env.get("SMTP_SSL") or "1").strip() not in ("0", "false", "False", ""),
    }


def build_subject(exit_code: int, account_summary: str = "") -> str:
    reason = EXIT_CODE_REASONS.get(exit_code, "未知错误")
    subject = f"[BingPoints] 运行失败 - {reason}"
    if account_summary:
        subject += f"（{account_summary}）"
    return subject


def build_body(exit_code: int, log_text: str = "", tail_lines: int = 40) -> str:
    """正文：结论 + 退出码 + 日志尾部（排查主要靠这段）"""
    reason = EXIT_CODE_REASONS.get(exit_code, "未知错误")
    lines = [
        "BingPoints 无人值守运行失败。",
        "",
        f"退出码：{exit_code}",
        f"原因：{reason}",
        "",
        "---- 日志尾部 ----",
    ]
    tail = [ln for ln in log_text.strip().splitlines() if ln.strip()]
    if tail:
        lines.extend(tail[-tail_lines:])
    else:
        lines.append("（未取到日志内容）")
    lines += [
        "",
        "---- 排查建议 ----",
        "- 完整日志见项目 logs 目录下最新的 run_*.log",
        "- 看日志里的“会话号”：0=服务会话(无窗口站)，非0=用户桌面会话",
        "- 登录失败多为登录态失效：检查 tmp_playwright_profile 是否被删除",
    ]
    return "\n".join(lines)


def send_mail(cfg: dict, subject: str, body: str, timeout: int = 20) -> None:
    """发送纯文本邮件；失败抛异常，由调用方决定如何处理"""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("BingPoints", "utf-8")), cfg["sender"]))
    msg["To"] = ", ".join(cfg["to"])

    if cfg["use_ssl"]:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=context) as smtp:
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["sender"], cfg["to"], msg.as_string())
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout) as smtp:
            smtp.ehlo()
            try:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            except smtplib.SMTPException:
                # 服务器不支持 STARTTLS 时按明文继续（自建内网 SMTP 常见）
                pass
            if cfg["user"]:
                smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["sender"], cfg["to"], msg.as_string())


def read_log_tail(log_path=None, tail_lines: int = 40) -> str:
    """读取日志尾部；文件不存在/读不了都返回空串（不能因为读日志而报错）"""
    if log_path is None:
        return ""
    try:
        from pathlib import Path

        path = Path(log_path)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(text.splitlines()[-tail_lines:])
    except Exception:
        return ""


def notify_failure(exit_code: int, log_text: str = "", logger=print) -> bool:
    """失败时推送通知。

    返回是否真的发出了邮件。任何异常都只记日志、返回 False，
    绝不向上抛——通知失败不能连带把运行结果改坏。
    """
    if exit_code == 0:
        return False  # 只推失败

    try:
        cfg = load_smtp_config()
    except Exception as exc:
        logger(f"  [推送] 读取 SMTP 配置失败，跳过推送: {exc}")
        return False

    if cfg is None:
        logger("  [推送] 未配置 SMTP（.env 缺 SMTP_HOST/SMTP_TO），跳过失败推送")
        return False

    subject = build_subject(exit_code)
    body = build_body(exit_code, log_text)
    try:
        send_mail(cfg, subject, body)
    except Exception as exc:
        logger(f"  [推送] 发送失败（不影响本次运行结果）: {type(exc).__name__}: {exc}")
        return False

    logger(f"  [推送] 已发送失败通知到 {', '.join(cfg['to'])}")
    return True


def _selftest() -> int:
    """CLI 自检：`python -m utils.notify` 发一封测试邮件"""
    cfg = load_smtp_config()
    if cfg is None:
        print("未配置 SMTP，推送功能处于关闭状态。")
        print(f"请在 {ENV_PATH} 中填写 SMTP_HOST / SMTP_TO（可参考 .env.example）")
        return 1

    print(f"SMTP    : {cfg['host']}:{cfg['port']} (SSL={cfg['use_ssl']})")
    print(f"发件人  : {cfg['sender']}")
    print(f"收件人  : {', '.join(cfg['to'])}")
    try:
        send_mail(cfg, "[BingPoints] 推送测试", "这是一封测试邮件。\n收到即表示失败推送配置正确。")
    except Exception as exc:
        print(f"发送失败: {type(exc).__name__}: {exc}")
        print("常见原因：密码填了登录密码而非授权码；端口/SSL 组合不对；网络或代理拦截。")
        return 1
    print("发送成功，请检查收件箱（可能在垃圾邮件里）。")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
