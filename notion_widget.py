# -*- coding: utf-8 -*-
"""
Notion「今日任务」桌面挂件
- Win11 Acrylic 毛玻璃 / 无边框 / 不抢焦点 / 层级可切换（悬浮置顶 ↔ 只在桌面）
- 显示当天任务，点勾选同步 Notion，点任务名跳转 Notion
- 每小时 + 手动刷新；跨天自动切换；托盘常驻
"""
import sys, os, json, ssl, time, webbrowser, datetime, ctypes, threading, subprocess, base64
import ctypes.wintypes
import urllib.request, urllib.error
import winreg
# 关键：网络请求整条链路会惰性加载的模块，必须在主线程一次性预导入。
# 若留到 QThread 子线程首次 import，importlib 的文件查找会触发 access violation（pythonw 必现）。
import socket, http.client, selectors
import encodings.idna, encodings.utf_8, encodings.ascii, encodings.latin_1
import email.parser, email.message, email.utils, email.header
import zlib, gzip
# 关键修复（pythonw 必现崩溃）：进程加载 Qt6 DLL 后，若首个 socket 由后台线程创建，
# 会与代理软件注入的 Winsock LSP 冲突，在 socket() 处 access violation。
# 必须在 import PyQt6 之前、由主线程先完成 Winsock 首次初始化，之后任何线程建 socket 都安全。
for _af, _tk in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM)):
    try:
        _s = socket.socket(_af, _tk); _s.close()
    except Exception:
        pass
from PyQt6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QPushButton, QScrollArea, QFrame, QSystemTrayIcon, QMenu)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint, QRectF, QEvent
from PyQt6.QtGui import QFont, QCursor, QIcon, QPixmap, QPainter, QColor, QAction, QPen, QPainterPath

# 打包成 exe 后 __file__ 位于临时解包目录，配置必须放在 exe 同目录；脚本运行时用脚本目录
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
ICO_PATH = os.path.join(BASE, "app.ico")

class ConfigError(Exception):
    """配置缺失或不可用。单独一类，便于在入口处给出可读提示而不是一条 traceback。"""

REQUIRED_KEYS = ("token", "database_id")
PLACEHOLDER_MARK = "在此填入"

def load_cfg():
    if not os.path.exists(CFG_PATH):
        raise ConfigError(
            "找不到配置文件：\n%s\n\n"
            "请把同目录下的 config.example.json 复制为 config.json，"
            "填入 Notion 集成令牌与数据库 ID 后重试。" % CFG_PATH)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError("配置文件不是合法 JSON：\n%s\n\n%s" % (CFG_PATH, e))
    except OSError as e:
        raise ConfigError("无法读取配置文件：\n%s\n\n%s" % (CFG_PATH, e))
    bad = [k for k in REQUIRED_KEYS
           if not cfg.get(k) or PLACEHOLDER_MARK in str(cfg.get(k))]
    if bad:
        raise ConfigError(
            "配置文件还没填好，缺少或仍是占位值：\n  %s\n\n路径：%s"
            % ("、".join(bad), CFG_PATH))
    return cfg
def save_cfg(cfg):
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ---------------- 系统代理（动态读取，不写死端口） ----------------
def system_proxy():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        en, _ = winreg.QueryValueEx(k, "ProxyEnable")
        if not en: return None
        sv, _ = winreg.QueryValueEx(k, "ProxyServer")
        winreg.CloseKey(k)
        if not sv: return None
        if "=" in sv:
            for part in sv.split(";"):
                if part.lower().startswith("https="): return "http://" + part.split("=",1)[1]
            sv = sv.split(";")[0].split("=",1)[-1]
        if not sv.startswith("http"): sv = "http://" + sv
        return sv
    except Exception:
        return None

# ---------------- Notion API ----------------
# 关键：SSLContext 必须在主线程创建一次并复用，若首次 create_default_context 发生在
# QThread 子线程，OpenSSL 在子线程首次初始化会触发 access violation（pythonw 下必现）。
_SSL_CTX = ssl.create_default_context()
class Notion:
    def __init__(self, cfg):
        self.token = cfg["token"]; self.db = cfg["database_id"]
        self.ver = cfg.get("notion_version", "2022-06-28")
    def _opener(self):
        hs = []
        px = system_proxy()
        if px: hs.append(urllib.request.ProxyHandler({"http": px, "https": px}))
        hs.append(urllib.request.HTTPSHandler(context=_SSL_CTX))
        return urllib.request.build_opener(*hs)
    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request("https://api.notion.com/v1"+path, data=data, method=method)
        r.add_header("Authorization", "Bearer "+self.token)
        r.add_header("Notion-Version", self.ver)
        r.add_header("Content-Type", "application/json")
        with self._opener().open(r, timeout=8) as x:
            return json.loads(x.read().decode())
    def today(self, date_str):
        q = {"filter": {"property": "日期", "date": {"equals": date_str}},
             "sorts": [{"property": "提醒", "direction": "ascending"}]}
        res = self._call("POST", f"/databases/{self.db}/query", q)
        out = []
        for row in res.get("results", []):
            P = row["properties"]
            name = "".join(t.get("plain_text","") for t in P["任务"]["title"]) or "(未命名)"
            seg = "".join(t.get("plain_text","") for t in P["时段"]["rich_text"])
            typ = (P["类型"].get("select") or {}).get("name","") if P["类型"].get("select") else ""
            out.append({"id": row["id"], "name": name, "done": P["完成"]["checkbox"],
                        "seg": seg, "type": typ, "url": row.get("url","")})
        return out
    def set_done(self, pid, val):
        self._call("PATCH", f"/pages/{pid}", {"properties": {"完成": {"checkbox": val}}})

# 网络执行方式：直接在主线程同步跑（QTimer 延迟到事件循环，让“加载中”先绘制）。
# 原因：本机代理软件注入 Winsock LSP，进程加载 Qt6 后，若 socket 由后台线程创建、
# 且与主线程 GUI 操作并发，会在 socket()/create_connection 处 access violation（pythonw 必现）。
# 主线程建 socket 始终安全；每小时仅刷新一次、1~2 秒短暂阻塞可接受，换取零崩溃。
def run_net(fn, on_ok, on_err, delay=40):
    def job():
        try: on_ok(fn())
        except Exception as e:
            if on_err: on_err(f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__)
    QTimer.singleShot(delay, job)

# ---------------- Win32 毛玻璃 / 圆角 / 不抢焦点 ----------------
class ACCENTPOLICY(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]
class WINDOWCOMPATTRDATA(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]

def aarrggbb_to_abgr(c):
    a=(c>>24)&0xff; r=(c>>16)&0xff; g=(c>>8)&0xff; b=c&0xff
    return (a<<24)|(b<<16)|(g<<8)|r

def apply_acrylic(hwnd, color_aarrggbb):
    acc = ACCENTPOLICY(4, 2, aarrggbb_to_abgr(color_aarrggbb), 0)  # 4=Acrylic
    data = WINDOWCOMPATTRDATA(19, ctypes.addressof(acc), ctypes.sizeof(acc))
    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    # Win11 圆角
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception: pass

def make_noactivate(hwnd):
    GWL_EXSTYLE = -20
    WS_EX_NOACTIVATE = 0x08000000; WS_EX_TOOLWINDOW = 0x00000080
    user32 = ctypes.windll.user32
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

# 层级（置顶/贴底）统一用 SetWindowPos 控制，避免在 flags 里固定 Hint 后 Qt 与手动切换相互打架
_u32 = ctypes.windll.user32
_u32.SetWindowPos.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.HWND, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_u32.SetWindowPos.restype = ctypes.c_int
_HWND_TOPMOST = ctypes.wintypes.HWND(-1)
_HWND_NOTOPMOST = ctypes.wintypes.HWND(-2)
_SWP_KEEP = 0x10 | 0x2 | 0x1  # NOACTIVATE | NOMOVE | NOSIZE

def trim_memory():
    """把空闲挂件暂不用的工作集页换出，降低任务管理器显示的物理占用（无明显性能代价）。"""
    try:
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
        k.SetProcessWorkingSetSize.restype = ctypes.c_int
        h = k.GetCurrentProcess()
        mx = ctypes.c_size_t(-1).value  # SIZE_T 最大值，等价 EmptyWorkingSet
        k.SetProcessWorkingSetSize(h, mx, mx)
    except Exception: pass

# ---------------- 自绘勾选框：圆角方框 + 对勾（非蓝色实心块） ----------------
class CheckBox(QCheckBox):
    def __init__(self):
        super().__init__()
        self.setFixedSize(22,22)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    def paintEvent(self, e):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r=QRectF(3,3,16,16)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor("#3fb96b"))
            p.drawRoundedRect(r,4.5,4.5)
            pen=QPen(QColor("#ffffff"),2.3)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            path=QPainterPath()
            path.moveTo(6.6,11.2); path.lineTo(9.4,13.9); path.lineTo(15.4,7.4)
            p.drawPath(path)
        else:
            p.setPen(QPen(QColor(255,255,255,120),1.6))
            p.setBrush(QColor(255,255,255,22))
            p.drawRoundedRect(r,4.5,4.5)
        p.end()

# ---------------- 任务行 ----------------
class TaskRow(QFrame):
    toggled = pyqtSignal(str, bool)
    def __init__(self, t):
        super().__init__(); self.t = t
        h = QHBoxLayout(self); h.setContentsMargins(12,6,12,6); h.setSpacing(10)
        self.cb = CheckBox(); self.cb.setChecked(t["done"])
        self.cb.stateChanged.connect(self._on_cb)
        self.lbl = QLabel(t["name"]); self.lbl.setWordWrap(True)
        self.lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        meta = t["seg"] or t["type"]
        self.meta = QLabel(meta); self.meta.setObjectName("meta")
        self.meta.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        h.addWidget(self.cb); h.addWidget(self.lbl, 1); h.addWidget(self.meta)
        self._apply_style()
    def _on_cb(self, st):
        v = (st == Qt.CheckState.Checked.value)
        self._set_done_look(v)
        self.toggled.emit(self.t["id"], v)
    def _set_done_look(self, done):
        f = self.lbl.font(); f.setStrikeOut(done); self.lbl.setFont(f)
        self.lbl.setObjectName("done" if done else "todo")
        self.lbl.style().unpolish(self.lbl); self.lbl.style().polish(self.lbl)
        self.meta.setObjectName("metadone" if done else "meta")
        self.meta.style().unpolish(self.meta); self.meta.style().polish(self.meta)
    def _apply_style(self): self._set_done_look(self.t["done"])
    def set_checked_quiet(self, v):
        self.cb.blockSignals(True); self.cb.setChecked(v); self.cb.blockSignals(False)
        self._set_done_look(v); self.cb.update()
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self.t["url"]: webbrowser.open(self.t["url"])
        super().mousePressEvent(e)

# ---------------- 主挂件 ----------------
class Widget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.notion = Notion(self.cfg)
        self.tasks = []; self.cur_date = ""; self.threads = []
        self._drag = None
        # 层级可切换：on_top=true 悬浮置顶（免疫 Win+D，同豆包球）；false 只贴桌面底层
        self._apply_window_flags()
        self.setWindowTitle("Notion今日任务挂件")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        # 默认位置按当前屏幕可用区域算（贴右上角），不写死某个分辨率下的坐标；
        # 已保存的 pos 也要钳制回屏幕内——配置可能是从更大分辨率的机器带过来的。
        w = self.cfg.get("width", 364)
        ag = QApplication.primaryScreen().availableGeometry()
        saved = self.cfg.get("pos") or {}
        x = min(max(int(saved.get("x", ag.right() - w - 8)), ag.left()), max(ag.left(), ag.right() - w))
        y = min(max(int(saved.get("y", ag.top() + 60)), ag.top()), max(ag.top(), ag.bottom() - 120))
        self.setGeometry(x, y, w, 200)
        self._build_ui()
        # 定时器：每 60s 检查跨天；按 refresh_minutes 自动刷新；周期回收物理工作集
        self.tick = QTimer(self); self.tick.timeout.connect(self._on_tick); self.tick.start(60000)
        mins = max(1, int(self.cfg.get("refresh_minutes", 60)))
        self.refresh_t = QTimer(self); self.refresh_t.timeout.connect(self.refresh)
        self.refresh_t.start(mins * 60000)
        self.trim_t = QTimer(self); self.trim_t.timeout.connect(trim_memory); self.trim_t.start(120000)
        self.refresh()
        QTimer.singleShot(5000, trim_memory)
        QTimer.singleShot(12000, trim_memory)
    def _build_ui(self):
        root = QVBoxLayout(self); root.setContentsMargins(2,2,2,8); root.setSpacing(0)
        # 标题栏
        bar = QHBoxLayout(); bar.setContentsMargins(14,10,8,6); bar.setSpacing(6)
        self.title = QLabel("今日任务"); self.title.setObjectName("title")
        self.date_lbl = QLabel(""); self.date_lbl.setObjectName("date")
        self.btn_r = QPushButton("⟳"); self.btn_r.setObjectName("iconbtn")
        self.btn_r.setFixedSize(26,26); self.btn_r.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_r.clicked.connect(self.refresh)
        self.btn_x = QPushButton("—"); self.btn_x.setObjectName("iconbtn")
        self.btn_x.setFixedSize(26,26); self.btn_x.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_x.clicked.connect(self.hide)
        # 层级开关：悬浮置顶 ↔ 只在桌面
        self.btn_pin = QPushButton("顶"); self.btn_pin.setObjectName("iconbtn")
        self.btn_pin.setFixedSize(28,26); self.btn_pin.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_pin.clicked.connect(self._toggle_pin); self._update_pin_btn()
        bar.addWidget(self.title); bar.addWidget(self.date_lbl); bar.addStretch(1)
        bar.addWidget(self.btn_pin); bar.addWidget(self.btn_r); bar.addWidget(self.btn_x)
        root.addLayout(bar)
        line = QFrame(); line.setFrameShape(QFrame.Shape.HLine); line.setObjectName("line")
        root.addWidget(line)
        # 滚动任务区
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame); self.scroll.setObjectName("scroll")
        self.box = QWidget(); self.rows_v = QVBoxLayout(self.box)
        self.rows_v.setContentsMargins(2,6,2,6); self.rows_v.setSpacing(1)
        self.rows_v.addStretch(1)
        self.scroll.setWidget(self.box); root.addWidget(self.scroll, 1)
        # 底部状态
        self.status = QLabel("加载中…"); self.status.setObjectName("status")
        self.status.setContentsMargins(14,4,14,0)
        root.addWidget(self.status)
        self.setStyleSheet(QSS)
    # ---- 数据 ----
    def refresh(self):
        self.cur_date = datetime.date.today().isoformat()
        wd = "周一周二周三周四周五周六周日"[datetime.date.today().weekday()*2:][:2]
        self.date_lbl.setText(f"{datetime.date.today().strftime('%m/%d')} {wd}")
        self.status.setText("加载中…"); self.btn_r.setText("…")
        run_net(lambda: self.notion.today(self.cur_date), self._render, self._fail)
    def _clear_rows(self):
        while self.rows_v.count()>1:
            it = self.rows_v.takeAt(0); w=it.widget()
            if w: w.deleteLater()
    def _render(self, tasks):
        self.tasks = tasks; self._clear_rows(); done=0
        for t in tasks:
            row = TaskRow(t); row.toggled.connect(self._toggle)
            self.rows_v.insertWidget(self.rows_v.count()-1, row)
            done += t["done"]
        if not tasks:
            empty = QLabel("今天没有安排，休息一下")
            empty.setObjectName("empty"); empty.setContentsMargins(14,14,14,14)
            self.rows_v.insertWidget(self.rows_v.count()-1, empty)
        self.status.setText(f"已完成 {done}/{len(tasks)}    ·    下次自动刷新 {self.cfg.get('refresh_minutes',60)} 分钟内")
        self.btn_r.setText("⟳"); self._fit_height(); QTimer.singleShot(1500, trim_memory)
    def _fail(self, msg):
        self.status.setText(f"拉取失败（{msg}），点 ⟳ 重试"); self.btn_r.setText("⟳")
    def _toggle(self, pid, val):
        run_net(lambda: self.notion.set_done(pid, val),
               lambda r: self._patched(pid, True, val),
               lambda e: self._patched(pid, False, val))
    def _patched(self, pid, ok, val):
        if not ok:  # 回滚
            for i in range(self.rows_v.count()):
                w = self.rows_v.itemAt(i).widget()
                if isinstance(w, TaskRow) and w.t["id"]==pid:
                    w.set_checked_quiet(not val)
            self.status.setText("同步失败，已回滚（点 ⟳ 重试）")
        else:
            done = sum(1 for t in self.tasks if (t["done"] or t["id"]==pid and val))
            for t in self.tasks:
                if t["id"]==pid: t["done"]=val
            d = sum(1 for t in self.tasks if t["done"])
            self.status.setText(f"已完成 {d}/{len(self.tasks)}    ·    已同步 Notion")
    def _on_tick(self):
        if datetime.date.today().isoformat() != self.cur_date:
            self.refresh()
    # ---- 层级：悬浮置顶 / 只在桌面，可随时切换并记住 ----
    def _apply_window_flags(self):
        # 只设无边框工具窗；置顶/贴底交给 _apply_zorder，切换时不重建窗口、不闪、不掉毛玻璃
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
    def _apply_zorder(self):
        after = _HWND_TOPMOST if self.cfg.get("on_top", True) else _HWND_NOTOPMOST
        _u32.SetWindowPos(int(self.winId()), after, 0, 0, 0, 0, _SWP_KEEP)
    def _update_pin_btn(self):
        on = self.cfg.get("on_top", True)
        self.btn_pin.setText("顶" if on else "桌")
        self.btn_pin.setToolTip(
            "当前：悬浮置顶（Win+D 不消失，浮在窗口最上层）\n点击切换为「只在桌面显示」" if on
            else "当前：只在桌面显示（不挡工作窗口）\n点击切换为「悬浮置顶」")
    def _toggle_pin(self):
        self.cfg["on_top"] = not self.cfg.get("on_top", True)
        save_cfg(self.cfg)
        self._apply_zorder()
        self._update_pin_btn()
    # ---- 自适应高度 ----
    def _fit_height(self):
        h = 52 + max(1,len(self.tasks))*42 + 30
        from PyQt6.QtWidgets import QApplication
        sh = QApplication.primaryScreen().availableGeometry().bottom()
        h = min(h, sh - self.y() - 8)
        self.resize(self.width(), max(120,h))
    # ---- 拖动标题栏 ----
    def mousePressEvent(self, e):
        if e.button()==Qt.MouseButton.LeftButton: self._drag=(e.globalPosition().toPoint()-self.pos())
    def mouseMoveEvent(self, e):
        if self._drag: self.move(e.globalPosition().toPoint()-self._drag)
    def mouseReleaseEvent(self, e):
        self._drag=None
        p=self.pos(); self.cfg["pos"]={"x":p.x(),"y":p.y()}; save_cfg(self.cfg)
    # ---- Win32 效果 ----
    def showEvent(self, e):
        hwnd = int(self.winId())
        apply_acrylic(hwnd, int(self.cfg.get("acrylic_color","0xE61A1A20"),16))
        make_noactivate(hwnd)
        self._apply_zorder()
        super().showEvent(e)
    # ---- Win+D / 显示桌面 ----
    # 置顶模式(on_top=true)下显示桌面盖不住挂件（同豆包悬浮球）；贴底模式下挂件只在桌面可见，
    # 被工作窗口压住属预期。不重写 nativeEvent（PyQt6.11 重写它会在窗口创建早期
    # access violation），这里仅保留“万一被最小化则还原”的兜底。
    def changeEvent(self, e):
        if e.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self._unminimize)  # 让本次状态变更先落定，再恢复
        super().changeEvent(e)
    def _unminimize(self):
        if self.isMinimized(): self.showNormal()

QSS = """
QWidget { background: transparent; color:#ECECF1; font-family:'Microsoft YaHei UI','Segoe UI'; font-size:9.5pt; }
QLabel#title { font-size:11pt; font-weight:600; color:#FFFFFF; }
QLabel#date { font-size:9pt; color:#9a9aa6; margin-left:6px; }
QLabel#status { font-size:8.5pt; color:#8b8b96; }
QLabel#empty { color:#8b8b96; font-size:9pt; }
QFrame#line { background: rgba(255,255,255,0.10); max-height:1px; border:none; }
QLabel#todo { color:#ECECF1; font-size:9.5pt; }
QLabel#done { color:#66666f; font-size:9.5pt; }
QLabel#meta { color:#8b8b96; font-size:8.5pt; }
QLabel#metadone { color:#52525b; font-size:8.5pt; }
QPushButton#iconbtn { background:transparent; border:none; color:#b9b9c4; font-size:11pt; border-radius:13px; }
QPushButton#iconbtn:hover { background:rgba(255,255,255,0.12); color:#fff; }
QScrollArea#scroll { background:transparent; border:none; }
QScrollBar:vertical { width:0px; background:transparent; }
"""

def tray_icon():
    if os.path.exists(ICO_PATH):
        return QIcon(ICO_PATH)
    # 回退：代码绘制绿色圆角方块
    pm = QPixmap(32,32); pm.fill(QColor(0,0,0,0))
    p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#3fb96b")); p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(2,2,28,28,7,7); p.end()
    return QIcon(pm)

# ---------------- 开机自启（启动文件夹快捷方式，英文名避免编码问题） ----------------
STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup\NotionTodayWidget.lnk")
LEGACY_LNK = os.path.join(os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup\Notion今日任务挂件.lnk")

def is_autostart():
    return os.path.exists(STARTUP_LNK)

def set_autostart(on):
    if on:
        if getattr(sys, "frozen", False):
            target = sys.executable; args = ""; work = os.path.dirname(sys.executable)
        else:
            target = sys.executable; args = '"%s"' % os.path.abspath(__file__); work = BASE
        ps = (
            "$ws=New-Object -ComObject WScript.Shell;"
            "$l=$ws.CreateShortcut('%s');"
            "$l.TargetPath='%s';$l.Arguments='%s';$l.WorkingDirectory='%s';"
            "$l.WindowStyle=7;$l.Save()"
        ) % (STARTUP_LNK, target, args.replace("'", "''"), work.replace("'", "''"))
        enc = base64.b64encode(ps.encode("utf-16-le")).decode()
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], capture_output=True)
    else:
        try: os.remove(STARTUP_LNK)
        except OSError: pass

def _show_error(title, msg):
    """pythonw 下没有控制台，出错必须主动弹窗，否则用户只看到「双击了但没反应」。
    弹窗失败（如无图形环境）时退回写日志，绝不静默。"""
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(None, "Notion 今日任务 · " + title, msg)
    except Exception:
        pass
    try:
        with open(os.path.join(BASE, "_error.log"), "w", encoding="utf-8") as f:
            f.write(title + "\n\n" + msg + "\n")
    except Exception:
        pass


_mutex_handle = None

def acquire_single_instance():
    """已有实例在运行时返回 False。
    用命名互斥量而不是锁文件：进程崩溃时内核自动释放，不会留下死锁导致再也起不来。"""
    global _mutex_handle
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k.CreateMutexW(None, 0, "NotionTodayWidget_SingleInstance")
        if not h:
            return True  # 保护机制本身失败时放行，不要因此挡住用户
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _mutex_handle = h  # 持有到进程结束，互斥量才不会被回收
        return True
    except Exception:
        return True


def main():
    if not acquire_single_instance():
        _show_error("已经在运行",
            "Notion 今日任务挂件已经在运行了。\n\n"
            "请在任务栏通知区域找到绿色对勾图标，左键单击即可显示窗口。")
        return 3
    try: socket.getaddrinfo("127.0.0.1", 1, type=socket.SOCK_STREAM)  # 主线程预热 DNS
    except Exception: pass
    # 迁移旧版中文名自启快捷方式：若旧的存在则保持开启（创建英文名新快捷方式），再删旧的
    try:
        if os.path.exists(LEGACY_LNK) and not os.path.exists(STARTUP_LNK):
            set_autostart(True)
        if os.path.exists(LEGACY_LNK):
            os.remove(LEGACY_LNK)
    except OSError: pass
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = Widget()
    tray = QSystemTrayIcon(tray_icon()); tray.setToolTip("Notion 今日任务")
    menu = QMenu()
    a_auto = QAction("开机自启", menu); a_auto.setCheckable(True); a_auto.setChecked(is_autostart())
    a_auto.triggered.connect(lambda checked: set_autostart(checked))
    a_show = QAction("显示/隐藏", menu); a_show.triggered.connect(lambda: w.hide() if w.isVisible() else w.show())
    a_ref = QAction("立即刷新", menu); a_ref.triggered.connect(w.refresh)
    a_quit = QAction("退出", menu); a_quit.triggered.connect(app.quit)
    menu.addAction(a_auto); menu.addSeparator()
    menu.addAction(a_show); menu.addAction(a_ref); menu.addSeparator(); menu.addAction(a_quit)
    tray.setContextMenu(menu); tray.activated.connect(lambda r: w.show() if r==QSystemTrayIcon.ActivationReason.Trigger else None)
    tray.show()
    w.show()
    QTimer.singleShot(4000, trim_memory)
    sys.exit(app.exec())

if __name__ == "__main__":
    import traceback
    try:  # pythonw 无控制台，兜底标准流，避免第三方库写 stdout 崩溃
        if sys.stdout is None: sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None: sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception: pass
    try:
        sys.exit(main())
    except ConfigError as e:
        # 首次运行最常见的失败就是配置问题，单独给可读提示，不要只留一条 traceback
        _show_error("配置有问题", str(e))
        sys.exit(2)
    except Exception:
        try:
            with open(os.path.join(BASE,"_crash.log"), "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
        except Exception: pass
        _show_error("启动失败", "程序启动时出错，详情已写入：\n%s\n\n%s"
                    % (os.path.join(BASE, "_crash.log"), traceback.format_exc(limit=3)))
        sys.exit(1)

