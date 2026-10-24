# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 构建 macOS 桌面应用 Local Dev Project Manager.app

用法（在项目根目录执行）：
    .venv-desktop/bin/pyinstaller --noconfirm --clean app_build.spec
或直接用一键脚本：
    ./build.sh

⚠️ 关键：本文件必须放在项目根目录，且脚本路径用相对路径 'project_console.py'。
   PyInstaller 按 **spec 文件所在目录** 解析相对路径。

依赖说明：本应用只用 Python 标准库（tkinter/subprocess/ssh 系统命令），
无需 hiddenimports。projects.json 不打包进 app —— 运行时按 base_dir()
在 .app 旁边查找，改注册表不需要重新打包。
"""

block_cipher = None

a = Analysis(
    ['project_console.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['test', 'pytest', 'numpy', 'PIL'],
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
    name='LocalDevProjectManager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # console=False：GUI 应用不弹终端窗口
    # 若要排查启动失败，可临时改成 True 并用 ./build.sh debug 打包
    console=False,
)

app = BUNDLE(
    exe,
    name='Local Dev Project Manager.app',
    icon='assets/icon.icns',
    bundle_identifier='com.localdevprojectmanager.app',
    info_plist={
        'CFBundleName': 'Local Dev Project Manager',
        'CFBundleDisplayName': 'Local Dev Project Manager',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '11.0',
    },
)
