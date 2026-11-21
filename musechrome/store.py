"""配置、账号与 Chrome 启动参数（纯逻辑，不依赖 Qt，可独立测试）。

数据目录：exe 旁边的 MuseChromeData/ 文件夹。
- MuseChromeData/config.json          全局设置
- MuseChromeData/accounts.json        账号列表 [{id, name, browser}]
- MuseChromeData/chrome-profiles/<id> 每个账号独立的 Chrome 配置目录
- MuseChromeData/edge-profiles/<id>   每个账号独立的 Edge 配置目录（与 Chrome 分开）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from . import DEFAULT_HOME_URL

DATA_DIR_NAME = "MuseChromeData"
ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
WINDOWS_RESERVED_IDS = {"CON", "PRN", "AUX", "NUL",
                        *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}


class AccountDataError(ValueError):
    """Account metadata is invalid; preserve the original data for recovery."""


DEFAULT_CONFIG = {
    "proxy_enabled": True,
    "proxy_mode": "local",  # local 本地代理 / system 跟随系统代理 / off 直连（TUN）
    "proxy_type": "http",  # http | socks5 —— v2rayN 的 10808 两种都收
    "proxy_host": "127.0.0.1",
    "proxy_port": 10808,
    "home_url": DEFAULT_HOME_URL,
    "chrome_path": "",  # 空 = 自动找
    "edge_path": "",  # 空 = 自动找
    "mail_accounts_csv": "",  # 邮箱辅助登录列表（邮箱、可选收件地址列）
    "mail_config_path": "",  # 仅兼容旧版 mail.apiBase 配置文件
    "mail_address": "",  # 新安装仅复制邮箱；None 保留旧版收件配置
    "mail_open_target": "account",  # account 当前账号浏览器 / system 系统默认浏览器
    "lang_en": True,  # 启动参数 --lang=en-US
    "webrtc_protect": True,  # 禁止 WebRTC 走非代理 UDP，防真实 IP 泄漏
    "theme": "light",  # light 浅色 / dark 深色（下次启动生效）
    "win_geo": None,  # [x, y, w, h] 外观切换进程重启时把窗口放回原位用
}


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = base_dir() / DATA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def profiles_dir() -> Path:
    d = data_dir() / "chrome-profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def profile_dir_for(acc_id: str) -> Path:
    d = _profile_path(profiles_dir(), acc_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def edge_profiles_dir() -> Path:
    d = data_dir() / "edge-profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def edge_profile_dir_for(acc_id: str) -> Path:
    d = _profile_path(edge_profiles_dir(), acc_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(root: Path, acc_id: str) -> Path:
    _validate_account_id(acc_id)
    expected_root = data_dir().resolve() / root.name
    root = root.resolve()
    if root != expected_root or root.name not in ("chrome-profiles", "edge-profiles"):
        raise AccountDataError("浏览器数据根目录越出数据目录，已阻止操作。")
    target = (root / acc_id).resolve()
    if target.parent != root or target != root / acc_id:
        raise AccountDataError("账号浏览器目录越出数据目录，已阻止操作。")
    return target


def _validate_account_id(acc_id: str) -> None:
    if (not isinstance(acc_id, str) or not ACCOUNT_ID_RE.fullmatch(acc_id)
            or acc_id.upper() in WINDOWS_RESERVED_IDS):
        raise AccountDataError("账号 ID 格式无效，已阻止访问浏览器数据目录。")


def _validate_accounts(accounts: list[dict]) -> None:
    if not isinstance(accounts, list):
        raise AccountDataError("账号列表格式无效。")
    seen_ids = set()
    for item in accounts:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip()):
            raise AccountDataError("账号列表包含无效记录。")
        acc_id = item.get("id")
        _validate_account_id(acc_id)
        # Windows paths are case-insensitive; portable backups must not alias.
        key = acc_id.casefold()
        if key in seen_ids:
            raise AccountDataError("账号 ID 重复，已阻止访问共享的浏览器数据目录。")
        seen_ids.add(key)


def profile_dir_for_browser(acc_id: str, browser: str) -> Path:
    """按账号选的浏览器给配置目录：Chrome 与 Edge 各用各的，互不干扰。"""
    if browser == "edge":
        return edge_profile_dir_for(acc_id)
    return profile_dir_for(acc_id)


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    saved = _read_json(data_dir() / "config.json", {})
    if isinstance(saved, dict):
        cfg.update({k: saved[k] for k in DEFAULT_CONFIG if k in saved})
    try:
        cfg["proxy_port"] = int(cfg["proxy_port"])
        if not (1 <= cfg["proxy_port"] <= 65535):
            raise ValueError
    except Exception:
        cfg["proxy_port"] = DEFAULT_CONFIG["proxy_port"]
    if cfg["proxy_type"] not in ("http", "socks5"):
        cfg["proxy_type"] = "http"
    if not str(cfg["home_url"]).startswith(("http://", "https://")):
        cfg["home_url"] = DEFAULT_HOME_URL
    for key in ("proxy_enabled", "lang_en", "webrtc_protect"):
        cfg[key] = bool(cfg[key])
    # 代理模式：老配置没有这个字段时，按原来的开关推导（开=本地代理，关=直连）
    # 注意要看「存盘文件里有没有」而不是合并后的值——默认值里有 local，会把推导挡住
    if isinstance(saved, dict) and saved.get("proxy_mode") in ("local", "system", "off"):
        cfg["proxy_mode"] = saved["proxy_mode"]
    else:
        cfg["proxy_mode"] = "local" if cfg["proxy_enabled"] else "off"
    cfg["proxy_host"] = str(cfg["proxy_host"]).strip() or "127.0.0.1"
    cfg["chrome_path"] = str(cfg["chrome_path"]).strip()
    cfg["edge_path"] = str(cfg["edge_path"]).strip()
    cfg["mail_accounts_csv"] = str(cfg["mail_accounts_csv"] or "").strip()
    cfg["mail_config_path"] = str(cfg["mail_config_path"] or "").strip()
    # 旧版已经选择 CSV 时保留原收件方式；仅记录兼容标识，不读取外部配置。
    legacy_mail = isinstance(saved, dict) and bool(cfg["mail_accounts_csv"])
    if legacy_mail and "mail_address" not in saved:
        cfg["mail_address"] = None
    if legacy_mail and "mail_open_target" not in saved:
        cfg["mail_open_target"] = "system"
    if cfg["mail_open_target"] not in ("account", "system"):
        cfg["mail_open_target"] = "account"
    return cfg


def save_config(cfg: dict) -> None:
    _write_json(data_dir() / "config.json", cfg)


def load_accounts() -> list[dict]:
    path = data_dir() / "accounts.json"
    if not path.exists():
        return []
    try:
        saved = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccountDataError(
            f"账号列表无法读取，原文件已保留：{path}。请先复制该文件备份后再修复。"
        ) from exc
    try:
        _validate_accounts(saved)
    except AccountDataError as exc:
        raise AccountDataError(f"{exc}原文件已保留：{path}。") from exc
    accounts = []
    for item in saved:
        acc_id = item["id"]
        browser = str(item.get("browser") or "chrome")
        if browser not in ("chrome", "edge"):
            browser = "chrome"
        proxy = item.get("proxy") or ""
        if isinstance(proxy, dict):
            try:
                port = int(proxy.get("port"))
                if not (1 <= port <= 65535):
                    raise ValueError
                proxy = {
                    "type": proxy.get("type") if proxy.get("type") in ("http", "socks5") else "http",
                    "host": str(proxy.get("host") or "127.0.0.1").strip() or "127.0.0.1",
                    "port": port,
                }
            except Exception:
                proxy = ""
        elif proxy != "direct":
            proxy = ""
        accounts.append({
            "id": acc_id,
            "name": str(item["name"]),
            "browser": browser,
            "proxy": proxy,
            # Retired UI metadata stays intact when editing account names.
            "note": item.get("note", ""),
            "color": item.get("color", ""),
        })
    return accounts


def save_accounts(accounts: list[dict]) -> None:
    _validate_accounts(accounts)
    _write_json(data_dir() / "accounts.json", accounts)


def new_account_id() -> str:
    return uuid.uuid4().hex[:12]


def next_account_name(accounts: list[dict]) -> str:
    n = len(accounts) + 1
    names = {a["name"] for a in accounts}
    while f"账号 {n}" in names:
        n += 1
    return f"账号 {n}"


def delete_profile_dir(acc_id: str) -> None:
    """删账号时连它的 Chrome 配置一起删（浏览器开着时可能删不净，忽略残留）。"""
    shutil.rmtree(_profile_path(profiles_dir(), acc_id), ignore_errors=True)


def delete_edge_profile_dir(acc_id: str) -> None:
    """同上，Edge 的配置目录。"""
    shutil.rmtree(_profile_path(edge_profiles_dir(), acc_id), ignore_errors=True)


def export_backup(dest: str, include_profiles: bool) -> None:
    """把数据打包成 zip 备份。

    轻量备份只有 config.json + accounts.json；完整备份再加上两个浏览器的
    配置目录（含登录态——等于钥匙，界面上会提醒用户自己收好）。
    """
    import zipfile

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("config.json", "accounts.json"):
            f = data_dir() / name
            if f.is_file():
                z.write(f, f"MuseChromeData/{name}")
        if include_profiles:
            for base in ("chrome-profiles", "edge-profiles"):
                root = data_dir() / base
                if root.is_dir():
                    for f in sorted(root.rglob("*")):
                        if f.is_file():
                            z.write(f, f"MuseChromeData/{base}/{f.relative_to(root)}")


def proxy_server_url(cfg: dict) -> str | None:
    """本地代理模式下返回代理地址；跟随系统/直连模式返回 None（不给浏览器传代理参数）。

    跟随系统模式不传参数，是因为 Chromium 默认就用 Windows 系统代理；
    直连模式面向开了 TUN 的用户，流量在网络层就走了。
    """
    if cfg.get("proxy_mode", "local") != "local":
        return None
    if cfg.get("proxy_enabled") is False:
        return None  # 老配置/手拼配置里开关关着就是关着
    scheme = "socks5" if cfg.get("proxy_type") == "socks5" else "http"
    return f"{scheme}://{cfg['proxy_host']}:{cfg['proxy_port']}"


def system_proxy_url() -> str | None:
    """读 Windows 系统代理设置，返回 http://host:port；没启用/读不到返回 None。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                return None
            if not enabled:
                return None
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except (ImportError, OSError):
        return None
    server = str(server or "").strip()
    if not server:
        return None
    if "=" in server:  # 形如 http=127.0.0.1:7890;https=127.0.0.1:7890
        parts = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return None
    if "://" not in server:
        server = "http://" + server
    return server


# 设置页的工具预设：点一下把类型/地址/端口填好
PROXY_PRESETS = [
    ("v2rayN", "http", "127.0.0.1", 10808),
    ("Clash", "http", "127.0.0.1", 7890),
    ("Shadowsocks", "socks5", "127.0.0.1", 1080),
]


def effective_cfg(cfg: dict, acc: dict) -> dict:
    """把账号自己的代理覆盖合并进全局设置，得到这个账号实际用的设置。

    acc["proxy"]："" 跟随全局 / "direct" 这个账号直连 /
    {"type","host","port"} 这个账号用自己的本地代理。
    """
    proxy = (acc or {}).get("proxy") or ""
    if proxy == "direct":
        return {**cfg, "proxy_mode": "off"}
    if isinstance(proxy, dict):
        return {
            **cfg,
            "proxy_mode": "local",
            "proxy_enabled": True,
            "proxy_type": proxy.get("type", "http"),
            "proxy_host": proxy.get("host", "127.0.0.1"),
            "proxy_port": proxy.get("port", 10808),
        }
    return cfg


def build_chrome_args(
    cfg: dict,
    profile_dir: Path,
    browser: str = "chrome",
    *,
    target_url: str | None = None,
) -> list[str]:
    """组装启动浏览器的命令行参数（网址放最后）。

    Chrome 与 Edge 同为 Chromium 内核，这套参数两边通用：
    独立配置目录、本地代理、WebRTC 防泄漏、英文语言标识。
    """
    args = [
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-signin",
        "--disable-sync",
    ]
    if browser == "edge":
        args.extend([
            "--disable-features=msImplicitSignin",
        ])
    url = proxy_server_url(cfg)
    if url:
        args.append(f"--proxy-server={url}")
    if cfg.get("webrtc_protect"):
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    if cfg.get("lang_en"):
        args.append("--lang=en-US")
    args.append(str(cfg["home_url"] if target_url is None else target_url))
    return args


def chrome_candidates() -> list[Path]:
    """常见的 Chrome 安装位置（按优先级）。"""
    out: list[Path] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for root in (pf, pf86, local):
        if root:
            out.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    # 注册表 App Paths（chrome.exe 的标准登记处）
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                ) as key:
                    val, _ = winreg.QueryValueEx(key, None)
                    if val:
                        out.insert(0, Path(val))
            except OSError:
                pass
    except ImportError:
        pass
    return out


def find_chrome(cfg: dict) -> str:
    """返回可用的 chrome.exe 路径，找不到返回空串。"""
    manual = str(cfg.get("chrome_path", "")).strip()
    if manual and Path(manual).is_file():
        return manual
    for cand in chrome_candidates():
        if cand.is_file():
            return str(cand)
    return ""


def edge_candidates() -> list[Path]:
    """常见的 Edge 安装位置（按优先级）。"""
    out: list[Path] = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for root in (pf, pf86, local):
        if root:
            out.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    # 注册表 App Paths（msedge.exe 的标准登记处）
    try:
        import winreg

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
                ) as key:
                    val, _ = winreg.QueryValueEx(key, None)
                    if val:
                        out.insert(0, Path(val))
            except OSError:
                pass
    except ImportError:
        pass
    return out


def find_edge(cfg: dict) -> str:
    """返回可用的 msedge.exe 路径，找不到返回空串。"""
    manual = str(cfg.get("edge_path", "")).strip()
    if manual and Path(manual).is_file():
        return manual
    for cand in edge_candidates():
        if cand.is_file():
            return str(cand)
    return ""


def find_browser_exe(cfg: dict, browser: str) -> str:
    """按账号选的浏览器找可执行文件：edge 找 Edge，其余找 Chrome。"""
    if browser == "edge":
        return find_edge(cfg)
    return find_chrome(cfg)
