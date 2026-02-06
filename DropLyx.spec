# -*- mode: python ; coding: utf-8 -*-

import os
import sys

# Try to find conda Library/bin directory dynamically
conda_lib_bin = None
if sys.platform == 'win32':
    # Check common conda locations
    possible_paths = [
        os.path.join(sys.prefix, 'Library', 'bin'),
        r'C:\ProgramData\miniconda3\Library\bin',
        r'C:\Users\runneradmin\miniconda3\Library\bin',
        os.path.join(os.environ.get('CONDA_PREFIX', ''), 'Library', 'bin'),
    ]
    for path in possible_paths:
        if os.path.exists(path):
            conda_lib_bin = path
            break

# Only add binaries if we found the conda lib bin directory
binaries = []
if conda_lib_bin and os.path.exists(conda_lib_bin):
    dll_files = ['ffi-8.dll', 'ffi.dll', 'libexpat.dll', 'liblzma.dll', 'libbz2.dll']
    for dll in dll_files:
        dll_path = os.path.join(conda_lib_bin, dll)
        if os.path.exists(dll_path):
            binaries.append((dll_path, '.'))

a = Analysis(
    ['DropLyx.py'],
    pathex=[],
    binaries=binaries,
    datas=[
        ('DropLyx_logo.png', '.'),
    ],
    hiddenimports=['plyer.platforms', 'plyer.platforms.win', 'plyer.platforms.win.notification'],
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
    [],
    exclude_binaries=True,
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
    icon='DropLyx_icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DropLyx',
)
