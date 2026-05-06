#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_pyside6_android.py — single-pass PySide6 → Android APK builder

A focused rewrite of the original pyside6_android_builder.py that produces a
working APK from a PySide6 6.10.x project on a single deploy invocation,
instead of the two-pass (build → patch spec → rebuild) dance the original
needed.

WHY A REWRITE
=============

Two upstream issues prevent stock pyside6-android-deploy from producing a
working APK:

(1) pyside6's deploy_lib/android/buildozer.py hardcodes:

        line 27:  requirements = python3,shiboken6,PySide6
        line 47:  p4a.branch   = develop

    Develop's python3 recipe builds Python 3.14, but PySide6 wheels are
    cp311.  The APK ends up shipping libpython3.14.so while libshiboken6
    has DT_NEEDED libpython3.11.so → dlopen failure at app startup.

    pyside6 forces 'develop' because it needs commit b92522f from p4a (qt
    bootstrap fix, March 2024) which has never been merged to master and
    has never been tagged in a release.  Tag v2024.01.21 has the right
    Python version but not the qt fix; develop has the qt fix but the
    wrong Python.  The sweet-spot commit is 2ebea90d (2025-07-25) which
    has both.

    This script patches pyside6's buildozer.py at venv-setup time so the
    spec it generates is correct on the first try.

(2) APK ZIPs do not preserve symlinks.  p4a's python3 recipe builds
    libpython3.11.so.1.0 and creates a libpython3.11.so → ...so.1.0
    symlink in the staging dir, but only the regular file ends up in the
    APK.  dlopen("libpython3.11.so") fails.

    This script post-processes the APK after build: copies the versioned
    file under the unversioned name, strips signatures, re-zipaligns,
    re-signs with the debug keystore.

USAGE
=====

    build_pyside6_android.py /path/to/project --arch arm64-v8a [--mode debug]

Project layout requirement: a main.py at the project root.

The script does NOT install system packages.  The CI workflow is
responsible for apt-installing build-essential, libffi-dev, libssl-dev,
zlib1g-dev, libncurses-dev, libsqlite3-dev, libbz2-dev, liblzma-dev,
libreadline-dev, libgl1, libegl1, libxkbcommon0, libdbus-1-3,
libfontconfig1, openjdk-17-jdk-headless, autoconf, automake, libtool,
pkg-config, m4, cmake, unzip, zip, git, curl, ccache.

Host Python: 3.11 only.  Other versions are rejected up front because
PySide6 6.10.x wheels are cp311-only on Linux.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from os.path import (basename, dirname, exists, expanduser, getmtime, getsize,
                     isdir, join)
from venv import create as venv_create

# ─── Constants ────────────────────────────────────────────────────────────────

PYSIDE6_VERSION = '6.10.2'
TARGET_PYTHON   = '3.11.5'

# p4a fork + commit-ish that has BOTH:
#   - the qt bootstrap fix (commit b92522f, not in any tagged release)
#   - the Python 3.11.5 recipe (next commit on develop bumps to 3.14)
# This commit is 2ebea90d on kivy/python-for-android (2025-07-25, "Update:
# numpy, pandas, sdl2 to newer versions which support ndk28c").  It is the
# last develop commit that satisfies both constraints.
P4A_FORK   = 'kivy'
P4A_BRANCH = '2ebea90d'

# Python packages required in the venv beyond what PySide6's
# requirements-android.txt installs.  cython is needed by buildozer to compile
# native modules.  The rest are p4a 2024.01.21's runtime deps; without them
# any p4a recipe import (e.g. for the qt bootstrap) raises ModuleNotFoundError.
EXTRA_VENV_PACKAGES = (
    'cython',
    'appdirs', 'build', 'colorama', 'jinja2', 'packaging', 'sh', 'toml',
)

DEFAULT_VENV_DIR = '~/.cache/pyside6-android-builder/venv-{arch}'
DEFAULT_WHEELS_DIR = '~/.cache/pyside6-android-builder/wheels'
DEFAULT_P4A_DIR = '~/.cache/pyside6-android-builder/p4a-pinned'
DEFAULT_NDK_VERSION = '27.2.12479018'   # Recommended by Qt for 6.10.x
DEFAULT_API = '34'

# Qt's official wheel server. Hosts cross-compiled PySide6 + shiboken6 wheels
# for Android, named e.g. PySide6-6.10.2-6.10.2-cp311-cp311-android_aarch64.whl.
WHEEL_BASE_URL = 'https://download.qt.io/official_releases/QtForPython'

ARCH_ABIS = {
    'arm64-v8a':   'aarch64',
    'armeabi-v7a': 'armv7a',
    'x86_64':      'x86_64',
    'x86':         'i686',
}

log = logging.getLogger('builder')


# ─── Logging ──────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool) -> None:
    fmt = '%(asctime)s  %(levelname)-8s  %(message)s'
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=fmt, datefmt='%H:%M:%S')


# ─── Process helpers ──────────────────────────────────────────────────────────

def run(cmd, *, cwd=None, env=None, capture=False, check=True):
    """Run a subprocess, log it, and return the CompletedProcess."""
    log.debug('$ %s%s', ' '.join(map(str, cmd)),
              '   (cwd={})'.format(cwd) if cwd else '')
    return subprocess.run(
        list(map(str, cmd)),
        cwd=cwd, env=env,
        capture_output=capture, text=True if capture else None,
        check=check,
    )


def section(title: str) -> None:
    log.info('-' * 60)
    log.info('  %s', title)
    log.info('-' * 60)


# ─── Step 1 — preflight checks ────────────────────────────────────────────────

def preflight(args) -> None:
    """Validate environment before doing any expensive work."""
    section('Step 1/8 — Preflight')

    # Host Python must be 3.11.x.
    if sys.version_info[:2] != (3, 11):
        raise SystemExit(
            'Host Python is {}.{}.{} but PySide6 6.10.x wheels are cp311-only.\n'
            'Run this script with Python 3.11.'
            .format(*sys.version_info[:3]))
    log.info('Host Python: %s.%s.%s ✓', *sys.version_info[:3])

    # Project layout.
    if not isdir(args.project_dir):
        raise SystemExit('Project dir not found: ' + args.project_dir)
    if not exists(join(args.project_dir, 'main.py')):
        raise SystemExit('Project must contain main.py: ' + args.project_dir)
    log.info('Project: %s ✓', args.project_dir)

    # Arch must be supported.
    if args.arch not in ARCH_ABIS:
        raise SystemExit('Unsupported arch: {}.  Pick one of: {}'
                         .format(args.arch, ', '.join(ARCH_ABIS)))
    log.info('Target arch: %s', args.arch)

    # Venv must be OUTSIDE the project (pyside6-android-deploy refuses
    # otherwise — it scans the whole project tree for .py files and the
    # venv would balloon scan time and confuse it).
    venv_abs = os.path.abspath(args.venv_dir)
    proj_abs = os.path.abspath(args.project_dir)
    if venv_abs == proj_abs or venv_abs.startswith(proj_abs + os.sep):
        raise SystemExit(
            'Virtual environment must be outside the project directory.\n'
            '  venv:    {}\n  project: {}'.format(venv_abs, proj_abs))
    log.info('Venv path: %s', venv_abs)


# ─── Step 2 — venv setup ──────────────────────────────────────────────────────

def setup_venv(args):
    """Create venv, install PySide6 and all build-time deps. Returns paths."""
    section('Step 2/8 — Virtual environment')
    venv_dir = os.path.abspath(args.venv_dir)
    py_exe   = join(venv_dir, 'bin', 'python')
    pip_exe  = join(venv_dir, 'bin', 'pip')

    if not exists(py_exe):
        log.info('Creating venv at %s', venv_dir)
        venv_create(venv_dir, with_pip=True, clear=False)
    else:
        log.info('Reusing venv at %s', venv_dir)

    run([py_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'])

    log.info('Installing PySide6 %s ...', PYSIDE6_VERSION)
    run([pip_exe, 'install', '--quiet', '--no-warn-script-location',
         'pyside6=={}'.format(PYSIDE6_VERSION)])

    # PySide6 ships its own Android-deploy requirements file.  Without these
    # (in particular pkginfo), pyside6-android-deploy crashes on first run.
    log.info('Installing PySide6 Android-deploy runtime requirements ...')
    py = run([py_exe, '-c',
              'import os, PySide6; '
              'p = os.path.join(os.path.dirname(PySide6.__file__), '
              '"scripts", "requirements-android.txt"); '
              'print(p if os.path.exists(p) else "")'],
             capture=True).stdout.strip()
    if py:
        run([pip_exe, 'install', '-r', py, '--quiet', '--no-warn-script-location'])

    log.info('Installing extra build-time deps: %s', ', '.join(EXTRA_VENV_PACKAGES))
    run([pip_exe, 'install', '--quiet', '--no-warn-script-location',
         *EXTRA_VENV_PACKAGES])

    return venv_dir, py_exe


# ─── Step 3 — pre-clone python-for-android at the pinned commit ───────────────

def prepare_p4a_pinned() -> str:
    """
    Clone (or update) python-for-android at the commit that has BOTH:
      - the qt bootstrap fix PySide6 needs (commit b92522f, March 2024)
      - the Python 3.11.5 recipe (next commit on develop bumps to 3.14)
    Returns the absolute path to the local clone.

    Why we do this ourselves instead of letting buildozer clone:
    buildozer invokes `git clone --branch <p4a.branch>`, which only accepts
    branch/tag names — not commit hashes.  Our target (2ebea90d) is a
    detached commit that no tag points at, so we have to clone first and
    then `git checkout` the SHA.  We then tell PySide6 to use this local
    clone via p4a.source_dir, bypassing buildozer's clone step entirely.
    """
    section('Step 3/8 — Pin python-for-android to ' + P4A_BRANCH)
    p4a_dir = os.path.abspath(expanduser(DEFAULT_P4A_DIR))
    repo_url = 'https://github.com/{}/python-for-android.git'.format(P4A_FORK)

    if isdir(join(p4a_dir, '.git')):
        # Existing clone — verify it's at the right commit, otherwise
        # fetch+checkout to bring it up to date.
        head = run(['git', '-C', p4a_dir, 'rev-parse', 'HEAD'],
                   capture=True).stdout.strip()
        if head.startswith(P4A_BRANCH):
            log.info('Reusing existing clone at %s', p4a_dir)
            log.info('  HEAD: %s ✓', head[:12])
            return p4a_dir
        log.info('Existing clone at %s is on %s; updating to %s',
                 p4a_dir, head[:12], P4A_BRANCH)
        run(['git', '-C', p4a_dir, 'fetch', '--all', '--quiet'])
    else:
        log.info('Cloning %s → %s', repo_url, p4a_dir)
        os.makedirs(dirname(p4a_dir), exist_ok=True)
        run(['git', 'clone', '--quiet', repo_url, p4a_dir])

    run(['git', '-C', p4a_dir, 'checkout', '--quiet', P4A_BRANCH])
    head = run(['git', '-C', p4a_dir, 'rev-parse', 'HEAD'],
               capture=True).stdout.strip()
    log.info('  HEAD: %s ✓', head[:12])

    # Sanity-check: the python3 recipe in the clone should say 3.11.5.
    recipe = join(p4a_dir, 'pythonforandroid', 'recipes', 'python3', '__init__.py')
    if exists(recipe):
        with open(recipe) as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith('version'):
                    log.info('  python3 recipe: %s', stripped)
                    if TARGET_PYTHON not in stripped:
                        log.warning('  Recipe version does not contain %s — '
                                    'p4a commit may have shifted.', TARGET_PYTHON)
                    break
    return p4a_dir


# ─── Step 4 — patch PySide6's buildozer.py (the load-bearing fix) ─────────────

def patch_pyside6_buildozer(venv_dir: str, p4a_source_dir: str) -> None:
    """
    Edit the venv copy of pyside6/scripts/deploy_lib/android/buildozer.py:
      * line 27: requirements = python3,...     →  python3=={ver},...
      * line 47: p4a.branch  = "develop"         →  p4a.source_dir = {our_clone}

    The first edit pins the bundled Python version inside the APK.  The
    second redirects buildozer at our pre-cloned p4a (pinned to a commit
    with both the qt bootstrap fix PySide6 needs AND the Python 3.11.5
    recipe), bypassing buildozer's own `git clone --branch <hash>` which
    git rejects for non-branch/non-tag refs.
    """
    section('Step 4/8 — Patch PySide6 buildozer-spec generator')
    py_exe = join(venv_dir, 'bin', 'python')
    out = run([py_exe, '-c',
               'import os, PySide6; '
               'print(os.path.join(os.path.dirname(PySide6.__file__), '
               '"scripts", "deploy_lib", "android", "buildozer.py"))'],
              capture=True).stdout.strip()
    if not exists(out):
        raise RuntimeError('PySide6 buildozer.py not found at: ' + out)
    log.info('Patching: %s', out)

    with open(out, 'r', encoding='utf-8') as fh:
        src = fh.read()

    new_src, requirements_n = re.subn(
        r'(self\.set_value\(\s*"app"\s*,\s*"requirements"\s*,\s*")'
        r'python3'
        r'(\s*,\s*shiboken6\s*,\s*PySide6\s*"\s*\))',
        r'\g<1>python3==' + TARGET_PYTHON + r'\g<2>',
        src, count=1,
    )
    # Replace the p4a.branch = "develop" line with a p4a.source_dir line.
    new_src, source_dir_n = re.subn(
        r'self\.set_value\(\s*"app"\s*,\s*"p4a\.branch"\s*,\s*"develop"\s*\)',
        'self.set_value("app", "p4a.source_dir", "' + p4a_source_dir + '")',
        new_src, count=1,
    )

    if requirements_n == 0:
        log.warning('Could not find the "requirements" set_value() call to patch.')
        log.warning('PySide6 layout may have changed.  Build will proceed but '
                    'may produce a broken APK.')
    else:
        log.info('  requirements line patched (python3==%s)', TARGET_PYTHON)
    if source_dir_n == 0:
        log.warning('Could not find the p4a.branch line to redirect to source_dir.')
        log.warning('Build will use whatever p4a buildozer clones from develop, '
                    'which currently has Python 3.14.')
    else:
        log.info('  p4a.branch → p4a.source_dir = %s', p4a_source_dir)

    if new_src != src:
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(new_src)
        cache_dir = join(dirname(out), '__pycache__')
        if isdir(cache_dir):
            shutil.rmtree(cache_dir, ignore_errors=True)
        log.info('  ✓ wrote patched buildozer.py')
    else:
        log.warning('No edits applied — file may already be patched, or layout '
                    'has shifted.  Continuing.')


# ─── Step 5 — Android SDK / NDK ──────────────────────────────────────────────

def ensure_android_sdk_ndk(args):
    """
    Use SDK/NDK paths from args (typically set by the CI workflow that
    cached them) — or trust ~/.buildozer to manage its own.  We do not
    download here; that's the CI workflow's job.
    """
    section('Step 5/8 — Android SDK / NDK')
    sdk = args.sdk_path or expanduser('~/.android/sdk')
    ndk = args.ndk_path or join(sdk, 'ndk', DEFAULT_NDK_VERSION)
    if not isdir(ndk):
        log.warning('NDK not at %s — buildozer will download its own copy.', ndk)
        ndk = ''      # signal pyside6-android-deploy to let buildozer manage it
    if not isdir(sdk):
        log.warning('SDK not at %s — buildozer will download its own copy.', sdk)
        sdk = ''
    log.info('SDK: %s', sdk or '(buildozer will manage)')
    log.info('NDK: %s', ndk or '(buildozer will manage)')
    return sdk, ndk


# ─── Step 6 — download cross-compiled PySide6 + shiboken6 wheels ─────────────

def download_android_wheels(args):
    """
    Fetch the cross-compiled Android wheels from Qt's server and return
    (wheel_pyside_path, wheel_shiboken_path).  Wheels are cached at
    ~/.cache/pyside6-android-builder/wheels/ — ~80 MB each, so we very
    much do not want to re-download on every run.

    Wheel filenames look like:
        PySide6-6.10.2-6.10.2-cp311-cp311-android_aarch64.whl
        shiboken6-6.10.2-6.10.2-cp311-cp311-android_aarch64.whl

    The arch component uses Linux-style names (aarch64, armv7a, x86_64,
    i686), not Android ABI names — so we map via ARCH_ABIS.
    """
    section('Step 6/8 — PySide6 Android wheels')
    import urllib.request

    wheels_dir = expanduser(DEFAULT_WHEELS_DIR)
    os.makedirs(wheels_dir, exist_ok=True)

    wheel_arch = ARCH_ABIS[args.arch]   # e.g. 'arm64-v8a' -> 'aarch64'
    py_tag = 'cp311-cp311'              # PySide6 6.10.x is cp311-only on Android
    base = '{ver}-{ver}-{tag}-android_{arch}.whl'.format(
        ver=PYSIDE6_VERSION, tag=py_tag, arch=wheel_arch)

    wheels = {
        'pyside': ('PySide6-' + base,  WHEEL_BASE_URL + '/pyside6/PySide6-' + base),
        'shiboken': ('shiboken6-' + base, WHEEL_BASE_URL + '/shiboken6/shiboken6-' + base),
    }

    paths = {}
    for kind, (filename, url) in wheels.items():
        dest = join(wheels_dir, filename)
        if exists(dest) and getsize(dest) > 0:
            log.info('cached: %s (%.1f MB)', filename, getsize(dest) / 1024 ** 2)
        else:
            log.info('downloading: %s', url)
            try:
                tmp = dest + '.partial'
                urllib.request.urlretrieve(url, tmp)
                os.rename(tmp, dest)
                log.info('  → %s (%.1f MB)', dest, getsize(dest) / 1024 ** 2)
            except Exception as e:
                raise SystemExit(
                    'Failed to download {}\n  url:  {}\n  err:  {}\n'
                    'If this is a 404, check the Qt download page at\n  {}/pyside6/\n'
                    'and update PYSIDE6_VERSION or the wheel-naming code.'
                    .format(filename, url, e, WHEEL_BASE_URL))
        paths[kind] = dest

    return paths['pyside'], paths['shiboken']


# ─── Step 7 — run pyside6-android-deploy ──────────────────────────────────────

def run_deploy(args, venv_dir, py_exe, sdk, ndk, wheel_pyside, wheel_shiboken):
    """Invoke pyside6-android-deploy in single-pass mode."""
    section('Step 7/8 — pyside6-android-deploy')
    deploy = join(venv_dir, 'bin', 'pyside6-android-deploy')
    if not exists(deploy):
        raise RuntimeError('pyside6-android-deploy not found at ' + deploy)

    # Buildozer invokes shell tools (cython, m4, autoconf...).  Without our
    # venv's bin/ on PATH it can't see cython and fails partway through.
    env = os.environ.copy()
    env['PATH'] = join(venv_dir, 'bin') + os.pathsep + env.get('PATH', '')
    env['PYTHONUNBUFFERED'] = '1'

    cmd = [
        deploy,
        '--name', args.app_name,
        '--wheel-pyside', wheel_pyside,
        '--wheel-shiboken', wheel_shiboken,
        '--force',
    ]
    if sdk:
        cmd += ['--sdk-path', sdk]
    if ndk:
        cmd += ['--ndk-path', ndk]

    log.info('Running: %s', ' '.join(cmd))
    log.info('  cwd=%s', args.project_dir)
    run(cmd, cwd=args.project_dir, env=env)

    ext = '.apk' if args.mode == 'debug' else '.aab'
    candidates = []
    for root, _dirs, files in os.walk(args.project_dir):
        for f in files:
            if f.endswith(ext):
                candidates.append(join(root, f))
    if not candidates:
        raise RuntimeError('No {} produced.  Inspect build log for errors.'.format(ext))
    artifact = sorted(candidates, key=getmtime)[-1]
    log.info('Build artifact: %s (%.1f MB)', artifact, getsize(artifact) / 1024 ** 2)
    return artifact


# ─── Step 8 — APK post-processing ─────────────────────────────────────────────

def post_process_apk(apk_path, sdk):
    """
    Add unversioned libpython3.11.so alongside the versioned libpython3.11.so.1.0
    that p4a produces.  APKs do not preserve symlinks, so without this libshiboken6
    cannot resolve its DT_NEEDED entry at runtime.
    """
    section('Step 8/8 — APK post-processing (libpython SONAME fix)')

    with zipfile.ZipFile(apk_path, 'r') as z:
        names = z.namelist()
    pylibs = sorted(n for n in names if 'libpython' in n.lower())
    log.info('libpython entries in APK:')
    for p in pylibs:
        log.info('   %s', p)

    if not any('libpython3.11' in n for n in pylibs):
        # No 3.11 → the patched build did not stick.  Fail loudly.
        raise RuntimeError(
            'APK does not contain libpython3.11.* — the PySide6 buildozer.py '
            'patch did not take effect, or buildozer used a different p4a.\n'
            'Check the build log for "p4a.branch" and "requirements = python3" '
            'to see what was actually written into buildozer.spec.')

    # Plan the alias additions: for each lib/<arch>/libpython3.11.so.X[.Y],
    # if there's no plain libpython3.11.so in the same arch dir, add one.
    versioned_pat   = re.compile(r'^(lib/[^/]+/libpython3\.11\.so)(?:\.\d+)+$')
    aliases = {}    # versioned name -> unversioned name
    arch_dirs = sorted({n.split('/', 2)[1] for n in names
                        if n.startswith('lib/') and n.count('/') >= 2})
    for arch in arch_dirs:
        prefix = 'lib/' + arch + '/'
        unversioned = prefix + 'libpython3.11.so'
        if unversioned in names:
            log.info('  lib/%s/: already has unversioned libpython3.11.so', arch)
            continue
        versioned_here = [n for n in names
                          if n.startswith(prefix) and versioned_pat.match(n)]
        if versioned_here:
            # Pick libpython3.11.so.1.0 if present, else the first versioned.
            best = next((v for v in versioned_here if v.endswith('.1.0')),
                        versioned_here[0])
            aliases[best] = unversioned
            log.info('  lib/%s/: will alias %s → libpython3.11.so',
                     arch, basename(best))

    if not aliases:
        log.info('No aliasing needed.')
        return

    # Rewrite the APK with the new entries; strip old signatures so we can
    # re-sign cleanly.
    sig_pat = re.compile(r'^META-INF/.*\.(SF|RSA|DSA|EC)$')
    tmp = apk_path + '.tmp'
    with zipfile.ZipFile(apk_path, 'r') as zin, \
         zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if sig_pat.match(item.filename):
                continue
            zout.writestr(item, zin.read(item.filename))
        for src, dst in aliases.items():
            zout.writestr(dst, zin.read(src), zipfile.ZIP_DEFLATED)
    shutil.move(tmp, apk_path)
    log.info('  Added %d alias entries.', len(aliases))

    # Locate apksigner + zipalign.
    bt = _find_buildtools(sdk)
    if not bt:
        log.warning('  Android build-tools not found.  APK is NOT re-signed.  '
                    '`adb install` will probably fail.')
        return

    log.info('  Using build-tools at: %s', bt)
    aligned = apk_path + '.aligned'
    run([join(bt, 'zipalign'), '-f', '-p', '4', apk_path, aligned])
    shutil.move(aligned, apk_path)

    keystore = expanduser('~/.android/debug.keystore')
    if not exists(keystore):
        log.info('  Generating debug keystore at %s', keystore)
        os.makedirs(dirname(keystore), exist_ok=True)
        run(['keytool', '-genkeypair', '-v',
             '-keystore', keystore,
             '-storepass', 'android', '-keypass', 'android',
             '-alias', 'androiddebugkey',
             '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000',
             '-dname', 'CN=Android Debug,O=Android,C=US'])
    run([join(bt, 'apksigner'), 'sign',
         '--ks', keystore,
         '--ks-pass', 'pass:android',
         '--key-pass', 'pass:android',
         apk_path])
    log.info('  ✓ APK re-signed.')


def _find_buildtools(sdk_hint):
    """Locate Android SDK build-tools/<latest>/ that has apksigner + zipalign."""
    candidates = [
        sdk_hint,
        expanduser('~/.buildozer/android/platform/android-sdk'),
        expanduser('~/.android/sdk'),
    ]
    for sdk in filter(None, candidates):
        bt_root = join(sdk, 'build-tools')
        if not isdir(bt_root):
            continue
        for v in sorted(os.listdir(bt_root), reverse=True):
            cand = join(bt_root, v)
            if all(exists(join(cand, t)) for t in ('apksigner', 'zipalign')):
                return cand
    return None


# ─── Argument parsing ─────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description='Build a PySide6 Android APK (single-pass, fixed-up).',
    )
    p.add_argument('project_dir',
                   help='Path to the PySide6 project (must contain main.py)')
    p.add_argument('--app-name',  default='pyside6app',
                   help='Application name (default: pyside6app)')
    p.add_argument('--arch',      default='arm64-v8a', choices=list(ARCH_ABIS),
                   help='Target ABI (default: arm64-v8a)')
    p.add_argument('--mode',      default='debug', choices=['debug', 'release'],
                   help='Build mode (default: debug)')
    p.add_argument('--venv-dir',  default=None,
                   help='Virtualenv directory (default: '
                        '~/.cache/pyside6-android-builder/venv-<arch>)')
    p.add_argument('--sdk-path',  default='',
                   help='Android SDK path (optional; buildozer manages its own)')
    p.add_argument('--ndk-path',  default='',
                   help='Android NDK path (optional; buildozer manages its own)')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Verbose subprocess logging')
    args = p.parse_args(argv)

    if not args.venv_dir:
        args.venv_dir = expanduser(DEFAULT_VENV_DIR.format(arch=args.arch))

    args.project_dir = os.path.abspath(args.project_dir)
    return args


# ─── Entry point ──────────────────────────────────────────────────────────────

def main(argv=None):
    args = parse_args(argv)
    setup_logging(args.verbose)

    log.info('PySide6 → Android build')
    log.info('   project    : %s', args.project_dir)
    log.info('   arch       : %s', args.arch)
    log.info('   mode       : %s', args.mode)
    log.info('   target Py  : %s', TARGET_PYTHON)
    log.info('   p4a branch : %s @ %s', P4A_FORK, P4A_BRANCH)
    log.info('')

    preflight(args)
    venv_dir, py_exe = setup_venv(args)
    p4a_dir = prepare_p4a_pinned()
    patch_pyside6_buildozer(venv_dir, p4a_dir)
    sdk, ndk = ensure_android_sdk_ndk(args)
    wheel_pyside, wheel_shiboken = download_android_wheels(args)
    apk = run_deploy(args, venv_dir, py_exe, sdk, ndk, wheel_pyside, wheel_shiboken)
    post_process_apk(apk, sdk)

    log.info('')
    log.info('=' * 60)
    log.info('Build complete: %s', apk)
    log.info('=' * 60)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as e:
        log.error('Subprocess failed: %s (exit %d)', ' '.join(map(str, e.cmd)),
                  e.returncode)
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        log.error('Interrupted')
        sys.exit(130)
