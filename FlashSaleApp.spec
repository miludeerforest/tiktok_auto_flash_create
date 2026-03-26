# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path


datas = []

captcha_spec = find_spec('captcha_recognizer')
if captcha_spec and captcha_spec.submodule_search_locations:
    model_dir = Path(list(captcha_spec.submodule_search_locations)[0]) / 'models'
    if model_dir.exists():
        datas.append((str(model_dir), 'captcha_recognizer/models'))

a = Analysis(
    ['app_gui.py'],
    pathex=[],
    binaries=[],
    datas=datas,
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
    name='FlashSaleApp',
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
)
