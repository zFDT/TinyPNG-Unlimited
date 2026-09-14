# -*- coding: utf-8 -*-
"""
TinyPNG-Unlimited 图形界面。

设计取舍（写给后来改这个文件的人）：

1. **只用 Tkinter（标准库）**。不引入界面框架，PyInstaller 产物不会因此膨胀，
   也不需要额外打包主题资源。观感靠 ttk 自定义样式 + 一套统一的间距/字号刻度做到
   「干净的工具型」，而不是靠第三方皮肤。唯一的可选依赖是 tkinterdnd2（拖拽），
   它缺失时整个界面照常工作，只是不能把文件拖进来。

2. **配置在界面里改，用户不必碰 .env**。写盘时逐行替换 `KEY=` 的右值，
   保留 config.env 里的注释、空行和顺序，所以那份文件依然是可读、可手改的。
   配置的读取入口仍然只有 config.py 一处。

3. **压缩逻辑一行都不复制**。界面只负责「收集参数 → 起工作线程 → 用回调回报进度」，
   真正干活的是 TinyImg.compress_from_file_list。这样 CLI 和 GUI 永远不会行为分叉，
   引擎那边的性能优化（连接池、代理池、跳过无收益下载）GUI 自动全部继承。

4. **尺寸一律走 px()**。Tk 用「点」描述字号（会随系统 DPI 自动缩放），
   但 padding / 宽度这些是裸像素（不会）。高分屏上如果只缩放字体、不缩放间距，
   界面就会显得挤。所以这里按「设计稿像素 → 实际像素」换算一遍（见 px()），
   两块缩放比例一致，界面在 100% / 125% / 150% 下观感才会一致。

线程模型：所有网络/压缩都在工作线程里跑，界面更新一律通过 queue 回到主线程，
避免 Tkinter 的「非主线程操作控件」崩溃。
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser

import requests
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from loguru import logger

from tinypng_unlimited import KeyManager, TinyImg, __version__
from tinypng_unlimited.config import (
    DEFAULTS, Config, get_app_dir, reload_config, save_env_values,
)

try:  # 可选依赖：装了能拖拽，没装其它功能一律照常
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:  # pragma: no cover - 取决于运行环境
    DND_FILES = None
    TkinterDnD = None
    HAS_DND = False

# ---------------- 配色（深色，参照常见的代码编辑器配色） ----------------
BG = '#16181c'            # 窗口底色
SURFACE = '#1f2226'       # 卡片
SURFACE_2 = '#282c31'     # 输入框 / 列表
SURFACE_3 = '#32373d'     # 悬停
LOG_BG = '#131417'
BORDER = '#33383f'
FG = '#e6edf3'
FG_DIM = '#8b949e'
FG_MUTED = '#6b7480'
ACCENT = '#4c8dff'
ACCENT_HI = '#6ba3ff'
ACCENT_SOFT = '#25344a'
SEL_BG = '#2b4a7a'
OK = '#3fb950'
WARN = '#d29922'
ERR = '#f85149'

#: 设计稿基准尺寸（100% 缩放下的像素），实际窗口按屏幕 DPI 等比放大
BASE_W, BASE_H = 1040, 806

# ---------------- 缩放 ----------------
_SCALE = 1.0


def px(value) -> int:
    """把设计稿像素换算成当前缩放下应有的像素值。"""
    return int(round(value * _SCALE))


def pxs(*values):
    """px() 的元组版，方便直接喂给 padx / pady。"""
    return tuple(px(v) for v in values)


#: 图片后缀，与引擎 compress_from_dir 的默认正则保持一致
IMG_REG = re.compile(r'.*\.(jpe?g|png|svga)$', re.IGNORECASE)

#: 「输出到子目录」时用的目录名
OUTPUT_SUBDIR = '_compressed'

#: 界面状态的落盘文件名（窗口位置、上次勾的选项）
STATE_FILE = 'gui_state.json'

#: 需要校验为正整数的配置项
NUMERIC_KEYS = ('KEY_THRESHOLD', 'KEY_USAGE_LIMIT', 'THREAD_NUM',
                'MAX_RETRY', 'UPLOAD_TIMEOUT', 'DOWNLOAD_TIMEOUT')

#: 设置页的表单定义：(键, 显示名, 说明, 控件类型, 分组)
SETTING_FIELDS = [
    ('TINYPNG_API_KEYS', 'TinyPNG API Keys', '多个用逗号分隔；留空则用手动添加/注册的密钥', 'entry', '密钥与申请'),
    ('APIHZ_ID', '接口盒子 ID', 'apihz.cn 个人中心获取；「手动注册」时建临时邮箱要用', 'entry', '密钥与申请'),
    ('APIHZ_KEY', '接口盒子 KEY', '同上', 'entry', '密钥与申请'),
    ('KEY_THRESHOLD', '密钥数量阈值', '可用密钥少于该值时只提醒，不再触发自动申请', 'entry', '密钥与申请'),
    ('KEY_USAGE_LIMIT', '密钥使用上限', '单条密钥用到该次数后切换（TinyPNG 每月 500 次）', 'entry', '密钥与申请'),

    ('PROXY_LIST', '代理列表', '逗号/分号分隔，分散到多个出口 IP 可突破单 IP 限流', 'text', '网络与代理'),
    ('HTTP_PROXY', '单条代理 (HTTP)', '留空表示直连', 'entry', '网络与代理'),
    ('HTTPS_PROXY', '单条代理 (HTTPS)', '留空表示直连', 'entry', '网络与代理'),

    ('THREAD_NUM', '并发线程数', '过高会撞上 TinyPNG 按 IP 的限流，建议 8~16', 'entry', '性能'),
    ('MAX_RETRY', '最大重试次数', '单张图片失败后的重试上限', 'entry', '性能'),
    ('UPLOAD_TIMEOUT', '上传超时（秒）', '', 'entry', '性能'),
    ('DOWNLOAD_TIMEOUT', '下载超时（秒）', '', 'entry', '性能'),

    ('LOG_LEVEL', '日志级别', 'DEBUG / INFO / WARNING / ERROR', 'combo', '日志'),
    ('OUTPUT_COMPRESSION_LOG', '输出压缩日志到文件夹', '仅命令行 --log 时生效，界面请用「导出 log.json」', 'combo', '日志'),
]

COMBO_CHOICES = {
    'LOG_LEVEL': ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
    'OUTPUT_COMPRESSION_LOG': ['false', 'true'],
}

#: 这些字段默认以掩码显示。设置页经常被截图/共享，把密钥直接铺在屏幕上没必要。
SECRET_KEYS = ('TINYPNG_API_KEYS', 'APIHZ_KEY')

#: 界面字体候选（按优先级）。第一顺位是各平台自带的中文界面字体。
UI_FONT_STACK = ('Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Hiragino Sans GB',
                 'Noto Sans CJK SC', 'Source Han Sans SC', 'Segoe UI', 'Helvetica Neue',
                 'DejaVu Sans')
MONO_FONT_STACK = ('JetBrains Mono', 'Cascadia Mono', 'Consolas', 'Menlo',
                   'DejaVu Sans Mono', 'Courier New')


def hide_console_if_owned():
    """
    Windows 下双击 exe 时，把随之弹出的黑色控制台窗口藏起来。

    关键判断：用 GetConsoleProcessList 数一下这个控制台上挂了几个进程。
    只有「自己一个」才说明控制台是双击 exe 时新开的，可以安全隐藏；
    如果是从已有终端里敲命令启动的，控制台上会有第二个进程，
    这时隐藏会把用户自己的终端一起藏掉——那显然不是我们想要的。

    非 Windows 平台直接跳过。
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return
        pid = k32.GetCurrentProcessId()
        buf = (ctypes.c_uint * 8)()
        count = k32.GetConsoleProcessList(buf, 8)
        if count == 1 and buf[0] == pid:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        # 纯装饰性操作，失败绝不影响功能
        pass


def open_in_explorer(path: str):
    """用系统文件管理器打开目录（跨平台）。"""
    try:
        if sys.platform == 'win32':
            os.startfile(path)  # noqa: S606
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception as e:
        logger.warning('打开目录失败: {} ({})', path, e)


def reveal_in_explorer(path: str):
    """在文件管理器里定位到某个文件（选中的状态）。"""
    try:
        if sys.platform == 'win32':
            # explorer 的 /select 需要逗号紧贴路径，拆成两个参数在某些 shell 下会失效
            subprocess.Popen(['explorer', f'/select,{os.path.normpath(path)}'])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            open_in_explorer(os.path.dirname(path))
    except Exception as e:
        logger.warning('定位文件失败: {} ({})', path, e)


def _elide_middle(text: str, max_chars: int) -> str:
    """
    把过长文本从中间省略，保留头尾。

    路径类文本的头（盘符/根目录）和尾（文件名）信息量最大，中间层级反而可省，
    所以这里省略中段而不是尾部。用单个省略号「…」连接。

    :param text: 原始文本
    :param max_chars: 目标最大字符数（含省略号）。<=0 或文本本就不超长时原样返回。
    :return: 省略后的文本，长度不超过 max_chars
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars == 1:
        return '…'
    head = (max_chars - 1) // 2
    tail = max_chars - 1 - head
    return text[:head] + '…' + (text[-tail:] if tail else '')


class _Tooltip:
    """
    极简悬浮提示。

    界面上有几个按钮（「清空」「移除选中」）光看文字不够明确，鼠标停一下给一句解释，
    比在界面上堆一堆说明文字干净得多。
    """
    def __init__(self, widget, text, font, delay=550):
        self.widget = widget
        self.text = text
        self.font = font
        self.delay = delay
        self._after = None
        self._win = None
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _event=None):
        self._cancel()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except Exception:
            self._after = None

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _hide(self, _event=None):
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None

    def _show(self):
        self._after = None
        if self._win is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + px(10)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(6)
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f'+{x}+{y}')
            tk.Label(win, text=self.text, bg='#0c0e11', fg=FG, bd=0,
                     padx=px(9), pady=px(5), font=self.font,
                     highlightthickness=1, highlightbackground=BORDER).pack()
            self._win = win
        except Exception:
            self._win = None


def create_root():
    """
    创建 Tk 根窗口。

    优先用 TkinterDnD.Tk()（天然支持把文件拖进来）；tkdnd 动态库没能加载时
    （例如打包时漏了资源）退回标准 Tk，界面其余部分不受影响——拖拽只是锦上添花，
    绝不能因为它让程序打不开。
    :return: (root, 是否具备拖拽能力)
    """
    if HAS_DND:
        root = None
        try:
            root = TkinterDnD.Tk()
            root._tpu_dnd_ready = True
            logger.debug('拖拽支持已启用（tkdnd {}）', getattr(root, 'TkdndVersion', '?'))
            return root, True
        except Exception as e:
            logger.warning('拖拽库加载失败，已退回标准窗口（不影响其它功能）: {}', e)
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
    return tk.Tk(), False


class GuiApp:
    """TinyPNG-Unlimited 主窗口。"""

    def __init__(self, root: tk.Tk, dnd_enabled: bool = None):
        self.root = root
        self.targets: list = []          # 待处理的文件/文件夹路径
        self.ui_queue = queue.Queue()    # 工作线程 -> 主线程 的消息通道
        self.worker: threading.Thread = None
        self.stop_event = threading.Event()
        self.running = False
        self.setting_widgets: dict = {}
        self.secret_widgets: list = []
        self.key_items: list = []
        self.last_output_dir: str = None
        self._size_gen = 0               # 待处理体积统计的代次，用于丢弃过期结果
        self._size_job = None

        if dnd_enabled is None:
            dnd_enabled = bool(getattr(root, '_tpu_dnd_ready', False))
        self.dnd_enabled = dnd_enabled

        self._state = self._read_state()
        self.ui_family, self.mono_family = self._resolve_fonts()
        self.f = self._make_fonts()

        # 软件名只在这里出现一次（OS 标题栏）；版本号移入「帮助→关于」，
        # 界面里不再有横幅式的重复标识。
        root.title('TinyPNG-Unlimited')
        root.configure(bg=BG)
        self._apply_scaling()
        # 这两个变量被「设置页」「状态栏」共用，必须在建控件之前就存在，
        # 否则先构建的设置页会在引用它们时抛 AttributeError。
        self.status_var = tk.StringVar(value='就绪')
        self.config_path_var = tk.StringVar(value=os.path.join(get_app_dir(), 'config.env'))
        # 状态栏右侧只显示缩短后的路径（过长时中间省略），完整路径挂在悬浮提示里，
        # 避免长路径把状态栏撑变形。
        self.config_path_display_var = tk.StringVar(value='')

        # 必须在建控件前设好窗口尺寸：theme/样式里的 padding 都是按比例算出来的
        self._apply_geometry()
        self._set_window_icon()

        self._build_style()
        self._build_menu()
        self._build_notebook()
        self._build_statusbar()

        self._install_log_sink()
        self._load_settings_into_form()
        self._load_state()
        self._setup_shortcuts()
        self.refresh_keys(show_log=False)

        logger.info('配置目录: {}', get_app_dir())
        logger.info('就绪。{}点击「开始压缩」。',
                    '把图片或文件夹拖进窗口，或' if self.dnd_enabled else '添加图片或文件夹后')

        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.after(80, self._poll_queue)

    # ==========================================================
    # 缩放 / 字体
    # ==========================================================
    def _apply_scaling(self):
        """按屏幕 DPI 定出设计稿到实际像素的换算系数。"""
        global _SCALE
        try:
            dpi = float(self.root.winfo_fpixels('1i'))
        except Exception:
            dpi = 96.0
        scale = dpi / 96.0 if dpi else 1.0
        _SCALE = min(2.0, max(1.0, scale))

    def _resolve_fonts(self):
        """挑一个当前系统真实存在的中文字体，避免落到字体缺失的兜底字形上。"""
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        ui = next((f for f in UI_FONT_STACK if f in families), 'TkDefaultFont')
        mono = next((f for f in MONO_FONT_STACK if f in families), 'TkFixedFont')
        return ui, mono

    def _make_fonts(self) -> dict:
        u, m = self.ui_family, self.mono_family
        return {
            'title': (u, 16, 'bold'),
            'h2': (u, 11, 'bold'),
            'body': (u, 10),
            'strong': (u, 10, 'bold'),
            'small': (u, 9),
            'tiny': (u, 8),
            'big': (u, 13, 'bold'),
            'mono': (m, 9),
            'mono_big': (m, 10),
        }

    def _apply_geometry(self):
        """
        定窗口大小与位置。

        位置/尺寸优先取上一次退出时记下的值，但要过两道检查：
        尺寸不能小于最小可用尺寸（否则界面会被压坏），坐标不能跑到屏幕外面
        （换了显示器/分辨率之后，旧坐标可能整块落在可视区之外，窗口就「不见了」）。
        """
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        min_w = min(px(940), int(sw * 0.7))
        min_h = min(px(660), int(sh * 0.7))
        self.root.minsize(min_w, min_h)

        w = min(px(BASE_W), int(sw * 0.94))
        h = min(px(BASE_H), int(sh * 0.92))
        x = y = None

        geom = self._state.get('geometry')
        if isinstance(geom, str):
            m = re.match(r'^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$', geom.strip())
            if m:
                gw, gh = int(m.group(1)), int(m.group(2))
                if gw >= min_w and gh >= min_h:
                    w, h = min(gw, int(sw * 0.98)), min(gh, int(sh * 0.98))
                if m.group(3) is not None:
                    x = max(0, min(int(m.group(3)), sw - w))
                    y = max(0, min(int(m.group(4)), sh - h))

        self.root.geometry(f'{w}x{h}+{x}+{y}' if x is not None else f'{w}x{h}')

    def _read_state(self) -> dict:
        path = os.path.join(get_app_dir(), STATE_FILE)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        data = {
            'geometry': self.root.winfo_geometry(),
            'recur': bool(self.var_recur.get()),
            'subdir': bool(self.var_subdir.get()),
            'write_log': bool(self.var_log.get()),
            'show_log': bool(self.var_show_log.get()),
            'tab': self.nb.index(self.nb.select()) if self.nb.select() else 0,
        }
        try:
            with open(os.path.join(get_app_dir(), STATE_FILE), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.debug('界面状态保存失败: {}', e)

    def _load_state(self):
        """把上次的勾选状态填回界面（窗口位置已在 _apply_geometry 里处理）。"""
        for key, var in (('recur', self.var_recur), ('subdir', self.var_subdir),
                         ('write_log', self.var_log)):
            if isinstance(self._state.get(key), bool):
                var.set(self._state[key])
        if isinstance(self._state.get('show_log'), bool):
            self.var_show_log.set(self._state['show_log'])
        else:
            self.var_show_log.set(True)
        self._toggle_log_visibility()
        tab = self._state.get('tab')
        if isinstance(tab, int) and 0 <= tab < 3:
            try:
                self.nb.select(tab)
            except Exception:
                pass
        self._refresh_target_list()

    def _set_window_icon(self):
        """尽量给窗口设置图标；找不到就静默跳过。"""
        candidates = []
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            candidates.append(os.path.join(meipass, 'icon.ico'))
        candidates.append(os.path.join(get_app_dir(), 'icon.ico'))
        if getattr(sys, 'frozen', False):
            candidates.append(sys.executable)
        for path in candidates:
            if path and os.path.exists(path):
                try:
                    self.root.iconbitmap(path)
                    return
                except Exception:
                    continue

    # ==========================================================
    # 外观
    # ==========================================================
    def _build_style(self):
        style = ttk.Style(self.root)
        # clam 是 ttk 里少数允许自由改配色的内置主题
        style.theme_use('clam')
        st, f = style, self.f

        # 全局兜底：先铺一层，再逐个覆盖，避免漏配的控件露白底
        st.configure('.', background=BG, foreground=FG, fieldbackground=SURFACE_2,
                     bordercolor=BORDER, focuscolor=ACCENT, troughcolor=SURFACE_2,
                     selectbackground=SEL_BG, selectforeground='#ffffff',
                     darkcolor=SURFACE_2, lightcolor=SURFACE_2, font=f['body'])

        st.configure('TFrame', background=BG)
        st.configure('Card.TFrame', background=SURFACE)
        st.configure('Bar.TFrame', background=SURFACE)

        st.configure('TLabel', background=BG, foreground=FG)
        st.configure('Card.TLabel', background=SURFACE, foreground=FG)
        st.configure('Dim.TLabel', background=BG, foreground=FG_DIM, font=f['small'])
        st.configure('DimCard.TLabel', background=SURFACE, foreground=FG_DIM, font=f['small'])
        st.configure('Hint.TLabel', background=SURFACE, foreground=FG_MUTED, font=f['tiny'])
        st.configure('CardTitle.TLabel', background=SURFACE, foreground=FG, font=f['h2'])

        # 统计数字：数值大、标签小，一眼能分辨主次
        st.configure('Stat.TLabel', background=SURFACE, foreground=FG, font=f['big'])
        st.configure('StatOk.TLabel', background=SURFACE, foreground=OK, font=f['big'])
        st.configure('StatErr.TLabel', background=SURFACE, foreground=ERR, font=f['big'])
        st.configure('StatAccent.TLabel', background=SURFACE, foreground=ACCENT, font=f['big'])
        st.configure('Pct.TLabel', background=SURFACE, foreground=ACCENT, font=f['h2'])
        st.configure('ResultOk.TLabel', background=SURFACE, foreground=OK, font=f['strong'])
        st.configure('ResultWarn.TLabel', background=SURFACE, foreground=WARN, font=f['strong'])
        st.configure('ResultErr.TLabel', background=SURFACE, foreground=ERR, font=f['strong'])

        st.configure('TButton', background=SURFACE_3, foreground=FG, borderwidth=0,
                     focusthickness=1, padding=pxs(14, 7), font=f['body'])
        st.map('TButton',
               background=[('disabled', SURFACE), ('pressed', BORDER), ('active', '#3d434a')],
               foreground=[('disabled', FG_MUTED)])
        st.configure('Accent.TButton', background=ACCENT, foreground='#ffffff', font=f['strong'])
        st.map('Accent.TButton',
               background=[('disabled', SURFACE), ('pressed', '#3a76dd'), ('active', ACCENT_HI)],
               foreground=[('disabled', FG_MUTED)])
        st.configure('Danger.TButton', background='#4a3235', foreground=ERR)
        st.map('Danger.TButton',
               background=[('disabled', SURFACE), ('pressed', '#5c3c3f'), ('active', '#5f4043')],
               foreground=[('disabled', FG_MUTED)])
        st.configure('Tiny.TButton', background=SURFACE, foreground=FG_DIM,
                     padding=pxs(8, 3), font=f['small'])
        st.map('Tiny.TButton',
               background=[('active', SURFACE_3)], foreground=[('active', FG)])

        st.configure('TNotebook', background=BG, borderwidth=0,
                     tabmargins=pxs(6, 4, 6, 0))
        st.configure('TNotebook.Tab', background=SURFACE, foreground=FG_DIM,
                     padding=pxs(22, 8), borderwidth=0, font=f['body'])
        st.map('TNotebook.Tab',
               background=[('disabled', SURFACE), ('selected', SURFACE_2), ('active', SURFACE_3)],
               foreground=[('disabled', FG_MUTED), ('selected', FG)])

        st.configure('TEntry', fieldbackground=SURFACE_2, foreground=FG,
                     insertcolor=FG, bordercolor=BORDER, borderwidth=1, padding=pxs(6, 4))
        st.map('TEntry', bordercolor=[('focus', ACCENT)])
        st.configure('TCombobox', fieldbackground=SURFACE_2, background=SURFACE_3,
                     foreground=FG, arrowcolor=FG_DIM, bordercolor=BORDER,
                     borderwidth=1, padding=pxs(6, 4))
        st.map('TCombobox',
               fieldbackground=[('readonly', SURFACE_2), ('disabled', SURFACE)],
               bordercolor=[('focus', ACCENT)],
               foreground=[('disabled', FG_MUTED)])

        # 槽色要比卡片底色亮一档：否则 0% 时整条进度条等于隐形，
        # 用户只看到卡片里凭空多出一条分隔线，不知道那是进度条。
        track = '#363b42'
        st.configure('Thick.Horizontal.TProgressbar', background=ACCENT, troughcolor=track,
                     bordercolor=track, lightcolor=ACCENT, darkcolor=ACCENT,
                     thickness=px(10))
        st.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=track,
                     bordercolor=track, lightcolor=ACCENT, darkcolor=ACCENT)

        for orient in ('Vertical', 'Horizontal'):
            st.configure(f'{orient}.TScrollbar', background=SURFACE_3, troughcolor=BG,
                         bordercolor=BG, arrowcolor=FG_DIM, gripcount=0)

        st.configure('TSeparator', background=BORDER)
        st.configure('TLabelframe', background=BG, foreground=FG_DIM, bordercolor=BORDER)
        st.configure('TLabelframe.Label', background=BG, foreground=FG_DIM)

    def _tip(self, widget, text):
        """给控件挂一句悬浮说明。"""
        return _Tooltip(widget, text, self.f['small'])

    def _checkbox(self, parent, text, variable, command=None, bg=SURFACE):
        """
        统一的复选框。

        这里**故意不用 ttk.Checkbutton**：clam 主题下的复选框指示器不受
        -indicatorcolor 影响，未勾选时会画成一个浅色实心方块，看上去和「已勾选」
        几乎一样，语义是反的（实测把 indicatorcolor / 明暗色全部映射到深色也无效）。
        退回原生 tk.Checkbutton 之后，未勾选是空框、勾选是打勾，区分度清楚得多，
        配色仍然跟着卡片走。实验对照见 .workbuddy/_checkbox_variants.png 的生成脚本。
        """
        return tk.Checkbutton(
            parent, text=text, variable=variable, command=command,
            bg=bg, fg=FG, activebackground=bg, activeforeground=FG,
            selectcolor=SURFACE_3, highlightthickness=0, bd=0,
            font=self.f['body'], anchor='w', cursor='hand2',
        )

    def _build_menu(self):
        menubar = tk.Menu(self.root, tearoff=0, bg=SURFACE, fg=FG,
                          activebackground=ACCENT, activeforeground='#ffffff',
                          activeborderwidth=0, bd=0, relief='flat',
                          font=self.f['body'])

        # 菜单栏只保留「文件 / 帮助」：软件名与「压缩/设置/密钥」的导航已由
        # 笔记本页签承担，菜单里再放一份只是重复。「开始压缩/停止」交给按钮与 F5/Esc。
        m_file = tk.Menu(menubar, tearoff=0, **self._menu_kw())
        m_file.add_command(label='添加图片…', accelerator='Ctrl+O', command=self.add_files)
        m_file.add_command(label='添加文件夹…', accelerator='Ctrl+Shift+O',
                           command=self.add_folder)
        m_file.add_separator()
        m_file.add_command(label='清空待处理列表', command=self.clear_targets)
        m_file.add_separator()
        # 从原「操作」菜单迁入
        m_file.add_command(label='打开输出目录', command=self.open_output_dir)
        m_file.add_command(label='打开配置目录', command=lambda: open_in_explorer(get_app_dir()))
        m_file.add_command(label='打开配置文件', command=self.open_config_file)
        m_file.add_separator()
        m_file.add_command(label='退出', accelerator='Alt+F4', command=self._on_close)
        menubar.add_cascade(label='文件', menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0, **self._menu_kw())
        m_help.add_command(label='快捷键', accelerator='F1', command=self.show_shortcuts)
        m_help.add_command(label='关于', command=self.show_about)
        menubar.add_cascade(label='帮助', menu=m_help)

        self.menubar = menubar
        self.root.config(menu=menubar)

    def _menu_kw(self) -> dict:
        return dict(bg=SURFACE, fg=FG, activebackground=ACCENT, activeforeground='#ffffff',
                    activeborderwidth=0, bd=0, relief='flat', font=self.f['body'])

    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill='both', expand=True, padx=pxs(12), pady=pxs(0, 8))

        self.tab_compress = ttk.Frame(self.nb, padding=pxs(12, 10))
        self.tab_settings = ttk.Frame(self.nb, padding=pxs(12, 10))
        self.tab_keys = ttk.Frame(self.nb, padding=pxs(12, 10))
        self.nb.add(self.tab_compress, text='压缩')
        self.nb.add(self.tab_settings, text='设置')
        self.nb.add(self.tab_keys, text='密钥')

        self._build_compress_tab()
        self._build_settings_tab()
        self._build_keys_tab()

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=SURFACE)
        bar.pack(fill='x', side='bottom')
        tk.Frame(bar, bg=BORDER, height=1).pack(fill='x', side='top')
        inner = ttk.Frame(bar, style='Bar.TFrame', padding=pxs(14, 7))
        inner.pack(fill='x')
        ttk.Label(inner, textvariable=self.status_var, style='Card.TLabel').pack(side='left')
        path_label = ttk.Label(inner, textvariable=self.config_path_display_var,
                               style='DimCard.TLabel')
        path_label.pack(side='right')
        # 完整路径挂在悬浮提示里（缩短后的标签看不全时，鼠标停一下即可）
        self.tip_config_path = self._tip(path_label, self.config_path_var.get())
        self._update_config_path_label()

    def _update_config_path_label(self):
        """刷新状态栏右侧的配置路径显示（中间省略 + 悬浮提示放完整值）。"""
        full = self.config_path_var.get()
        self.config_path_display_var.set(_elide_middle(full, 48))
        tip = getattr(self, 'tip_config_path', None)
        if tip is not None:
            tip.text = full

    # ----------------------------------------------------------
    def _make_card(self, parent, title=None, right_hint=None):
        """
        造一张卡片：1px 描边 + 卡片底色。

        Tk 没有圆角/阴影，能做出「卡片感」的只有描边和深浅对比，
        所以这里统一用「深色外框 + 略浅内层」来分层，替代实际不存在的阴影。

        :return: (外层容器, 内容容器, 标题行或 None)
                 内容容器是一层刻意留白的子框——调用方只往它里面放东西。
                 少了这一层，标题行（pack）和正文（grid）会抢同一个容器的几何管理器，
                 直接抛 TclError。
        """
        outer = tk.Frame(parent, bg=BORDER)
        inner = ttk.Frame(outer, style='Card.TFrame', padding=pxs(12, 9))
        inner.pack(fill='both', expand=True, padx=1, pady=1)
        header = None
        if title is not None:
            header = ttk.Frame(inner, style='Card.TFrame')
            header.pack(fill='x', pady=(0, px(7)))
            ttk.Label(header, text=title, style='CardTitle.TLabel').pack(side='left')
            if right_hint is not None:
                ttk.Label(header, text=right_hint,
                          style='DimCard.TLabel').pack(side='right')
        content = ttk.Frame(inner, style='Card.TFrame')
        content.pack(fill='both', expand=True)
        return outer, content, header

    # ==========================================================
    # 「压缩」页
    # ==========================================================
    def _build_compress_tab(self):
        tab = self.tab_compress
        tab.columnconfigure(0, weight=1)
        # 待处理列表 : 日志区 = 3 : 2。两者都给权重，多余空间按这个比例分。
        tab.rowconfigure(2, weight=3)
        tab.rowconfigure(5, weight=2)

        self._build_toolbar(tab)
        self._build_notice(tab)
        self._build_target_list(tab)
        self._build_options(tab)
        self._build_status_card(tab)
        self._build_log_card(tab)
        self._build_action_bar(tab)

        self._register_dnd()

    # ---- 工具条 ----
    def _build_toolbar(self, tab):
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky='ew', pady=(0, px(8)))

        self.btn_add_files = ttk.Button(bar, text='添加图片', command=self.add_files)
        self.btn_add_files.pack(side='left')
        self.tip_add_files = self._tip(self.btn_add_files, '选择一张或多张图片（Ctrl+O）')
        self.btn_add_folder = ttk.Button(bar, text='添加文件夹', command=self.add_folder)
        self.btn_add_folder.pack(side='left', padx=(px(6), 0))
        self.tip_add_folder = self._tip(self.btn_add_folder,
                                        '整个文件夹加入队列（Ctrl+Shift+O）')
        ttk.Separator(bar, orient='vertical').pack(side='left', fill='y', padx=px(10))

        self.btn_remove = ttk.Button(bar, text='移除选中', command=self.remove_selected)
        self.btn_remove.pack(side='left')
        self.tip_remove = self._tip(self.btn_remove, '从列表里移除选中的项（Delete）')
        self.btn_clear = ttk.Button(bar, text='清空', command=self.clear_targets)
        self.btn_clear.pack(side='left', padx=(px(6), 0))
        self.tip_clear = self._tip(self.btn_clear, '清空整个待处理列表')

        ttk.Button(bar, text='打开配置目录',
                   command=lambda: open_in_explorer(get_app_dir())).pack(side='right')

    # ---- 顶部提示条（只在缺少密钥等需要用户动手时出现） ----
    def _build_notice(self, tab):
        self.notice_var = tk.StringVar(value='')
        # 这里用 tk.Frame 而不是 ttk：需要「警示色卡片」的底色，
        # 走 ttk 样式表反而要绕一圈才能改底色。
        notice_bg = '#2a2312'
        outer = tk.Frame(tab, bg='#4a3c18')
        row = tk.Frame(outer, bg=notice_bg)
        row.pack(fill='both', expand=True, padx=1, pady=1)
        tk.Label(row, text='!', bg=notice_bg, fg=WARN, font=self.f['strong']).pack(
            side='left', padx=pxs(12, 8))
        tk.Label(row, textvariable=self.notice_var, bg=notice_bg, fg=WARN,
                 font=self.f['small'], anchor='w', justify='left').pack(side='left')
        self.notice_btn = tk.Button(
            row, text='前往密钥页', command=lambda: self.nb.select(2),
            bg='#4a3c18', fg=WARN, activebackground='#5f4d1e', activeforeground='#ffffff',
            bd=0, relief='flat', padx=px(10), pady=px(3), font=self.f['small'],
            cursor='hand2', highlightthickness=0)
        self.notice_btn.pack(side='right', padx=px(10), pady=px(6))
        self.notice_outer = outer
        outer.grid(row=1, column=0, sticky='ew', pady=(0, px(8)))
        outer.grid_remove()   # 默认不占位置

    # ---- 待处理列表 ----
    def _build_target_list(self, tab):
        outer, content, header = self._make_card(tab, '待处理列表')
        outer.grid(row=2, column=0, sticky='nsew')

        self.detail_var = tk.StringVar(value='')
        ttk.Label(header, textvariable=self.detail_var,
                  style='DimCard.TLabel').pack(side='right')
        self.count_var = tk.StringVar(value='0 项')
        ttk.Label(header, textvariable=self.count_var,
                  style='Card.TLabel').pack(side='right', padx=pxs(0, 8))

        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        # 列表区：外层再用 BORDER 色包一圈，拖拽悬停时把这一圈点亮成强调色，
        # 这样「可以放这里」的反馈不依赖额外控件，也不会让布局跳动。
        self.drop_border = tk.Frame(content, bg=BORDER)
        self.drop_border.grid(row=0, column=0, sticky='nsew')
        body = tk.Frame(self.drop_border, bg=SURFACE_2)
        body.pack(fill='both', expand=True, padx=1, pady=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.drop_body = body

        self.listbox = tk.Listbox(
            body, selectmode='extended', activestyle='none', height=6,
            bg=SURFACE_2, fg=FG, selectbackground=SEL_BG, selectforeground='#ffffff',
            highlightthickness=0, borderwidth=0, font=self.f['mono'],
            disabledforeground=FG_MUTED,
        )
        self.listbox.grid(row=0, column=0, sticky='nsew')
        vsb = ttk.Scrollbar(body, orient='vertical', command=self.listbox.yview)
        vsb.grid(row=0, column=1, sticky='ns')
        hsb = ttk.Scrollbar(body, orient='horizontal', command=self.listbox.xview)
        hsb.grid(row=1, column=0, sticky='ew')
        self.listbox.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # 空状态：列表为空时盖一层引导，告诉用户下一步该干什么
        self.empty_state = tk.Frame(body, bg=SURFACE_2)
        tk.Label(self.empty_state,
                 text='↓' if self.dnd_enabled else '＋',
                 bg=SURFACE_2, fg=FG_MUTED, font=(self.ui_family, 22)).pack(
            pady=(0, px(6)))
        tk.Label(self.empty_state,
                 text='把图片或文件夹拖到这里' if self.dnd_enabled
                 else '还没有待处理的图片',
                 bg=SURFACE_2, fg=FG_DIM, font=self.f['strong']).pack()
        self.empty_hint = tk.Label(
            self.empty_state, bg=SURFACE_2, fg=FG_MUTED, font=self.f['small'],
            text=('支持 png / jpg / jpeg / svga；也可以点下面的按钮添加'
                  if self.dnd_enabled else '支持 png / jpg / jpeg / svga；点下面的按钮添加'))
        self.empty_hint.pack(pady=(px(2), px(10)))
        empty_btns = tk.Frame(self.empty_state, bg=SURFACE_2)
        empty_btns.pack()
        for text, cmd in (('添加图片', self.add_files), ('添加文件夹', self.add_folder)):
            tk.Button(empty_btns, text=text, command=cmd, bg=SURFACE_3, fg=FG,
                      activebackground=BORDER, activeforeground=FG, bd=0, relief='flat',
                      padx=px(14), pady=px(5), font=self.f['small'],
                      cursor='hand2', highlightthickness=0).pack(side='left', padx=px(4))

        # 右键菜单
        self.list_menu = tk.Menu(self.root, tearoff=0, **self._menu_kw())
        self.list_menu.add_command(label='打开所在位置', command=self._open_selected_location)
        self.list_menu.add_command(label='在文件管理器中打开', command=self._open_selected_dir)
        self.list_menu.add_separator()
        self.list_menu.add_command(label='移除选中', command=self.remove_selected)
        self.list_menu.add_command(label='清空列表', command=self.clear_targets)
        self.listbox.bind('<Button-3>', self._popup_list_menu)
        self.listbox.bind('<Double-Button-1>', lambda e: self._open_selected_location())
        self.listbox.bind('<Delete>', lambda e: (self.remove_selected(), 'break')[1])
        self.listbox.bind('<BackSpace>', lambda e: (self.remove_selected(), 'break')[1])
        self.listbox.bind('<Control-a>', self._select_all_targets)
        self.listbox.bind('<Control-A>', self._select_all_targets)

    def _popup_list_menu(self, event):
        try:
            index = self.listbox.nearest(event.y)
        except Exception:
            index = -1
        if index >= 0 and index < self.listbox.size():
            if index not in self.listbox.curselection():
                self.listbox.selection_clear(0, 'end')
                self.listbox.selection_set(index)
            self.listbox.activate(index)
        try:
            self.list_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.list_menu.grab_release()

    def _select_all_targets(self, _event=None):
        self.listbox.selection_set(0, 'end')
        return 'break'

    def _selected_targets(self) -> list:
        return [self.targets[i] for i in self.listbox.curselection() if i < len(self.targets)]

    def _open_selected_location(self):
        targets = self._selected_targets()
        if targets:
            reveal_in_explorer(targets[0])

    def _open_selected_dir(self):
        targets = self._selected_targets()
        if targets:
            path = targets[0]
            open_in_explorer(path if os.path.isdir(path) else os.path.dirname(path))

    # ---- 选项 ----
    def _build_options(self, tab):
        row = ttk.Frame(tab)
        row.grid(row=3, column=0, sticky='ew', pady=(px(8), px(10)))

        self.var_recur = tk.BooleanVar(value=True)
        self.var_subdir = tk.BooleanVar(value=False)
        self.var_log = tk.BooleanVar(value=False)

        self.cb_recur = self._checkbox(row, '递归子文件夹', self.var_recur,
                                       command=self._refresh_target_list)
        self.cb_recur.pack(side='left')
        self.tip_recur = self._tip(self.cb_recur, '连同子文件夹里的图片一起处理')
        self.cb_subdir = self._checkbox(row, f'输出到 {OUTPUT_SUBDIR} 子目录',
                                        self.var_subdir, command=self._update_output_hint)
        self.cb_subdir.pack(side='left', padx=(px(18), 0))
        self.tip_subdir = self._tip(
            self.cb_subdir, '压缩结果另存到子目录，原图保持不动；不勾选则直接覆盖原图')
        self.cb_log = self._checkbox(row, '导出 log.json', self.var_log)
        self.cb_log.pack(side='left', padx=(px(18), 0))
        self.tip_log = self._tip(self.cb_log, '把本次压缩明细写到输出目录的 log.json')

        self.output_hint_var = tk.StringVar(value='')
        ttk.Label(row, textvariable=self.output_hint_var,
                  style='Dim.TLabel').pack(side='right')
        self._update_output_hint()

    def _update_output_hint(self):
        if self.var_subdir.get():
            self.output_hint_var.set(f'结果写入 {OUTPUT_SUBDIR}/，原图不变')
        else:
            self.output_hint_var.set('结果直接覆盖原图（不可撤销）')

    # ---- 进度 + 统计 ----
    def _build_status_card(self, tab):
        outer, inner, header = self._make_card(tab, '进度')
        outer.grid(row=4, column=0, sticky='ew', pady=(0, px(10)))

        self.pct_var = tk.StringVar(value='0%')
        ttk.Label(header, textvariable=self.pct_var, style='Pct.TLabel').pack(side='right')

        self.progress = ttk.Progressbar(inner, mode='determinate', maximum=100,
                                        style='Thick.Horizontal.TProgressbar')
        self.progress.pack(fill='x')

        line = ttk.Frame(inner, style='Card.TFrame')
        line.pack(fill='x', pady=(px(6), 0))
        self.progress_var = tk.StringVar(value='等待开始')
        ttk.Label(line, textvariable=self.progress_var,
                  style='DimCard.TLabel').pack(side='left')

        # 结果横幅：跑完才出现，出现时是一行明确的结论 + 后续动作入口
        self.result_row = ttk.Frame(inner, style='Card.TFrame')
        self.result_var = tk.StringVar(value='')
        self.result_label = ttk.Label(self.result_row, textvariable=self.result_var,
                                      style='ResultOk.TLabel')
        self.result_label.pack(side='left')
        self.btn_result_open = ttk.Button(self.result_row, text='打开输出目录',
                                          style='Tiny.TButton', command=self.open_output_dir)
        self.btn_result_open.pack(side='left', padx=px(10))
        self.result_row.pack(fill='x', pady=(px(6), 0))
        self.result_row.pack_forget()

        # 记下分隔线：「结果横幅」重新出现时要靠它定回中间那一行，
        # 否则 pack 会把它排到统计数字下面去。
        self.status_sep = ttk.Separator(inner, orient='horizontal')
        self.status_sep.pack(fill='x', pady=px(8))

        self.stat_vars = {
            'total': tk.StringVar(value='0'),
            'ok': tk.StringVar(value='0'),
            'err': tk.StringVar(value='0'),
            'ratio': tk.StringVar(value='—'),
            'saved': tk.StringVar(value='—'),
            'speed': tk.StringVar(value='—'),
        }
        stats = ttk.Frame(inner, style='Card.TFrame')
        stats.pack(fill='x')
        cells = [
            ('total', '待处理', 'Stat.TLabel'),
            ('ok', '成功', 'StatOk.TLabel'),
            ('err', '失败', 'StatErr.TLabel'),
            ('ratio', '压缩率', 'StatAccent.TLabel'),
            ('saved', '省下空间', 'StatOk.TLabel'),
            ('speed', '速度', 'Stat.TLabel'),
        ]
        # 「有值才上色」：失败数是 0 的时候亮红色只会让人以为出了问题，
        # 还没跑出来的指标（—）同理。所以记下每个格子的强调样式，由 _set_stat 决定用不用。
        self.stat_labels = {}
        self.stat_active_style = {}
        for i, (key, label_text, style_name) in enumerate(cells):
            cell = ttk.Frame(stats, style='Card.TFrame')
            cell.grid(row=0, column=i, sticky='w', padx=(0, px(28)))
            ttk.Label(cell, text=label_text, style='Hint.TLabel').pack(anchor='w')
            label = ttk.Label(cell, textvariable=self.stat_vars[key], style='Stat.TLabel')
            label.pack(anchor='w')
            self.stat_labels[key] = label
            self.stat_active_style[key] = style_name

    # ---- 日志 ----
    def _build_log_card(self, tab):
        outer, content, header = self._make_card(tab, '运行日志')
        outer.grid(row=5, column=0, sticky='nsew')
        self.log_outer = outer

        self.var_show_log = tk.BooleanVar(value=True)
        self.btn_log_toggle = ttk.Button(header, text='隐藏', style='Tiny.TButton',
                                         command=self._toggle_log_visibility)
        self.btn_log_toggle.pack(side='right')
        ttk.Button(header, text='清空', style='Tiny.TButton',
                   command=self.clear_log).pack(side='right', padx=px(6))
        ttk.Button(header, text='复制', style='Tiny.TButton',
                   command=self._copy_log).pack(side='right')

        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        wrap = tk.Frame(content, bg=BORDER)
        wrap.grid(row=0, column=0, sticky='nsew')
        body = tk.Frame(wrap, bg=LOG_BG)
        body.pack(fill='both', expand=True, padx=1, pady=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            body, bg=LOG_BG, fg=FG, insertbackground=FG, highlightthickness=0,
            borderwidth=0, wrap='none', font=self.f['mono'], state='disabled',
            selectbackground=SEL_BG, selectforeground='#ffffff',
            # 必须给一个小的请求高度：tk.Text 默认 24 行（约 360px），
            # 在窗口空间不够时，Tk 会按权重反向压缩其他行，
            # 结果就是「待处理列表被挤成一条缝」。给了 6 行之后，
            # 它依然靠 rowconfigure 的权重向上生长，但不会再抢列表的地方。
            height=6,
        )
        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_vsb = ttk.Scrollbar(body, orient='vertical', command=self.log_text.yview)
        log_vsb.grid(row=0, column=1, sticky='ns')
        self.log_text.configure(yscrollcommand=log_vsb.set)
        self.log_text.tag_configure('info', foreground=FG)
        self.log_text.tag_configure('ok', foreground=OK)
        self.log_text.tag_configure('warn', foreground=WARN)
        self.log_text.tag_configure('err', foreground=ERR)
        self.log_text.tag_configure('dim', foreground=FG_MUTED)

        self.log_menu = tk.Menu(self.root, tearoff=0, **self._menu_kw())
        self.log_menu.add_command(label='复制全部', command=self._copy_log)
        self.log_menu.add_command(label='全选', command=self._select_all_log)
        self.log_menu.add_separator()
        self.log_menu.add_command(label='清空日志', command=self.clear_log)
        self.log_text.bind('<Button-3>', self._popup_log_menu)

        self.log_body = outer

    def _popup_log_menu(self, event):
        try:
            self.log_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_menu.grab_release()

    def _toggle_log_visibility(self):
        show = bool(self.var_show_log.get())
        if show:
            self.log_body.grid()
            self.tab_compress.rowconfigure(5, weight=2)
            self.btn_log_toggle.configure(text='隐藏')
        else:
            self.log_body.grid_remove()
            self.tab_compress.rowconfigure(5, weight=0)
            self.btn_log_toggle.configure(text='显示')

    def _copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_text.get('1.0', 'end-1c'))
        self.status_var.set('日志已复制到剪贴板')

    def _select_all_log(self):
        self.log_text.tag_add('sel', '1.0', 'end-1c')
        return 'break'

    def clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    # ---- 底部动作条 ----
    def _build_action_bar(self, tab):
        bar = ttk.Frame(tab)
        bar.grid(row=6, column=0, sticky='ew', pady=(px(10), 0))

        self.btn_start = ttk.Button(bar, text='开始压缩', style='Accent.TButton',
                                    command=self.start_compress)
        self.btn_start.pack(side='left')
        self.tip_start = self._tip(self.btn_start, '按 F5 也可以开始')
        self.btn_stop = ttk.Button(bar, text='停止', style='Danger.TButton',
                                   command=self.request_stop, state='disabled')
        self.btn_stop.pack(side='left', padx=(px(8), 0))
        self.tip_stop = self._tip(
            self.btn_stop, '未开始的图片会被取消，正在压缩的会先跑完（避免留下半截文件）')

        self.btn_open_output = ttk.Button(bar, text='打开输出目录', state='disabled',
                                         command=self.open_output_dir)
        self.btn_open_output.pack(side='right')
        self.tip_open_output = self._tip(self.btn_open_output, '压缩完成后可直接跳过去看结果')

    def open_output_dir(self):
        path = self.last_output_dir
        if path and os.path.isdir(path):
            open_in_explorer(path)
        else:
            messagebox.showinfo('还没有输出目录', '先压缩一次，或勾选「输出到子目录」后再试。')

    # ---- 拖拽 ----
    def _register_dnd(self):
        if not self.dnd_enabled:
            return
        targets = [w for w in (self.drop_body, self.listbox, self.empty_state,
                               self.drop_border) if w is not None]
        for widget in targets:
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self._on_drop)
                widget.dnd_bind('<<DropEnter>>', self._on_drop_enter)
                widget.dnd_bind('<<DropLeave>>', self._on_drop_leave)
            except Exception as e:
                logger.debug('注册拖拽目标失败（{}）: {}', type(widget).__name__, e)

    def _on_drop_enter(self, _event=None):
        try:
            self.drop_border.configure(bg=ACCENT)
        except Exception:
            pass

    def _on_drop_leave(self, _event=None):
        try:
            self.drop_border.configure(bg=BORDER)
        except Exception:
            pass

    @staticmethod
    def _parse_drop_paths(data) -> list:
        """
        解析拖拽事件里带出来的文件列表。

        **这里刻意不用 tk.splitlist()**：它按 Tcl 列表语法解析，把反斜杠当转义符，
        于是 `D:\\py project\\tip.png` 里的 `\\p`、`\\t` 会被吃掉（`\\t` 直接变成制表符），
        路径失效、文件加不进来——而且不报任何错，只是「拖了没反应」，最难查。
        实测 `{带空格的路径} 不带空格的路径` 这种混合形态下，只有被 {} 包住的那个能活下来。

        tkdnd 对含空格/特殊字符的路径本来就会用 {} 括起来，所以这里只认两种形态：
        花括号包起来的原样路径，以及括号外按空白切分的路径。两者都原样保留反斜杠。
        """
        text = str(data or '').strip()
        if not text:
            return []
        paths, buf, in_brace = [], [], False
        for ch in text:
            if ch == '{':
                in_brace, buf = True, []
            elif ch == '}':
                in_brace = False
                paths.append(''.join(buf))
                buf = []
            elif in_brace:
                buf.append(ch)
            elif ch.isspace():
                if buf:
                    paths.append(''.join(buf))
                    buf = []
            else:
                buf.append(ch)
        if buf:
            paths.append(''.join(buf))

        out = []
        for item in paths:
            item = item.strip().strip('"').strip()
            if not item:
                continue
            # 个别 tkdnd 版本会把列表里的反斜杠再转义一层，遇到不存在的路径时还原一次
            if not os.path.exists(item) and '\\\\' in item:
                alt = item.replace('\\\\', '\\')
                if os.path.exists(alt):
                    item = alt
            out.append(item)
        return out

    @staticmethod
    def _resolve_drop_tokens(tokens) -> tuple:
        """
        把解析出的碎片对到真实路径上，返回 (存在的路径, 找不到的碎片)。

        比单纯过滤多一步拼接：个别 tkdnd 版本或文件管理器会把含空格的路径
        **不加花括号**地丢出来，于是按空白切分会把一个路径切成好几段。
        这里从左往右贪心拼接，把碎片还原回一个真实存在的路径。
        拼接只在「碎片自己不存在」时才会尝试，所以不会把两个正常路径粘在一起。
        """
        existing, missing, i, n = [], [], 0, len(tokens)
        while i < n:
            if os.path.exists(tokens[i]):
                existing.append(tokens[i])
                i += 1
                continue
            merged, end = None, i
            for j in range(i + 2, min(n, i + 8) + 1):
                candidate = ' '.join(tokens[i:j])
                if os.path.exists(candidate):
                    merged, end = candidate, j
                    break
            if merged:
                existing.append(merged)
                i = end
            else:
                missing.append(tokens[i])
                i += 1
        return existing, missing

    def _on_drop(self, event):
        self._on_drop_leave()
        paths, missing = self._resolve_drop_tokens(self._parse_drop_paths(event.data))
        if missing:
            logger.warning('拖入的内容里有 {} 项找不到，已忽略（例如 {}）',
                           len(missing), missing[0])
        if paths:
            self._add_targets(paths)
        else:
            logger.warning('拖入的内容里没有可用路径')
        return 'break'

    # ==========================================================
    # 「设置」页
    # ==========================================================
    def _build_settings_tab(self):
        tab = self.tab_settings
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        head = ttk.Frame(tab)
        head.grid(row=0, column=0, sticky='ew', pady=(0, px(8)))
        ttk.Label(head, text='改完点「保存配置」即刻生效，不需要自己创建或编辑 .env 文件。',
                  style='Dim.TLabel').pack(side='left')

        outer, inner = self._make_scrollable(tab)
        outer.grid(row=1, column=0, sticky='nsew')

        current_group = None
        body = None
        for key, label_text, hint, kind, group in SETTING_FIELDS:
            if group != current_group:
                current_group = group
                card_outer, body, _ = self._make_card(inner, group)
                card_outer.pack(fill='x', pady=(0, px(10)))
                body.columnconfigure(0, minsize=px(150))
                body.columnconfigure(1, weight=1)

            row = self._next_row(body)
            ttk.Label(body, text=label_text, style='Card.TLabel').grid(
                row=row, column=0, sticky='nw', pady=px(4), padx=(0, px(16)))
            cell = ttk.Frame(body, style='Card.TFrame')
            cell.grid(row=row, column=1, sticky='ew', pady=px(4))

            if kind == 'entry':
                widget = ttk.Entry(cell)
                if key in SECRET_KEYS:
                    widget.configure(show='•')
                    self.secret_widgets.append(widget)
                widget.pack(fill='x')
            elif kind == 'text':
                widget = tk.Text(cell, height=3, bg=SURFACE_2, fg=FG, insertbackground=FG,
                                 highlightthickness=1, highlightbackground=BORDER,
                                 highlightcolor=ACCENT, borderwidth=0,
                                 font=self.f['mono'], selectbackground=SEL_BG,
                                 selectforeground='#ffffff')
                widget.pack(fill='x')
                widget.bind('<KeyRelease>',
                            lambda e: self.root.after(1, self._sync_text_height, e.widget))
            else:
                widget = ttk.Combobox(cell, values=COMBO_CHOICES.get(key, []),
                                      state='readonly')
                widget.pack(fill='x')

            if hint:
                ttk.Label(cell, text=hint, style='Hint.TLabel').pack(anchor='w', pady=(px(2), 0))
            self.setting_widgets[key] = widget

        tail = ttk.Frame(tab)
        tail.grid(row=2, column=0, sticky='ew', pady=(px(10), 0))
        ttk.Button(tail, text='保存配置', style='Accent.TButton',
                   command=self.save_settings).pack(side='left')
        ttk.Button(tail, text='从文件重新载入', command=self._reload_settings_clicked).pack(
            side='left', padx=(px(8), 0))
        ttk.Button(tail, text='恢复默认值', command=self._restore_defaults).pack(
            side='left', padx=(px(8), 0))

        self.var_show_secret = tk.BooleanVar(value=False)
        self._checkbox(tail, '显示密钥明文', self.var_show_secret,
                       command=self._toggle_secrets).pack(side='left', padx=(px(14), 0))

        ttk.Button(tail, text='打开配置文件', command=self.open_config_file).pack(
            side='right')

    def _toggle_secrets(self):
        show = '' if self.var_show_secret.get() else '•'
        for widget in self.secret_widgets:
            widget.configure(show=show)

    def _restore_defaults(self):
        """把表单填回出厂默认值。只改界面，不落盘，用户点「保存配置」才算数。"""
        if not messagebox.askyesno(
                '恢复默认值',
                '将把下面的表单填回出厂默认值。\n\n'
                '注意：这不会立刻写入文件，你还需要点一次「保存配置」。'):
            return
        for key, widget in self.setting_widgets.items():
            value = self._default_text(key, source=DEFAULTS)
            if isinstance(widget, tk.Text):
                widget.delete('1.0', 'end')
                widget.insert('1.0', value)
                self._sync_text_height(widget)
            elif isinstance(widget, ttk.Combobox):
                widget.set(value if value in COMBO_CHOICES.get(key, []) else
                           COMBO_CHOICES.get(key, [value])[0])
            else:
                widget.delete(0, 'end')
                widget.insert(0, value)
        logger.info('表单已填回默认值（尚未保存）')
        self.status_var.set('已填回默认值，点「保存配置」后生效')

    def _make_scrollable(self, parent):
        """把一个可滚动区域装进 parent，返回 (外层容器, 内层容器)。"""
        outer = ttk.Frame(parent)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        inner = ttk.Frame(canvas, padding=pxs(2, 2))
        window = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(window, width=e.width))

        def on_wheel(event):
            delta = -1 if getattr(event, 'delta', 0) > 0 else 1
            canvas.yview_scroll(delta, 'units')

        # 只在鼠标进入区域时接管滚轮，避免影响其他控件
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', on_wheel))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))
        return outer, inner

    @staticmethod
    def _next_row(inner) -> int:
        rows = [w.grid_info().get('row', 0) for w in inner.grid_slaves()]
        return (max(rows) + 1) if rows else 0

    def _sync_text_height(self, widget):
        """让多行输入框随内容长高（上限 8 行）。"""
        try:
            lines = int(widget.index('end-1c').split('.')[0])
            widget.configure(height=max(3, min(8, lines)))
            inner = widget.master
            if inner.winfo_exists():
                inner.event_generate('<Configure>')
        except Exception:
            pass

    # ==========================================================
    # 「密钥」页
    # ==========================================================
    def _build_keys_tab(self):
        tab = self.tab_keys
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)

        info = ttk.Frame(tab)
        info.grid(row=0, column=0, sticky='ew')

        card_outer, card, _ = self._make_card(info)
        card_outer.pack(fill='x')
        cells = ttk.Frame(card, style='Card.TFrame')
        cells.pack(side='left')
        self.keys_avail_var = tk.StringVar(value='0')
        self.keys_used_var = tk.StringVar(value='0')
        self.keys_summary_var = tk.StringVar(value='可用 0 条 · 不可用 0 条')
        for var, label_text, style_name in ((self.keys_avail_var, '可用密钥', 'StatOk.TLabel'),
                                            (self.keys_used_var, '已用尽', 'Stat.TLabel')):
            cell = ttk.Frame(cells, style='Card.TFrame')
            cell.pack(side='left', padx=(0, px(30)))
            ttk.Label(cell, text=label_text, style='Hint.TLabel').pack(anchor='w')
            ttk.Label(cell, textvariable=var, style=style_name).pack(anchor='w')
        ttk.Button(card, text='刷新', command=lambda: self.refresh_keys()).pack(side='right')

        ttk.Label(tab,
                  text='阈值只用于提醒，不再触发自动申请（TinyPNG 注册已加验证码）。点「手动注册（过验证码）」在浏览器里过验证码，程序自动收信激活。',
                  style='Dim.TLabel').grid(row=1, column=0, sticky='w', pady=(px(8), px(6)))

        outer, content, header = self._make_card(tab, '密钥列表')
        outer.grid(row=3, column=0, sticky='nsew')
        ttk.Label(header, textvariable=self.keys_summary_var,
                  style='DimCard.TLabel').pack(side='right')

        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        wrap = tk.Frame(content, bg=BORDER)
        wrap.grid(row=0, column=0, sticky='nsew')
        body = tk.Frame(wrap, bg=SURFACE_2)
        body.pack(fill='both', expand=True, padx=1, pady=1)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.keys_listbox = tk.Listbox(
            body, selectmode='extended', activestyle='none',
            bg=SURFACE_2, fg=FG, selectbackground=SEL_BG, selectforeground='#ffffff',
            highlightthickness=0, borderwidth=0, font=self.f['mono'],
        )
        self.keys_listbox.grid(row=0, column=0, sticky='nsew')
        kvsb = ttk.Scrollbar(body, orient='vertical', command=self.keys_listbox.yview)
        kvsb.grid(row=0, column=1, sticky='ns')
        self.keys_listbox.configure(yscrollcommand=kvsb.set)
        self.keys_listbox.bind('<Double-Button-1>', lambda e: self._copy_selected_key())

        self.keys_empty = tk.Frame(body, bg=SURFACE_2)
        tk.Label(self.keys_empty, text='还没有密钥', bg=SURFACE_2, fg=FG_DIM,
                 font=self.f['strong']).pack(pady=(0, px(2)))
        tk.Label(self.keys_empty, text='点下面的「手动注册（过验证码）」在浏览器里过验证码，'
                                      '程序自动取回；或手动粘贴已有密钥',
                 bg=SURFACE_2, fg=FG_MUTED, font=self.f['small']).pack()

        btns = ttk.Frame(tab)
        btns.grid(row=4, column=0, sticky='ew', pady=(px(10), 0))
        self.btn_apply = ttk.Button(btns, text='手动注册（过验证码）', style='Accent.TButton',
                                    command=self._manual_signup_start)
        self.btn_apply.pack(side='left')
        ttk.Button(btns, text='按用量整理排序',
                   command=lambda: self._keys_action('rearrange')).pack(side='left', padx=(px(8), 0))
        ttk.Button(btns, text='手动添加',
                   command=lambda: self._keys_action('add')).pack(side='left', padx=(px(8), 0))
        ttk.Button(btns, text='复制选中密钥',
                   command=self._copy_selected_key).pack(side='right')

    def _copy_selected_key(self):
        indexes = self.keys_listbox.curselection()
        if not indexes:
            self.status_var.set('先在上面的列表里选中一条密钥')
            return
        index = indexes[0]
        if index >= len(self.key_items):
            return
        key = self.key_items[index]
        self.root.clipboard_clear()
        self.root.clipboard_append(key)
        self.status_var.set(f'已复制密钥 {self._mask(key)} 到剪贴板')
        logger.info('密钥已复制到剪贴板: {}', self._mask(key))

    # ==========================================================
    # 日志：loguru -> 队列 -> Text
    # ==========================================================
    def _install_log_sink(self):
        # __init__.py 装的是「写进 tqdm 进度条」的 sink，GUI 里换成往界面送
        logger.remove()
        logger.add(self._log_sink, level=Config.LOG_LEVEL, colorize=False,
                   format='{time:HH:mm:ss} | {level: <7} | {message}')

    def _log_sink(self, message):
        self.ui_queue.put(('log', (str(message).rstrip(), None)))

    def _append_log(self, text: str, tag: str = None):
        if not tag:
            upper = text.upper()
            if '| ERROR' in upper:
                tag = 'err'
            elif '| WARNING' in upper:
                tag = 'warn'
            elif '| SUCCESS' in upper:
                tag = 'ok'
            else:
                tag = 'info'
        self.log_text.configure(state='normal')
        self.log_text.insert('end', text + '\n', tag)
        # 日志无限增长会拖慢界面，只保留最近 2000 行
        line_count = int(self.log_text.index('end-1c').split('.')[0])
        if line_count > 2000:
            self.log_text.delete('1.0', f'{line_count - 2000}.0')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    # ==========================================================
    # 主线程消息泵
    # ==========================================================
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == 'log':
                    self._append_log(payload[0], payload[1])
                elif kind == 'progress':
                    done, total, name, ok, ok_cum, err_cum = payload
                    self.progress.configure(maximum=max(1, total), value=done)
                    self.progress_var.set(f'{done} / {total}    {name}')
                    self.pct_var.set(f'{int(100 * done / max(1, total))}%')
                    self._set_stat('total', total)
                    self._set_stat('ok', ok_cum)
                    self._set_stat('err', err_cum)
                elif kind == 'status':
                    self.status_var.set(payload)
                elif kind == 'size':
                    gen, count, nbytes = payload
                    if gen == self._size_gen:
                        self._on_size_ready(count, nbytes)
                elif kind == 'finished':
                    self._on_worker_finished(payload)
                elif kind == 'keys':
                    self._refresh_keys_view()
                elif kind == 'keys_done':
                    self._set_keys_busy(False)
                elif kind == 'manual_signup_ready':
                    self._manual_signup_dialog(payload)
                elif kind == 'manual_signup_done':
                    self._on_manual_signup_done(payload)
                elif kind == 'manual_signup_needkey':
                    self._on_manual_signup_needkey(payload)
                elif kind == 'manual_key_done':
                    self._on_manual_key_done(payload)
                elif kind == 'open_url':
                    # 由后台线程请求、在主线程里开浏览器（webbrowser 属于界面行为）
                    if payload:
                        try:
                            webbrowser.open(payload)
                        except Exception as e:
                            logger.warning('打开浏览器失败: {}', e)
                elif kind == 'quota_checked':
                    self._on_quota_checked(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # ==========================================================
    # 待处理列表
    # ==========================================================
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title='选择图片',
            filetypes=[('图片', '*.png *.jpg *.jpeg *.PNG *.JPG *.JPEG *.svga'),
                       ('全部文件', '*.*')])
        self._add_targets(paths)

    def add_folder(self):
        path = filedialog.askdirectory(title='选择文件夹')
        if path:
            self._add_targets([path])

    def _add_targets(self, paths):
        if self.running:
            messagebox.showinfo('正在压缩', '压缩进行中，请先停止再修改列表。')
            return
        added = 0
        for p in paths or ():
            p = os.path.abspath(p)
            if p not in self.targets:
                self.targets.append(p)
                added += 1
        self._refresh_target_list()
        if added:
            logger.info('已添加 {} 项，待处理共 {} 项', added, len(self.targets))

    def remove_selected(self):
        if self.running:
            return
        selected = sorted(self.listbox.curselection(), reverse=True)
        if not selected:
            self.status_var.set('先在列表里选中要移除的项')
            return
        for index in selected:
            if index < len(self.targets):
                self.targets.pop(index)
        self._refresh_target_list()

    def clear_targets(self):
        if self.running:
            return
        if not self.targets:
            return
        if not messagebox.askyesno('清空列表', f'确定清空这 {len(self.targets)} 项吗？'):
            return
        self.targets.clear()
        self._refresh_target_list()

    def _refresh_target_list(self):
        self.listbox.delete(0, 'end')
        for p in self.targets:
            prefix = '[目录] ' if os.path.isdir(p) else '[文件] '
            self.listbox.insert('end', prefix + p)
        self.count_var.set(f'{len(self.targets)} 项')
        self._sync_start_button()
        self._toggle_empty_state()
        self._schedule_size_scan()

    def _toggle_empty_state(self):
        if self.targets:
            self.empty_state.place_forget()
        else:
            self.empty_state.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.empty_state.lift()

    # ---- 待处理体积统计（后台算，避免大目录卡住界面） ----
    def _schedule_size_scan(self):
        if self._size_job is not None:
            try:
                self.root.after_cancel(self._size_job)
            except Exception:
                pass
            self._size_job = None
        self._size_gen += 1
        if not self.targets:
            self.detail_var.set('')
            return
        self.detail_var.set('正在统计…')
        self._size_job = self.root.after(220, self._start_size_scan)

    def _start_size_scan(self):
        self._size_job = None
        gen = self._size_gen
        threading.Thread(target=self._size_worker,
                         args=(gen, list(self.targets), bool(self.var_recur.get())),
                         daemon=True).start()

    def _size_worker(self, gen, targets, recursive):
        count = 0
        total = 0
        for target in targets:
            try:
                if os.path.isdir(target):
                    for path in self._scan_dir(target, recursive):
                        count += 1
                        try:
                            total += os.path.getsize(path)
                        except OSError:
                            pass
                elif os.path.isfile(target):
                    count += 1
                    total += os.path.getsize(target)
            except Exception:
                continue
        self.ui_queue.put(('size', (gen, count, total)))

    def _on_size_ready(self, count, nbytes):
        if count:
            self.detail_var.set(f'{count} 张图片 · 约 {self._fmt_bytes(nbytes)}')
        else:
            self.detail_var.set('没有找到图片')

    def _scan_dir(self, dir_path, recursive: bool) -> list:
        """展开一个文件夹里的图片；递归时跳过输出目录，避免把产物再压一遍。"""
        found = []
        try:
            entries = os.listdir(dir_path)
        except OSError as e:
            logger.warning('无法读取文件夹 {}: {}', dir_path, e)
            return found
        for name in entries:
            full = os.path.join(dir_path, name)
            if os.path.isdir(full):
                if recursive and name != OUTPUT_SUBDIR:
                    found.extend(self._scan_dir(full, recursive))
            elif IMG_REG.match(name):
                found.append(os.path.abspath(full))
        return found

    # ==========================================================
    # 压缩流程
    # ==========================================================
    def start_compress(self):
        if self.running:
            return
        if self.nb.index(self.nb.select()) != 0:
            self.nb.select(self.tab_compress)
        if not self.targets:
            # 不弹对话框：F5 快捷键会绕过按钮的 disabled 状态，任何入口都不该弹框打断。
            # 统一改成状态栏提示（对应验收 A-9）。
            self.status_var.set('还没有待处理项，请先添加图片或文件夹')
            return

        recursive = bool(self.var_recur.get())
        to_subdir = bool(self.var_subdir.get())
        write_log = bool(self.var_log.get())

        # 先把任务展开、按输出目录分组，这样总进度是准的，停止也能立刻生效
        groups, total_files = [], 0
        by_output = {}
        for target in self.targets:
            if os.path.isdir(target):
                files = self._scan_dir(target, recursive)
                if not files:
                    logger.warning('文件夹内没有匹配的图片: {}', target)
                    continue
                out_dir = os.path.join(target, OUTPUT_SUBDIR) if to_subdir else None
                by_output.setdefault(out_dir, []).extend(files)
            elif os.path.isfile(target):
                out_dir = os.path.join(os.path.dirname(target), OUTPUT_SUBDIR) if to_subdir else None
                by_output.setdefault(out_dir, []).append(os.path.abspath(target))
            else:
                logger.warning('路径不存在，已跳过: {}', target)

        if not by_output:
            messagebox.showwarning('没有可压缩的图片', '待处理列表里没有找到 png/jpg/svga 图片。')
            return
        for out_dir, files in by_output.items():
            groups.append((out_dir, files))
            total_files += len(files)

        # 输出目录：优先记第一个真实存在的目录，供「打开输出目录」使用
        output_dirs = []
        for out_dir, files in groups:
            candidate = out_dir or os.path.dirname(files[0])
            if candidate not in output_dirs:
                output_dirs.append(candidate)
        self.last_output_dir = output_dirs[0] if output_dirs else None
        self.btn_open_output.configure(state='normal' if (
            self.last_output_dir and os.path.isdir(self.last_output_dir)) else 'disabled')

        self.progress.configure(maximum=total_files, value=0)
        self.pct_var.set('0%')
        self.progress_var.set(f'0 / {total_files}    准备中…')
        for key in ('ok', 'err'):
            self._set_stat(key, '0')
        for key in ('ratio', 'saved', 'speed'):
            self._set_stat(key, '—')
        self._set_stat('total', total_files)
        self.result_row.pack_forget()
        self.result_var.set('')

        # 额度预检：压到一半才发现额度耗尽是最糟的体验。
        # 逐条查密钥要联网，所以放后台线程，查完再决定要不要真的开跑。
        self._pending_start = (groups, total_files, write_log, to_subdir)
        self.progress_var.set(f'0 / {total_files}    正在检查额度…')
        self.status_var.set('正在检查密钥额度……')
        threading.Thread(target=self._quota_check_worker,
                         args=(total_files,), daemon=True).start()

    def _quota_check_worker(self, total_files: int):
        """后台逐条查密钥剩余额度。查不到就别拦着用户，交回主线程照常开跑。"""
        try:
            KeyManager.init_working_dir()
            KeyManager.load_keys()
            remain, rows, failed = KeyManager.estimate_quota()
        except Exception as e:
            logger.debug('额度预检失败，直接进入压缩: {}', e)
            remain, rows, failed = None, [], 0
        self.ui_queue.put(('quota_checked', (total_files, remain, rows, failed)))

    def _on_quota_checked(self, payload):
        total_files, remain, rows, failed = payload
        pending = getattr(self, '_pending_start', None)
        if not pending:
            return
        self._pending_start = None

        # 离线或接口异常时查不出来——这种情况不拦用户，按原计划开跑
        if remain is None:
            self._start_compress_now(*pending)
            return

        limit = getattr(Config, 'KEY_USAGE_LIMIT', 490)
        need = -(-total_files * 11 // 10)        # 预留 10% 余量（整数上取整）
        if remain >= need:
            self._start_compress_now(*pending)
            return

        need_keys = max(1, -(-(need - remain) // max(1, limit)))
        choice = self._ask_quota_shortage(total_files, need, remain, need_keys,
                                          len(rows), failed, limit)
        if choice == 'register':
            self._manual_signup_start()
        elif choice == 'continue':
            self._start_compress_now(*pending)
        else:
            self.progress_var.set('已取消')
            self.status_var.set('已取消压缩（额度可能不足）')

    def _ask_quota_shortage(self, total_files, need, remain, need_keys,
                            n_keys, failed, limit) -> str:
        """额度可能不足时的三选一。返回 'register' / 'continue' / 'cancel'。"""
        result = {'v': 'cancel'}
        win = tk.Toplevel(self.root)
        win.title('密钥额度可能不足')
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        card_outer, card, _ = self._make_card(win)
        card_outer.pack(fill='both', expand=True, padx=px(14), pady=px(14))

        tk.Label(card, text='密钥额度可能不足', bg=SURFACE, fg=FG,
                 font=self.f['h2']).pack(anchor='w', pady=(0, px(8)))
        detail = (f'待压缩：{total_files} 张（预留 10% 余量后约需 {need} 次额度）\n'
                  f'当前剩余：约 {remain} 次'
                  + (f'（{n_keys} 条可用密钥'
                     + (f'，其中 {failed} 条查询失败' if failed else '') + '）'
                     if n_keys else '（当前没有可用密钥）')
                  + f'\n\n继续压可能会在中途因额度耗尽而失败。\n'
                    f'建议再注册 {need_keys} 条新密钥（每条约 {limit} 次额度）。')
        tk.Label(card, text=detail, bg=SURFACE, fg=FG, font=self.f['body'],
                 justify='left', anchor='w').pack(anchor='w', pady=(0, px(12)))

        btns = tk.Frame(card, bg=SURFACE)
        btns.pack(fill='x')
        ttk.Button(btns, text='去注册新密钥', style='Accent.TButton',
                   command=lambda: (result.__setitem__('v', 'register'),
                                    win.destroy())).pack(side='left')
        ttk.Button(btns, text='仍要继续',
                   command=lambda: (result.__setitem__('v', 'continue'),
                                    win.destroy())).pack(side='left', padx=(px(8), 0))
        ttk.Button(btns, text='取消',
                   command=win.destroy).pack(side='right')

        win.grab_set()
        self.root.wait_window(win)
        return result['v']

    def _start_compress_now(self, groups, total_files, write_log, to_subdir):
        """预检通过（或用户选择继续）后真正开跑。"""
        # 先切回压缩页再禁用其他页签，否则会去禁用「当前正选中」的页签
        self.nb.select(self.tab_compress)
        self._set_running(True)

        logger.info('=' * 64)
        logger.info('开始压缩：共 {} 张图片，输出方式: {}',
                    total_files, f'{OUTPUT_SUBDIR} 子目录' if to_subdir else '覆盖原文件')
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self._worker, args=(groups, total_files, write_log), daemon=True)
        self.worker.start()

    def request_stop(self):
        if not self.running:
            return
        self.stop_event.set()
        self.btn_stop.configure(state='disabled')
        # 如实说明：未开始的任务会被取消，但正在压缩中的几张会先跑完（这是为了保证
        # 不留下半截文件）。所以「停止」不是瞬时的，而是等这几张收尾。
        self.status_var.set('正在停止…… 等待正在压缩的图片收尾')
        self.progress_var.set('正在停止…… 等待已开始的图片收尾')
        logger.warning('已请求停止：将取消尚未开始的任务，进行中的图片会先完成')

    def _sync_start_button(self) -> None:
        """
        按「是否有待处理项 + 是否正在跑」统一决定「开始压缩」按钮的可用状态。

        收敛到这一处的原因：之前 _refresh_target_list 从不碰按钮、_set_running 又只按
        running 开关，于是「清空列表后按钮仍可点」——而 F5 快捷键还会绕过 disabled，
        两个入口行为不一致。现在两个入口都调这里，状态始终与 targets/running 对齐。
        """
        state = 'normal' if (self.targets and not self.running) else 'disabled'
        try:
            self.btn_start.configure(state=state)
        except Exception:
            pass

    def _set_running(self, running: bool):
        self.running = running
        self._sync_start_button()
        self.btn_stop.configure(state='normal' if running else 'disabled')
        for widget in (self.btn_add_files, self.btn_add_folder,
                       self.btn_remove, self.btn_clear,
                       self.cb_recur, self.cb_subdir, self.cb_log):
            try:
                widget.configure(state='disabled' if running else 'normal')
            except Exception:
                pass
        self.listbox.configure(state='disabled' if running else 'normal')
        self.nb.tab(1, state='disabled' if running else 'normal')
        self.nb.tab(2, state='disabled' if running else 'normal')
        self.status_var.set('正在压缩…' if running else self.status_var.get())

    # ----------------------------------------------------------
    def _worker(self, groups, total_files, write_log):
        """工作线程：只在最后把结果丢回队列，中间所有界面更新都走 ui_queue。"""
        done = 0                 # 已经整体处理完的文件数（按「组」推进）
        base_ok = base_err = 0   # 前面各组累计的成功/失败数
        ok_total = err_total = 0
        old_total = new_total = 0
        elapsed = 0.0
        stopped = False
        error_files = []

        def on_progress(count, total, name, ok, group_ok, group_err):
            # 多组任务共用一个进度条：done/base_* 负责把「上一组的结果」累加进来，
            # 引擎给的则是「本组内累计」的数，两者相加才是全局的真实进度。
            self.ui_queue.put(('progress', (
                min(done + count, total_files), total_files, name, ok,
                base_ok + group_ok, base_err + group_err)))

        try:
            if not self._init_engine():
                self.ui_queue.put(('finished', {
                    'ok': 0, 'err': 0, 'stopped': False,
                    'fatal': '初始化失败：没有可用密钥。请到「密钥」页申请，或检查网络与代理配置。'}))
                return

            for out_dir, files in groups:
                if self.stop_event.is_set():
                    stopped = True
                    break
                if out_dir and not os.path.exists(out_dir):
                    os.makedirs(out_dir, exist_ok=True)
                logger.info('处理 {} 张图片 → {}', len(files), out_dir or '覆盖原文件')

                res = TinyImg.compress_from_file_list(
                    files, out_dir,
                    on_progress=on_progress,
                    should_stop=self.stop_event.is_set,
                )
                basic = res['basic']
                ok_total += basic['success_count']
                err_total += basic['error_count']
                old_total += self._parse_size(basic['input_size'])
                new_total += self._parse_size(basic['output_size'])
                elapsed += float(basic['time'].rstrip(' s') or 0)
                error_files.extend(res['error_files'])
                # 先推进「已处理数」和本组基线：即使这一组是被停止中断的，
                # 它已经完成的那部分也要如实计入，不能因为停止就把数字清零。
                done += basic['success_count'] + basic['error_count']
                base_ok, base_err = ok_total, err_total
                if basic.get('stopped'):
                    stopped = True
                    break

                if write_log:
                    log_path = os.path.join(out_dir or os.path.dirname(files[0]), 'log.json')
                    try:
                        with open(log_path, 'w', encoding='utf-8') as f:
                            json.dump(res, f, ensure_ascii=False, indent=2)
                        logger.success('压缩日志已输出: {}', log_path)
                    except OSError as e:
                        logger.warning('压缩日志写入失败: {}', e)

            # 失败重试：TinyPNG 偶发 5xx / 网络抖动，重试两轮能救回大部分。
            # 重试只针对上一轮的失败项，所以这一轮的成功数就是「救回来的数量」，
            # 失败总数相应减少。
            round_no = 0
            while error_files and not self.stop_event.is_set() and round_no < 2:
                round_no += 1
                logger.warning('第 {} 轮重试，剩余 {} 张失败图片', round_no, len(error_files))
                # 重试期间进度条不应倒退：done 已经走满，靠 base_* 把统计基线挪到当前值
                base_ok, base_err = ok_total, err_total
                retry_list, error_files = error_files, []
                res = TinyImg.compress_from_file_list(
                    retry_list, None, on_progress=on_progress,
                    should_stop=self.stop_event.is_set)
                basic = res['basic']
                ok_total += basic['success_count']
                old_total += self._parse_size(basic['input_size'])
                new_total += self._parse_size(basic['output_size'])
                elapsed += float(basic['time'].rstrip(' s') or 0)
                error_files.extend(res['error_files'])
                # 重试成功即意味着该项不再是失败项，最终失败数就等于剩余列表长度
                err_total = len(error_files)

            if not error_files:
                err_total = 0

            if error_files:
                logger.error('最终仍有 {} 张图片压缩失败', len(error_files))
                for path in error_files[:20]:
                    logger.error('  · {}', path)
                if len(error_files) > 20:
                    logger.error('  · …… 其余 {} 张已写入 error_files.json', len(error_files) - 20)
                try:
                    with open(os.path.join(get_app_dir(), 'error_files.json'), 'w',
                              encoding='utf-8') as f:
                        json.dump(error_files, f, ensure_ascii=False, indent=2)
                except OSError:
                    pass

        except Exception as e:
            logger.error('压缩过程出现未预期错误: {}', e)
            self.ui_queue.put(('log', (repr(e), 'err')))
        finally:
            # 「压缩率」按用户的理解来算：压掉了多少。引擎报告里的口径是
            # 「压缩后占原图的百分比」，直接拿来当 29.5% 展示会让人以为只压下来三成，
            # 而实际是省了七成——两者正好互补，很容易搞反。
            pct = round(100 * (old_total - new_total) / old_total, 2) if old_total else None
            ratio = f'{pct:.2f}%' if pct is not None else '—'
            speed = f'{ok_total / elapsed:.2f} 张/s' if elapsed > 0 else '—'
            self.ui_queue.put(('finished', {
                'ok': ok_total, 'err': len(error_files), 'stopped': stopped,
                'ratio': ratio, 'speed': speed, 'saved_pct': pct,
                'saved': self._fmt_bytes(max(0, old_total - new_total)),
            }))

    def _init_engine(self) -> bool:
        """按命令行同样的顺序初始化密钥与代理。"""
        logger.info('正在初始化……')
        KeyManager.init(get_app_dir())
        reload_config()

        if not KeyManager.Keys.available:
            logger.error('没有可用密钥')
            return False

        for key in list(KeyManager.Keys.available):
            try:
                TinyImg.set_key(key)
                logger.success('当前密钥: {}…（可用 {} 条）', key[:8],
                               len(KeyManager.Keys.available))
                break
            except Exception as e:
                logger.warning('密钥无效，已移入不可用: {}… ({})', key[:8], e)
                KeyManager.Keys.available.remove(key)
                KeyManager.Keys.unavailable.append(key)
                KeyManager.store_key()
        else:
            logger.error('所有密钥均无效')
            return False

        proxies = Config.get_proxy_list()
        if proxies:
            TinyImg.set_proxy(proxies)
            logger.info('已启用 {} 条代理（分散出口 IP）', len(proxies))
        else:
            logger.info('未配置代理，使用直连')
        self.ui_queue.put(('keys', None))
        return True

    def _on_worker_finished(self, result: dict):
        self._set_running(False)
        self.btn_stop.configure(state='disabled')

        if result.get('fatal'):
            logger.error(result['fatal'])
            self.status_var.set('初始化失败')
            self.progress_var.set('未能开始')
            messagebox.showerror('无法开始', result['fatal'])
            return

        self._set_stat('ok', result['ok'])
        self._set_stat('err', result['err'])
        self._set_stat('ratio', result.get('ratio', '—'))
        self._set_stat('speed', result.get('speed', '—'))
        self._set_stat('saved', result.get('saved', '—'))
        self.refresh_keys(show_log=False)

        if self.last_output_dir and os.path.isdir(self.last_output_dir):
            self.btn_open_output.configure(state='normal')
            self.btn_result_open.configure(state='normal')
        else:
            self.btn_open_output.configure(state='disabled')
            self.btn_result_open.configure(state='disabled')

        if result.get('stopped'):
            self.status_var.set(f'已停止 · 成功 {result["ok"]} · 失败 {result["err"]}')
            self.progress_var.set('已停止（未开始的图片未处理）')
            logger.warning('压缩已停止（未开始的图片未处理）')
            self._show_result(
                f'已停止 · 成功 {result["ok"]} · 失败 {result["err"]}', 'ResultWarn.TLabel')
        else:
            self.progress.configure(value=self.progress['maximum'])
            self.pct_var.set('100%')
            saved = result.get('saved', '—')
            ratio = result.get('ratio', '—')
            detail = f'成功 {result["ok"]} · 失败 {result["err"]} · 省下 {saved}'
            # 百分比只在真有减小的时候才附上，否则「省下 1.2 KB（0.0%）」这种读起来很别扭
            if result.get('saved_pct'):
                detail += f'（{ratio}）'
            self.status_var.set(f'完成 · {detail}')
            # 结论只写在下面的横幅里，这里只交代进度收尾，避免同一句话出现两遍
            self.progress_var.set(
                f'{self.progress["maximum"]} / {self.progress["maximum"]}    全部处理完毕')
            # 配色分三档：全成功、部分失败、一张都没成——最后一档得说清「这次是白跑了」
            if not result['err']:
                style, mark = 'ResultOk.TLabel', '✓'
            elif result['ok']:
                style, mark = 'ResultWarn.TLabel', '✓'
            else:
                style, mark = 'ResultErr.TLabel', '✗'
            self._show_result(f'{mark} 完成 · {detail}', style)
            logger.success('全部完成：成功 {} 张，失败 {} 张，共省下 {}',
                           result['ok'], result['err'], saved)

    def _show_result(self, text: str, style: str):
        self.result_var.set(text)
        self.result_label.configure(style=style)
        self.result_row.pack(fill='x', pady=(px(6), 0), before=self.status_sep)

    def _set_stat(self, key: str, value):
        """
        更新一个统计数字，并按「它有没有实际意义」决定要不要上强调色。

        中性色用于 0 与占位符「—」：失败数是 0 时亮红只会让人以为出了问题，
        还没跑出来的指标（—）上色同样没有信息量，只会让整行花掉。
        """
        text = str(value)
        self.stat_vars[key].set(text)
        label = self.stat_labels.get(key)
        if label is None:
            return
        if key in ('ok', 'err'):
            active = text.isdigit() and int(text) > 0
        else:
            active = text not in ('', '—')
        label.configure(style=self.stat_active_style.get(key, 'Stat.TLabel')
                        if active else 'Stat.TLabel')

    @staticmethod
    def _parse_size(text: str) -> float:
        """把 '1.23 MB' 这类展示字符串还原成字节数，用于汇总压缩率。"""
        try:
            number, unit = str(text).split()
            scale = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}.get(unit.upper(), 1)
            return float(number) * scale
        except Exception:
            return 0.0

    @staticmethod
    def _fmt_bytes(num: float) -> str:
        if num < 1024:
            return f'{num:.0f} B'
        if num < 1024 ** 2:
            return f'{num / 1024:.2f} KB'
        if num < 1024 ** 3:
            return f'{num / 1024 ** 2:.2f} MB'
        return f'{num / 1024 ** 3:.2f} GB'

    # ==========================================================
    # 设置读写
    # ==========================================================
    def _read_env_values(self) -> dict:
        path = os.path.join(get_app_dir(), 'config.env')
        values = {}
        if not os.path.exists(path):
            return values
        with open(path, encoding='utf-8') as f:
            for line in f:
                m = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$', line)
                if m:
                    values[m.group(1)] = m.group(2).strip()
        return values

    def _load_settings_into_form(self):
        file_values = self._read_env_values()
        for key, widget in self.setting_widgets.items():
            # 文件里没有这个键时，回退到 Config 上的默认值（唯一来源）
            value = file_values.get(key, '')
            if value == '':
                value = self._default_text(key)
            if isinstance(widget, tk.Text):
                widget.delete('1.0', 'end')
                widget.insert('1.0', value)
                self._sync_text_height(widget)
            elif isinstance(widget, ttk.Combobox):
                if value not in COMBO_CHOICES.get(key, []):
                    value = COMBO_CHOICES.get(key, [value])[0]
                widget.set(value)
            else:
                widget.delete(0, 'end')
                widget.insert(0, value)

    @staticmethod
    def _default_text(key: str, source=None) -> str:
        """
        把默认值转成「表单里应该显示的文本」。

        这里必须按类型分别处理：TINYPNG_API_KEYS 的默认值是列表 []，
        直接 str() 会得到字符串 "[]"，一旦存回 config.env 就写成 `TINYPNG_API_KEYS=[]`，
        再读出来会被解析成一条并不存在的密钥 "[]"——密钥全部失效，压缩直接跑不起来。

        :param source: 取值来源；默认取当前 Config（此时已被 config.env 覆盖），
                       传 config.DEFAULTS 则取出厂默认值。
        """
        holder = Config if source is None else source
        if isinstance(holder, dict):
            if key not in holder:
                return ''
            value = holder[key]
        else:
            if not hasattr(holder, key):
                return ''
            value = getattr(holder, key)
        if value is None:
            return ''
        if isinstance(value, (list, tuple)):
            return ','.join(str(item) for item in value)
        if isinstance(value, bool):
            return 'true' if value else 'false'
        return str(value)

    def _collect_settings(self) -> dict:
        values = {}
        for key, widget in self.setting_widgets.items():
            if isinstance(widget, tk.Text):
                values[key] = widget.get('1.0', 'end-1c')
            else:
                values[key] = widget.get().strip()
        return values

    def _validate_settings(self, values: dict) -> list:
        """
        保存前做最基本的检查。

        这里不是为了「防用户」，而是因为写错了要等到真正压缩时才在网络层报错，
        那时用户看到的是一句莫名其妙的失败原因，很难定位到是配置写错了。
        """
        problems = []
        for key in NUMERIC_KEYS:
            raw = (values.get(key) or '').strip()
            if raw == '':
                continue
            if not re.match(r'^\d+$', raw):
                problems.append(f'{key} 需要填写正整数，当前是「{raw}」')
            elif int(raw) <= 0:
                problems.append(f'{key} 必须大于 0，当前是「{raw}」')
        api_keys = values.get('TINYPNG_API_KEYS', '')
        if api_keys:
            bad = [k for k in re.split(r'[,\s;]+', api_keys)
                   if k and not KeyManager.KEY_PATTERN.match(k)]
            if bad:
                problems.append('TinyPNG API Keys 里有 {} 条格式不像密钥（长度不足或含特殊字符）：'
                                '{}'.format(len(bad), '、'.join(b[:8] + '…' for b in bad[:3])))
        return problems

    def save_settings(self):
        values = self._collect_settings()
        problems = self._validate_settings(values)
        if problems:
            logger.warning('配置未保存，存在 {} 处问题', len(problems))
            for item in problems:
                logger.warning('  · {}', item)
            messagebox.showwarning('配置有误，未保存', '\n'.join(f'· {p}' for p in problems))
            self.status_var.set('配置有误，未保存')
            return
        try:
            path = save_env_values(values)
        except Exception as e:
            logger.error('配置保存失败: {}', e)
            messagebox.showerror('保存失败', f'写入 config.env 出错：\n{e}')
            return
        logger.success('配置已保存: {}', path)
        self._apply_config_reload()

    def _reload_settings_clicked(self):
        self._load_settings_into_form()
        self._apply_config_reload()
        logger.info('已从 {} 重新载入配置', os.path.join(get_app_dir(), 'config.env'))

    def _apply_config_reload(self):
        """让改动在当前进程里立刻生效（包括日志级别）。"""
        reload_config()
        logger.remove()
        logger.add(self._log_sink, level=Config.LOG_LEVEL, colorize=False,
                   format='{time:HH:mm:ss} | {level: <7} | {message}')
        logger.info('配置已生效 · 线程数 {} · 日志级别 {}',
                    Config.THREAD_NUM, Config.LOG_LEVEL)
        self.status_var.set('配置已保存并生效')
        self._update_notice()

    def open_config_file(self):
        path = os.path.join(get_app_dir(), 'config.env')
        if not os.path.exists(path):
            save_env_values({})
        try:
            if sys.platform == 'win32':
                os.startfile(path)  # noqa: S606
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            logger.warning('打开配置文件失败: {}', e)

    # ==========================================================
    # 密钥管理
    # ==========================================================
    def refresh_keys(self, show_log: bool = True):
        try:
            KeyManager.init_working_dir()
            KeyManager.load_keys()
        except Exception as e:
            if show_log:
                logger.warning('读取密钥失败: {}', e)
        self._refresh_keys_view()

    def _refresh_keys_view(self):
        available = list(getattr(KeyManager.Keys, 'available', []) or [])
        unavailable = list(getattr(KeyManager.Keys, 'unavailable', []) or [])
        self.keys_listbox.delete(0, 'end')
        self.key_items = []
        for key in available:
            self.keys_listbox.insert('end', f'[可用]   {self._mask(key)}')
            self.key_items.append(key)
        for key in unavailable:
            self.keys_listbox.insert('end', f'[已用尽] {self._mask(key)}')
            self.key_items.append(key)
        self.keys_avail_var.set(str(len(available)))
        self.keys_used_var.set(str(len(unavailable)))
        self.keys_summary_var.set(
            f'可用 {len(available)} 条 · 不可用 {len(unavailable)} 条'
            + ('　（双击某条可复制）' if self.key_items else ''))
        if self.key_items:
            self.keys_empty.place_forget()
        else:
            self.keys_empty.place(relx=0, rely=0, relwidth=1, relheight=1)
            self.keys_empty.lift()
        self._update_notice()

    def _update_notice(self):
        """没有可用密钥时，在压缩页顶部摆一条明确的行动指引。"""
        try:
            available = list(getattr(KeyManager.Keys, 'available', []) or [])
        except Exception:
            available = []
        if available:
            self.notice_outer.grid_remove()
            return
        if Config.TINYPNG_API_KEYS or (Config.APIHZ_ID and Config.APIHZ_KEY):
            text = '当前没有可用密钥，压缩无法开始。请到「密钥」页手动注册或粘贴一条。'
        else:
            text = ('还没有配置任何密钥。到「密钥」页点「手动注册（过验证码）」'
                    '在浏览器里过验证码，程序会自动取回；或直接粘贴已有的 Key。')
        self.notice_var.set(text)
        self.notice_outer.grid()

    @staticmethod
    def _mask(key: str) -> str:
        return f'{key[:10]}…{key[-4:]}' if len(key) > 16 else key

    def _set_keys_busy(self, busy: bool):
        state = 'disabled' if busy else 'normal'
        for btn in (self.btn_apply,):
            try:
                btn.configure(state=state)
            except Exception:
                pass

    def _keys_action(self, action: str):
        if self.running:
            messagebox.showinfo('正在压缩', '压缩进行中，请先停止再操作密钥。')
            return
        if action == 'add':
            key = simpledialog.askstring('手动添加密钥', '粘贴你的 TinyPNG API Key：',
                                         parent=self.root)
            if not key or not key.strip():
                return
            key = key.strip()
            if not KeyManager.KEY_PATTERN.match(key):
                messagebox.showwarning(
                    '这个密钥看起来不对',
                    'TinyPNG 的 API Key 是一长串字母数字（通常 32 位）。\n'
                    '请确认复制完整、没有多余的空格或引号。')
                return
            target = lambda: self._do_add_key(key)
        else:                                       # action == 'rearrange'
            target = self._do_rearrange

        self._set_keys_busy(True)
        self.status_var.set('正在处理密钥操作……')
        threading.Thread(target=self._keys_worker, args=(target,), daemon=True).start()

    def _keys_worker(self, target):
        try:
            KeyManager.init_working_dir()
            KeyManager.load_keys()
            target()
        except Exception as e:
            logger.error('密钥操作失败: {}', e)
        finally:
            self.ui_queue.put(('keys', None))
            self.ui_queue.put(('keys_done', None))

    def _do_rearrange(self):
        KeyManager.rearrange_keys()

    def _do_add_key(self, key: str):
        KeyManager.add_key(key)

    # ==========================================================
    # 手动注册（人工过验证码）
    #
    # 2026-09 起 TinyPNG 注册加了验证码，自动申请走不通，阈值也只用于提醒。
    # 但验证码之后的「收激活邮件 → 点链接 → 生成 key」全是 HTTP，程序能自己做。
    # 所以只在「填表 + 过验证码」这一步把人请出来，前后都自动化。
    # ==========================================================
    def _manual_signup_start(self):
        if self.running:
            messagebox.showinfo('正在压缩', '压缩进行中，请先停止再操作密钥。')
            return
        self._set_keys_busy(True)
        self.status_var.set('正在创建临时邮箱……')
        threading.Thread(target=self._manual_signup_begin_worker, daemon=True).start()

    def _manual_signup_begin_worker(self):
        """后台建邮箱（apihz 要联网），建好再回主线程弹窗。"""
        try:
            KeyManager.init_working_dir()
            KeyManager.load_keys()
            handle = KeyManager.begin_manual_signup()
        except Exception as e:
            logger.error('创建临时邮箱失败: {}', e)
            self.ui_queue.put(('keys_done', None))
            self.ui_queue.put(('status', f'创建临时邮箱失败：{e}'))
            return
        self.ui_queue.put(('manual_signup_ready', handle))

    def _manual_signup_dialog(self, handle):
        """把邮箱交给人，等他在浏览器里填表、过验证码、提交。"""
        self._set_keys_busy(False)
        self.status_var.set('已创建临时邮箱，等待你在注册页提交')

        win = tk.Toplevel(self.root)
        win.title('手动注册 TinyPNG 账号')
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        card_outer, card, _ = self._make_card(win)
        card_outer.pack(fill='both', expand=True, padx=px(14), pady=px(14))

        tk.Label(card, text='手动注册（只需你过一次验证码）', bg=SURFACE, fg=FG,
                 font=self.f['h2']).pack(anchor='w', pady=(0, px(8)))
        tk.Label(card,
                 text='TinyPNG 的注册页加了验证码，程序没法自动提交。\n'
                      '你只需要在浏览器里填一次表，剩下的由程序自动完成。',
                 bg=SURFACE, fg=FG_DIM, font=self.f['small'],
                 justify='left', anchor='w').pack(anchor='w', pady=(0, px(12)))

        # 邮箱（只读展示 + 复制）
        tk.Label(card, text='请在注册页的 Email 一栏填这个地址：', bg=SURFACE, fg=FG,
                 font=self.f['body'], anchor='w').pack(anchor='w')
        row = tk.Frame(card, bg=SURFACE)
        row.pack(fill='x', pady=(px(4), px(12)))
        mail_var = tk.StringVar(value=handle.mail)
        entry = tk.Entry(row, textvariable=mail_var, readonlybackground=SURFACE_3,
                         fg=FG, relief='flat', font=self.f['mono'],
                         highlightthickness=0, state='readonly', width=38)
        entry.pack(side='left', ipady=px(5))
        ttk.Button(row, text='复制', width=6,
                   command=lambda: self._copy_to_clipboard(handle.mail, win)).pack(
            side='left', padx=(px(8), 0))

        steps = ('1. 点「打开开发者页」，在里面填 Name / Email 并点 Create API key\n'
                 '2. Email 必须填上面这个地址（填错就收不到激活邮件），Name 随便填\n'
                 '3. 完成验证码，提交\n'
                 '4. 回到这里点「我已提交，等激活邮件」——程序会自动收信、激活、并把你的浏览器登录好')
        tk.Label(card, text=steps, bg=SURFACE, fg=FG, font=self.f['body'],
                 justify='left', anchor='w').pack(anchor='w', pady=(0, px(12)))

        status_var = tk.StringVar(value='等待你在注册页提交……')
        tk.Label(card, textvariable=status_var, bg=SURFACE, fg=ACCENT,
                 font=self.f['small'], anchor='w').pack(anchor='w', pady=(0, px(10)))

        btns = tk.Frame(card, bg=SURFACE)
        btns.pack(fill='x')
        ttk.Button(btns, text='打开开发者页',
                   command=lambda: webbrowser.open(handle.SIGNUP_URL)).pack(side='left')
        ttk.Button(btns, text='备用注册页',
                   command=lambda: webbrowser.open(handle.SIGNUP_URL_ALT)).pack(
            side='left', padx=(px(8), 0))
        btn_done = ttk.Button(
            btns, text='我已提交，等激活邮件', style='Accent.TButton',
            command=lambda: self._manual_signup_submit(handle, win, status_var, btn_done))
        btn_done.pack(side='left', padx=(px(8), 0))
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='right')

        win.protocol('WM_DELETE_WINDOW', win.destroy)

    def _manual_signup_submit(self, handle, win, status_var, btn_done):
        """用户在浏览器提交后：后台收激活邮件 → 激活 → 取 key。"""
        btn_done.configure(state='disabled')
        status_var.set('正在等待激活邮件（最多 3 分钟）……')
        self.status_var.set('正在等待激活邮件……')
        threading.Thread(target=self._manual_signup_finish_worker,
                         args=(handle, win), daemon=True).start()

    def _manual_signup_finish_worker(self, handle, win):
        try:
            handle.activate(timeout=180)      # 收激活邮件 → 点链接 → 登录

            # 程序是用自己的 requests.Session 登录的，人自己的浏览器没有这个登录态，
            # 直接打开控制台只会看到游客页。这里把同一个激活链接再喂给人的浏览器一次
            # （会让 HockeyStack 打点略脏，但换来的「点开就是登录态」值这个价）。
            act_url = getattr(handle, 'activation_url', '') or ''
            if act_url:
                self.ui_queue.put(('open_url', act_url))
                logger.info('已在你的浏览器里打开激活链接，接下来打开的页面都是登录态')

            keys = handle.fetch_keys()        # 尽力自动列一次，多半是空的
        except Exception as e:
            logger.error('激活失败: {}', e)
            self.ui_queue.put(('manual_signup_done', (False, win, str(e))))
            self.ui_queue.put(('keys_done', None))
            return

        key = ''
        if keys:
            last = keys[-1]
            key = last.get('key', '') if isinstance(last, dict) else str(last)
        if key:
            try:
                KeyManager.init_working_dir()
                KeyManager.load_keys()
                KeyManager.add_key(key)
                self.ui_queue.put(('manual_signup_done', (True, win, key)))
                self.ui_queue.put(('keys', None))
                return
            except Exception as e:
                logger.debug('自动保存 key 失败，转人工: {}', e)

        # 登录成功但没自动读到 key —— 实测这是常态，不是异常：
        # key 由浏览器端渲染，程序拿不到（详见 key_manager._list_keys）。
        self.ui_queue.put(('manual_signup_needkey', (win, bool(act_url))))
        self.ui_queue.put(('keys_done', None))

    def _on_manual_signup_needkey(self, payload):
        win, browser_signed_in = payload
        try:
            win.destroy()
        except Exception:
            pass
        self.status_var.set('账号已激活，请到控制台复制 Key')
        self._manual_key_fallback(
            '账号已激活，但 API Key 只在网页上显示（浏览器端渲染，程序读不到）。'
            '控制台里如果还没有 Key，先去开发者页点「Create API key」过一次验证码。',
            need_apply=True,
            browser_signed_in=browser_signed_in)

    def _on_manual_signup_done(self, payload):
        ok, win, detail = payload
        try:
            win.destroy()
        except Exception:
            pass
        if ok:
            messagebox.showinfo('注册成功', f'已获取并保存新密钥：\n{detail}')
            self.status_var.set('新密钥已保存')
        else:
            # 自动取 key 这一步上游不确定因素太多（cookie 域、端点再改、风控…），
            # 失败时不能把人晾在一句报错里。给一条一定能走通的路：
            # 打开控制台自己复制 key 回来粘，程序负责校验和保存。
            self._manual_key_fallback(detail)

    # --------------------------------------------------------------
    # 自动取 key 失败时的兜底：让人去控制台复制 key 回来粘贴。
    #
    # 这不是「功能没做完的补丁」，而是这条链路上唯一零依赖的一段：
    # 不管 TinyPNG 明天把接口改成什么样，人在网页上总能看到自己的 key。
    # 程序负责的是校验（真的能用才收）和保存，避免粘进一条废 key。
    # --------------------------------------------------------------
    def _manual_key_fallback(self, reason, need_apply=False, browser_signed_in=False):
        win = tk.Toplevel(self.root)
        win.title('手动粘贴 API Key')
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        card_outer, card, _ = self._make_card(win)
        card_outer.pack(fill='both', expand=True, padx=px(14), pady=px(14))

        tk.Label(card,
                 text=('账号已激活，来拿 API Key' if need_apply
                       else '自动取 Key 没成功，手动补一步'),
                 bg=SURFACE, fg=FG,
                 font=self.f['h2']).pack(anchor='w', pady=(0, px(8)))
        tk.Label(card, text=f'原因：{reason}', bg=SURFACE, fg=FG_DIM,
                 font=self.f['small'], wraplength=px(470), justify='left',
                 anchor='w').pack(anchor='w', pady=(0, px(10)))

        if browser_signed_in:
            tk.Label(card,
                     text='已帮你在浏览器的新标签页里完成登录，现在打开的页面就是登录态。',
                     bg=SURFACE, fg=ACCENT, font=self.f['body'],
                     wraplength=px(470), justify='left', anchor='w').pack(
                anchor='w', pady=(0, px(8)))

        if need_apply:
            steps = ('1. 先点「打开开发者页」，在里面点 Create API key 并过一次验证码\n'
                     '2. 再点「打开控制台」，页面会显示刚生成的 API Key\n'
                     '3. 复制后粘到下面，点「保存并验证」——程序会先试一次，确认能用再收')
        else:
            steps = ('1. 点「打开控制台」（已经用激活链接登录过，进去就是登录态）\n'
                     '2. 复制页面上那串 API Key\n'
                     '3. 粘到下面，点「保存并验证」——程序会先试一次，确认能用再收')
        tk.Label(card, text=steps, bg=SURFACE, fg=FG, font=self.f['body'],
                 justify='left', anchor='w').pack(anchor='w', pady=(0, px(10)))

        row = tk.Frame(card, bg=SURFACE)
        row.pack(fill='x', pady=(0, px(8)))
        var = tk.StringVar()
        tk.Entry(row, textvariable=var, bg=SURFACE_2, fg=FG, relief='flat',
                 font=self.f['mono'], highlightthickness=0,
                 insertbackground=FG, width=38).pack(side='left', ipady=px(5))
        ttk.Button(row, text='打开控制台',
                   command=lambda: webbrowser.open('https://tinify.com/dashboard/api')
                   ).pack(side='left', padx=(px(8), 0))
        if need_apply:
            ttk.Button(row, text='打开开发者页',
                       command=lambda: webbrowser.open('https://tinify.com/developers')
                       ).pack(side='left', padx=(px(8), 0))

        status_var = tk.StringVar(value='')
        tk.Label(card, textvariable=status_var, bg=SURFACE, fg=ACCENT,
                 font=self.f['small'], anchor='w').pack(anchor='w', pady=(0, px(10)))

        def _worker(key, btn):
            try:
                with requests.Session() as s:
                    proxy = Config.get_proxy()
                    if proxy:
                        s.proxies = {'http': proxy, 'https': proxy}
                    KeyManager.get_api_count(s, key)     # 不消耗配额，只探活
            except Exception as e:
                self.ui_queue.put(('manual_key_done', (False, win, str(e), key, btn)))
                return
            KeyManager.init_working_dir()
            KeyManager.load_keys()
            KeyManager.add_key(key)
            self.ui_queue.put(('manual_key_done', (True, win, key, key, btn)))
            self.ui_queue.put(('keys', None))

        def _save():
            key = var.get().strip()
            if not key:
                status_var.set('请先把 Key 粘进来')
                return
            status_var.set('正在验证……')
            btn.configure(state='disabled')
            threading.Thread(target=_worker, args=(key, btn), daemon=True).start()

        btns = tk.Frame(card, bg=SURFACE)
        btns.pack(fill='x')
        btn = ttk.Button(btns, text='保存并验证', style='Accent.TButton', command=_save)
        btn.pack(side='left')
        ttk.Button(btns, text='取消', command=win.destroy).pack(side='right')

    def _on_manual_key_done(self, payload):
        ok, win, detail, key, btn = payload
        if ok:
            try:
                win.destroy()
            except Exception:
                pass
            messagebox.showinfo('已保存', f'密钥验证通过并已保存：\n{key}')
            self.status_var.set('密钥已保存')
        else:
            try:
                btn.configure(state='normal')
            except Exception:
                pass
            messagebox.showerror(
                '这条 Key 用不了',
                f'{detail}\n\n请确认复制完整（32 位左右字母数字），或换一条重试。')

    def _copy_to_clipboard(self, text: str, win):
        win.clipboard_clear()
        win.clipboard_append(text)
        self.status_var.set(f'已复制：{text}')

    # ==========================================================
    # 快捷键 / 帮助
    # ==========================================================
    def _setup_shortcuts(self):
        bindings = (
            ('<Control-o>', lambda e: (self.add_files(), 'break')[1]),
            ('<Control-Shift-O>', lambda e: (self.add_folder(), 'break')[1]),
            ('<Control-s>', lambda e: (self.save_settings(), 'break')[1]),
            ('<F5>', lambda e: (self.start_compress(), 'break')[1]),
            ('<Control-Return>', lambda e: (self.start_compress(), 'break')[1]),
            ('<Escape>', lambda e: (self.request_stop(), 'break')[1]),
            ('<F1>', lambda e: (self.show_shortcuts(), 'break')[1]),
        )
        for seq, handler in bindings:
            try:
                self.root.bind(seq, handler)
            except Exception as e:
                logger.debug('绑定快捷键 {} 失败: {}', seq, e)

    SHORTCUTS = (
        ('Ctrl+O', '添加图片'),
        ('Ctrl+Shift+O', '添加文件夹'),
        ('Delete', '移除列表中选中的项'),
        ('双击列表项', '在文件管理器里定位它'),
        ('Ctrl+A', '全选列表（焦点在列表上时）'),
        ('F5 / Ctrl+Enter', '开始压缩'),
        ('Esc', '停止压缩'),
        ('Ctrl+S', '保存配置（在设置页）'),
        ('F1', '显示这份快捷键说明'),
    )

    def show_shortcuts(self):
        self._show_text_dialog(
            '快捷键',
            '\n'.join(f'{key:<18}{desc}' for key, desc in self.SHORTCUTS),
            note='拖拽：把图片或文件夹直接拖进「待处理列表」'
                 + ('' if self.dnd_enabled else '（当前运行环境未加载拖拽组件）'))

    def show_about(self):
        self._show_text_dialog(
            '关于',
            f'TinyPNG-Unlimited  v{__version__}\n\n'
            '无限量批量压缩图片：自动轮换/申请 TinyPNG 密钥，\n'
            '支持多代理分散出口 IP，CLI 与图形界面共用同一套压缩引擎。\n\n'
            f'配置目录：{get_app_dir()}',
            note='图片会先上传到 TinyPNG 服务器压缩后再下载回来，请自行判断是否适合你的素材。')

    def _show_text_dialog(self, title, body, note=''):
        """一个统一风格的小弹窗（快捷键/关于共用），比系统 messagebox 更贴合配色。"""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=BG)
        win.transient(self.root)
        win.resizable(False, False)

        card_outer, card, _ = self._make_card(win)
        card_outer.pack(fill='both', expand=True, padx=px(14), pady=px(14))
        tk.Label(card, text=title, bg=SURFACE, fg=FG, font=self.f['h2']).pack(
            anchor='w', pady=(0, px(10)))
        tk.Label(card, text=body, bg=SURFACE, fg=FG, font=self.f['mono'] if title == '快捷键'
                 else self.f['body'], justify='left', anchor='w').pack(anchor='w')
        if note:
            tk.Label(card, text=note, bg=SURFACE, fg=FG_MUTED, font=self.f['tiny'],
                     justify='left', anchor='w', wraplength=px(420)).pack(
                anchor='w', pady=(px(10), 0))
        ttk.Button(card, text='关闭', style='Accent.TButton', command=win.destroy).pack(
            anchor='e', pady=(px(12), 0))

        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f'+{x}+{y}')
        try:
            win.grab_set()
        except Exception:
            pass

    # ==========================================================
    def _on_close(self):
        if self.running:
            if not messagebox.askyesno('仍在压缩', '压缩还没结束，确定要退出吗？'):
                return
            self.stop_event.set()
        try:
            self._save_state()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    """GUI 入口。"""
    hide_console_if_owned()
    # 必须在创建 Tk() 之前声明 DPI 感知，否则高分屏上字体是糊的
    if sys.platform == 'win32':
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root, dnd = create_root()
    GuiApp(root, dnd_enabled=dnd)
    root.mainloop()


if __name__ == '__main__':
    main()
