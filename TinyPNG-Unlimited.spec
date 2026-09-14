# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（Windows / macOS / Linux 通用）。

跨平台注意点：
- 图标：Windows 用 icon.ico；macOS 需要 icon.icns（CI 里由 iconutil 从 icon.ico 生成，
  没生成出来就自动跳过，避免因为图标问题让 macOS 构建整个失败）；Linux 不使用图标。
- UPX：macOS 的 GitHub Actions runner 上默认没有 upx，这里检测到才启用。
- 产物名统一为 TinyPNG-Unlimited，**不带**平台后缀，由 CI 在打包完成后重命名，
  这样本地构建保持简单。

一个产物两种用法：双击 = 图形界面，带参数 = 命令行。
Windows 下产物为无控制台（windowed）子系统，双击不再弹黑色窗口；CLI 分支会在
bin/main.py 里用「继承句柄 / AttachConsole」把标准流接回终端与管道，因此命令行
输出照常（含 CI 的 `OUT=$(exe --version)` 捕获）。macOS / Linux 保持 console=True。

用法（在仓库根目录执行）：
    pyinstaller --clean --noconfirm TinyPNG-Unlimited.spec
"""
import os
import sys
from shutil import which

from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

IS_WIN = sys.platform.startswith('win')
IS_MAC = sys.platform == 'darwin'
SPEC_ROOT = str(SPECPATH)


def find_asset(name):
    """在 spec 所在目录与当前工作目录下查找资源，返回绝对路径；找不到返回 None。"""
    for base in (SPEC_ROOT, os.getcwd()):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return os.path.abspath(path)
    return None


if IS_WIN:
    ICON = find_asset('icon.ico')
elif IS_MAC:
    ICON = find_asset('icon.icns')
else:
    ICON = None  # Linux 不使用图标

# runner 上没有 upx 时不要硬开，否则会刷一堆告警
USE_UPX = which('upx') is not None
TEMPLATE = find_asset('config.env.template')
# 窗口图标：打包进产物内部供 GUI 的 root.iconbitmap 使用
# （EXE 的 icon= 只决定资源管理器里 exe 的图标，运行时的窗口图标要单独给文件）
WIN_ICON = find_asset('icon.ico')

# tkinterdnd2（拖拽）自带各平台的 tkdnd 动态库。它们是 data files 而不是 Python 代码，
# PyInstaller 的静态分析收不到，不显式搬进来时产物一启动就会
# RuntimeError: Unable to load tkdnd library —— 好在 gui.create_root() 会捕获并
# 降级成「无拖拽」的正常窗口，所以漏了也不会崩，只会安静地少一个功能。
# 没装这个可选依赖时 collect_data_files 抛异常，这里直接跳过。
try:
    DND_DATAS = collect_data_files('tkinterdnd2')
    DND_HIDDEN = ['tkinterdnd2']
except Exception:
    DND_DATAS = []
    DND_HIDDEN = []

a = Analysis(
    [os.path.join(SPEC_ROOT, 'bin', 'main.py')],
    pathex=[SPEC_ROOT],          # 项目根目录加入 sys.path，让 tinypng_unlimited 包可被发现
    binaries=[],
    # 只打包配置模板与窗口图标，绝不打包 config.env / keys.json（它们含密钥）
    datas=([d for d in [(TEMPLATE, '.'), (WIN_ICON, '.')] if d[0]] + DND_DATAS),
    hiddenimports=[
        'tinypng_unlimited',
        'tinypng_unlimited.config',
        'tinypng_unlimited.version',
        'tinypng_unlimited.errors',
        'tinypng_unlimited.apihz_mail',
        'tinypng_unlimited.key_manager',
        'tinypng_unlimited.tiny_img',
        # GUI 是在 bin/main.py 里延迟导入的（`from tinypng_unlimited.gui import ...`），
        # 这种导入 PyInstaller 的静态分析常常抓不到，必须显式声明，
        # 否则打包后双击 exe 会报 ModuleNotFoundError。
        'tinypng_unlimited.gui',
        # 拖拽是可选依赖：装了就被收进来，没装则 import 失败、界面自动降级
        *DND_HIDDEN,
        'tkinter',
        'tkinter.ttk',
        'tkinter.filedialog',
        'tkinter.messagebox',
        'tkinter.simpledialog',
        'tinify',
        'loguru',
        'tqdm',
        'tqdm.utils',
        'dotenv',
        'requests',
        'urllib3',
        'colorama',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 显式排除运行时用不到的重型包。除了减肥，更重要的是让「在 conda / 全量 site-packages
    # 环境里打包」也能得到和干净 venv 接近的结果——否则 PyInstaller 会顺着 hook 把
    # sphinx / IPython / jupyter 这类东西一路拉进来，产物暴涨、构建变成几分钟。
    #
    # 注意：这里**不能**排除 tkinter —— 图形界面就是用它做的。
    # （早先的版本把 tkinter 写在排除列表里，一旦启用 GUI 会导致打包后无法启动。）
    excludes=[
        'setuptools', 'distutils', 'pip', 'wheel',
        'matplotlib', 'numpy', 'scipy', 'pandas', 'PIL', 'cv2', 'sklearn',
        'IPython', 'ipykernel', 'jupyter', 'nbformat', 'notebook',
        'pygments', 'parso', 'jedi', 'docutils', 'sphinx', 'zmq',
        'pytest', 'nose', 'unittest2',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TinyPNG-Unlimited',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=USE_UPX,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windows：无控制台（windowed）子系统 —— 双击 exe 不再弹黑色窗口。
    # 代价是产物启动时 sys.std* 为 None，由 bin/main.py 的两层兜底接回：
    #   第 0 层把 None 流接到 devnull（保证 import 阶段不崩、GUI 能起来），
    #   第 1 层在 CLI 分支用继承句柄/AttachConsole 把流真正接回终端与管道。
    # macOS / Linux：保持 console=True（无 windowed 概念，CLI 行为完全不变）。
    console=(not IS_WIN),
    # 保持 False：windowed 下未捕获异常仍会弹 messagebox，问题才看得见。
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
