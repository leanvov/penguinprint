# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

    pyinstaller penguinprint.spec

产物：

    dist/penguinprint.exe                单文件版
    dist/penguinprint/penguinprint.exe   文件夹版（需要整个 dist/penguinprint 目录）

图标和 assets/ 只能靠参数带进去，漏了不报错、只会静默降级，所以固定在这里：
``icon`` 是 exe 文件图标；``datas`` 把 assets/ 放进 ``sys._MEIPASS/assets``，
供 ``gui._asset_path()`` 读运行期窗口图标。

不要用 ``pyinstaller --name penguinprint ...`` 的命令行形式打包：它生成的 spec
会覆盖本文件。
"""

from pathlib import Path

ROOT = Path(SPECPATH)
ASSETS = ROOT / 'assets'
ICON = ASSETS / 'penguinprint.ico'

if not ICON.is_file():
    raise SystemExit(f'找不到图标文件，无法打包：{ICON}')

a = Analysis(
    [str(ROOT / 'run_gui.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ASSETS), 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 单文件版：一个 exe；运行时解包到 %TEMP%\_MEIxxxxxx，退出后自动删除
exe_onefile = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='penguinprint',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,  # None = 用系统 %TEMP%；启动器不展开环境变量，别写 %APPDATA%
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)

# 文件夹版：不解包、不产生任何临时文件夹
exe_onedir = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='penguinprint',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)
coll = COLLECT(
    exe_onedir,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='penguinprint',
)
