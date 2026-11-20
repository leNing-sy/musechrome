"""Muse 邮箱辅助登录用的 CSV、配置和取件链接解析。"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit


EMAIL_COLUMN = "邮箱"
DEFAULT_MAIL_API_BASE = "https://mail.xiongmaodianjing.top"


class MailAssistError(RuntimeError):
    """可安全显示给用户的邮箱辅助登录错误。"""


def load_mailboxes(csv_path: str | Path) -> list[str]:
    """只取 CSV 的邮箱列，不使用或返回其他字段。"""
    path = Path(csv_path).expanduser()
    if not path.is_file():
        raise MailAssistError("找不到邮箱 CSV 文件，请检查设置中的路径。")

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = csv.reader(source)
            headers = next(rows, None)
            if not headers or EMAIL_COLUMN not in headers:
                raise MailAssistError(f"邮箱 CSV 缺少「{EMAIL_COLUMN}」列。")
            email_index = headers.index(EMAIL_COLUMN)
            result: list[str] = []
            seen: set[str] = set()
            for row in rows:
                if email_index >= len(row):
                    continue
                email = row[email_index].strip()
                if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                    continue
                key = email.casefold()
                if key not in seen:
                    seen.add(key)
                    result.append(email)
    except MailAssistError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise MailAssistError("无法读取邮箱 CSV，请检查文件编码和格式。") from None

    if not result:
        raise MailAssistError("邮箱 CSV 中没有可用邮箱。")
    return result


def load_mail_api_base(config_path: str | Path) -> str:
    """默认使用约定取件站点；仅在指定配置时读取 mail.apiBase。"""
    if not str(config_path).strip():
        return DEFAULT_MAIL_API_BASE
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise MailAssistError("找不到自定义取件配置文件。请重新选择，或清空该项使用默认取件地址。")
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            config = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise MailAssistError("无法读取邮件服务配置文件。") from None

    mail_config = config.get("mail") if isinstance(config, dict) else None
    base = mail_config.get("apiBase") if isinstance(mail_config, dict) else None
    if not isinstance(base, str) or not base.strip():
        raise MailAssistError("邮件服务配置中缺少 mail.apiBase。")
    base = base.strip().rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in base)
            and "\\" not in base
        )
        parsed.port  # 触发非法端口校验，避免点击取件时才失败。
    except ValueError:
        valid = False
    if not valid:
        raise MailAssistError("自定义取件配置的 mail.apiBase 必须是 HTTPS 地址，不能包含账号密码、查询参数或片段。")
    return base


def mailbox_url(api_base: str, email: str) -> str:
    """构造用户指定的 {mail.apiBase}/{URL 编码邮箱} 地址。"""
    return f"{api_base.rstrip('/')}/{quote(email, safe='')}"
