"""Muse 登录自动化的纯逻辑与浏览器流程。"""

from __future__ import annotations

import csv
import html
import json
import re
import os
import subprocess
import base64
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


EMAIL_COLUMN = "邮箱"
OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SENDER_PATTERN = re.compile(r"meta|account|security|notification", re.I)
OTP_WAIT_SECONDS = 180
OTP_POLL_SECONDS = 3


class AutoLoginError(RuntimeError):
    """可安全显示给用户的自动登录错误。"""


def ensure_profile_available(profile_dir: str | Path) -> None:
    """检查是否已有 Edge 进程占用该账号 profile；无法检查时拒绝并发启动。"""
    if os.name != "nt":
        raise AutoLoginError("自动登录的 profile 占用检查仅支持 Windows。")
    escaped_path = str(Path(profile_dir).resolve()).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$target = '{escaped_path}'; "
        "try { $items = Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe'\" "
        "| Where-Object { $_.CommandLine -and "
        "$_.CommandLine.IndexOf($target, [StringComparison]::OrdinalIgnoreCase) -ge 0 }; "
        "if ($items) { exit 1 } else { exit 0 } } catch { exit 2 }"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AutoLoginError("无法确认 Edge profile 是否被占用；请关闭该账号的 Edge 后重试。") from None
    if result.returncode == 1:
        raise AutoLoginError("这个账号的 Edge profile 正在使用；请先关闭该账号的 Edge。")
    if result.returncode != 0:
        raise AutoLoginError("无法确认 Edge profile 是否被占用；请关闭该账号的 Edge 后重试。")


def load_mailboxes(csv_path: str | Path) -> list[str]:
    """只取 CSV 的邮箱列，不使用或返回其他字段。"""
    path = Path(csv_path).expanduser()
    if not path.is_file():
        raise AutoLoginError("找不到邮箱 CSV 文件，请检查设置中的路径。")

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            rows = csv.reader(source)
            headers = next(rows, None)
            if not headers or EMAIL_COLUMN not in headers:
                raise AutoLoginError(f"邮箱 CSV 缺少「{EMAIL_COLUMN}」列。")
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
    except AutoLoginError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise AutoLoginError("无法读取邮箱 CSV，请检查文件编码和格式。") from None

    if not result:
        raise AutoLoginError("邮箱 CSV 中没有可用邮箱。")
    return result


def load_mail_api_base(config_path: str | Path) -> str:
    """只提取外部项目配置中的 mail.apiBase。"""
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise AutoLoginError("找不到邮件服务配置文件，请检查设置中的路径。")
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            config = json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AutoLoginError("无法读取邮件服务配置文件。") from None

    mail_config = config.get("mail") if isinstance(config, dict) else None
    base = mail_config.get("apiBase") if isinstance(mail_config, dict) else None
    if not isinstance(base, str) or not base.strip():
        raise AutoLoginError("邮件服务配置中缺少 mail.apiBase。")
    base = base.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise AutoLoginError("mail.apiBase 必须是有效的 HTTPS 地址。")
    return base


def mailbox_url(api_base: str, email: str) -> str:
    """构造用户指定的 {mail.apiBase}/{URL 编码邮箱} 地址。"""
    return f"{api_base.rstrip('/')}/{quote(email, safe='')}"


def _parse_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _message_text(message: dict) -> str:
    parts = []
    for field in ("subject", "html_body", "body"):
        value = message.get(field)
        if isinstance(value, str):
            parts.append(value)
    text = html.unescape(" ".join(parts))
    return re.sub(r"<[^>]*>", " ", text)


def extract_new_otp(payload: object, after: datetime) -> str | None:
    """从 after 之后的最新邮件中取六位码；不返回邮件内容或其他字段。"""
    messages = payload.get("emails") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None

    cutoff = after.astimezone(timezone.utc) if after.tzinfo else after.replace(tzinfo=timezone.utc)
    dated_messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("from")
        if not isinstance(sender, str) or not SENDER_PATTERN.search(sender):
            continue
        received = _parse_date(message.get("date"))
        if received is not None and received > cutoff:
            dated_messages.append((received, message))

    for _, message in sorted(dated_messages, key=lambda item: item[0], reverse=True):
        match = OTP_PATTERN.search(_message_text(message))
        if match:
            return match.group(1)
    return None


def fetch_new_otp(api_base: str, email: str, after: datetime, timeout: float = 10) -> str | None:
    """读取邮件接口；错误中不包含 URL、邮箱、响应正文或验证码。"""
    request = Request(
        mailbox_url(api_base, email),
        headers={"Accept": "application/json", "User-Agent": "MuseChrome"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8-sig"))
    except HTTPError as exc:
        raise AutoLoginError(f"邮件接口返回 HTTP {exc.code}。") from None
    except (URLError, TimeoutError, OSError):
        raise AutoLoginError("邮件接口连接失败或超时。") from None
    except (UnicodeError, json.JSONDecodeError):
        raise AutoLoginError("邮件接口没有返回有效 JSON。") from None
    return extract_new_otp(payload, after)


def _is_visible(locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False


def _has_human_challenge(page) -> bool:
    challenge_frames = page.locator(
        'iframe[src*="captcha" i], iframe[title*="captcha" i], '
        '[data-sitekey], [id*="captcha" i], [class*="captcha" i]'
    )
    if _is_visible(challenge_frames):
        return True
    try:
        body = page.locator("body").inner_text(timeout=1500).casefold()
    except Exception:
        return False
    return any(
        marker in body
        for marker in ("verify you are human", "verify you're human", "人机验证", "请完成人机验证")
    )


def _is_authenticated(page) -> bool:
    if _find_email_input(page) is not None or _find_otp_input(page) is not None:
        return False
    signed_in_controls = page.locator(
        'a[href*="logout" i], button[aria-label*="sign out" i], '
        '[data-testid*="user-menu" i], [data-testid*="account-menu" i]'
    )
    if _is_visible(signed_in_controls):
        return True
    try:
        body = page.locator("body").inner_text(timeout=1500).casefold()
    except Exception:
        return False
    return any(
        marker in body
        for marker in ("sign out", "log out", "my account", "account settings", "退出登录", "我的账户")
    )


def _find_email_input(page):
    candidates = page.locator(
        'input[type="email"], input[autocomplete="username"], input[name*="email" i], '
        'input[placeholder*="email" i], '
        'input[aria-label*="email" i]'
    )
    if _is_visible(candidates):
        return candidates.first
    return None


def _find_otp_input(page):
    selectors = (
        'input[autocomplete="one-time-code"], input[name*="code" i], '
        'input[placeholder*="code" i], input[inputmode="numeric"]'
    )
    for selector in selectors:
        candidate = page.locator(selector)
        if _is_visible(candidate):
            return candidate.first
    return None


def _click_action(page, pattern: re.Pattern) -> bool:
    controls = page.locator('button, input[type="submit"], [role="button"]')
    try:
        count = min(controls.count(), 30)
    except Exception:
        return False
    for index in range(count):
        control = controls.nth(index)
        try:
            if not control.is_visible() or not control.is_enabled():
                continue
            label = " ".join(
                str(value or "")
                for value in (
                    control.inner_text(timeout=500),
                    control.get_attribute("aria-label"),
                    control.get_attribute("value"),
                )
            ).strip()
            if pattern.search(label) and not re.search(r"apple|google|microsoft|facebook", label, re.I):
                control.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


def _wait_for(predicate, timeout: float, cancelled) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            raise AutoLoginError("已取消自动登录。")
        if predicate():
            return True
        time.sleep(0.25)
    return False


def _poll_for_otp(api_base: str, email: str, after: datetime, cancelled, status) -> str:
    deadline = time.monotonic() + OTP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if cancelled():
            raise AutoLoginError("已取消自动登录。")
        otp = fetch_new_otp(api_base, email, after)
        if otp:
            return otp
        status("正在等待新验证码…")
        time.sleep(OTP_POLL_SECONDS)
    raise AutoLoginError("等待验证码超时；可以改用手动登录。")


def run_auto_login(
    profile_dir: str | Path,
    edge_path: str,
    launch_args: list[str],
    api_base: str,
    email: str,
    status,
    cancelled,
) -> None:
    """在指定 Edge profile 中完成邮箱验证码登录，不读取或导出浏览器 Cookie。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise AutoLoginError("自动登录组件未安装；请使用包含 Playwright 的构建版本。") from None

    context = None
    try:
        status("正在启动所选账号的 Edge 配置…")
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                executable_path=edge_path,
                channel="msedge",
                headless=False,
                args=launch_args,
                viewport=None,
            )
            page = context.pages[0] if context.pages else context.new_page()
            status("正在打开 Muse 登录页…")
            page.goto("https://muse.ai", wait_until="domcontentloaded", timeout=45000)
            if _has_human_challenge(page):
                raise AutoLoginError("页面要求人工安全验证；请切换到手动登录。")
            if _is_authenticated(page):
                status("这个 Edge 配置中已经有 Muse 登录态。")
                return

            email_input = _find_email_input(page)
            if email_input is None:
                _click_action(page, re.compile(r"sign\s*in|log\s*in|登录", re.I))
                _wait_for(lambda: _find_email_input(page) is not None, 20, cancelled)
                email_input = _find_email_input(page)
            if email_input is None:
                raise AutoLoginError("没有识别到邮箱登录表单；请切换到手动登录。")

            status("正在填写所选邮箱…")
            email_input.fill(email)
            if not _click_action(page, re.compile(r"continue|next|send.*(code|link)|verify|sign\s*in|log\s*in|继续", re.I)):
                email_input.press("Enter")
            requested_at = datetime.now(timezone.utc)

            otp_visible = _wait_for(lambda: _find_otp_input(page) is not None, 30, cancelled)
            if _has_human_challenge(page):
                raise AutoLoginError("页面要求人工安全验证；请切换到手动登录。")
            if not otp_visible:
                raise AutoLoginError("登录页没有进入验证码步骤；请切换到手动登录。")

            status("正在从邮件接口读取新验证码…")
            otp = _poll_for_otp(api_base, email, requested_at, cancelled, status)
            if _has_human_challenge(page):
                raise AutoLoginError("页面要求人工安全验证；请切换到手动登录。")

            status("正在提交验证码…")
            single_digit_inputs = page.locator('input[maxlength="1"]')
            try:
                digit_count = single_digit_inputs.count()
            except Exception:
                digit_count = 0
            if digit_count >= 6:
                for index, digit in enumerate(otp):
                    single_digit_inputs.nth(index).fill(digit)
            else:
                otp_input = _find_otp_input(page)
                if otp_input is None:
                    raise AutoLoginError("验证码输入框已消失；请切换到手动登录。")
                otp_input.fill(otp)

            if not _click_action(page, re.compile(r"verify|continue|submit|sign\s*in|log\s*in|确认|登录", re.I)):
                page.keyboard.press("Enter")
            _wait_for(lambda: _has_human_challenge(page) or _is_authenticated(page), 25, cancelled)
            if _has_human_challenge(page):
                raise AutoLoginError("页面要求人工安全验证；请切换到手动登录。")
            if not _is_authenticated(page):
                raise AutoLoginError("未能确认 Muse 登录成功；请在手动 Edge 中检查并继续。")
            status("登录步骤已完成，正在释放 Edge 配置…")
    except AutoLoginError:
        raise
    except Exception as exc:
        if exc.__class__.__name__ == "Error" and "playwright" in exc.__class__.__module__:
            raise AutoLoginError("Edge 自动化遇到错误；请确认 Edge 版本后改用手动登录。") from None
        raise AutoLoginError("自动登录过程中发生错误；可以改用手动登录。") from None
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
