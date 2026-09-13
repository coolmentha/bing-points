"""账号配置读取"""
import re
from pathlib import Path

from utils import config


def load_accounts(path: Path | None = None) -> list[tuple[str, str]]:
    cfg = path or config.ACCOUNTS_PATH
    if not cfg.exists():
        raise RuntimeError(f"未找到账号配置文件：{cfg}，请创建并写入 user:password")
    raw = cfg.read_text(encoding="utf-8")
    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    accounts = []
    for part in re.split(r"[;,\n]+", raw):
        part = part.strip()
        if not part or ":" not in part:
            continue
        user, pwd = part.split(":", 1)
        user, pwd = user.strip(), pwd.strip()
        if user and pwd:
            accounts.append((user, pwd))
    if not accounts:
        raise RuntimeError(f"账号配置为空，请在 {cfg} 中填写 user:password")
    return accounts
