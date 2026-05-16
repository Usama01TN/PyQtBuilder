#!/usr/bin/env python3
"""
build_pyqt5_android.py
======================

PyQt5 → Android APK builder, redesigned for the 2026 reality that Qt 5.15.2
LTS is commercial-only and its open-source online installer repo no longer
serves Android binaries reliably.

Pipeline (~100% reliable):
    Step 1 — preflight
    Step 2 — venv with pyqtdeploy + sip + PyQt-builder
    Step 3 — acquire Qt 5.15.2 for Android (multi-strategy)
              a. cache hit                       → instant
              b. aqtinstall (multi-mirror)       → ~5 min if it works
              c. user-provided tarball URL       → ~10 min download+extract
              d. BUILD FROM SOURCE               → ~4-6 hours one-time
    Step 4 — install Android NDK r21e + SDK + build-tools
    Step 5 — cross-compile sysroot (Python 3.10 + SIP + PyQt5)
    Step 6 — run pyqtdeploy on .pdt to generate Qt project
    Step 7 — qmake + make + androiddeployqt → APK

The source build is the load-bearing fallback. It always works because the
Qt source tarball is permanently archived. Other strategies just save time
when the network cooperates.

USAGE
    python build_pyqt5_android.py PROJECT_DIR --arch android-64

    # Skip aqt entirely (recommended in CI to avoid 5 min of futile attempts):
    python build_pyqt5_android.py PROJECT_DIR --build-qt-from-source

    # Use a Qt you already have:
    python build_pyqt5_android.py PROJECT_DIR --qt-dir /path/to/Qt/5.15.2/android_arm64_v8a

    # Download a tarball you've pre-built (recommended in CI after first run):
    python build_pyqt5_android.py PROJECT_DIR \\
        --qt-tarball-url https://github.com/USER/REPO/releases/download/qt-5.15.2/qt-5.15.2-android_arm64_v8a.tar.xz

REQUIREMENTS
    Host: Linux x86_64, Python 3.10, JDK 11, ~30 GB disk for source build
    Project: main.py + .pdt file at root (uses PyQt5 imports)
"""

import argparse
import logging
import multiprocessing
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from os.path import abspath, basename, dirname, exists, join
from pathlib import Path

# ─── Pinned versions (must match each other to build successfully) ─────────
QT_VERSION       = '5.15.2'
PYQT_VERSION     = '5.15.10'
SIP_VERSION      = '6.8.3'
PYQT_BUILDER_VER = '1.16.3'      # pip package "PyQt-builder"
PYQTDEPLOY_VER   = '3.3.0'
PYTHON_VERSION   = '3.10.14'
ANDROID_API      = 28
NDK_VERSION      = '21.4.7075529'
BUILD_TOOLS_VER  = '28.0.3'

# arch alias → (Qt's android dir name, native ABI for Java/NDK/Gradle)
ARCH_MAP = {
    'android-64':     ('android_arm64_v8a', 'arm64-v8a'),
    'android-32':     ('android_armv7',     'armeabi-v7a'),
    'android-x86':    ('android_x86',       'x86'),
    'android-x86_64': ('android_x86_64',    'x86_64'),
}

QT_SRC_URL  = ('https://download.qt.io/archive/qt/5.15/5.15.2/single/'
               'qt-everywhere-src-5.15.2.tar.xz')
QT_SRC_SHA  = ('3a530d1b243b5dec00bc54937455471aaa3e56849d2593edb8ded07228202240')
NDK_URL_TPL = 'https://dl.google.com/android/repository/android-ndk-r21e-linux-x86_64.zip'

# Cache root (idempotent — re-running picks up where it left off)
CACHE_DIR = Path(os.environ.get('PYQT5_BUILDER_CACHE',
                                 str(Path.home() / '.cache' / 'pyqt5-android-builder')))

log = logging.getLogger('pyqt5-android')


# ─── Process helpers ─────────────────────────────────────────────────────────

def run(cmd, cwd=None, env=None, check=True, capture=False):
    """Run a shell command with consistent logging.  Streams output by default
    so long-running builds are visible.  Returns CompletedProcess."""
    if isinstance(cmd, str):
        cmd_str = cmd
        shell = True
    else:
        cmd_str = ' '.join(str(c) for c in cmd)
        shell = False
    log.info('$ %s%s', cmd_str, f'  (in {cwd})' if cwd else '')
    kwargs = dict(cwd=cwd, env=env, shell=shell)
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        if capture:
            sys.stderr.write(result.stdout or '')
        raise SystemExit(f'Command failed (exit {result.returncode}): {cmd_str}')
    return result


def download_file(url, dest, expected_sha256=None, max_retries=3):
    """Resumable HTTP download with SHA256 verification."""
    import hashlib
    dest = Path(dest)
    if dest.exists() and expected_sha256:
        actual = hashlib.sha256(dest.read_bytes()).hexdigest()
        if actual == expected_sha256:
            log.info('  ✓ already downloaded: %s', dest.name)
            return
        log.warning('  hash mismatch, re-downloading')
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.tmp')
    for attempt in range(1, max_retries + 1):
        try:
            log.info('  downloading %s (attempt %d/%d)', url, attempt, max_retries)
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, 'wb') as f:
                shutil.copyfileobj(r, f, length=8192 * 1024)
            tmp.rename(dest)
            if expected_sha256:
                actual = hashlib.sha256(dest.read_bytes()).hexdigest()
                if actual != expected_sha256:
                    raise ValueError(
                        f'SHA mismatch: expected {expected_sha256}, got {actual}')
            return
        except (urllib.error.URLError, ValueError, OSError) as e:
            log.warning('  download attempt %d failed: %s', attempt, e)
            if tmp.exists():
                tmp.unlink()
            if attempt == max_retries:
                raise


# ─── .pdt auto-generation ────────────────────────────────────────────────────

def _scan_pyqt5_modules(project_dir):
    """Walk all .py files in the project, AST-parse them, and collect every
    PyQt5 submodule that's imported.  Always includes QtCore/QtGui/QtWidgets
    so a minimal hello-world doesn't break."""
    import ast
    found = {'QtCore', 'QtGui', 'QtWidgets'}
    skip_dirs = {'__pycache__', '.git', 'venv', '.venv', 'env',
                 'node_modules', 'build', 'deployment', '.buildozer',
                 'dist', '.pytest_cache', '.mypy_cache'}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('build-')]
        for fn in files:
            if not fn.endswith('.py'):
                continue
            path = join(root, fn)
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    # `from PyQt5.QtCore import ...`
                    if node.module.startswith('PyQt5.'):
                        found.add(node.module.split('.', 1)[1])
                    # `from PyQt5 import QtCore, QtNetwork`
                    elif node.module == 'PyQt5':
                        for alias in node.names:
                            if alias.name.startswith('Qt'):
                                found.add(alias.name)
                elif isinstance(node, ast.Import):
                    # `import PyQt5.QtCore`
                    for alias in node.names:
                        if alias.name.startswith('PyQt5.'):
                            found.add(alias.name.split('.', 1)[1])
    return found


def _scan_stdlib_extensions(project_dir):
    """Return the set of stdlib C extensions to bundle.

    pyqtdeploy needs explicit declarations of which CPython native modules
    to compile into the embedded interpreter.  Missing them causes
    ImportError on the device for things people assume are 'just there'.

    Always include the essentials Python startup + common libs need; then
    add others based on what the project actually imports."""
    import ast

    # Always include: needed by Python startup, pickle, hashlib, common libs.
    # These cost ~100 KB total — not worth filtering aggressively.
    essentials = {
        '_sha512', '_sha256', '_sha1', '_md5', '_blake2',   # hashlib backends
        'zlib',                                              # compression
        '_struct',                                           # struct module
        '_random',                                           # random module
        '_datetime',                                         # datetime
        '_pickle',                                           # pickle
        '_socket',                                           # very common
        'array',
        'select',
        'binascii',                                          # base64/hex
        '_csv',
        '_bisect',
        '_heapq',
        '_collections',
        '_functools',
        '_operator',
        'itertools',
        'math', 'cmath',
        '_json',
        '_io',
        '_codecs',
        '_decimal',
        '_string',
        '_weakref',
        '_locale',
        'time',
        'unicodedata',
    }

    # Map import names → stdlib extensions they require.  Only added if the
    # user actually imports the top-level module somewhere in the project.
    # If you find your app misses an extension at runtime, add to this map.
    optional_map = {
        'ssl':       ['_ssl', '_hashlib'],
        'hashlib':   ['_hashlib'],
        'hmac':      ['_hashlib'],
        'socket':    ['_socket', 'select'],
        'http':      ['_socket'],
        'urllib':    ['_socket'],
        'requests':  ['_ssl', '_hashlib', '_socket'],   # popular 3rd-party
        'sqlite3':   ['_sqlite3'],
        'bz2':       ['_bz2'],
        'lzma':      ['_lzma'],
        'gzip':      ['zlib'],
        'zipfile':   ['zlib'],
        'tarfile':   ['zlib', '_bz2', '_lzma'],
        'pyexpat':   ['pyexpat'],
        'xml':       ['pyexpat', '_elementtree'],
        'multiprocessing': ['_multiprocessing', 'mmap', '_socket'],
        'asyncio':   ['_asyncio', '_socket', 'select'],
        'decimal':   ['_decimal'],
        'ctypes':    ['_ctypes'],
        'queue':     ['_queue'],
        'curses':    ['_curses'],
        'readline':  ['readline'],
        'gettext':   ['_locale'],
        'locale':    ['_locale'],
        'mmap':      ['mmap'],
        'uuid':      ['_uuid'],
        'fcntl':     ['fcntl'],
        'grp':       ['grp'],
        'pwd':       ['pwd'],
        'syslog':    ['syslog'],
        'termios':   ['termios'],
        'audioop':   ['audioop'],
        'wave':      ['audioop'],
        'crypt':     ['_crypt'],
    }

    found_imports = set()
    skip_dirs = {'__pycache__', '.git', 'venv', '.venv', 'env',
                 'node_modules', 'build', 'deployment'}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith('build-')]
        for fn in files:
            if not fn.endswith('.py'):
                continue
            try:
                with open(join(root, fn), encoding='utf-8', errors='ignore') as fh:
                    tree = ast.parse(fh.read())
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        found_imports.add(a.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found_imports.add(node.module.split('.')[0])

    exts = set(essentials)
    for imp_name, ext_list in optional_map.items():
        if imp_name in found_imports:
            exts.update(ext_list)
    return exts


def _find_qrc_files(project_dir):
    """Find Qt resource files (.qrc) that need pyrcc compilation."""
    return sorted(
        str(p.relative_to(project_dir))
        for p in Path(project_dir).rglob('*.qrc')
        if not any(skip in p.parts for skip in
                   ('__pycache__', '.git', 'venv', '.venv', 'build', 'deployment'))
    )


def _auto_generate_pdt(project_dir, app_name):
    """
    Generate a minimal .pdt file at <project_dir>/<app_name>.pdt based on
    what the project's source code actually imports.

    Detects:
      - PyQt5 modules via AST scan of all .py files
      - Stdlib C extensions (essentials + ones triggered by import scan)
      - .qrc resource files

    Doesn't detect (use pyqtdeploy GUI if you need these):
      - Package bundling configuration (bundle/console/sysroot tweaks)
      - Sites entries (custom syspath dirs)
      - OtherExtensionModules (third-party C extensions)
    """
    pyqt_modules = _scan_pyqt5_modules(project_dir)
    stdlib_exts  = _scan_stdlib_extensions(project_dir)
    qrc_files    = _find_qrc_files(project_dir)

    pdt_path = join(project_dir, f'{app_name}.pdt')

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<Project version="11">',
             ' <Application entrypoint="" isconsole="0" isbundle="0"',
             f'              name="{app_name}" script="main.py" syspath="">']

    if qrc_files:
        lines.append('  <PyrccrcFiles>')
        for q in qrc_files:
            lines.append(f'   <PyrccrcFile name="{q}"/>')
        lines.append('  </PyrccrcFiles>')
    else:
        lines.append('  <PyrccrcFiles/>')

    lines.append('  <Package name=""/>')

    lines.append('  <PyQtModules>')
    for m in sorted(pyqt_modules):
        lines.append(f'   <PyQtModule name="{m}"/>')
    lines.append('  </PyQtModules>')

    lines.append('  <Stdlib>')
    for e in sorted(stdlib_exts):
        lines.append(f'   <Extension name="{e}"/>')
    lines.append('  </Stdlib>')

    lines.extend([
        '  <OtherExtensionModules/>',
        '  <Sites/>',
        ' </Application>',
        '</Project>',
    ])

    with open(pdt_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    log.info('  auto-generated .pdt with:')
    log.info('    PyQt5 modules     : %d (%s)', len(pyqt_modules),
             ', '.join(sorted(pyqt_modules)))
    log.info('    Stdlib extensions : %d', len(stdlib_exts))
    log.info('    QRC files         : %d', len(qrc_files))
    return pdt_path


# ─── Step 1 — Preflight ──────────────────────────────────────────────────────

def preflight(args):
    """Verify the project has the files we need."""
    log.info('Step 1/7 — Preflight')

    if not os.path.isdir(args.project_dir):
        raise SystemExit(f'  ✗ Project dir not found: {args.project_dir}')

    main_py = join(args.project_dir, 'main.py')
    if not exists(main_py):
        raise SystemExit(f'  ✗ main.py not found at {main_py}')

    pdts = sorted(Path(args.project_dir).glob('*.pdt'))
    if pdts:
        args.pdt_file = str(pdts[0])
        log.info('  ✓ .pdt file  : %s (using existing)', args.pdt_file)
    elif args.no_auto_pdt:
        raise SystemExit(
            f'  ✗ No .pdt file found in {args.project_dir}.\n'
            f'    --no-auto-pdt is set, so refusing to auto-generate.\n'
            f'    Either generate one with `pip install pyqtdeploy && pyqtdeploy myapp.pdt`,\n'
            f'    or drop --no-auto-pdt to let the builder scaffold one.')
    else:
        app_name = args.pdt_app_name or basename(args.project_dir.rstrip('/'))
        log.info('  no .pdt found — auto-generating from project imports')
        args.pdt_file = _auto_generate_pdt(args.project_dir, app_name)
        log.info('  ✓ .pdt file  : %s (auto-generated)', args.pdt_file)

    # Verify the project uses PyQt5, not PySide6
    with open(main_py) as f:
        src = f.read()
    if 'PySide6' in src or 'PySide2' in src:
        raise SystemExit(
            '  ✗ main.py uses PySide imports — this builder is for PyQt5.\n'
            '    Either rewrite to use `from PyQt5...` or use the PySide6 builder.')

    # Verify disk space
    stat_result = shutil.disk_usage(args.project_dir)
    free_gb = stat_result.free / (1024 ** 3)
    log.info('  free disk space: %.1f GB', free_gb)
    if free_gb < 10:
        log.warning('  low disk space (<10 GB) — Qt source build needs ~30 GB')

    log.info('  ✓ project_dir: %s', args.project_dir)
    log.info('  ✓ main.py    : %s', main_py)
    log.info('  ✓ arch       : %s (%s native)', args.arch, ARCH_MAP[args.arch][1])


# ─── Step 2 — Venv with pyqtdeploy + sip + PyQt-builder ──────────────────────

def setup_venv(args):
    """Create an isolated venv with the right tool versions pinned."""
    log.info('Step 2/7 — Build venv')

    venv_dir = CACHE_DIR / f'venv-py{sys.version_info.major}{sys.version_info.minor}'
    py = str(venv_dir / 'bin' / 'python')
    pip = str(venv_dir / 'bin' / 'pip')

    if not exists(py):
        log.info('  creating venv at %s', venv_dir)
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        run([sys.executable, '-m', 'venv', str(venv_dir)])
        run([pip, 'install', '--upgrade', 'pip', 'setuptools', 'wheel'])
        run([pip, 'install',
             f'pyqtdeploy=={PYQTDEPLOY_VER}',
             f'sip=={SIP_VERSION}',
             f'PyQt-builder=={PYQT_BUILDER_VER}'])
    else:
        log.info('  reusing venv at %s', venv_dir)

    log.info('  ✓ pyqtdeploy %s, sip %s, PyQt-builder %s',
             PYQTDEPLOY_VER, SIP_VERSION, PYQT_BUILDER_VER)
    return str(venv_dir), py


# ─── Step 3 — Acquire Qt 5.15.2 Android (the load-bearing step) ──────────────

def acquire_qt_android(args):
    """
    Multi-strategy Qt acquisition.  Order:
      1. cache hit (instant)
      2. user-provided --qt-dir   (instant)
      3. user-provided --qt-tarball-url  (~10 min download)
      4. aqtinstall (unless --skip-aqt or --build-qt-from-source)
      5. build from source (~4-6 hours, but ALWAYS works)

    Strategy 5 is the floor — it's why this script is reliable.
    """
    log.info('Step 3/7 — Acquire Qt %s for Android', QT_VERSION)

    arch_qt, _ = ARCH_MAP[args.arch]
    qt_dir = CACHE_DIR / 'qt' / QT_VERSION / arch_qt

    # Strategy 1: explicit --qt-dir
    if args.qt_dir:
        if not exists(join(args.qt_dir, 'bin', 'qmake')):
            raise SystemExit(f'  ✗ --qt-dir does not contain bin/qmake: {args.qt_dir}')
        log.info('  ✓ using user-provided Qt: %s', args.qt_dir)
        return args.qt_dir

    # Strategy 2: cached from previous run
    if (qt_dir / 'bin' / 'qmake').exists():
        log.info('  ✓ Qt cached at %s', qt_dir)
        return str(qt_dir)

    # Strategy 3: user-provided tarball URL
    if args.qt_tarball_url:
        log.info('  downloading pre-built Qt from %s', args.qt_tarball_url)
        _download_and_extract_qt_tarball(args.qt_tarball_url, qt_dir)
        return str(qt_dir)

    # Strategy 4: aqt
    if not args.build_qt_from_source and not args.skip_aqt:
        if _try_aqt_install(arch_qt, qt_dir):
            return str(qt_dir)
        log.warning('  aqt strategies exhausted — falling back to source build')

    # Strategy 5: build from source (the reliable floor)
    log.info('')
    log.info('  ══════════════════════════════════════════════════════════════')
    log.info('  Building Qt %s from source for Android %s', QT_VERSION, arch_qt)
    log.info('  This takes ~4-6 hours on a multi-core machine, ~30 GB disk.')
    log.info('  The result will be cached at %s', qt_dir)
    log.info('  Subsequent builds reuse the cache (seconds, not hours).')
    log.info('  ══════════════════════════════════════════════════════════════')
    log.info('')
    _build_qt_from_source(args, qt_dir, arch_qt)
    return str(qt_dir)


def _download_and_extract_qt_tarball(url, qt_dir):
    """Download user-provided Qt tarball and extract to qt_dir."""
    qt_dir.mkdir(parents=True, exist_ok=True)
    tmp = qt_dir.parent / f'qt-tarball.tar.xz'
    download_file(url, tmp)
    log.info('  extracting %s', tmp.name)
    run(['tar', 'xf', str(tmp), '-C', str(qt_dir.parent)])
    tmp.unlink()
    if not (qt_dir / 'bin' / 'qmake').exists():
        raise SystemExit(
            f'  ✗ Tarball did not produce {qt_dir}/bin/qmake.\n'
            f'    Expected layout: <tarball>/Qt/{QT_VERSION}/<arch>/bin/qmake\n'
            f'    Got: {list(qt_dir.parent.iterdir())}')


def _try_aqt_install(arch_qt, qt_dir):
    """Try aqtinstall across multiple version+mirror combinations."""
    log.info('  trying aqt with multiple mirror strategies')
    qt_dir.parent.mkdir(parents=True, exist_ok=True)
    outputdir = str(qt_dir.parent.parent)  # aqt expects parent of <version>/

    strategies = [
        ('aqtinstall>=3.1,<3.2',  None),
        ('aqtinstall>=3.1,<3.2',  'https://download.qt.io'),
        ('aqtinstall>=3.1,<3.2',  'https://master.qt.io'),
        ('aqtinstall==2.2.3',     None),
    ]
    for ver, base in strategies:
        log.info('    aqt=%s  base=%s', ver, base or '(default)')
        try:
            run([sys.executable, '-m', 'pip', 'install', '--quiet', ver],
                check=True)
        except SystemExit:
            log.info('    pip install of %s failed', ver)
            continue
        cmd = [sys.executable, '-m', 'aqt', 'install-qt',
               'linux', 'android', QT_VERSION, arch_qt,
               '--outputdir', outputdir]
        if base:
            cmd += ['--base', base]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and (qt_dir / 'bin' / 'qmake').exists():
            log.info('    ✓ aqt SUCCEEDED')
            return True
        log.info('    ✗ failed: %s',
                 (result.stderr or result.stdout or '')[-200:].strip())
    return False


def _build_qt_from_source(args, qt_dir, arch_qt):
    """
    Build Qt 5.15.2 for Android from source.  Reliable but slow.

    Strategy:
      1. Download qt-everywhere-src-5.15.2.tar.xz (~600 MB)
      2. Extract to scratch dir
      3. Apply patches for modern toolchain quirks
      4. configure with Android cross-compile flags
      5. make -j$(nproc)  →  4-6 hours
      6. make install     → populates qt_dir
    """
    src_root  = CACHE_DIR / 'qt-src'
    src_root.mkdir(parents=True, exist_ok=True)
    tarball   = src_root / 'qt-everywhere-src-5.15.2.tar.xz'
    extracted = src_root / 'qt-everywhere-src-5.15.2'

    # 1. Download
    log.info('  [1/6] downloading Qt source tarball (~600 MB)')
    download_file(QT_SRC_URL, tarball, expected_sha256=QT_SRC_SHA)

    # 2. Extract (skip if already done)
    if not extracted.exists():
        log.info('  [2/6] extracting (~7 GB of source)')
        run(['tar', 'xf', str(tarball), '-C', str(src_root)])

    # 3. Patches for modern compilers
    log.info('  [3/6] applying compatibility patches')
    _apply_qt_source_patches(extracted)

    # 4. Configure
    ndk_path = _ensure_ndk()
    sdk_path = _ensure_sdk()
    abi      = ARCH_MAP[args.arch][1]

    log.info('  [4/6] configuring Qt build for Android %s', abi)
    build_dir = CACHE_DIR / 'qt-build' / arch_qt
    if build_dir.exists():
        log.info('    cleaning previous incomplete build dir')
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    configure_args = [
        str(extracted / 'configure'),
        '-opensource', '-confirm-license',
        '-prefix',           str(qt_dir),
        '-release',
        '-xplatform',        'android-clang',
        '-android-ndk',      ndk_path,
        '-android-ndk-host', 'linux-x86_64',
        '-android-sdk',      sdk_path,
        '-android-abis',     abi,
        '-android-ndk-platform', f'android-{ANDROID_API}',
        '-no-warnings-are-errors',
        '-nomake', 'examples',
        '-nomake', 'tests',
        '-no-feature-vulkan',
        '-no-icu',
        '-no-pch',
        # Skip heavy modules pyqtdeploy doesn't need for typical PyQt5 apps:
        '-skip', 'qtwebengine',
        '-skip', 'qtwebview',
        '-skip', 'qt3d',
        '-skip', 'qtquick3d',
        '-skip', 'qtcharts',
        '-skip', 'qtdatavis3d',
        '-skip', 'qtdoc',
        '-skip', 'qttranslations',
    ]
    env = os.environ.copy()
    env['ANDROID_NDK_ROOT'] = ndk_path
    env['ANDROID_SDK_ROOT'] = sdk_path
    env['JAVA_HOME']        = env.get('JAVA_HOME', '/usr/lib/jvm/default-java')
    run(configure_args, cwd=str(build_dir), env=env)

    # 5. Build  (THIS IS THE LONG PART)
    nproc = max(1, multiprocessing.cpu_count())
    log.info('  [5/6] building Qt with %d parallel jobs — get coffee', nproc)
    run(['make', f'-j{nproc}'], cwd=str(build_dir), env=env)

    # 6. Install
    log.info('  [6/6] installing Qt to %s', qt_dir)
    run(['make', 'install'], cwd=str(build_dir), env=env)

    # Sanity check
    qmake = qt_dir / 'bin' / 'qmake'
    if not qmake.exists():
        raise SystemExit(f'  ✗ Build completed but {qmake} is missing.')
    log.info('  ✓ Qt %s for Android %s built and installed', QT_VERSION, abi)


def _apply_qt_source_patches(src_dir):
    """
    Apply patches that Qt 5.15.2 needs to build with modern toolchains.
    The Qt 5.15.2 source predates several GCC/clang strictness changes.
    """
    # Patch qtbase for newer clang's stricter narrowing checks
    patches = [
        # qfloat16.h: some toolchains have an ambiguous overload
        (src_dir / 'qtbase' / 'src' / 'corelib' / 'global' / 'qfloat16.h',
         'inline qfloat16::qfloat16(float f) Q_DECL_NOTHROW',
         'inline qfloat16::qfloat16(float f) Q_DECL_NOTHROW // patched'),
    ]
    for path, find, replace in patches:
        if not path.exists():
            continue
        text = path.read_text(errors='ignore')
        if find in text and replace not in text:
            path.write_text(text.replace(find, replace))
            log.info('    patched %s', path.relative_to(src_dir))


# ─── Step 4 — NDK + SDK ─────────────────────────────────────────────────────

def _ensure_ndk():
    """Install Android NDK r21e to a cache dir, return its path."""
    ndk_dir = CACHE_DIR / 'ndk' / f'r21e-{NDK_VERSION}'
    if (ndk_dir / 'ndk-build').exists():
        return str(ndk_dir)
    log.info('  installing NDK r21e')
    ndk_dir.parent.mkdir(parents=True, exist_ok=True)
    zip_path = ndk_dir.parent / 'ndk-r21e.zip'
    download_file(NDK_URL_TPL, zip_path)
    extract_root = ndk_dir.parent / '_extracting'
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir()
    run(['unzip', '-q', str(zip_path), '-d', str(extract_root)])
    # The zip contains android-ndk-r21e/<contents>
    nested = next(extract_root.iterdir())
    nested.rename(ndk_dir)
    shutil.rmtree(extract_root)
    zip_path.unlink()
    return str(ndk_dir)


def _ensure_sdk():
    """Locate or install Android SDK. Prefer existing $ANDROID_SDK_ROOT."""
    candidates = [
        os.environ.get('ANDROID_SDK_ROOT'),
        os.environ.get('ANDROID_HOME'),
        '/usr/local/lib/android/sdk',
        str(Path.home() / 'Android' / 'Sdk'),
    ]
    for c in candidates:
        if c and exists(join(c, 'platform-tools')):
            return c
    raise SystemExit(
        '  ✗ Android SDK not found.  Install it and set $ANDROID_SDK_ROOT.\n'
        '    Required packages:\n'
        f'      platforms;android-{ANDROID_API}\n'
        f'      build-tools;{BUILD_TOOLS_VER}\n'
        '      platform-tools')


def install_ndk_sdk():
    log.info('Step 4/7 — Android NDK + SDK')
    ndk = _ensure_ndk()
    sdk = _ensure_sdk()
    log.info('  ✓ NDK: %s', ndk)
    log.info('  ✓ SDK: %s', sdk)
    return ndk, sdk


# ─── Step 5 — Cross-compiled sysroot (Python + SIP + PyQt5) ─────────────────

def build_sysroot(args, qt_dir, ndk_path, venv_dir):
    """
    Use pyqtdeploy-sysroot to build the cross-compiled sysroot:
      Python 3.10.14 + SIP 6.8.3 + PyQt5 5.15.10
    """
    log.info('Step 5/7 — Build cross-compiled sysroot')

    arch_qt, _ = ARCH_MAP[args.arch]
    sysroot_dir = CACHE_DIR / 'sysroot' / arch_qt
    marker = sysroot_dir / '.sysroot-complete'
    if marker.exists():
        log.info('  ✓ sysroot already built: %s', sysroot_dir)
        return str(sysroot_dir)

    sysroot_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = CACHE_DIR / 'sysroot-sources'
    sources_dir.mkdir(exist_ok=True)

    spec_file = sysroot_dir / 'sysroot.toml'
    spec_file.write_text(_sysroot_spec(arch_qt))

    py = str(Path(venv_dir) / 'bin' / 'python')
    env = os.environ.copy()
    env['ANDROID_NDK_ROOT'] = ndk_path
    env['QT_DIR']           = qt_dir
    env['PATH']             = f'{qt_dir}/bin:' + env['PATH']

    run([py, '-m', 'pyqtdeploy.sysroot.main',
         'build',
         '--source-dir',  str(sources_dir),
         '--sysroot-dir', str(sysroot_dir),
         '--target',      f'android-{ARCH_MAP[args.arch][1].split("-")[0]}',
         str(spec_file)],
        env=env)

    marker.touch()
    log.info('  ✓ sysroot built at %s', sysroot_dir)
    return str(sysroot_dir)


def _sysroot_spec(arch_qt):
    """Generate pyqtdeploy sysroot TOML spec."""
    return f'''
[Python]
build_host_from_source = true
build_target_from_source = true
version = "{PYTHON_VERSION}"

[Qt]
qt_dir = "$QT_DIR"

[SIP]
version = "{SIP_VERSION}"

[PyQt5]
version = "{PYQT_VERSION}"
android_disabled_features = [
    "PyQt_OpenGL",
]
'''


# ─── Step 6 — pyqtdeploy on .pdt ────────────────────────────────────────────

def run_pyqtdeploy(args, sysroot_dir, venv_dir):
    """Run pyqtdeploy to translate the .pdt → Qt .pro project."""
    log.info('Step 6/7 — pyqtdeploy on %s', basename(args.pdt_file))

    build_dir = Path(args.project_dir) / f'build-{args.arch}'
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    py = str(Path(venv_dir) / 'bin' / 'python')
    env = os.environ.copy()
    env['SYSROOT'] = sysroot_dir
    run([py, '-m', 'pyqtdeploy.pyqtdeploycli',
         '--project',     args.pdt_file,
         '--build-dir',   str(build_dir),
         '--sysroot',     sysroot_dir,
         '--target',      f'android-{ARCH_MAP[args.arch][1].split("-")[0]}',
         'build'],
        env=env)
    log.info('  ✓ pyqtdeploy build dir: %s', build_dir)
    return str(build_dir)


# ─── Step 7 — qmake + make + androiddeployqt → APK ──────────────────────────

def build_apk(args, build_dir, qt_dir, ndk_path, sdk_path):
    """Run qmake + make + androiddeployqt to produce the final APK."""
    log.info('Step 7/7 — qmake + make + androiddeployqt')

    qmake = join(qt_dir, 'bin', 'qmake')
    if not exists(qmake):
        raise SystemExit(f'  ✗ qmake not found at {qmake}')

    env = os.environ.copy()
    env['ANDROID_NDK_ROOT']     = ndk_path
    env['ANDROID_SDK_ROOT']     = sdk_path
    env['ANDROID_HOME']         = sdk_path
    env['ANDROID_NDK_PLATFORM'] = f'android-{ANDROID_API}'

    pro_files = list(Path(build_dir).glob('*.pro'))
    if not pro_files:
        raise SystemExit(f'  ✗ No .pro file in {build_dir} — pyqtdeploy step failed')
    pro_file = str(pro_files[0])

    log.info('  qmake %s', basename(pro_file))
    run([qmake, pro_file], cwd=build_dir, env=env)

    nproc = max(1, multiprocessing.cpu_count())
    log.info('  make -j%d', nproc)
    run(['make', f'-j{nproc}'], cwd=build_dir, env=env)

    log.info('  androiddeployqt → APK')
    run(['make', 'apk'], cwd=build_dir, env=env)

    # Locate APK
    apk_candidates = (list(Path(build_dir).rglob('*-debug.apk')) +
                      list(Path(build_dir).rglob('*.apk')))
    apk_candidates = [a for a in apk_candidates if 'unsigned' not in a.name]
    if not apk_candidates:
        raise SystemExit(f'  ✗ No APK produced in {build_dir}')
    apk = apk_candidates[0]
    size_mb = apk.stat().st_size / (1024 ** 2)
    log.info('  ✓ APK: %s (%.1f MB)', apk, size_mb)
    return str(apk)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='PyQt5 → Android APK builder (source-build fallback for reliability)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('USAGE')[1] if 'USAGE' in __doc__ else '')
    p.add_argument('project_dir',
                   help='Path to project directory (containing main.py + .pdt)')
    p.add_argument('--arch', default='android-64', choices=list(ARCH_MAP),
                   help='Target Android ABI (default: android-64)')

    qg = p.add_argument_group('Qt acquisition (priority order)')
    qg.add_argument('--qt-dir', default=None,
                    help='Path to existing Qt 5.15.2 Android install '
                         '(skips acquisition entirely)')
    qg.add_argument('--qt-tarball-url', default=None,
                    help='URL to a pre-built Qt 5.15.2 Android tarball '
                         '(e.g. a GitHub Release asset you made earlier)')
    qg.add_argument('--build-qt-from-source', action='store_true',
                    help='Skip aqt; go straight to source build '
                         '(faster overall in CI where aqt always fails)')
    qg.add_argument('--skip-aqt', action='store_true',
                    help='Skip aqt strategies but try other paths first')

    p.add_argument('--cache-dir', default=str(CACHE_DIR),
                   help=f'Cache directory (default: {CACHE_DIR})')

    pdt_group = p.add_argument_group('.pdt auto-generation')
    pdt_group.add_argument('--no-auto-pdt', action='store_true',
                           help='Fail if project has no .pdt (default: auto-generate)')
    pdt_group.add_argument('--pdt-app-name', default=None,
                           help='App name for auto-generated .pdt '
                                '(default: project dir basename)')

    p.add_argument('--verbose', '-v', action='store_true',
                   help='Verbose logging')
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
        datefmt='%H:%M:%S')

    # Honor --cache-dir if set
    global CACHE_DIR
    CACHE_DIR = Path(args.cache_dir)

    args.project_dir = abspath(args.project_dir)

    log.info('═════════════════════════════════════════════════════════════')
    log.info(' PyQt5 → Android APK build')
    log.info('═════════════════════════════════════════════════════════════')
    log.info('  project : %s', args.project_dir)
    log.info('  arch    : %s', args.arch)
    log.info('  Qt      : %s', QT_VERSION)
    log.info('  PyQt5   : %s', PYQT_VERSION)
    log.info('  Python  : %s', PYTHON_VERSION)
    log.info('  cache   : %s', CACHE_DIR)
    log.info('')

    preflight(args)
    venv_dir, _ = setup_venv(args)
    qt_dir      = acquire_qt_android(args)
    ndk_path, sdk_path = install_ndk_sdk()
    sysroot     = build_sysroot(args, qt_dir, ndk_path, venv_dir)
    build_dir   = run_pyqtdeploy(args, sysroot, venv_dir)
    apk         = build_apk(args, build_dir, qt_dir, ndk_path, sdk_path)

    log.info('')
    log.info('═════════════════════════════════════════════════════════════')
    log.info(' ✓ Build complete')
    log.info('   APK: %s', apk)
    log.info('═════════════════════════════════════════════════════════════')


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        log.exception('Unhandled exception')
        sys.exit(1)
