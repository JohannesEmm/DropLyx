# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['DropLyx.py'],
    pathex=[],
    binaries=[
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\libexpat.dll', '.'),
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\liblzma.dll', '.'),
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\libbz2.dll', '.'),
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\ffi.dll', '.'),
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\tcl86t.dll', '.'),
        ('C:\\ProgramData\\miniconda3\\Library\\bin\\tk86t.dll', '.'),
    ],
    datas=[
        ('lyx_logo_small.png', '.'),
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
    icon='lyx_icon.ico',
)
