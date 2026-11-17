"""配置、账号与 Chrome 启动参数（纯逻辑，不依赖 Qt，可独立测试）。

数据目录：exe 旁边的 MuseChromeData/ 文件夹。
- MuseChromeData/config.json          全局设置
- MuseChromeData/accounts.json        账号列表 [{id, name}]
- MuseChromeData/chrome-profiles/<id> 每个账号独立的 Chrome 配置目录
  （Cookie、登录态、插件都在里面，账号之间互不串）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from . import DEFAULT_HOME_URL

DATA_DIR_NAME = "MuseChromeData"

DEFAULT_CONFIG = {
    "proxy_enabled": True,
    "proxy_type": "http",  # http | socks5 —— v2rayN 的 10808 两种都收
    "proxy_host": "127.0.0.1",
    "proxy_port": 10808,
    "home_url": DEFAULT_HOME_URL,
    "chrome_path": "",  # 空 = 自动找
    "lang_en": True,  # 启动参数 --lang=en-US
    "webrtc_protect": True,  # 禁止 WebRTC 走非代理 UDP，防真实 IP 泄漏
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
    d = profiles_dir() / acc_id
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    cfg["proxy_host"] = str(cfg["proxy_host"]).strip() or "127.0.0.1"
    cfg["chrome_path"] = str(cfg["chrome_path"]).strip()
    return cfg


def save_config(cfg: dict) -> None:
    _write_json(data_dir() / "config.json", cfg)


def load_accounts() -> list[dict]:
    saved = _read_json(data_dir() / "accounts.json", [])
    accounts = []
    if isinstance(saved, list):
        for item in saved:
            if isinstance(item, dict) and item.get("id") and item.get("name"):
                accounts.append({"id": str(item["id"]), "name": str(item["name"])})
    return accounts


def save_accounts(accounts: list[dict]) -> None:
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
    """删账号时连它的 Chrome 配置一起删（Chrome 开着时可能删不净，忽略残留）。"""
    shutil.rmtree(profiles_dir() / acc_id, ignore_errors=True)


def proxy_server_url(cfg: dict) -> str | None:
    if not cfg.get("proxy_enabled"):
        return None
    scheme = "socks5" if cfg.get("proxy_type") == "socks5" else "http"
    return f"{scheme}://{cfg['proxy_host']}:{cfg['proxy_port']}"


def ca_bundle_path() -> Path | None:
    """随软件打包的 CA 证书包位置（检查更新做 HTTPS 校验用）。

    打包后的 exe 不能依赖系统 Python 的证书路径（目标机器上往往不存在），
    所以把 Mozilla 根证书包跟软件一起带走。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "cacert.pem")
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "cacert.pem")
    for c in candidates:
        if c.is_file():
            return c
    return None


def parse_version(s: str) -> tuple[int, ...]:
    """把 'v1.2.0' / '1.2' 解析成可比较的整数元组。"""
    out = []
    for part in str(s).strip().lstrip("vV").split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def is_newer(remote: str, local: str) -> bool:
    """远端版本号是否比本地新（检查更新用）。"""
    return parse_version(remote) > parse_version(local)


def main_chrome_user_data() -> Path | None:
    """用户日常 Chrome 的数据目录（插件助手从这里读已装插件清单）。"""
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    p = Path(local) / "Google" / "Chrome" / "User Data"
    return p if p.is_dir() else None


def _resolve_ext_name(profile_dir: Path, ext_id: str) -> str:
    """读插件 manifest 拿显示名（处理 __MSG_xx__ 国际化名字）。"""
    ext_root = profile_dir / "Extensions" / ext_id
    try:
        versions = sorted(d for d in ext_root.iterdir() if d.is_dir())
    except OSError:
        return ext_id
    if not versions:
        return ext_id
    vdir = versions[-1]
    try:
        manifest = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return ext_id
    name = str(manifest.get("name") or "").strip()
    if name.startswith("__MSG_") and name.endswith("__"):
        key = name[6:-2]
        locales = [manifest.get("default_locale"), "en_US", "en", "zh_CN", "zh_TW"]
        for loc in locales:
            if not loc:
                continue
            try:
                msgs = json.loads(
                    (vdir / "_locales" / loc / "messages.json").read_text(encoding="utf-8")
                )
            except Exception:
                continue
            entry = msgs.get(key) or msgs.get(key.lower())
            if isinstance(entry, dict) and entry.get("message"):
                return str(entry["message"])
        return key
    return name or ext_id


def list_source_extensions() -> list[dict]:
    """扫描日常 Chrome 各配置的已装插件，返回 [{id, name}]（按名排序、去重）。"""
    ud = main_chrome_user_data()
    if ud is None:
        return []
    profiles = [ud / "Default"] + sorted(ud.glob("Profile *"))
    found: dict[str, dict] = {}
    for prof in profiles:
        sp = prof / "Secure Preferences"
        if not sp.is_file():
            continue
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            continue
        settings = ((data.get("extensions") or {}).get("settings")) or {}
        for ext_id, st in settings.items():
            if ext_id in found or not isinstance(st, dict):
                continue
            if not (prof / "Extensions" / ext_id).is_dir():
                continue  # 内置组件插件不在配置目录里，跳过
            found[ext_id] = {"id": ext_id, "name": _resolve_ext_name(prof, ext_id)}
    return sorted(found.values(), key=lambda e: e["name"].lower())


def store_url(ext_id: str) -> str:
    return f"https://chromewebstore.google.com/detail/{ext_id}"


def build_chrome_args(cfg: dict, profile_dir: Path, extra_urls: list[str] | None = None) -> list[str]:
    """组装启动 Chrome 的命令行参数（网址放最后）。"""
    args = [
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    url = proxy_server_url(cfg)
    if url:
        args.append(f"--proxy-server={url}")
    if cfg.get("webrtc_protect"):
        args.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    if cfg.get("lang_en"):
        args.append("--lang=en-US")
    args.append(str(cfg["home_url"]))
    for u in extra_urls or []:
        args.append(str(u))
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
