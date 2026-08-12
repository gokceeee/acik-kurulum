# -*- mode: python ; coding: utf-8 -*-
r"""
ACIK Kurulum – PyInstaller spec dosyası.

Kullanım:
    .\.venv\Scripts\pyinstaller ACIK_Kurulum.spec

Çıktı:
    dist\ACIK-Kurulum\ACIK-Kurulum.exe   (tek-klasör modu)
"""

import os
from pathlib import Path

SPEC_DIR = os.path.abspath(SPECPATH)
# Public source delivery intentionally contains no deployment payloads,
# connection profiles, installer binaries, or secrets.  A private release
# pipeline may supply its own approved payload list outside this repository.
SAFE_PAYLOAD_ITEMS: list[str] = []
PAYLOAD_DATAS = [
    (
        os.path.join(SPEC_DIR, 'payloads', item),
        os.path.join('payloads', item) if os.path.isdir(os.path.join(SPEC_DIR, 'payloads', item)) else 'payloads',
    )
    for item in SAFE_PAYLOAD_ITEMS
    if os.path.exists(os.path.join(SPEC_DIR, 'payloads', item))
]

a = Analysis(
    [os.path.join(SPEC_DIR, 'run_app.py')],
    pathex=[os.path.join(SPEC_DIR, 'src')],
    binaries=[],
    datas=[
        # Görsel varlıklar (logo, ikonlar)
        (os.path.join(SPEC_DIR, 'assets'), 'assets'),
        # Yalnizca izin verilen payload dosyalari paketlenir.
        *PAYLOAD_DATAS,
        # Yalnizca secretsiz ornek config paketlenir.
        (os.path.join(SPEC_DIR, 'app_config.example.json'), '.'),
        (os.path.join(SPEC_DIR, 'payload_manifest.json'), '.'),
    ],
    hiddenimports=[
        'acik_onboarding',
        'acik_onboarding.app',
        'acik_onboarding.config',
        'acik_onboarding.payload_catalog',
        'acik_onboarding.services',
        'acik_onboarding.ui',
        'acik_onboarding.workflow',
        'PySide6.QtWidgets',
        'PySide6.QtCore',
        'PySide6.QtGui',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ACIK-Kurulum',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI uygulaması, konsol penceresi açılmasın
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Normal mod run_app.py tarafindan gerektiğinde yukseltilir. Bu manifest
    # false olmali ki --post-login standart kullanici oturumunda UAC istemesin.
    uac_admin=False,
    contents_directory='.',  # _internal/ yerine EXE ile aynı klasöre koy
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ACIK-Kurulum-v5.21',
)
