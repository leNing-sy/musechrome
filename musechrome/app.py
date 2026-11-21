"""Muse 账号管理器主界面。

管理器本身不含浏览器：每个账号对应一个独立的浏览器配置目录，
账号可在卡片上选 Chrome 或 Edge，点「打开」就以该配置启动真浏览器
（带本地代理、WebRTC 防泄漏、英文语言标识），
登录一次后登录态保存在该账号自己的目录里，互不串号。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from PyQt6.QtCore import (
    QLibraryInfo,
    QTimer,
    Qt,
    QThread,
    QTranslator,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
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
    """探活并计时（毫秒）：代理检测页仅作参考展示。"""
    t0 = time.monotonic()
    ok, detail = probe_proxy(ptype, host, port, timeout=timeout)
    return ok, detail, int((time.monotonic() - t0) * 1000)


class HealthChecker(QThread):
    """Only probe the configured global proxy on explicit request."""
    item = pyqtSignal(str, str, str)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)

    def run(self):
        mode = self.cfg.get("proxy_mode", "local")
        if mode == "off":
            self.item.emit("直连模式", "warn", "当前未配置代理，无需检测。")
            return
        if mode == "system":
            url = store.system_proxy_url()
            if not url:
                self.item.emit("系统代理", "fail", "未读取到系统代理，请检查代理工具的系统代理开关。")
                return
            parsed = urllib.parse.urlparse(url)
            ptype = "socks5" if parsed.scheme == "socks5" else "http"
            host, port = parsed.hostname or "", parsed.port or 80
        else:
            ptype = self.cfg["proxy_type"]
            host, port = self.cfg["proxy_host"], int(self.cfg["proxy_port"])
        ok, detail, ms = _timed_probe(ptype, host, port, timeout=8)
        self.item.emit("代理连接", "ok" if ok else "fail",
                       f"{ptype.upper()} {host}:{port}：" + ("可用" if ok else f"不可用（{detail}）"))
        if ok:
            self.item.emit("请求耗时", "ok", f"约 {ms} ms，仅供参考。")


class HealthPage(QWidget):
    """An opt-in connection test for the configured global proxy."""
    def __init__(self, parent=None):
        super().__init__(parent)
        title = QLabel("代理检测")
        title.setStyleSheet(f"font-size: 19px; font-weight: bold; color: {C['text']};")
        intro = QLabel("仅通过设置中的全局代理请求 gstatic 测试页；不查询出口 IP、不扫描端口、不修改设置。账号单独配置的代理不在此检测。")
        intro.setWordWrap(True)
        self.btn_start = QPushButton("开始检测")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_back = QPushButton("返回账号列表")
        row = QHBoxLayout()
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_back)
        row.addStretch(1)
        self._results_host = QWidget()
        self._results = QVBoxLayout(self._results_host)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.addWidget(title)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addWidget(self._results_host)
        layout.addStretch(1)

    def clear_results(self):
        while self._results.count():
            item = self._results.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def add_result(self, name: str, status: str, text: str):
        color = {"ok": "#2e7d32", "warn": "#e8930c", "fail": "#c62828"}.get(status, C["dim"])
        label = QLabel(f"{name}：{text}")
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {color};")
        self._results.addWidget(label)


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
• 点「打开」启动该账号独立的 Chrome / Edge；首次使用需自行登录，后续保留本账号的浏览器数据。
• Chrome 与 Edge 的配置目录分开。Edge 的微软登录和同步行为可能随版本、策略变化；首次打开请自行确认登录与同步状态。
• 选 Edge 后可点「复制邮箱并打开取件链接」，选择 CSV 邮箱、自行取验证码，再回账号窗口完成登录。
• 打开前确认代理已就绪；全局代理可在左侧「代理检测」测试，按账号代理需单独确认。
• 改浏览器、代理或语言设置前，先关闭该账号的浏览器；外观可从左侧切换。"""

class SettingsPage(QWidget):
    """设置页：在主区里切换显示，不再是独立弹窗。"""

    mail_path_selected = pyqtSignal(str, str)

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
        self.edt_mail_config.setPlaceholderText("留空使用默认取件地址，无需准备 config.json")
        btn_browse_mail_config = QPushButton("浏览…")
        btn_browse_mail_config.clicked.connect(self._browse_mail_config)
        mail_config_row = QHBoxLayout()
        mail_config_row.addWidget(self.edt_mail_config, 1)
        mail_config_row.addWidget(btn_browse_mail_config)
        self.edt_mail_csv.editingFinished.connect(
            lambda: self.mail_path_selected.emit("mail_accounts_csv", self.edt_mail_csv.text())
        )
        self.edt_mail_config.editingFinished.connect(
            lambda: self.mail_path_selected.emit("mail_config_path", self.edt_mail_config.text())
        )
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
        form.addRow("自定义取件配置（可选）", mail_config_row)
        form.addRow(self.chk_lang)
        form.addRow(self.chk_webrtc)
        form.addRow("外观", self.cmb_theme)

        note = QLabel(
            "邮箱辅助登录只需选择含「邮箱」列的 CSV，邮箱文件路径选择后自动保存。\n"
            f"默认取件地址：{auto_login.DEFAULT_MAIL_API_BASE}；自定义取件配置可留空。\n"
            "其他设置修改后请点击「保存设置」。Chrome / Edge 路径留空 = 自动查找。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")

        self.btn_export = QPushButton("导出备份")
        backup_note = QLabel("仅提供导出，导入恢复暂不可用。完整备份前关闭所有账号浏览器；备份含敏感登录数据，不保证跨电脑恢复登录态。")
        backup_note.setWordWrap(True)
        backup_note.setStyleSheet(f"color: {C['dim']}; font-size: 12px;")
        backup_row = QHBoxLayout()
        backup_row.addWidget(self.btn_export)
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
            self.mail_path_selected.emit("mail_accounts_csv", path)

    def _browse_mail_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择邮件服务 config.json", self.edt_mail_config.text(),
            "JSON 文件 (*.json);;所有文件 (*)",
        )
        if path:
            self.edt_mail_config.setText(path)
            self.mail_path_selected.emit("mail_config_path", path)


ABOUT_TEXT = """Muse 账号管理器 · 精简二开版

为每个账号准备独立的 Chrome / Edge 配置目录，保留账号管理、代理设置和邮箱辅助。浏览器由本机提供，程序不内置浏览器或自动化运行时。

邮箱辅助只读取 CSV 的邮箱列，把选中的邮箱复制到剪贴板，并交给默认浏览器打开取件链接；验证码和网站登录由你完成。

数据位于软件旁的 MuseChromeData 文件夹。代理检测仅在手动点击后请求测试页；已移除公告、插件助手及软件内更新，新版需手动获取。"""

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
    """Edit only the account label; keep legacy metadata in storage."""
    def __init__(self, acc: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑账号名称")
        self.edt_name = QLineEdit(str(acc["name"]))
        form = QFormLayout()
        form.addRow("名称", self.edt_name)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def result_name(self) -> str:
        return self.edt_name.text().strip()


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
        self.btn_nav_home = QPushButton("账号列表")
        self.btn_nav_home.clicked.connect(lambda: self._show_page(0))
        sl.addWidget(self.btn_nav_home)
        self.btn_nav_settings = QPushButton("设置")
        self.btn_nav_settings.clicked.connect(self.show_settings_page)
        sl.addWidget(self.btn_nav_settings)
        self.btn_nav_health = QPushButton("代理检测")
        self.btn_nav_health.clicked.connect(lambda: self._show_page(2))
        sl.addWidget(self.btn_nav_health)
        self.btn_nav_about = QPushButton("关于软件")
        self.btn_nav_about.clicked.connect(self.show_about_page)
        sl.addWidget(self.btn_nav_about)
        self._nav_btns = [
            self.btn_nav_home, self.btn_nav_settings, self.btn_nav_health, self.btn_nav_about,
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
        btn_add = QPushButton("＋ 添加账号")
        btn_add.setObjectName("primaryBtn")
        btn_add.clicked.connect(self.add_account)
        head.addWidget(btn_add)
        lbl_sub = QLabel("每个账号一个独立浏览器配置，登录一次，点开即用，互不串号。")
        lbl_sub.setStyleSheet(f"color: {C['dim']};")

        prep_note = QFrame()
        prep_note.setObjectName("noticeBar")
        nl = QHBoxLayout(prep_note)
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
        main.addWidget(prep_note)
        main.addWidget(scroll, 1)
        main.addWidget(lbl_note)
        accounts_page = QWidget()
        accounts_page.setLayout(main)
        _paint_bg(accounts_page)

        # ---- 页面切换：账号列表 / 设置 / 代理检测 / 关于 ----
        self.page_settings = SettingsPage()
        self.page_settings.mail_path_selected.connect(self.save_mail_path)
        self.page_settings.btn_save.clicked.connect(self.save_settings)
        self.page_settings.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_settings.btn_export.clicked.connect(self.export_backup)
        self.page_health = HealthPage()
        self.page_health.btn_back.clicked.connect(lambda: self._show_page(0))
        self.page_health.btn_start.clicked.connect(self.start_health_check)
        self.page_about = AboutPage()
        self.page_about.btn_back.clicked.connect(lambda: self._show_page(0))
        self.stack = QStackedWidget()
        _paint_bg(self.stack)
        self.stack.addWidget(accounts_page)
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


    def show_settings_page(self):
        if self.stack.currentIndex() != 1:
            self.page_settings.load(self.cfg)
        self._show_page(1)

    def show_about_page(self):
        self._show_page(3)

    # ---------- 代理检测 ----------
    def start_health_check(self):
        if getattr(self, "_health", None) is not None and self._health.isRunning():
            return
        self.page_health.clear_results()
        self.page_health.btn_start.setEnabled(False)
        self.page_health.btn_start.setText("检测中…")
        self._health = HealthChecker(self.cfg, self)
        self._health.item.connect(self.page_health.add_result)
        self._health.finished.connect(self._on_health_done)
        self._health.start()

    def _on_health_done(self):
        self.page_health.btn_start.setEnabled(True)
        self.page_health.btn_start.setText("开始检测")


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
        for idx, acc in enumerate(self.accounts):
            card = self._make_card(acc)
            self._card_widgets[acc["id"]] = card
            self._cards_grid.addWidget(card, idx // 3, idx % 3)
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
            "background: #e8930c; color: #ffffff; font-size: 17px; font-weight: bold; border-radius: 20px;"
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
        """重启进程应用新主题，已打开的浏览器窗口不受影响。"""
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
        name = dlg.result_name()
        if not name:
            return
        acc["name"] = name
        store.save_accounts(self.accounts)
        self._rebuild_cards()


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
                self, "邮箱辅助登录", str(exc)
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

    def open_account(self):
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
            store.effective_cfg(self.cfg, acc), profile, browser=kind
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


    # ---------- 设置 ----------
    def save_mail_path(self, key: str, path: str):
        fields = {
            "mail_accounts_csv": self.page_settings.edt_mail_csv,
            "mail_config_path": self.page_settings.edt_mail_config,
        }
        if key not in fields:
            return
        path = path.strip()
        old_path = self.cfg.get(key, "")
        if path == old_path:
            return
        candidate = {**self.cfg, key: path}
        try:
            if key == "mail_accounts_csv" and path:
                auto_login.load_mailboxes(path)
            elif key == "mail_config_path":
                auto_login.load_mail_api_base(path)
            store.save_config(candidate)
        except (auto_login.MailAssistError, OSError) as exc:
            fields[key].setText(old_path)
            message = (str(exc) if isinstance(exc, auto_login.MailAssistError)
                       else "无法保存设置，请确认软件所在目录可写。原设置已保留。")
            self.page_settings.lbl_saved.setText("邮箱设置未保存，已保留原设置")
            QMessageBox.warning(self, "邮箱设置未保存", message)
            return
        self.cfg = candidate
        fields[key].setText(path)
        self.page_settings.lbl_saved.setText("邮箱设置已自动保存 ✓")

    def save_settings(self):
        old_theme = self.cfg.get("theme", "light")
        candidate = {**self.cfg, **self.page_settings.collect()}
        try:
            if candidate.get("mail_accounts_csv"):
                auto_login.load_mailboxes(candidate["mail_accounts_csv"])
            auto_login.load_mail_api_base(candidate.get("mail_config_path", ""))
            store.save_config(candidate)
        except (auto_login.MailAssistError, OSError) as exc:
            message = (str(exc) if isinstance(exc, auto_login.MailAssistError)
                       else "无法保存设置，请确认软件所在目录可写。原设置已保留。")
            self.page_settings.lbl_saved.setText("未保存，请检查提示后重试")
            QMessageBox.warning(self, "设置未保存", message)
            return
        self.cfg = candidate
        self._refresh_proxy_label()
        if self.cfg.get("theme") != old_theme:
            self.page_settings.lbl_saved.setText("已保存 ✓（外观下次打开软件时生效）")
        else:
            self.page_settings.lbl_saved.setText("已保存 ✓")

    # ---------- 备份导出 ----------
    def export_backup(self):
        if self._launched:
            QMessageBox.warning(self, "导出备份", "请先关闭所有账号浏览器后再导出。若浏览器由外部打开，也请先手动关闭，避免备份不完整。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("导出备份")
        box.setText("选备份范围：\n\n完整备份：账号列表、设置和浏览器配置文件；可能含登录凭证，不保证跨电脑恢复登录态。\n轻量备份：仅账号列表和设置。\n\n请先手动关闭所有账号浏览器。备份可能包含邮箱、代理配置、浏览历史等敏感信息，请妥善保管。当前版本不提供导入恢复入口。")
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


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    cfg = store.load_config()
    # 对话框颜色方案跟随外观设置。
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
    try:
        window = MainWindow(cfg)
    except store.AccountDataError as exc:
        QMessageBox.critical(None, "账号数据无法读取", str(exc))
        return 1
    window.show()
    return app.exec()
