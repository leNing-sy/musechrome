"""Muse 账号管理器主界面。

管理器本身不含浏览器：每个账号对应一个独立的浏览器配置目录，
账号可在卡片上选 Chrome 或 Edge，点「打开」就以该配置启动真浏览器
（带本地代理、WebRTC 防泄漏、英文语言标识），
登录一次后登录态保存在该账号自己的目录里，互不串号。
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QLibraryInfo,
    QPropertyAnimation,
    QTimer,
    Qt,
    QThread,
    QTranslator,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QBrush, QColor, QDesktopServices, QIcon, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressDialog,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, APP_NAME, REPO_URL
from . import store
from . import auto_login


def icon_path() -> Path | None:
    """图标文件位置：打包后在 _MEIPASS/assets，源码运行时在仓库 assets/。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "icon.png")
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "icon.png")
    for c in candidates:
        if c.is_file():
            return c
    return None


def app_icon() -> QIcon:
    p = icon_path()
    return QIcon(str(p)) if p else QIcon()


LATEST_API = (
    REPO_URL.replace("https://github.com/", "https://api.github.com/repos/")
    + "/releases/latest"
)

# 公告文件放在作者的 Gitee 仓库里（国内直连可读），作者在 Gitee 网页上
# 改这个文件即可更新公告，所有用户的软件打开时读它。
# 地址内置在软件里，用户侧无需任何设置。注意要用 gitee.com 的这条
# 规范地址（会自动跳转到带签名的 CDN 地址），签名地址会过期，不能内置。
NOTICE_URL = "https://gitee.com/xiaoyaodev/musechrome/raw/master/announcements.md"


def parse_notices(text: str) -> list[dict]:
    """把公告文本切成一条条公告。

    约定格式：公告之间用单独一行 --- 隔开；每条里「标题：」「日期：」
    开头的行是标题和日期，其余行是正文；最新的公告放最上面。
    以 <!-- 开头的说明块跳过。解析尽量宽容：缺标题就拿正文第一行顶上。
    """
    import hashlib

    # 统一换行：公告文件经 Gitee 网页编辑器保存后是 CRLF（Windows 换行），
    # 不先归一化，分隔行 --- 后面挂着 \r，正则切不开，两条公告会黏成一条
    # （v1.7.0 真出过这个 bug，用户截图为证）。
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # 先整段抹掉 <!-- --> 注释（文件头部的格式说明就写在注释里，
    # 它后面紧跟第一条公告，不能按块跳过，否则会连公告一起吃掉）
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    notices = []
    for block in re.split(r"(?m)^[ \t]*---[ \t]*$", text):
        block = block.strip()
        if not block or block.startswith("<!--"):
            continue
        title, date, body_lines, images = "", "", [], []
        for line in block.splitlines():
            s = line.strip()
            if s.startswith(("标题：", "标题:")) and not title:
                title = s[3:].strip()
            elif s.startswith(("日期：", "日期:")) and not date:
                date = s[3:].strip()
            else:
                url = _notice_image_url(s)
                if url:
                    images.append(url)
                else:
                    body_lines.append(line.rstrip())
        body = "\n".join(body_lines).strip()
        if not title and not body:
            continue
        if not title:
            lines = body.splitlines()
            title = lines[0][:30]
            body = "\n".join(lines[1:]).strip()
        notices.append({
            "title": title,
            "date": date,
            "body": body,
            "images": images,
            "id": hashlib.sha256(
                f"{title}\n{date}\n{body}".encode("utf-8")
            ).hexdigest()[:16],
        })
    return notices


_MD_IMG_RE = re.compile(r"^!\[[^\]]*\]\((https?://[^)\s]+)\)$")
_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp)(\?\S*)?$", re.I)


def _notice_image_url(s: str) -> str:
    """公告正文里单独一行的图片地址（三种写法都认），不是图片行就返回空字符串。

    写法：①「图片：https://…」 ② Markdown 的 ![](https://…) ③ 一行就是图片
    地址本身（带图片扩展名）。普通链接行不会被误吃。
    """
    m = _MD_IMG_RE.match(s)
    if m:
        return m.group(1)
    if s.startswith(("图片：", "图片:")):
        rest = s[3:].strip()
        if rest.startswith(("http://", "https://")) and " " not in rest:
            return rest
        return ""
    if re.fullmatch(r"https?://\S+", s) and _IMG_EXT_RE.search(s):
        return s
    return ""


class NoticeFetcher(QThread):
    """后台拉公告文本：走软件里配置的本地代理（关代理则直连），证书用打包的 CA 包。"""

    done = pyqtSignal(bool, str, str)  # 成功?, 公告原文, 失败原因

    def __init__(self, proxy_url: str | None, parent=None):
        super().__init__(parent)
        self.proxy_url = proxy_url

    def run(self):
        try:
            opener = _make_opener(self.proxy_url)
            req = urllib.request.Request(
                NOTICE_URL, headers={"User-Agent": "MuseChrome"}
            )
            with opener.open(req, timeout=10) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            self.done.emit(True, text, "")
        except Exception as exc:
            self.done.emit(False, "", _update_error_detail(exc))


def probe_http_proxy(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """用裸 socket 探一个 HTTP 代理活没活：让它去取 gstatic 的 204 测试页。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(
                b"GET http://www.gstatic.com/generate_204 HTTP/1.1\r\n"
                b"Host: www.gstatic.com\r\nConnection: close\r\n\r\n"
            )
            line = s.recv(200).decode("latin-1", errors="replace").splitlines()[0]
        if " 204" in line or " 200" in line:
            return True, line.strip()
        return False, f"代理有回应但不对：{line.strip()[:60]}"
    except Exception as exc:
        return False, str(exc)[:60]


def probe_socks5_proxy(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """探 SOCKS5 代理：免密握手后让它连 gstatic:80 并取 204 测试页。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x05\x01\x00")
            if s.recv(2) != b"\x05\x00":
                return False, "SOCKS5 握手被拒"
            target = b"www.gstatic.com"
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(target)]) + target + (80).to_bytes(2, "big"))
            reply = s.recv(10)
            if len(reply) < 2 or reply[1] != 0:
                return False, "SOCKS5 连接目标失败"
            s.sendall(b"GET /generate_204 HTTP/1.1\r\nHost: www.gstatic.com\r\nConnection: close\r\n\r\n")
            line = s.recv(200).decode("latin-1", errors="replace").splitlines()[0]
        if " 204" in line or " 200" in line:
            return True, line.strip()
        return False, f"代理有回应但不对：{line.strip()[:60]}"
    except Exception as exc:
        return False, str(exc)[:60]


def probe_proxy(ptype: str, host: str, port: int, timeout: float = 6.0) -> tuple[bool, str]:
    if ptype == "socks5":
        return probe_socks5_proxy(host, port, timeout=timeout)
    return probe_http_proxy(host, port, timeout=timeout)


def _timed_probe(ptype: str, host: str, port: int, timeout: float = 8) -> tuple[bool, str, int]:
    """探活并计时（毫秒）：自检页拿这个数当代理延迟展示。"""
    t0 = time.monotonic()
    ok, detail = probe_proxy(ptype, host, port, timeout=timeout)
    return ok, detail, int((time.monotonic() - t0) * 1000)


class HealthChecker(QThread):
    """环境自检：浏览器在不在、代理通不通、出口在哪，一项项报给自检页。"""

    item = pyqtSignal(str, str, str)  # 项目名, ok/warn/fail, 说明
    scan_found = pyqtSignal(list)  # 扫描到的可用代理端口
    all_done = pyqtSignal()

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg

    def run(self):
        cfg = self.cfg
        chrome = store.find_chrome(cfg)
        self.item.emit("谷歌 Chrome", "ok" if chrome else "warn",
                       chrome or "没找到（不用 Chrome 可忽略；设置页里可以手动指定路径）")
        edge = store.find_edge(cfg)
        self.item.emit("微软 Edge", "ok" if edge else "warn",
                       edge or "没找到（不用 Edge 可忽略；设置页里可以手动指定路径）")

        mode = cfg.get("proxy_mode", "local")
        exit_proxy: str | None = None  # 拿出口信息时走的代理（None = 直连）
        exit_skip = ""
        if mode == "local":
            ptype, host, port = cfg["proxy_type"], cfg["proxy_host"], int(cfg["proxy_port"])
            # 冷连接经远端节点建连经常超过 3 秒（真机踩过：第一次误报超时、
            # 再测一次就通），所以放宽到 8 秒并自动重试一次。
            ok, detail, ms = _timed_probe(ptype, host, port, timeout=8)
            if not ok:
                ok, detail, ms = _timed_probe(ptype, host, port, timeout=8)
            if ok:
                self.item.emit("本地代理", "ok", f"{ptype.upper()} {host}:{port} 可用")
                self.item.emit("代理延迟", "ok" if ms < 800 else "warn",
                               f"约 {ms} ms（经代理取测试页的往返时间，仅供参考，可能有误差）")
                exit_proxy = store.proxy_server_url(cfg)
                if ptype == "socks5":
                    exit_skip = "SOCKS5 代理下自检拿不到出口信息，可点下面按钮在浏览器里看"
            else:
                # 再换另一种协议试一遍：端口通着但协议设错时也是干等超时，
                # 探出来直接告诉用户改哪个，别只报一句不可用。
                alt = "socks5" if ptype == "http" else "http"
                alt_ok, _alt_detail = probe_proxy(alt, host, port, timeout=8)
                if alt_ok:
                    self.item.emit("本地代理", "warn",
                                   f"{host}:{port} 通的是 {alt.upper()}，不是 {ptype.upper()}——去设置里把代理类型改成 {alt.upper()} 再测")
                    if alt == "socks5":
                        exit_skip = "SOCKS5 代理下自检拿不到出口信息，可点下面按钮在浏览器里看"
                    else:
                        exit_proxy = f"http://{host}:{port}"
                else:
                    self.item.emit("本地代理", "fail",
                                   f"{ptype.upper()} {host}:{port} 不可用（{detail}）——先确认代理工具开着")
                    found = []
                    for p in store.COMMON_PROXY_PORTS:
                        if p == port:
                            continue
                        if probe_http_proxy("127.0.0.1", p, timeout=2.5)[0]:
                            found.append(p)
                    self.scan_found.emit(found)
                    self.item.emit("常见端口扫描", "ok" if found else "fail",
                                   ("在端口 " + "、".join(str(p) for p in found) + " 找到可用代理，点下面按钮一键改用")
                                   if found else "常见端口都没找到可用代理，你的工具可能用的冷门端口，去设置里手动填")
        elif mode == "system":
            sp = store.system_proxy_url()
            if not sp:
                self.item.emit("系统代理", "fail",
                               "没读到已启用的系统代理——在你的代理工具里打开「系统代理」开关，或改用本地代理模式")
            else:
                u = urllib.parse.urlparse(sp)
                t0 = time.monotonic()
                ok, detail = probe_http_proxy(u.hostname or "", u.port or 80, timeout=8)
                ms = int((time.monotonic() - t0) * 1000)
                self.item.emit("系统代理", "ok" if ok else "fail",
                               f"系统代理是 {sp}" + ("，可用" if ok else f"，但不可用（{detail}）"))
                if ok:
                    self.item.emit("代理延迟", "ok" if ms < 800 else "warn",
                                   f"约 {ms} ms（经代理取测试页的往返时间，仅供参考，可能有误差）")
                    exit_proxy = sp
        else:
            ok, detail = False, ""
            try:
                with socket.create_connection(("www.gstatic.com", 80), timeout=3) as s:
                    s.settimeout(3)
                    s.sendall(b"GET /generate_204 HTTP/1.1\r\nHost: www.gstatic.com\r\nConnection: close\r\n\r\n")
                    line = s.recv(200).decode("latin-1", errors="replace").splitlines()[0]
                ok = " 204" in line or " 200" in line
                detail = line.strip()
            except Exception as exc:
                detail = str(exc)[:60]
            self.item.emit("直连", "ok" if ok else "warn",
                           "直连能打开外网，TUN 应该在工作" if ok
                           else f"直连打不开外网（{detail}）——没开 TUN 的话请改用本地代理或跟随系统代理")

        if exit_skip:
            self.item.emit("出口信息", "warn", exit_skip)
        else:
            try:
                opener = _make_opener(exit_proxy)
                req = urllib.request.Request(
                    "http://ip-api.com/json/?fields=status,query,country,regionName,city,isp",
                    headers={"User-Agent": "MuseChrome"},
                )
                with opener.open(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                if data.get("status") == "success":
                    where = " ".join(x for x in [data.get("country", ""), data.get("regionName", ""), data.get("city", "")] if x)
                    self.item.emit("出口信息", "ok", f"{data.get('query', '')} · {where} · {data.get('isp', '')}")
                else:
                    self.item.emit("出口信息", "warn", "拿不到出口信息（不影响使用）")
            except Exception:
                self.item.emit("出口信息", "warn", "拿不到出口信息（不影响使用）")
        self.all_done.emit()


class HealthPage(QWidget):
    """环境自检页：逐项列出检查结果，扫描到可用端口时给一键改用按钮。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel("环境自检")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        intro = QLabel("打不开账号，先在这里查一遍：浏览器在不在、代理通不通、出口 IP 在哪。")
        intro.setStyleSheet(f"color: {C['dim']};")
        intro.setWordWrap(True)
        self.btn_start = QPushButton("开始检测")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_leak = QPushButton("在当前账号浏览器里打开指纹检测页")
        self.btn_back = QPushButton("返回账号列表")
        row = QHBoxLayout()
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_leak)
        row.addWidget(self.btn_back)
        row.addStretch(1)
        self._results_host = QWidget()
        self._results = QVBoxLayout(self._results_host)
        self._results.setContentsMargins(0, 0, 0, 0)
        self._results.setSpacing(8)
        self._actions_host = QWidget()
        self._actions = QHBoxLayout(self._actions_host)
        self._actions.setContentsMargins(0, 0, 0, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self._results_host)
        layout.addWidget(self._actions_host)
        layout.addStretch(1)
        self.on_apply_port = None  # MainWindow 挂进来

    def clear_results(self):
        while self._results.count():
            item = self._results.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self.clear_actions()

    def clear_actions(self):
        while self._actions.count():
            item = self._actions.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def add_result(self, name: str, status: str, text: str):
        color = {"ok": "#2e7d32", "warn": "#e8930c", "fail": "#c62828"}.get(status, "#8a7f6e")
        row = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        dot.setFixedWidth(20)
        lbl_name = QLabel(name)
        lbl_name.setStyleSheet(f"font-weight: bold; color: {C['text']};")
        lbl_name.setFixedWidth(110)
        lbl_text = QLabel(text)
        lbl_text.setWordWrap(True)
        lbl_text.setStyleSheet(f"color: {C['body']};")
        row.addWidget(dot)
        row.addWidget(lbl_name)
        row.addWidget(lbl_text, 1)
        host = QWidget()
        host.setLayout(row)
        self._results.addWidget(host)

    def set_scan_actions(self, ports: list):
        self.clear_actions()
        if not ports:
            return
        lbl = QLabel("一键改用：")
        lbl.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        self._actions.addWidget(lbl)
        for p in ports[:3]:
            btn = QPushButton(f"端口 {p}")
            btn.setObjectName("primaryBtn")
            btn.clicked.connect(lambda _c=False, port=p: self.on_apply_port and self.on_apply_port(port))
            self._actions.addWidget(btn)
        self._actions.addStretch(1)


def _update_error_detail(exc: Exception) -> str:
    """把检查更新的异常翻成一句人话，方便定位（证书/代理/超时/限流）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"GitHub 返回 {exc.code}"
    text = str(exc)
    low = text.lower()
    if "certificate" in low or "ssl" in low:
        return "证书校验失败"
    if "refused" in low or "10061" in text:
        return "连不上代理（先确认代理工具开着）"
    if "timed out" in low or "timeout" in low:
        return "连接超时"
    if "403" in text:
        return "GitHub 返回 403（可能被限流）"
    return text[:80] if text else "网络异常"


def _make_opener(proxy_url: str | None):
    handlers = []
    if proxy_url:
        handlers.append(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    ca = store.ca_bundle_path()
    if ca is not None:
        ctx = ssl.create_default_context(cafile=str(ca))
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _pick_exe_asset(release: dict) -> tuple[str, int, str]:
    """从 Release JSON 里挑 exe 资产，返回 (下载地址, 字节数, sha256摘要)。
    跳过名字带 setup 的安装包资产，只认便携版主程序。"""
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".exe") or "setup" in name.lower():
            continue
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1] if digest.startswith("sha256:") else ""
        return str(asset.get("browser_download_url") or ""), int(asset.get("size") or 0), sha
    return "", 0, ""


def build_updater_bat(src: Path, dst: Path) -> str:
    """生成替换脚本：等旧 exe 退出后把新文件覆盖过去，再启动新版，最后自删。"""
    lines = [
        "@echo off",
        "setlocal",
        f'set "SRC={src}"',
        f'set "DST={dst}"',
        "for /L %%i in (1,1,40) do (",
        '  copy /y "%SRC%" "%DST%" >nul 2>&1 && goto done',
        "  timeout /t 1 /nobreak >nul",
        ")",
        ":done",
        "timeout /t 2 /nobreak >nul",
        'start "" "%DST%"',
        'del "%SRC%" >nul 2>&1',
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


def build_rename_bat(new_exe: Path, old_exe: Path) -> str:
    """生成换名更新脚本：新版已下载到旧 exe 同目录（新文件名），
    等旧 exe 退出后删掉旧文件，稍等片刻再启动新版，最后自删。
    延时启动是给杀毒软件留出扫描新文件的时间，免得首次启动解压被挡。"""
    lines = [
        "@echo off",
        "setlocal",
        f'set "NEW={new_exe}"',
        f'set "OLD={old_exe}"',
        "timeout /t 2 /nobreak >nul",
        "for /L %%i in (1,1,40) do (",
        '  del "%OLD%" >nul 2>&1 && goto startnew',
        "  timeout /t 1 /nobreak >nul",
        ")",
        ":startnew",
        "timeout /t 1 /nobreak >nul",
        'start "" "%NEW%"',
        'del "%~f0"',
    ]
    return "\r\n".join(lines) + "\r\n"


class UpdateChecker(QThread):
    """后台查 GitHub 最新 Release；走软件里配置的本地代理（关代理则直连）。

    证书校验用随软件打包的 CA 包；先查 API，失败再退到网页版
    /releases/latest 的跳转地址解析版本号（不受 API 限流影响）。
    """

    # 成功?, 最新 tag, 发布页地址, 失败原因, exe 下载地址, exe 字节数, exe sha256
    done = pyqtSignal(bool, str, str, str, str, int, str)

    def __init__(self, proxy_url: str | None, parent=None):
        super().__init__(parent)
        self.proxy_url = proxy_url

    def run(self):
        opener = _make_opener(self.proxy_url)
        headers = {"User-Agent": "MuseChrome"}
        try:
            req = urllib.request.Request(LATEST_API, headers=headers)
            with opener.open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = str(data.get("tag_name") or "")
            if tag:
                asset_url, size, sha = _pick_exe_asset(data)
                self.done.emit(
                    True, tag, str(data.get("html_url") or ""), "", asset_url, size, sha
                )
                return
            raise RuntimeError("GitHub 没返回版本号")
        except Exception as api_exc:
            try:
                req = urllib.request.Request(
                    REPO_URL + "/releases/latest", headers=headers
                )
                with opener.open(req, timeout=10) as resp:
                    final_url = resp.geturl()
                m = re.search(r"/tag/([^/?#]+)", final_url)
                if m:
                    tag = m.group(1)
                    asset_url = f"{REPO_URL}/releases/download/{tag}/MuseChrome-{tag}.exe"
                    self.done.emit(True, tag, final_url, "", asset_url, 0, "")
                    return
                raise RuntimeError("没解析出版本号")
            except Exception:
                self.done.emit(False, "", "", _update_error_detail(api_exc), "", 0, "")


class DownloadThread(QThread):
    """后台下载新版 exe：走本地代理，带进度；下完校验大小和 sha256（有值才验）。"""

    progress = pyqtSignal(int)  # 0-100，不知总长时发 -1
    done = pyqtSignal(bool, str, str)  # 成功?, 保存路径, 失败原因

    def __init__(self, url: str, dest: Path, proxy_url: str | None,
                 expect_size: int = 0, expect_sha: str = "", parent=None):
        super().__init__(parent)
        self.url = url
        self.dest = dest
        self.proxy_url = proxy_url
        self.expect_size = expect_size
        self.expect_sha = expect_sha

    def run(self):
        import hashlib

        part = self.dest.with_suffix(self.dest.suffix + ".part")
        try:
            self.dest.parent.mkdir(parents=True, exist_ok=True)
            opener = _make_opener(self.proxy_url)
            req = urllib.request.Request(self.url, headers={"User-Agent": "MuseChrome"})
            h = hashlib.sha256()
            got = 0
            with opener.open(req, timeout=30) as resp, open(part, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0) or self.expect_size
                while True:
                    if self.isInterruptionRequested():
                        f.close()
                        part.unlink(missing_ok=True)
                        self.done.emit(False, "", "已取消")
                        return
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.progress.emit(int(got * 100 / total) if total else -1)
            if self.expect_size and got != self.expect_size:
                part.unlink(missing_ok=True)
                self.done.emit(False, "", f"下载不完整（{got}/{self.expect_size} 字节）")
                return
            if self.expect_sha and h.hexdigest().lower() != self.expect_sha.lower():
                part.unlink(missing_ok=True)
                self.done.emit(False, "", "下载文件校验失败，已丢弃")
                return
            part.replace(self.dest)
            self.done.emit(True, str(self.dest), "")
        except Exception as exc:
            part.unlink(missing_ok=True)
            self.done.emit(False, "", _update_error_detail(exc))

PALETTES = {
    "light": {
        "bg": "#f7f5f2", "surface": "#ffffff", "border": "#e7e0d5",
        "border2": "#ddd3c2", "text": "#2b2620", "body": "#4a4238",
        "dim": "#8a7f6e", "hover": "#f3eee5", "pressed": "#eadfc9",
        "selbg": "#fdeecd", "cardSel": "#fff8ec", "segbg": "#f1ebdd",
        "segborder": "#e2d9c6", "noticeBg": "#fdf0d5", "noticeText": "#7a5200",
    },
    "dark": {
        "bg": "#201d1a", "surface": "#2c2825", "border": "#4a443c",
        "border2": "#4a443c", "text": "#ece7df", "body": "#cfc8bc",
        "dim": "#a89e8f", "hover": "#38332d", "pressed": "#453f36",
        "selbg": "#4a3a1c", "cardSel": "#3a2f1a", "segbg": "#38332d",
        "segborder": "#4a443c", "noticeBg": "#3d341f", "noticeText": "#f0c569",
    },
}

# 当前主题的颜色表：建界面时读它，切换主题在下次启动时整体换一套
C = dict(PALETTES["light"])


def set_theme(name: str | None) -> None:
    global C
    C = dict(PALETTES.get(name or "light", PALETTES["light"]))


def app_style(theme: str | None) -> str:
    p = PALETTES.get(theme or "light", PALETTES["light"])
    return f"""
QMainWindow, QDialog {{ background: {p['bg']}; color: {p['text']}; }}
QListWidget {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 10px; }}
QListWidget::item {{ padding: 8px; color: {p['text']}; }}
QListWidget::item:selected {{ background: {p['selbg']}; color: {p['text']}; border-radius: 6px; }}
QPushButton {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border2']};
              border-radius: 8px; padding: 7px 12px; }}
QPushButton:hover {{ background: {p['hover']}; }}
QPushButton:pressed {{ background: {p['pressed']}; }}
QPushButton#primaryBtn {{ background: #e8930c; color: #ffffff; border: none;
              font-weight: bold; padding: 9px 18px; }}
QPushButton#primaryBtn:hover {{ background: #f7a623; }}
QPushButton#primaryBtn:pressed {{ background: #c77400; }}
QPushButton#darkBtn {{ background: #26221e; color: #ffffff; border: none; font-weight: bold; padding: 10px; }}
QPushButton#darkBtn:hover {{ background: #3a352f; }}
QPushButton#darkBtn:pressed {{ background: #e8930c; }}
QFrame#browserSeg {{ background: {p['segbg']}; border: 1px solid {p['segborder']}; border-radius: 10px; }}
QPushButton#segBtn {{ background: transparent; color: {p['dim']}; border: none; border-radius: 8px; padding: 5px 0; font-size: 12px; }}
QPushButton#segBtn:hover {{ color: {p['text']}; }}
QPushButton#segBtn:checked {{ background: #26221e; color: #ffffff; font-weight: bold; }}
QPushButton#navBtn {{ background: transparent; color: #d8d0c2; border: none;
              text-align: left; padding: 10px 12px; }}
QPushButton#navBtn:hover {{ background: #3a352f; }}
QPushButton#navBtn:pressed {{ background: #e8930c; color: #ffffff; }}
QPushButton#navBtnSel {{ background: #e8930c; color: #ffffff; border: none;
              text-align: left; padding: 10px 12px; font-weight: bold; }}
QPushButton#miniBtn {{ background: transparent; color: {p['dim']}; border: none; padding: 4px 6px; }}
QPushButton#miniBtn:hover {{ color: {p['text']}; background: {p['hover']}; }}
QLineEdit, QSpinBox, QComboBox {{ background: {p['surface']}; color: {p['text']};
              border: 1px solid {p['border2']}; border-radius: 8px; padding: 5px 8px; }}
QLabel {{ color: {p['text']}; }}
QFrame#side {{ background: #26221e; border: none; }}
QFrame#card {{ background: {p['surface']}; border: 1px solid {p['border']}; border-radius: 14px; }}
QFrame#cardSel {{ background: {p['cardSel']}; border: 2px solid #e8930c; border-radius: 14px; }}
QFrame#noticeBar {{ background: {p['noticeBg']}; border: 1px solid #e8930c; border-radius: 10px; }}
QScrollArea {{ border: none; background: transparent; }}
"""


def _paint_bg(widget: QWidget) -> None:
    """把容器底色刷成当前主题的页面底色。

    滚动区视口、卡片容器这类纯 QWidget 在 Windows 上默认拿系统调色板刷底，
    深色主题下会露出白块（真机实测）；不用系统色，直接写成主题色。
    """
    pal = widget.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(C["bg"]))
    widget.setPalette(pal)
    widget.setAutoFillBackground(True)

NOTE_TEXT = """使用说明：
• 点「打开」→ 弹出这个账号自己的浏览器窗口（第一次是全新浏览器，登录一次 Muse 即可，之后免登录）。
• 每个账号的登录、Cookie、插件都存在它自己的目录里，账号之间完全隔离。
• 每个账号可以在卡片上单独选浏览器：谷歌 Chrome 或微软 Edge，哪个账号用哪个互不影响；两个浏览器各用各的配置目录，第一次用某个浏览器打开某账号时要登录一次。
• 代理在打开时通过启动参数带给浏览器（本地代理默认 127.0.0.1:10808；也可以在设置里选跟随系统代理，或开了 TUN 时选直连）；打开前先确认你的代理工具开着。打不开先去左侧「环境自检」查一遍。
• 插件要每个账号装一次：点账号卡片上的「插件助手」，把你日常 Chrome 的插件清单读出来，逐个打开商店页点「添加」即可，装好就一直都在。
• 账号多了不用一个个点：右上角「打开全部」会把没打开的账号排队逐个打开；卡片上的「编辑」可以给账号写备注、选颜色标记，靠备注和颜色认号。
• 左侧边栏下方的月亮按钮能一键切换深浅外观；环境自检会顺带显示代理延迟。
• 同一个账号重复点「打开」不会开两个号，只会再给它开一个窗口。
• 代理/语言这类参数只在浏览器第一次打开时生效；改了设置，先把该账号的浏览器窗口全关掉再打开。"""


class SettingsPage(QWidget):
    """设置页：在主区里切换显示，不再是独立弹窗。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel("设置")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("本地代理（自己填地址端口）", "local")
        self.cmb_mode.addItem("跟随系统代理（Clash 等工具开系统代理时选这个）", "system")
        self.cmb_mode.addItem("直连（开了 TUN 模式时选这个）", "off")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.cmb_type = QComboBox()
        self.cmb_type.addItem("HTTP", "http")
        self.cmb_type.addItem("SOCKS5", "socks5")
        self.edt_host = QLineEdit()
        self.spn_port = QSpinBox()
        self.spn_port.setRange(1, 65535)
        # 常用工具预设：点一下填好地址端口，不用查自己用的工具是哪个端口
        preset_row = QHBoxLayout()
        preset_label = QLabel("常用工具：")
        preset_label.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        preset_row.addWidget(preset_label)
        for name, ptype, phost, pport in store.PROXY_PRESETS:
            btn = QPushButton(name)
            btn.clicked.connect(
                lambda _c=False, t=ptype, h=phost, p=pport: self._apply_preset(t, h, p)
            )
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        self.preset_box = QWidget()
        self.preset_box.setLayout(preset_row)
        self.edt_home = QLineEdit()
        self.edt_chrome = QLineEdit()
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_chrome)
        chrome_row = QHBoxLayout()
        chrome_row.addWidget(self.edt_chrome, 1)
        chrome_row.addWidget(btn_browse)
        self.edt_edge = QLineEdit()
        btn_browse_edge = QPushButton("浏览…")
        btn_browse_edge.clicked.connect(self._browse_edge)
        edge_row = QHBoxLayout()
        edge_row.addWidget(self.edt_edge, 1)
        edge_row.addWidget(btn_browse_edge)
        self.edt_mail_csv = QLineEdit()
        btn_browse_mail_csv = QPushButton("浏览…")
        btn_browse_mail_csv.clicked.connect(self._browse_mail_csv)
        mail_csv_row = QHBoxLayout()
        mail_csv_row.addWidget(self.edt_mail_csv, 1)
        mail_csv_row.addWidget(btn_browse_mail_csv)
        self.edt_mail_config = QLineEdit()
        btn_browse_mail_config = QPushButton("浏览…")
        btn_browse_mail_config.clicked.connect(self._browse_mail_config)
        mail_config_row = QHBoxLayout()
        mail_config_row.addWidget(self.edt_mail_config, 1)
        mail_config_row.addWidget(btn_browse_mail_config)
        self.chk_lang = QCheckBox("英文语言标识（启动时带 --lang=en-US，让浏览器语言与代理出口地区一致）")
        self.chk_webrtc = QCheckBox("WebRTC 防泄漏（禁止 WebRTC 走非代理 UDP，避免真实 IP 从 WebRTC 泄漏）")
        self.cmb_theme = QComboBox()
        self.cmb_theme.addItem("浅色", "light")
        self.cmb_theme.addItem("深色", "dark")

        form = QFormLayout()
        form.addRow("代理方式", self.cmb_mode)
        form.addRow("代理类型", self.cmb_type)
        form.addRow("代理地址", self.edt_host)
        form.addRow("代理端口", self.spn_port)
        form.addRow("", self.preset_box)
        form.addRow("主页地址", self.edt_home)
        form.addRow("Chrome 路径", chrome_row)
        form.addRow("Edge 路径", edge_row)
        form.addRow("邮箱 CSV", mail_csv_row)
        form.addRow("邮件服务 config.json", mail_config_row)
        form.addRow(self.chk_lang)
        form.addRow(self.chk_webrtc)
        form.addRow("外观", self.cmb_theme)

        note = QLabel(
            "Chrome / Edge 路径留空 = 自动查找；邮箱辅助入口只读取 CSV 的邮箱列和 config.json 的 mail.apiBase。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")

        self.btn_export = QPushButton("导出备份")
        self.btn_import = QPushButton("导入恢复")
        backup_note = QLabel("换电脑/重装时用：导出一个 zip，另一台导入即可。完整备份含登录态，别外传。")
        backup_note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        backup_row = QHBoxLayout()
        backup_row.addWidget(self.btn_export)
        backup_row.addWidget(self.btn_import)
        backup_row.addWidget(backup_note)
        backup_row.addStretch(1)

        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("primaryBtn")
        self.btn_back = QPushButton("返回账号列表")
        self.lbl_saved = QLabel("")
        self.lbl_saved.setStyleSheet("color: #2e7d32; font-weight: bold;")
        row = QHBoxLayout()
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_back)
        row.addWidget(self.lbl_saved)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addLayout(backup_row)
        layout.addLayout(row)
        layout.addStretch(1)

    def load(self, cfg: dict):
        mode = cfg.get("proxy_mode", "local")
        idx = self.cmb_mode.findData(mode)
        self.cmb_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_mode_changed()
        self.cmb_type.setCurrentIndex(1 if cfg["proxy_type"] == "socks5" else 0)
        self.edt_host.setText(str(cfg["proxy_host"]))
        self.spn_port.setValue(int(cfg["proxy_port"]))
        self.edt_home.setText(str(cfg["home_url"]))
        self.edt_chrome.setText(str(cfg["chrome_path"]))
        self.edt_edge.setText(str(cfg["edge_path"]))
        self.edt_mail_csv.setText(str(cfg.get("mail_accounts_csv", "")))
        self.edt_mail_config.setText(str(cfg.get("mail_config_path", "")))
        self.chk_lang.setChecked(bool(cfg["lang_en"]))
        self.chk_webrtc.setChecked(bool(cfg["webrtc_protect"]))
        tidx = self.cmb_theme.findData(cfg.get("theme", "light"))
        self.cmb_theme.setCurrentIndex(tidx if tidx >= 0 else 0)
        self.lbl_saved.setText("")

    def collect(self) -> dict:
        mode = self.cmb_mode.currentData()
        return dict(
            proxy_enabled=mode == "local",
            proxy_mode=mode,
            proxy_type=self.cmb_type.currentData(),
            proxy_host=self.edt_host.text().strip() or "127.0.0.1",
            proxy_port=self.spn_port.value(),
            home_url=self.edt_home.text().strip(),
            chrome_path=self.edt_chrome.text().strip(),
            edge_path=self.edt_edge.text().strip(),
            mail_accounts_csv=self.edt_mail_csv.text().strip(),
            mail_config_path=self.edt_mail_config.text().strip(),
            lang_en=self.chk_lang.isChecked(),
            webrtc_protect=self.chk_webrtc.isChecked(),
            theme=self.cmb_theme.currentData(),
        )

    def _on_mode_changed(self):
        local = self.cmb_mode.currentData() == "local"
        for w in (self.cmb_type, self.edt_host, self.spn_port, self.preset_box):
            w.setEnabled(local)

    def _apply_preset(self, ptype: str, host: str, port: int):
        self.cmb_type.setCurrentIndex(1 if ptype == "socks5" else 0)
        self.edt_host.setText(host)
        self.spn_port.setValue(port)

    def _browse_chrome(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 chrome.exe", self.edt_chrome.text() or r"C:\Program Files\Google\Chrome\Application",
            "chrome.exe (chrome.exe)",
        )
        if path:
            self.edt_chrome.setText(path)

    def _browse_edge(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 msedge.exe", self.edt_edge.text() or r"C:\Program Files (x86)\Microsoft\Edge\Application",
            "msedge.exe (msedge.exe)",
        )
        if path:
            self.edt_edge.setText(path)

    def _browse_mail_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择邮箱 CSV", self.edt_mail_csv.text(), "CSV 文件 (*.csv);;所有文件 (*)"
        )
        if path:
            self.edt_mail_csv.setText(path)

    def _browse_mail_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择邮件服务 config.json", self.edt_mail_config.text(),
            "JSON 文件 (*.json);;所有文件 (*)",
        )
        if path:
            self.edt_mail_config.setText(path)


class PluginPage(QWidget):
    """插件助手页：列出日常 Chrome 的插件，勾选后在账号 Chrome 里逐个打开商店页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel("插件助手")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        self.lbl_target = QLabel("")
        self.lbl_target.setStyleSheet(f"color: {C['dim']};")

        intro = QLabel(
            "下面是你日常 Chrome 里装的插件。勾好后点「打开商店页安装」，"
            "我会在这个账号选定的浏览器里把它们的商店页一个个打开，你逐页点「添加」就行。\n"
            "SwitchyOmega 已自动取消勾选：代理由管理器接管，装它反而可能抢代理设置。"
        )
        intro.setWordWrap(True)
        self.list = QListWidget()
        self.lbl_empty = QLabel(
            "没在你的日常 Chrome 里读到插件清单。\n"
            "（日常 Chrome 装在默认位置才读得到；也可以在账号的 Chrome 里直接去应用商店装。）"
        )
        self.lbl_empty.setWordWrap(True)
        self.btn_go = QPushButton("打开商店页安装")
        self.btn_go.setObjectName("primaryBtn")
        self.btn_back = QPushButton("返回账号列表")
        row = QHBoxLayout()
        row.addWidget(self.btn_go)
        row.addWidget(self.btn_back)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addWidget(self.lbl_target)
        layout.addWidget(intro)
        layout.addWidget(self.lbl_empty)
        layout.addWidget(self.list, 1)
        layout.addLayout(row)

    def refresh(self, account_name: str, exts: list[dict]):
        self.lbl_target.setText(f"给账号「{account_name}」安装插件：")
        self.list.clear()
        for ext in exts:
            item = QListWidgetItem(f"{ext['name']}")
            item.setForeground(QBrush(QColor(C["text"])))
            item.setData(Qt.ItemDataRole.UserRole, ext["id"])
            low = ext["name"].lower()
            skip = "switchyomega" in low or "zeroomega" in low
            item.setCheckState(
                Qt.CheckState.Unchecked if skip else Qt.CheckState.Checked
            )
            self.list.addItem(item)
        has = bool(exts)
        self.list.setVisible(has)
        self.lbl_empty.setVisible(not has)
        self.btn_go.setEnabled(has)

    def selected_ids(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return out


ABOUT_TEXT = """Muse 账号管理器

给每个 Muse 账号准备一个完全独立的浏览器配置目录，点「打开」就用你电脑上的真 Chrome 或 Edge 启动它（每个账号可以在卡片上单独选）：独立的登录状态、Cookie、插件和浏览数据，账号之间互不串号。

打开时自动带上本地代理（默认 127.0.0.1:10808）、WebRTC 防泄漏和英文语言标识，指纹就是原装浏览器本人。管理器本身不含浏览器、不联网、不收集任何数据；所有账号数据只存在软件旁边的 MuseChromeData 文件夹里。

「插件助手」可以读出你日常 Chrome 的插件清单，在账号的浏览器里逐个打开商店页，点「添加」即装好同款插件。"""

DISCLAIMER_TEXT = """1. 永久免费，认准官方渠道：本软件永久免费、开源发布（MIT 许可），官方版本不含病毒和恶意代码。请只从官方 GitHub 仓库的 Releases 页面下载；任何收费出售本软件的都是骗局，从非官方渠道下载的版本可能被篡改或捆绑恶意程序，由此造成的损失作者概不负责。

2. 责任限制：本软件按「现状」提供，不作任何明示或暗示的担保。因使用本软件而引致的任何意外、疏忽、合同纠纷、名誉损害、版权或知识产权侵犯及其所造成的损失，以及账号受限、数据丢失等后果，作者概不负责，亦不承担任何法律责任，由使用者自行承担。

3. 合规使用：请遵守你所在地区的法律法规，并遵守 Muse 等目标平台的服务条款。因违规使用本软件导致的任何法律纠纷或损失，由使用者自行承担。

4. 数据在本地，自行保管：所有账号数据只保存在你本机的 MuseChromeData 文件夹，作者不收集、不上传。请自行备份；导出的完整备份包含登录状态，等同账号钥匙，切勿外传。

5. 无官方关联：本软件是独立的第三方工具，名称中的「Muse」仅用于表明用途（管理 Muse 账号），与 Google、Microsoft 及 Muse 官方没有任何隶属、合作或授权关系；Chrome、Edge、Muse 等商标归各自权利人所有。"""


class AboutPage(QWidget):
    """关于软件页：在主区里切换显示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel(f"{APP_NAME} v{__version__}")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        head = QHBoxLayout()
        p = icon_path()
        if p:
            pix = QLabel()
            pix.setPixmap(QPixmap(str(p)).scaled(
                56, 56,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            head.addWidget(pix)
        head.addWidget(title, 1)

        body = QLabel(ABOUT_TEXT)
        body.setWordWrap(True)

        link = QLabel(f'GitHub 项目地址：<a href="{REPO_URL}" style="color:#c77400;">{REPO_URL}</a>')
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)

        disc_title = QLabel("免责声明")
        disc_title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {C['text']};")
        disc = QLabel(DISCLAIMER_TEXT)
        disc.setWordWrap(True)
        disc.setStyleSheet("font-size: 12px; color: #6b6259;")

        self.btn_back = QPushButton("返回账号列表")
        row = QHBoxLayout()
        row.addWidget(self.btn_back)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addLayout(head)
        layout.addWidget(body)
        layout.addWidget(link)
        layout.addWidget(disc_title)
        layout.addWidget(disc)
        layout.addLayout(row)
        layout.addStretch(1)


class NoticeImageFetcher(QThread):
    """后台把公告图片下载到本地缓存，下好一张报一张，页面随即补上。"""

    got = pyqtSignal(str)  # 图片地址（其本地缓存已就位）

    def __init__(self, urls: list[str], proxy_url: str | None, parent=None):
        super().__init__(parent)
        self.urls = urls
        self.proxy_url = proxy_url

    def run(self):
        for url in self.urls:
            path = store.notice_image_path(url)
            if path.exists() and path.stat().st_size > 0:
                continue
            try:
                opener = _make_opener(self.proxy_url)
                req = urllib.request.Request(url, headers={"User-Agent": "MuseChrome"})
                with opener.open(req, timeout=15) as resp:
                    data = resp.read()
                if not data or len(data) > 8_000_000:  # 空图、超大图都不收
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.got.emit(url)
            except Exception:
                continue


class NoticePage(QWidget):
    """公告页：内容来自仓库里的公告文件，拉不到时显示上次缓存。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proxy_url: str | None = None
        self._notices: list[dict] = []
        self._img_fetch: NoticeImageFetcher | None = None
        title = QLabel("公告")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
        head = QHBoxLayout()
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.lbl_status)

        self._host = QWidget()
        _paint_bg(self._host)
        self._box = QVBoxLayout(self._host)
        self._box.setContentsMargins(2, 2, 2, 2)
        self._box.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._host)
        _paint_bg(scroll.viewport())

        self.btn_back = QPushButton("返回账号列表")
        row = QHBoxLayout()
        row.addWidget(self.btn_back)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)
        layout.addLayout(head)
        layout.addWidget(scroll, 1)
        layout.addLayout(row)
        self.set_notices([], "")

    def set_proxy_url(self, url: str | None):
        self._proxy_url = url

    def set_notices(self, notices: list[dict], status: str):
        self._notices = notices
        self.lbl_status.setText(status)
        while self._box.count():
            item = self._box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)  # 只 deleteLater 时旧控件会残留在页面上（公告页真出过）
                w.deleteLater()
        if not notices:
            lbl = QLabel("暂无公告")
            lbl.setStyleSheet(f"font-size: 14px; color: {C['dim']};")
            self._box.addWidget(lbl)
        for n in notices:
            self._box.addWidget(self._make_card(n))
        if notices:
            end = QLabel("— 没有更多公告了 —")
            end.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
            end.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._box.addWidget(end)
        self._box.addStretch(1)
        # 公告图片：本地还没有的，后台下载，下好一张就重排一次补上
        missing = []
        for n in notices:
            for url in n.get("images") or []:
                p = store.notice_image_path(url)
                if not (p.exists() and p.stat().st_size > 0):
                    missing.append(url)
        if missing:
            if self._img_fetch is not None and self._img_fetch.isRunning():
                self._img_fetch.got.disconnect(self._on_image_ready)
            self._img_fetch = NoticeImageFetcher(missing, self._proxy_url, self)
            self._img_fetch.got.connect(self._on_image_ready)
            self._img_fetch.start()

    def _on_image_ready(self, _url: str):
        self.set_notices(self._notices, self.lbl_status.text())

    @staticmethod
    def _make_card(n: dict) -> QFrame:
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 10px; }}"
            "QLabel { border: none; background: transparent; }"
        )
        cl = QVBoxLayout(card)
        cl.setContentsMargins(18, 16, 18, 16)
        cl.setSpacing(8)
        ct = QLabel(n["title"])
        ct.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {C['text']};")
        ct.setWordWrap(True)
        cl.addWidget(ct)
        if n["date"]:
            cd = QLabel(n["date"])
            cd.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
            cl.addWidget(cd)
        if n["body"]:
            cb = QLabel(n["body"])
            cb.setWordWrap(True)
            cb.setStyleSheet(f"color: {C['body']};")
            cl.addWidget(cb)
        for url in n.get("images") or []:
            path = store.notice_image_path(url)
            lbl = QLabel()
            pix = QPixmap(str(path)) if path.exists() and path.stat().st_size > 0 else QPixmap()
            if not pix.isNull():
                if pix.width() > 560:
                    pix = pix.scaledToWidth(560, Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(pix)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            else:
                lbl.setText("图片加载中…")
                lbl.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(lbl)
        return card


class AccountCard(QFrame):
    """账号卡片：点卡片任意处即选中该账号。"""

    clicked = pyqtSignal(str)

    def __init__(self, acc_id: str, parent=None):
        super().__init__(parent)
        self._acc_id = acc_id

    def mousePressEvent(self, event):
        self.clicked.emit(self._acc_id)
        super().mousePressEvent(event)


class AccountProxyDialog(QDialog):
    """单个账号的代理设置：跟随全局 / 直连 / 用自己的代理。"""

    def __init__(self, acc: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"代理设置 · {acc['name']}")
        self.setMinimumWidth(380)
        self.rb_global = QRadioButton("跟随全局设置（大多数账号用这个）")
        self.rb_direct = QRadioButton("这个账号直连（不走代理）")
        self.rb_custom = QRadioButton("这个账号用自己的代理：")
        self.cmb_type = QComboBox()
        self.cmb_type.addItem("HTTP", "http")
        self.cmb_type.addItem("SOCKS5", "socks5")
        self.edt_host = QLineEdit()
        self.spn_port = QSpinBox()
        self.spn_port.setRange(1, 65535)
        form = QFormLayout()
        form.addRow("类型", self.cmb_type)
        form.addRow("地址", self.edt_host)
        form.addRow("端口", self.spn_port)
        note = QLabel("给某个账号单独指一个代理出口，几个账号就不必挤同一个 IP。\n改完后，把这个账号的浏览器关掉重开才生效。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.rb_global)
        layout.addWidget(self.rb_direct)
        layout.addWidget(self.rb_custom)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

        proxy = acc.get("proxy") or ""
        if proxy == "direct":
            self.rb_direct.setChecked(True)
        elif isinstance(proxy, dict):
            self.rb_custom.setChecked(True)
            self.cmb_type.setCurrentIndex(1 if proxy.get("type") == "socks5" else 0)
            self.edt_host.setText(str(proxy.get("host", "127.0.0.1")))
            self.spn_port.setValue(int(proxy.get("port", 10808)))
        else:
            self.rb_global.setChecked(True)
            self.edt_host.setText("127.0.0.1")
            self.spn_port.setValue(10808)
        for rb in (self.rb_global, self.rb_direct, self.rb_custom):
            rb.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self):
        custom = self.rb_custom.isChecked()
        for w in (self.cmb_type, self.edt_host, self.spn_port):
            w.setEnabled(custom)

    def result_proxy(self):
        if self.rb_direct.isChecked():
            return "direct"
        if self.rb_custom.isChecked():
            return {
                "type": self.cmb_type.currentData(),
                "host": self.edt_host.text().strip() or "127.0.0.1",
                "port": self.spn_port.value(),
            }
        return ""


ACCOUNT_COLORS = ["", "#e74c3c", "#e67e22", "#f1c40f", "#27ae60", "#2980b9", "#8e44ad", "#95a5a6"]


class AccountEditDialog(QDialog):
    """编辑账号：名字、备注、颜色标记（卡片头像的底色，账号多了靠颜色认）。"""

    def __init__(self, acc: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑账号 · {acc['name']}")
        self.setMinimumWidth(380)
        self.edt_name = QLineEdit(acc["name"])
        self.edt_note = QLineEdit(str(acc.get("note") or ""))
        self.edt_note.setPlaceholderText("备注（选填）：比如「主号」「备用」")
        self._color = str(acc.get("color") or "")
        color_row = QHBoxLayout()
        self._swatches: list[QPushButton] = []
        for col in ACCOUNT_COLORS:
            btn = QPushButton("无" if not col else "")
            btn.setCheckable(True)
            btn.setFixedSize(30, 30)
            if col:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {col}; border: 2px solid transparent; border-radius: 15px; }}"
                    f"QPushButton:checked {{ border: 2px solid {C['text']}; }}"
                )
            else:
                btn.setStyleSheet("font-size: 11px; border-radius: 15px; padding: 0;")
            btn.setChecked(col == self._color)
            btn.clicked.connect(lambda _c=False, v=col: self._pick_color(v))
            self._swatches.append(btn)
            color_row.addWidget(btn)
        color_row.addStretch(1)
        form = QFormLayout()
        form.addRow("名字", self.edt_name)
        form.addRow("备注", self.edt_note)
        form.addRow("颜色标记", color_row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _pick_color(self, value: str):
        self._color = value
        for btn, col in zip(self._swatches, ACCOUNT_COLORS):
            btn.setChecked(col == value)

    def result_values(self) -> tuple[str, str, str]:
        return self.edt_name.text().strip(), self.edt_note.text().strip(), self._color


def _close_windows_for_pid(pid: int) -> int:
    """给某个进程的所有顶层窗口发关闭消息（优雅关闭），返回发出的窗口数。

    只在 Windows 上有意义；其他平台返回 0，调用方会退回结束进程。
    """
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            proc_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in found:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return len(found)
    except Exception:
        return 0


class MainWindow(QMainWindow):
    _last_theme_switch = 0.0  # 外观切换防连点锁（时间戳，全窗口共用）
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.accounts = store.load_accounts()
        if not self.accounts:
            self.accounts = [{"id": store.new_account_id(), "name": "账号 1", "browser": "chrome", "proxy": ""}]
            store.save_accounts(self.accounts)
        self._selected_id = self.accounts[0]["id"]
        self._launched: set[str] = set()
        self._launched_at: dict[str, float] = {}
        self._card_widgets: dict[str, QWidget] = {}
        self._cards_animated = False
        self._anims: list = []
        self._open_queue: list[str] = []
        self._open_total = 0
        self._open_timer = QTimer(self)
        self._open_timer.timeout.connect(self._open_next)
        self._procs: dict[str, subprocess.Popen] = {}
        # 每 2.5 秒看一眼已启动的 Chrome 主进程：退出了就把卡片状态改回「未打开」
        self._proc_timer = QTimer(self)
        self._proc_timer.timeout.connect(self._poll_procs)
        self._proc_timer.start(2500)

        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(980, 620)
        geo = self.cfg.get("win_geo")  # 外观切换是进程级重启，靠这个把窗口放回原位
        if isinstance(geo, list) and len(geo) == 4:
            try:
                self.setGeometry(int(geo[0]), int(geo[1]), int(geo[2]), int(geo[3]))
            except Exception:
                pass
        set_theme(self.cfg.get("theme"))
        self.setStyleSheet(app_style(self.cfg.get("theme")))

        # ---- 左侧深色边栏 ----
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(190)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(18, 22, 18, 18)
        sl.setSpacing(8)
        lbl_brand = QLabel("MuseChrome")
        lbl_brand.setStyleSheet("font-size: 18px; font-weight: bold; color: #ffffff;")
        sl.addWidget(lbl_brand)
        lbl_ver = QLabel(f"{APP_NAME} v{__version__}")
        lbl_ver.setStyleSheet("font-size: 12px; color: #a89e8f;")
        sl.addWidget(lbl_ver)
        sl.addSpacing(14)
        self.btn_nav_notice = QPushButton("📢 公告")
        self.btn_nav_notice.clicked.connect(self.show_notice_page)
        sl.addWidget(self.btn_nav_notice)
        self.btn_nav_home = QPushButton("账号列表")
        self.btn_nav_home.clicked.connect(lambda: self._show_page(0))
        sl.addWidget(self.btn_nav_home)
        self.btn_nav_plugins = QPushButton("插件助手")
        self.btn_nav_plugins.clicked.connect(self.show_plugins_page)
        sl.addWidget(self.btn_nav_plugins)
        self.btn_nav_settings = QPushButton("设置")
        self.btn_nav_settings.clicked.connect(self.show_settings_page)
        sl.addWidget(self.btn_nav_settings)
        self.btn_nav_health = QPushButton("环境自检")
        self.btn_nav_health.clicked.connect(lambda: self._show_page(4))
        sl.addWidget(self.btn_nav_health)
        self.btn_update = QPushButton("检查更新")
        self.btn_update.setObjectName("navBtn")
        self.btn_update.clicked.connect(self.check_update)
        sl.addWidget(self.btn_update)
        self.btn_nav_about = QPushButton("关于软件")
        self.btn_nav_about.clicked.connect(self.show_about_page)
        sl.addWidget(self.btn_nav_about)
        self._nav_btns = [
            self.btn_nav_home, self.btn_nav_notice, self.btn_nav_plugins,
            self.btn_nav_settings, self.btn_nav_health, self.btn_nav_about,
        ]
        sl.addStretch(1)
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("navBtn")
        self.btn_theme.clicked.connect(self.toggle_theme)
        self._sync_theme_btn()
        sl.addWidget(self.btn_theme)
        self.lbl_proxy = QLabel()
        self.lbl_proxy.setStyleSheet("font-size: 12px; color: #a89e8f;")
        self.lbl_proxy.setWordWrap(True)
        sl.addWidget(self.lbl_proxy)
        self._refresh_proxy_label()

        # ---- 右侧主区 ----
        head = QHBoxLayout()
        lbl_head = QLabel("我的 Muse 账号")
        lbl_head.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        head.addWidget(lbl_head)
        head.addStretch(1)
        self.btn_open_all = QPushButton("打开全部")
        self.btn_open_all.clicked.connect(self.open_all_accounts)
        head.addWidget(self.btn_open_all)
        btn_add = QPushButton("＋ 添加账号")
        btn_add.setObjectName("primaryBtn")
        btn_add.clicked.connect(self.add_account)
        head.addWidget(btn_add)
        lbl_sub = QLabel("每个账号一个独立浏览器配置，登录一次，点开即用，互不串号。")
        lbl_sub.setStyleSheet(f"color: {C['dim']};")

        notice = QFrame()
        notice.setObjectName("noticeBar")
        nl = QHBoxLayout(notice)
        nl.setContentsMargins(14, 9, 14, 9)
        lbl_notice = QLabel(
            "使用前准备：本机装有谷歌 Chrome 或微软 Edge 任一浏览器即可——本软件用你电脑上现成的浏览器打开账号，不自带浏览器；代理请提前开好。"
        )
        lbl_notice.setStyleSheet(f"color: {C['noticeText']}; font-weight: bold;")
        lbl_notice.setWordWrap(True)
        nl.addWidget(lbl_notice)

        self._cards_host = QWidget()
        _paint_bg(self._cards_host)
        self._cards_grid = QGridLayout(self._cards_host)
        self._cards_grid.setContentsMargins(2, 2, 2, 2)
        self._cards_grid.setSpacing(12)
        self._cards_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cards_host)
        _paint_bg(scroll.viewport())
        self._cards_scroll = scroll

        lbl_note = QLabel(NOTE_TEXT)
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        lbl_note.setAlignment(Qt.AlignmentFlag.AlignTop)

        main = QVBoxLayout()
        main.setContentsMargins(22, 20, 22, 16)
        main.setSpacing(10)
        main.addLayout(head)
        main.addWidget(lbl_sub)
        main.addWidget(notice)
        main.addWidget(scroll, 1)
        main.addWidget(lbl_note)
        accounts_page = QWidget()
        accounts_page.setLayout(main)
        _paint_bg(accounts_page)

        # ---- 页面切换：账号列表 / 公告 / 插件助手 / 设置 / 关于，边栏只亮当前页 ----
        self.page_notice = NoticePage()
        self.page_notice.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_plugins = PluginPage()
        self.page_plugins.btn_go.clicked.connect(self.install_selected_plugins)
        self.page_plugins.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_settings = SettingsPage()
        self.page_settings.btn_save.clicked.connect(self.save_settings)
        self.page_settings.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_settings.btn_export.clicked.connect(self.export_backup)
        self.page_settings.btn_import.clicked.connect(self.import_backup)
        self.page_health = HealthPage()
        self.page_health.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_health.btn_start.clicked.connect(self.start_health_check)
        self.page_health.btn_leak.clicked.connect(self.open_leak_page)
        self.page_health.on_apply_port = self.apply_scanned_port
        self.page_about = AboutPage()
        self.page_about.btn_back.clicked.connect(lambda: self._show_page(0))
        self.stack = QStackedWidget()
        _paint_bg(self.stack)
        self.stack.addWidget(accounts_page)
        self.stack.addWidget(self.page_notice)
        self.stack.addWidget(self.page_plugins)
        self.stack.addWidget(self.page_settings)
        self.stack.addWidget(self.page_health)
        self.stack.addWidget(self.page_about)
        for i in range(self.stack.count()):
            _paint_bg(self.stack.widget(i))

        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(side)
        root.addWidget(self.stack, 1)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self._rebuild_cards()
        self._show_page(0)

        # ---- 公告：先摆上次缓存，再后台拉最新 ----
        self._notices: list[dict] = []
        self._notice_fetch: NoticeFetcher | None = None
        cache = store.load_notices_cache()
        if cache["text"]:
            self._notices = parse_notices(cache["text"])
            self.page_notice.set_notices(self._notices, "正在更新…")
        self._refresh_notice_badge()
        self._fetch_notices()

    # ---------- 页面切换 ----------
    def _show_page(self, idx: int):
        """切到第 idx 页，边栏只有对应按钮亮橙，其余恢复常态（同一时刻只亮一个）。"""
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_btns):
            want = "navBtnSel" if i == idx else "navBtn"
            if btn.objectName() != want:
                btn.setObjectName(want)
                btn.style().unpolish(btn)
                btn.style().polish(btn)
        if idx == 0:
            self._cards_scroll.verticalScrollBar().setValue(0)

    def show_notice_page(self):
        self._show_page(1)
        if self._notices:
            self.cfg["notice_seen"] = self._notices[0]["id"]
            store.save_config(self.cfg)
            self._refresh_notice_badge()
        self._fetch_notices()

    # ---------- 公告 ----------
    def _fetch_notices(self):
        if self._notice_fetch is not None and self._notice_fetch.isRunning():
            return
        self.page_notice.set_proxy_url(store.fetch_proxy_url(self.cfg))
        self._notice_fetch = NoticeFetcher(store.fetch_proxy_url(self.cfg), self)
        self._notice_fetch.done.connect(self._on_notices_fetched)
        self._notice_fetch.start()

    def _on_notices_fetched(self, ok: bool, text: str, why: str):
        if ok:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            store.save_notices_cache(text, stamp)
            self._notices = parse_notices(text)
            self.page_notice.set_notices(self._notices, f"更新于 {stamp}")
        elif self._notices:
            self.page_notice.set_notices(
                self._notices, f"暂时拉不到公告（{why}），显示的是上次内容"
            )
        else:
            self.page_notice.set_notices([], f"暂时拉不到公告（{why}）")
        self._refresh_notice_badge()

    def _refresh_notice_badge(self):
        newest = self._notices[0]["id"] if self._notices else ""
        has_new = bool(newest) and newest != str(self.cfg.get("notice_seen") or "")
        self.btn_nav_notice.setText("📢 公告（新）" if has_new else "📢 公告")

    def show_plugins_page(self):
        acc = self._current_account()
        if acc is None:
            return
        self.page_plugins.refresh(acc["name"], store.list_source_extensions())
        self._show_page(2)

    def show_settings_page(self):
        self.page_settings.load(self.cfg)
        self._show_page(3)

    def show_about_page(self):
        self._show_page(5)

    # ---------- 环境自检 ----------
    def start_health_check(self):
        if getattr(self, "_health", None) is not None and self._health.isRunning():
            return
        self.page_health.clear_results()
        self.page_health.btn_start.setEnabled(False)
        self.page_health.btn_start.setText("检测中…")
        self._health = HealthChecker(self.cfg, self)
        self._health.item.connect(self.page_health.add_result)
        self._health.scan_found.connect(self.page_health.set_scan_actions)
        self._health.all_done.connect(self._on_health_done)
        self._health.start()

    def _on_health_done(self):
        self.page_health.btn_start.setEnabled(True)
        self.page_health.btn_start.setText("开始检测")

    def apply_scanned_port(self, port: int):
        self.cfg["proxy_mode"] = "local"
        self.cfg["proxy_enabled"] = True
        self.cfg["proxy_type"] = "http"
        self.cfg["proxy_host"] = "127.0.0.1"
        self.cfg["proxy_port"] = int(port)
        store.save_config(self.cfg)
        self._refresh_proxy_label()
        QMessageBox.information(
            self, "环境自检", f"已改用 127.0.0.1:{port} 作为代理。\n回账号列表打开账号试试。"
        )

    def open_leak_page(self):
        self.open_account(extra_urls=["https://browserleaks.com/ip"])
        self._show_page(0)

    # ---------- 账号卡片 ----------
    def _rebuild_cards(self):
        # 账号总数不满 3 个时，卡片拉宽铺满整排（2 个各占一半、1 个占满）；
        # 3 个及以上按原版等宽三列，最后一排不满也保持三列宽（Smith 拍板）
        n = len(self.accounts)
        cols = 3 if n >= 3 else max(1, n)
        for c in range(3):
            self._cards_grid.setColumnStretch(c, 1 if c < cols else 0)
        while self._cards_grid.count():
            item = self._cards_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._card_widgets = {}
        cards = []
        for idx, acc in enumerate(self.accounts):
            card = self._make_card(acc)
            self._card_widgets[acc["id"]] = card
            cards.append(card)
            self._cards_grid.addWidget(card, idx // 3, idx % 3)
        # 只在软件刚打开、第一次摆卡片时做一次错落淡入，之后重排不再动
        if not self._cards_animated:
            self._cards_animated = True
            for i, card in enumerate(cards):
                self._fade_in_card(card, i * 45)

    def _fade_in_card(self, card: QWidget, delay_ms: int):
        eff = QGraphicsOpacityEffect(card)
        eff.setOpacity(0.0)
        card.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", card)
        anim.setDuration(280)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anims.append(anim)
        anim.finished.connect(lambda c=card: c.setGraphicsEffect(None))
        if delay_ms:
            QTimer.singleShot(delay_ms, anim.start)
        else:
            anim.start()

    def _pulse_card(self, acc_id: str):
        """批量打开时给刚启动的卡片一个呼吸脉冲，眼睛能跟上进度。"""
        card = self._card_widgets.get(acc_id)
        if card is None:
            return
        eff = QGraphicsOpacityEffect(card)
        card.setGraphicsEffect(eff)
        anim = QPropertyAnimation(eff, b"opacity", card)
        anim.setDuration(550)
        anim.setKeyValueAt(0, 0.3)
        anim.setKeyValueAt(0.35, 1.0)
        anim.setKeyValueAt(1, 1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anims.append(anim)
        anim.finished.connect(lambda c=card: c.setGraphicsEffect(None))
        anim.start()

    def _make_card(self, acc: dict) -> AccountCard:
        selected = acc["id"] == self._selected_id
        card = AccountCard(acc["id"])
        card.setObjectName("cardSel" if selected else "card")
        card.setMinimumWidth(210)
        card.clicked.connect(self._select_account)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(16, 16, 16, 14)
        cl.setSpacing(8)
        avatar = QLabel(acc["name"][:1] or "M")
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {acc.get('color') or '#e8930c'}; color: #ffffff; font-size: 17px; font-weight: bold; border-radius: 20px;"
        )
        cl.addWidget(avatar)
        name = QLabel(acc["name"])
        name.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {C['text']};")
        cl.addWidget(name)
        opened = acc["id"] in self._launched
        if opened:
            started = self._launched_at.get(acc["id"])
            if started:
                mins = int((time.time() - started) // 60)
                if mins < 1:
                    status = "已打开 · 刚打开"
                elif mins < 60:
                    status = f"已打开 · 已开 {mins} 分钟"
                else:
                    status = f"已打开 · 已开 {mins // 60} 小时 {mins % 60} 分"
            else:
                status = "已打开"
        else:
            status = "未打开"
        proxy_hint = ""
        if acc.get("proxy") == "direct":
            proxy_hint = " · 代理：直连"
        elif isinstance(acc.get("proxy"), dict):
            proxy_hint = f" · 代理：{acc['proxy']['host']}:{acc['proxy']['port']}"
        lbl_status = QLabel(f"独立配置 · {status}{proxy_hint}")
        lbl_status.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
        cl.addWidget(lbl_status)
        note_text = str(acc.get("note") or "").strip()
        if note_text:
            lbl_note = QLabel(note_text)
            lbl_note.setStyleSheet(f"font-size: 12px; color: {C['dim']};")
            lbl_note.setWordWrap(True)
            cl.addWidget(lbl_note)
        # 浏览器二选一：分段开关（选中的一段填墨色），比下拉框直观
        seg = QFrame()
        seg.setObjectName("browserSeg")
        seg_l = QHBoxLayout(seg)
        seg_l.setContentsMargins(2, 2, 2, 2)
        seg_l.setSpacing(2)
        btn_seg_chrome = QPushButton("谷歌 Chrome")
        btn_seg_chrome.setObjectName("segBtn")
        btn_seg_chrome.setCheckable(True)
        btn_seg_edge = QPushButton("微软 Edge")
        btn_seg_edge.setObjectName("segBtn")
        btn_seg_edge.setCheckable(True)
        is_edge = acc.get("browser") == "edge"
        btn_seg_chrome.setChecked(not is_edge)
        btn_seg_edge.setChecked(is_edge)
        seg_l.addWidget(btn_seg_chrome, 1)
        seg_l.addWidget(btn_seg_edge, 1)
        cl.addWidget(seg)
        btn_open = QPushButton("打开 Edge" if is_edge else "打开 Chrome")
        btn_open.setObjectName("darkBtn")
        btn_open.clicked.connect(lambda _c=False, aid=acc["id"]: (self._select_account(aid), self.open_account()))
        cl.addWidget(btn_open)
        btn_mail_assist = QPushButton("复制邮箱并打开取件链接")
        btn_mail_assist.clicked.connect(
            lambda _c=False, aid=acc["id"]: self.start_mail_assist(aid)
        )
        btn_mail_assist.setVisible(is_edge)
        cl.addWidget(btn_mail_assist)
        btn_seg_chrome.clicked.connect(
            lambda _c=False, aid=acc["id"], bo=btn_open, ba=btn_mail_assist,
            bc=btn_seg_chrome, be=btn_seg_edge:
            self._set_account_browser(aid, "chrome", bo, ba, bc, be)
        )
        btn_seg_edge.clicked.connect(
            lambda _c=False, aid=acc["id"], bo=btn_open, ba=btn_mail_assist,
            bc=btn_seg_chrome, be=btn_seg_edge:
            self._set_account_browser(aid, "edge", bo, ba, bc, be)
        )
        btn_plugins = QPushButton("插件助手")
        btn_plugins.clicked.connect(lambda _c=False, aid=acc["id"]: (self._select_account(aid), self.show_plugins_page()))
        cl.addWidget(btn_plugins)
        row = QHBoxLayout()
        btn_rename = QPushButton("编辑")
        btn_rename.setObjectName("miniBtn")
        btn_rename.clicked.connect(lambda _c=False, aid=acc["id"]: (self._select_account(aid), self.rename_account()))
        row.addWidget(btn_rename)
        btn_proxy = QPushButton("代理")
        btn_proxy.setObjectName("miniBtn")
        btn_proxy.clicked.connect(lambda _c=False, aid=acc["id"]: (self._select_account(aid), self.edit_account_proxy()))
        row.addWidget(btn_proxy)
        if acc["id"] in self._launched:
            btn_close = QPushButton("关闭浏览器")
            btn_close.setObjectName("miniBtn")
            btn_close.clicked.connect(lambda _c=False, aid=acc["id"]: self.close_account_browser(aid))
            row.addWidget(btn_close)
        btn_delete = QPushButton("删除账号")
        btn_delete.setObjectName("miniBtn")
        btn_delete.clicked.connect(lambda _c=False, aid=acc["id"]: (self._select_account(aid), self.delete_account()))
        row.addWidget(btn_delete)
        row.addStretch(1)
        cl.addLayout(row)
        return card

    def _select_account(self, acc_id: str):
        if acc_id != self._selected_id:
            self._selected_id = acc_id
            self._rebuild_cards()

    def _set_account_browser(self, acc_id: str, browser: str, btn_open: QPushButton,
                             btn_mail_assist: QPushButton, btn_chrome: QPushButton,
                             btn_edge: QPushButton):
        """卡片上切换该账号用的浏览器：存盘，并同步打开按钮的文字。
        Chrome 与 Edge 各用各的配置目录，切换后第一次打开要登录一次。"""
        for acc in self.accounts:
            if acc["id"] == acc_id:
                if acc.get("browser") != browser:
                    acc["browser"] = browser
                    store.save_accounts(self.accounts)
                break
        btn_chrome.setChecked(browser == "chrome")
        btn_edge.setChecked(browser == "edge")
        btn_open.setText("打开 Edge" if browser == "edge" else "打开 Chrome")
        btn_mail_assist.setVisible(browser == "edge")

    # ---------- 账号管理 ----------
    def _current_account(self) -> dict | None:
        for acc in self.accounts:
            if acc["id"] == self._selected_id:
                return acc
        return self.accounts[0] if self.accounts else None

    def _sync_theme_btn(self):
        dark = self.cfg.get("theme") == "dark"
        self.btn_theme.setText("☀️ 浅色外观" if dark else "🌙 深色外观")

    def toggle_theme(self):
        """边栏月亮按钮：一键切换深浅外观。

        界面的颜色是建窗口时按主题定的。早期做法是同进程里关旧窗口开新窗口，
        但新窗口的后台线程（公告拉取等）还没跑完就被下一次切换连窗口销毁，
        会闪退；修过一版（防连点＋等线程收尾）在个别机器上仍崩。
        现在改成进程级重启：存好设置和窗口位置，拉起一个新进程，当前进程
        正常退出——和用户手动关掉重开完全一样，不存在窗口/线程生命周期问题。
        连点由 2 秒锁挡住，只认第一次。
        已经打开的浏览器窗口不受影响。
        """
        now = time.time()
        if now - MainWindow._last_theme_switch < 2.0:
            return
        MainWindow._last_theme_switch = now
        self.btn_theme.setEnabled(False)
        new_theme = "light" if self.cfg.get("theme") == "dark" else "dark"
        self.cfg["theme"] = new_theme
        geo = self.geometry()
        self.cfg["win_geo"] = [geo.x(), geo.y(), geo.width(), geo.height()]
        store.save_config(self.cfg)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable]
        else:
            cmd = [sys.executable, os.path.abspath(sys.argv[0])]
        try:
            subprocess.Popen(cmd, close_fds=True)
        except Exception:
            self.btn_theme.setEnabled(True)
            return
        QApplication.quit()

    def _refresh_proxy_label(self):
        mode = self.cfg.get("proxy_mode", "local")
        if mode == "local":
            self.lbl_proxy.setText(
                f"代理：本地\n{self.cfg['proxy_type'].upper()} {self.cfg['proxy_host']}:{self.cfg['proxy_port']}"
            )
        elif mode == "system":
            self.lbl_proxy.setText("代理：跟随系统")
        else:
            self.lbl_proxy.setText("代理：直连")

    def add_account(self):
        name, ok = QInputDialog.getText(
            self, "添加账号", "给新账号起个名字（如：工作号）：",
            text=store.next_account_name(self.accounts),
        )
        if not ok or not name.strip():
            return
        acc = {"id": store.new_account_id(), "name": name.strip(), "browser": "chrome",
               "proxy": "", "note": "", "color": ""}
        self.accounts.append(acc)
        store.save_accounts(self.accounts)
        self._selected_id = acc["id"]
        self._rebuild_cards()

    def rename_account(self):
        acc = self._current_account()
        if acc is None:
            return
        dlg = AccountEditDialog(acc, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, note, color = dlg.result_values()
        if name:
            acc["name"] = name
        acc["note"] = note
        acc["color"] = color
        store.save_accounts(self.accounts)
        self._rebuild_cards()

    def open_all_accounts(self):
        """批量打开：还没打开的账号排队，每 0.7 秒开一个（一起涌会把机器压住），
        按钮上滚动进度，刚开的卡片脉冲一下，眼睛跟得上。"""
        pending = [a["id"] for a in self.accounts if a["id"] not in self._launched]
        if not pending:
            QMessageBox.information(self, "打开全部", "所有账号都已经打开了。")
            return
        self._open_queue = pending
        self._open_total = len(pending)
        self.btn_open_all.setEnabled(False)
        self.btn_open_all.setText(f"正在打开…（0/{self._open_total}）")
        self._open_timer.start(700)
        self._open_next()

    def _open_next(self):
        if not self._open_queue:
            self._open_timer.stop()
            self.btn_open_all.setEnabled(True)
            self.btn_open_all.setText("打开全部")
            return
        aid = self._open_queue.pop(0)
        done = self._open_total - len(self._open_queue)
        self.btn_open_all.setText(f"正在打开…（{done}/{self._open_total}）")
        self._selected_id = aid
        self.open_account()
        self._pulse_card(aid)

    def edit_account_proxy(self):
        acc = self._current_account()
        if acc is None:
            return
        dlg = AccountProxyDialog(acc, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            acc["proxy"] = dlg.result_proxy()
            store.save_accounts(self.accounts)
            self._rebuild_cards()

    def delete_account(self):
        acc = self._current_account()
        if acc is None:
            return
        if len(self.accounts) == 1:
            QMessageBox.information(self, "删除账号", "至少保留一个账号，不能删光。")
            return
        ret = QMessageBox.question(self, "删除账号", f"确定删除「{acc['name']}」吗？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        wipe = QMessageBox.question(
            self, "删除账号",
            "连这个账号的浏览器数据一起删掉吗？\n（Chrome 和 Edge 两边的登录状态、Cookie、插件都会没；选「否」只删列表项，数据保留。）",
        )
        self.accounts = [a for a in self.accounts if a["id"] != acc["id"]]
        store.save_accounts(self.accounts)
        if wipe == QMessageBox.StandardButton.Yes:
            store.delete_profile_dir(acc["id"])
            store.delete_edge_profile_dir(acc["id"])
        self._launched.discard(acc["id"])
        self._launched_at.pop(acc["id"], None)
        self._procs.pop(acc["id"], None)
        if self._current_account() is None or self._selected_id == acc["id"]:
            self._selected_id = self.accounts[0]["id"]
        self._rebuild_cards()

    # ---------- 插件助手 ----------
    def install_selected_plugins(self):
        ids = self.page_plugins.selected_ids()
        if not ids:
            QMessageBox.information(self, "插件助手", "先勾选要安装的插件。")
            return
        self.open_account(extra_urls=[store.store_url(i) for i in ids])
        self._show_page(0)

    # ---------- 打开浏览器 ----------
    def start_mail_assist(self, acc_id: str):
        acc = next((item for item in self.accounts if item["id"] == acc_id), None)
        if acc is None or acc.get("browser") != "edge":
            QMessageBox.information(self, "邮箱辅助登录", "此入口仅适用于选择 Edge 的账号。")
            return

        try:
            mailboxes = auto_login.load_mailboxes(self.cfg.get("mail_accounts_csv", ""))
            api_base = auto_login.load_mail_api_base(self.cfg.get("mail_config_path", ""))
        except auto_login.MailAssistError as exc:
            QMessageBox.warning(
                self, "邮箱辅助登录", f"{exc}\n\n请在「设置」中检查 CSV 和配置文件路径。"
            )
            return

        email, ok = QInputDialog.getItem(
            self, "选择邮箱", "复制并用于本次 Muse 登录：", mailboxes, 0, False
        )
        if not ok or not email:
            return

        QApplication.clipboard().setText(email)
        url = auto_login.mailbox_url(api_base, email)
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.warning(
                self, "邮箱辅助登录",
                "邮箱已复制，但系统未能打开取件链接。你可以检查默认浏览器设置后重试。",
            )
            return
        QMessageBox.information(
            self, "邮箱辅助登录",
            "邮箱已复制，取件链接已在默认浏览器中打开。请自行查收验证码，再到账号的 Edge 窗口完成登录。",
        )

    def open_account(self, extra_urls: list[str] | None = None):
        acc = self._current_account()
        if acc is None:
            return
        kind = acc.get("browser") or "chrome"
        is_edge = kind == "edge"
        label = "Edge" if is_edge else "Chrome"
        exe_name = "msedge.exe" if is_edge else "chrome.exe"
        cfg_key = "edge_path" if is_edge else "chrome_path"
        exe = store.find_browser_exe(self.cfg, kind)
        if not exe:
            ret = QMessageBox.warning(
                self, f"找不到 {label}",
                f"没在常见位置找到 {exe_name}。\n点「是」手动选一次 {exe_name}，我会记到设置里。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret == QMessageBox.StandardButton.Yes:
                path, _ = QFileDialog.getOpenFileName(self, f"选择 {exe_name}", "", f"{exe_name} ({exe_name})")
                if path:
                    self.cfg[cfg_key] = path
                    store.save_config(self.cfg)
                    exe = path
            if not exe:
                return
        profile = store.profile_dir_for_browser(acc["id"], kind)
        args = [exe, *store.build_chrome_args(
            store.effective_cfg(self.cfg, acc), profile, extra_urls, browser=kind
        )]
        try:
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            proc = subprocess.Popen(args, close_fds=True, **kwargs)
            old = self._procs.get(acc["id"])
            if old is None or old.poll() is not None:
                self._procs[acc["id"]] = proc
                self._launched_at[acc["id"]] = time.time()
            self._launched.add(acc["id"])
            self._rebuild_cards()
        except Exception as exc:  # 启动失败要如实报错，不装没事
            QMessageBox.critical(self, "打开失败", f"{label} 启动失败：{exc}")

    def close_account_browser(self, acc_id: str):
        """关掉这个账号的浏览器：先给窗口发关闭消息（优雅关），3 秒没退就结束进程。"""
        proc = self._procs.get(acc_id)
        if proc is not None and proc.poll() is None:
            _close_windows_for_pid(proc.pid)
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._procs.pop(acc_id, None)
        self._launched.discard(acc_id)
        self._launched_at.pop(acc_id, None)
        self._rebuild_cards()

    def _poll_procs(self):
        """Chrome 主进程退出了，对应账号卡片的状态就改回「未打开」。
        （重复打开同一账号时新进程会把窗口交给已运行的实例后立刻退出，
        所以只认第一次启动的那个进程句柄，不被后续短命进程顶掉。）"""
        changed = False
        for acc_id, proc in list(self._procs.items()):
            if proc.poll() is not None:
                del self._procs[acc_id]
                self._launched_at.pop(acc_id, None)
                if acc_id in self._launched:
                    self._launched.discard(acc_id)
                    changed = True
        if changed:
            self._rebuild_cards()

    # ---------- 检查更新 / 软件内更新 ----------
    def check_update(self):
        self.btn_update.setEnabled(False)
        self.btn_update.setText("检查中…")
        self._checker = UpdateChecker(store.fetch_proxy_url(self.cfg), self)
        self._checker.done.connect(self._on_update_result)
        self._checker.start()

    def _restore_update_btn(self):
        self.btn_update.setEnabled(True)
        self.btn_update.setText("检查更新")

    def _on_update_result(self, ok: bool, tag: str, url: str, detail: str,
                          asset_url: str, size: int, sha: str):
        self._restore_update_btn()
        if not ok or not tag:
            QMessageBox.information(
                self, "检查更新",
                f"检查失败：{detail or '连不上 GitHub'}。\n（先确认代理工具开着、软件里的代理设置没关，再试一次。）",
            )
            return
        if not store.is_newer(tag, __version__):
            QMessageBox.information(
                self, "检查更新", f"已是最新版本（v{__version__}）。"
            )
            return
        frozen = bool(getattr(sys, "frozen", False))
        box = QMessageBox(self)
        box.setWindowTitle("检查更新")
        if frozen and asset_url:
            box.setText(
                f"发现新版本 {tag}（你现在是 v{__version__}）。\n"
                "点「立即更新」直接在软件里下载，下载完自动替换成新版并重新打开，不用去网页下。"
            )
            btn_go = box.addButton("立即更新", QMessageBox.ButtonRole.AcceptRole)
        else:
            box.setText(
                f"发现新版本 {tag}（你现在是 v{__version__}）。\n点「打开下载页」去下载新版 exe。"
            )
            btn_go = box.addButton("打开下载页", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("以后再说", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not btn_go:
            return
        if frozen and asset_url:
            self._start_self_update(tag, asset_url, size, sha)
        else:
            QDesktopServices.openUrl(QUrl(url or (REPO_URL + "/releases")))

    def _start_self_update(self, tag: str, asset_url: str, size: int, sha: str):
        # 更新落点分两种：
        # - 安装版（当前文件名就是 MuseChrome.exe）：新版下到数据目录，
        #   更新时原地覆盖同名文件，桌面快捷方式永远有效。
        # - 便携版（文件名带版本号）：新版直接下到当前 exe 同目录、用新版文件名，
        #   更新后文件名跟版本走，不再把新内容塞进旧文件名里。
        cur = Path(sys.executable)
        if cur.name.lower() == "musechrome.exe":
            dest = store.data_dir() / "update" / f"MuseChrome-{tag}.exe"
        else:
            dest = cur.parent / f"MuseChrome-{tag}.exe"
            if dest == cur:  # 同名兜底（理论上不会发生）：走覆盖模式
                dest = store.data_dir() / "update" / f"MuseChrome-{tag}.exe"
        dlg = QProgressDialog("正在下载新版本…", "取消", 0, 100, self)
        dlg.setWindowTitle("检查更新")
        dlg.setMinimumWidth(360)
        dlg.setValue(0)
        self._dl = DownloadThread(
            asset_url, dest, store.fetch_proxy_url(self.cfg), size, sha, self
        )
        self._dl_progress = dlg
        self._dl.progress.connect(lambda p: dlg.setValue(p if p >= 0 else 0))
        self._dl.done.connect(lambda ok2, path, why: self._on_download_done(ok2, path, why, tag))
        dlg.canceled.connect(self._dl.requestInterruption)
        self._dl.start()
        dlg.show()

    def _on_download_done(self, ok: bool, path: str, why: str, tag: str):
        if hasattr(self, "_dl_progress"):
            self._dl_progress.close()
        if not ok:
            if why != "已取消":
                QMessageBox.information(
                    self, "检查更新",
                    f"下载失败：{why}。\n（先确认代理工具开着，再试一次；也可以去 GitHub 发布页手动下载。）",
                )
            return
        new_exe = Path(path)
        cur_exe = Path(sys.executable)
        rename_mode = new_exe.parent == cur_exe.parent and new_exe != cur_exe
        if rename_mode:
            QMessageBox.information(
                self, "检查更新",
                f"新版本 {tag} 已下载并校验通过（{new_exe.name}）。\n"
                "点「确定」后软件会自动关闭、删掉旧文件并打开新版。",
            )
            bat = new_exe.parent / "apply_update.bat"
            bat.write_text(build_rename_bat(new_exe, cur_exe), encoding="mbcs")
        else:
            QMessageBox.information(
                self, "检查更新",
                f"新版本 {tag} 已下载并校验通过。\n点「确定」后软件会自动关闭、替换成新版并重新打开。",
            )
            bat = new_exe.parent / "apply_update.bat"
            bat.write_text(build_updater_bat(new_exe, cur_exe), encoding="mbcs")
        try:
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            subprocess.Popen(["cmd", "/c", str(bat)], close_fds=True, **kwargs)
        except Exception as exc:
            QMessageBox.critical(self, "检查更新", f"启动更新失败：{exc}\n新文件已下载在：{new_exe}")
            return
        QApplication.quit()

    # ---------- 设置 ----------
    def save_settings(self):
        seen = self.cfg.get("notice_seen", "")
        old_theme = self.cfg.get("theme", "light")
        self.cfg = {**store.DEFAULT_CONFIG, **self.page_settings.collect()}
        self.cfg["notice_seen"] = seen  # 公告已读标记不在设置页里，别被保存设置冲掉
        store.save_config(self.cfg)
        self._refresh_proxy_label()
        if self.cfg.get("theme") != old_theme:
            self.page_settings.lbl_saved.setText("已保存 ✓（外观下次打开软件时生效）")
        else:
            self.page_settings.lbl_saved.setText("已保存 ✓")

    # ---------- 备份 / 恢复 ----------
    def export_backup(self):
        box = QMessageBox(self)
        box.setWindowTitle("导出备份")
        box.setText("选备份范围：\n\n完整备份：账号、设置和每个账号的登录态全打包，换电脑导入后免登录。\n轻量备份：只打包账号列表和设置，导入后每个账号要重新登录一次。\n\n完整备份里含登录凭证，等于钥匙，请自己收好、不要外传。")
        btn_full = box.addButton("完整备份", QMessageBox.ButtonRole.AcceptRole)
        btn_light = box.addButton("轻量备份", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (btn_full, btn_light):
            return
        import time as _time
        default = f"MuseChrome-backup-{_time.strftime('%Y%m%d')}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "保存备份", default, "Zip 备份 (*.zip)")
        if not path:
            return
        try:
            store.export_backup(path, include_profiles=clicked is btn_full)
            QMessageBox.information(self, "导出备份", "备份已保存。")
        except Exception as exc:
            QMessageBox.critical(self, "导出备份", f"导出失败：{exc}")

    def import_backup(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择备份文件", "", "Zip 备份 (*.zip)")
        if not path:
            return
        ret = QMessageBox.question(
            self, "导入恢复",
            "导入会覆盖现在的账号列表和设置（覆盖前的版本会自动留一份在 MuseChromeData 里）。\n如果备份含登录态，正在打开的浏览器请先关掉。继续？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            summary = store.import_backup(path)
        except Exception as exc:
            QMessageBox.critical(self, "导入恢复", f"导入失败：{exc}")
            return
        self.cfg = store.load_config()
        self.accounts = store.load_accounts()
        if not self.accounts:
            self.accounts = [{"id": store.new_account_id(), "name": "账号 1", "browser": "chrome", "proxy": ""}]
            store.save_accounts(self.accounts)
        self._selected_id = self.accounts[0]["id"]
        self._refresh_proxy_label()
        self.page_settings.load(self.cfg)
        self._rebuild_cards()
        tail = "含登录态，打开账号应该免登录。" if summary["profiles"] else "不含登录态，每个账号要重新登录一次。"
        QMessageBox.information(self, "导入恢复", f"已恢复 {summary['accounts']} 个账号。{tail}")


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    cfg = store.load_config()
    # 颜色方案跟随外观设置：浅色时强制浅色（系统深色下调色板文字会变白，
    # 压到浅色底上看不见，插件名单就这么丢过）；选了深色就让对话框也走深色
    try:
        app.styleHints().setColorScheme(
            Qt.ColorScheme.Dark if cfg.get("theme") == "dark" else Qt.ColorScheme.Light
        )
    except Exception:
        pass
    # 让系统对话框按钮显示中文（关闭/取消/保存）；加载失败就保持英文，不影响功能
    translator = QTranslator(app)
    if translator.load(
        "qtbase_zh_CN",
        QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
    ):
        app.installTranslator(translator)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = MainWindow(cfg)
    window.show()
    return app.exec()
