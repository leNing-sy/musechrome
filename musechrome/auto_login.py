"""Muse 邮箱辅助登录用的 CSV、配置和取件链接解析。"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import quote, urlsplit


EMAIL_COLUMN = "邮箱"
ADDRESS_COLUMN = "收件地址"
DEFAULT_MAIL_API_BASE = "https://mail.xiongmaodianjing.top"


class MailAssistError(RuntimeError):
    """可安全显示给用户的邮箱辅助登录错误。"""


def load_mail_entries(csv_path: str | Path) -> list[dict]:
    """读取邮箱和可选收件地址，忽略密码等其他字段；重复邮箱保留首行。"""
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
            address_index = headers.index(ADDRESS_COLUMN) if ADDRESS_COLUMN in headers else None
            result: list[dict] = []
            seen: set[str] = set()
            for row_number, row in enumerate(rows, start=2):
                if email_index >= len(row):
                    continue
                email = row[email_index].strip()
                if not email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                    continue
                key = email.casefold()
                if key not in seen:
                    address = row[address_index] if address_index is not None and address_index < len(row) else ""
                    try:
                        address = validate_mail_address(address)
                    except MailAssistError:
                        raise MailAssistError(f"邮箱 CSV 第 {row_number} 行的收件地址无效，请检查网址或 {{email}} 模板。") from None
                    seen.add(key)
                    result.append({"email": email, "address": address})
    except MailAssistError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise MailAssistError("无法读取邮箱 CSV，请检查文件编码和格式。") from None

    if not result:
        raise MailAssistError("邮箱 CSV 中没有可用邮箱。")
    return result


def load_mailboxes(csv_path: str | Path) -> list[str]:
    """兼容调用：仅返回邮箱列表。"""
    return [entry["email"] for entry in load_mail_entries(csv_path)]


def validate_mail_address(text: str) -> str:
    """固定网页或仅在 path/query/fragment 使用 {email} 的模板；空串表示仅复制。"""
    message = "收件地址必须是 HTTP/HTTPS 网页或含 {email} 的地址模板，不能含账号密码、控制字符或其他占位符。"
    if not isinstance(text, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text):
        raise MailAssistError(message)
    address = text.strip()
    if not address:
        return ""
    remaining = address.replace("{email}", "")
    if "{" in remaining or "}" in remaining or "\\" in address or any(char.isspace() for char in address):
        raise MailAssistError(message)
    try:
        parsed = urlsplit(address)
        valid = (
            parsed.scheme in ("http", "https")
            and bool(parsed.hostname)
            and "{" not in parsed.netloc
            and "}" not in parsed.netloc
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise MailAssistError(message)
    return address


def resolve_mail_url(address: str, email: str) -> str:
    """编码所选邮箱，生成固定网址、模板网址或仅复制时的空串。"""
    return validate_mail_address(address).replace("{email}", quote(email, safe=""))


def configured_mail_address(cfg: dict) -> str:
    """新配置直接使用收件地址；None 仅用于显式保留旧版取件配置。"""
    address = cfg.get("mail_address", "")
    if address is None:
        address = load_mail_api_base(cfg.get("mail_config_path", "")) + "/{email}"
    return validate_mail_address(address)


def load_mail_api_base(config_path: str | Path) -> str:
    """默认使用约定取件站点；仅在指定配置时读取 mail.apiBase。"""
    if not str(config_path).strip():
        return DEFAULT_MAIL_API_BASE
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise MailAssistError("找不到旧版自定义取件配置文件。请在设置中填写收件地址，或点击「仅复制邮箱」。")
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
