# -*- mode: python ; coding: utf-8 -*-
import sys
import os

block_cipher = None

a = Analysis(
    ['bin/main.py'],
    pathex=['.'],          # 项目根目录加入 sys.path，让 tinypng_unlimited 包可被发现
    binaries=[],
    datas=[
        ('config.env.template', '.'),   # 打包配置模板
    ],
    hiddenimports=[
        'tinypng_unlimited',
        'tinypng_unlimited.config',
        'tinypng_unlimited.errors',
        'tinypng_unlimited.apihz_mail',
        'tinypng_unlimited.key_manager',
        'tinypng_unlimited.tiny_img',
        'tinify',
        'loguru',
        'tqdm',
        'tqdm.utils',
        'dotenv',
        'requests',
        'urllib3',
        'colorama',
        'jaraco.text',
        'jaraco.functools',
        'jaraco.context',
        'jaraco.classes',
        'pkg_resources',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['setuptools', 'distutils', 'matplotlib', 'numpy', 'scipy', 'pandas', 'PIL', 'cv2', 'sklearn'],
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
