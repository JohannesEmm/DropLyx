# -*- mode: python ; coding: utf-8 -*-
# macOS build spec for DropLyx

a = Analysis(
    ['DropLyx.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('DropLyx_logo.png', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DropLyx',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

app = BUNDLE(
    exe,
    name='DropLyx.app',
    icon=None,  # Can add .icns file later
    bundle_identifier='com.droplyx.app',
    info_plist={
        'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': 'True',
        'LSUIElement': '1',  # Makes it a menu bar app without dock icon
    },
)
