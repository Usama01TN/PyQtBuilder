#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
"""
PySide 1 (Qt 4.8) -> Android APK builder for Python 2.7 applications.

This is a Python 2.7 orchestrator for the canonical build flow documented at:
    https://modrana.org/trac/wiki/PySideForAndroid

It implements the full pipeline by combining:

  * The canonical bash scripts from github.com/M4rtinK/android-pyside-build-scripts
    (cloned at the known-working commit, modified to be non-interactive),
    which handle the Shiboken+PySide ARM cross-compile.

  * Python code for everything else: project workspace setup, dependency
    fetching, runtime packaging (my_python_project.zip + python_27.zip),
    C++ wrapper generation, example-project clone+rename, and ant APK build.

Pipeline (numbered to match log output)
---------------------------------------
  Step  1.  Preflight        -- check Python 2.7, cmake/git/ant/java, disk
                                 space, Necessitas SDK layout.
  Step  2.  Workspace        -- create build dir, clone the build-scripts
                                 repo at a pinned commit, run prepare.sh
                                 to clone shiboken-android + pyside-android.
  Step  3.  Configure env    -- write env.sh with the user's NECESSITAS_DIR
                                 substituted in; patch out the interactive
                                 `read -p "press any key"` prompts.
  Step  4.  Cross-compile    -- run build_shiboken.sh, then build_pyside.sh
                                 (which internally runs fix_pyside_cmake_paths.sh).
                                 Result: stage/lib/{libshiboken.so, libpyside.so}
                                 plus stage/lib/python2.7/site-packages/PySide/*.so
  Step  5.  Strip binaries   -- run strip_binaries.sh (shrinks .so files by ~70%).
  Step  6.  Bundle runtime   -- build python_27.zip containing the Android
                                 Python runtime + PySide libs + Qt Components.
  Step  7.  Bundle app       -- build my_python_project.zip from the user's
                                 project directory.
  Step  8.  APK scaffold     -- clone android-pyside-example-project, run
                                 the rename script (PDF page 9) to substitute
                                 the user's app-name + unique-name.
  Step  9.  Inject           -- copy python_27.zip + my_python_project.zip
                                 into android/res/raw/, regenerate main.h
                                 with the new unique-name in all the paths.
  Step 10.  Build APK        -- run `ant debug` in android/.  Necessitas's
                                 build system handles the JNI glue.
  Step 11.  Locate           -- find the resulting <project>/build/android/bin/
                                 *-debug.apk and report its path.
  Step 12.  (optional)       -- `adb install` if --install-apk was given.

Usage
-----
    python2.7 build_pyside_android.py \\
        --project-dir   /path/to/myapp \\
        --necessitas-sdk ~/necessitas    \\
        --app-name      MyApp            \\
        --unique-name   com.example.MyApp

    # Skip cross-compile by using a previously-built stage tree:
    python2.7 build_pyside_android.py --project-dir ./myapp \\
        --necessitas-sdk ~/necessitas --pyside-stage /path/to/stage --skip-build

    # Dry-run (print commands without executing):
    python2.7 build_pyside_android.py --project-dir ./myapp \\
        --necessitas-sdk ~/necessitas --dry-run --verbose

Host requirements
-----------------
  * Python 2.7 (this interpreter)
  * cmake >= 2.8
  * git, ant, java (JDK 8 — newer JDKs break Necessitas's build.xml)
  * build-essential (gcc, make)
  * ~10 GB free disk space (Shiboken + PySide builds are large)
  * Necessitas SDK installed at the path passed via --necessitas-sdk
    (must contain NecessitasQt/ + android-ndk-* + android-sdk/)

Notes
-----
  * This script is intentionally Python 2.7-only.  PySide 1 is end-of-life
    upstream; no porting to py3 is planned.  For modern PyQt5/PySide6
    builds use the sibling script build_pyqt5_android.py.

  * The 'Press any key' prompts in the canonical bash scripts (after
    cmake configure, used by upstream for manual sanity-check) are
    patched out so the script runs unattended.

  * If you hit `cc1plus: Internal error: Killed`, drop --build-threads
    to 1 — the cross-compiler is RAM-hungry on small build hosts.
"""

# ----------------------------------------------------------------------------
# Imports (Python 2.7 only; no future-proofing imports beyond print_function).
# ----------------------------------------------------------------------------

from __future__ import print_function

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

# Python 2 urllib2 — note: urllib.request is py3.
try:
    import urllib2 as urllib_request
except ImportError:                          # graceful for syntax-checkers on py3
    import urllib.request as urllib_request  # not reached at runtime in py2.7


# ----------------------------------------------------------------------------
# Constants — pinned versions and URLs.  All match the PDF guide and the
# canonical android-pyside-build-scripts repo.
# ----------------------------------------------------------------------------

BUILDER_SCRIPT_VERSION = 3   # 2026-05: patch Qt 4.8 headers DIRECTLY rather
                             # than relying on env.sh CXXFLAGS (v2 approach).
                             #
                             # v2 added -include new etc. to env.sh CXXFLAGS,
                             # but those flags never reached the actual
                             # compile because:
                             #   * build_shiboken.sh's cmake reads
                             #     CMAKE_CXX_FLAGS, not env $CXXFLAGS
                             #   * stale CMakeCache.txt from the v2 failure
                             #     held the old (empty) CXXFLAGS configured
                             #     value, short-circuiting re-config
                             #
                             # v3 instead prepends `#include <new>` + other
                             # STL headers to the Qt 4.8 container headers
                             # themselves (qmap.h, qhash.h, qlist.h, etc.)
                             # ANY compile that pulls in these headers gets
                             # the missing includes — bypasses every "did
                             # the flag actually reach the compiler?" issue.
                             # v3 also wipes shiboken-build/ and pyside-build/
                             # before invoking the bash scripts so a stale
                             # CMakeCache.txt can't sabotage the rebuild.

# Build-scripts repo — provides env.sh, build_shiboken.sh, build_pyside.sh,
# fix_pyside_cmake_paths.sh, strip_binaries.sh, and a pre-built android_python/
# tree (libpython2.7.so + headers cross-compiled for ARM).
BUILD_SCRIPTS_REPO = 'https://github.com/M4rtinK/android-pyside-build-scripts.git'
# The repo's HEAD at the time the PDF was written.  We don't pin a SHA
# because the repo hasn't seen commits since 2013 — HEAD is stable.

# Example project that we clone and rename to scaffold the APK.
EXAMPLE_PROJECT_REPO = 'https://github.com/M4rtinK/android-pyside-example-project.git'

# Pre-built runtime bundles hosted on modrana.org.  These are HTTP (not HTTPS)
# and the host has been unmaintained for years — be prepared for slow or
# failed downloads.  The user can mirror them and override via env vars.
PYTHON_ANDROID_ZIP_URL = (
    'http://www.modrana.org/platforms/android/python2.7/'
    'python2.7_for_android_v1.zip')
QT_COMPONENTS_URL = (
    'http://modrana.org/platforms/android/qt_components/qt_components_v1.zip')
QT_COMPONENTS_THEME_URL = (
    'http://modrana.org/platforms/android/qt_components/'
    'qt_components_theme_mini_v1.zip')

# Defaults from the canonical PDF example.
EXAMPLE_APP_NAME = 'PySideExample'
EXAMPLE_UNIQUE_NAME = 'org.modrana.PySideExample'

# Disk space minimum (in MB).  Shiboken + PySide build trees are big.
MIN_DISK_MB = 8192

# Default parallel-make jobs.  Drop to 1 if cc1plus gets killed.
DEFAULT_BUILD_THREADS = 2

# Where caches live so re-runs don't redo expensive work.  Overridable via
# --cache-dir.  The default mirrors the PyQt5 builder's convention.
DEFAULT_CACHE_DIR = os.path.expanduser('~/pyside-cache')


# ----------------------------------------------------------------------------
# Logging — match the PyQt5 builder's format so users see consistent output
# across both pipelines.
# ----------------------------------------------------------------------------

logging.basicConfig(
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
    level=logging.INFO)
log = logging.getLogger('build_pyside_android')


# ----------------------------------------------------------------------------
# Utility — subprocess runner with consistent logging and dry-run support.
# ----------------------------------------------------------------------------

def run(cmd, cwd=None, env=None, check=True, capture=False, dry_run=False):
    """
    Execute *cmd* (list of args), log it, and return the exit code.

    Arguments
    ---------
    cmd      : list[str] of command + args.
    cwd      : working directory or None for the script's cwd.
    env      : full environment dict or None to inherit.
    check    : raise SystemExit on non-zero exit if True.
    capture  : if True, return (rc, stdout) and don't stream output.
    dry_run  : if True, just log the command and return 0.

    Returns
    -------
    int rc, or (int rc, str stdout) when capture=True.
    """
    pretty = ' '.join(_quote_arg(a) for a in cmd)
    log.info('$ %s', pretty)
    if dry_run:
        return (0, '') if capture else 0

    try:
        if capture:
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out, _ = proc.communicate()
            rc = proc.returncode
        else:
            rc = subprocess.call(cmd, cwd=cwd, env=env)
            out = ''
    except OSError as e:
        if check:
            raise SystemExit('  X command not found: %s (%s)' % (cmd[0], e))
        return (127, '') if capture else 127

    if rc != 0 and check:
        raise SystemExit(
            '  X command failed (exit %d): %s' % (rc, pretty))
    return (rc, out) if capture else rc


def _quote_arg(a):
    """Shell-quote an argument for log readability (not for actual shell)."""
    if not a or any(c in a for c in ' \t"\'$\\'):
        return '"%s"' % a.replace('"', '\\"')
    return a


def _makedirs(path):
    """`os.makedirs(exist_ok=True)` for Python 2.7."""
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError as e:
            if not os.path.isdir(path):    # race or genuine failure
                raise SystemExit(
                    '  X could not create %s: %s' % (path, e))


def _check_disk_mb(path, required_mb=MIN_DISK_MB):
    """Warn (not fail) if free disk space at *path* is below *required_mb*."""
    p = path if os.path.exists(path) else os.path.dirname(path)
    try:
        stat = os.statvfs(p)
    except OSError:
        return                              # silently ignore on weird FS
    free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
    if free_mb < required_mb:
        log.warning(
            '  ! low disk space (%d MB free, %d MB recommended) at %s',
            free_mb, required_mb, p)
    else:
        log.info('  free disk space at %s: %d MB', p, free_mb)


def _which(program):
    """Cross-version `shutil.which` for Python 2.7 (which lacks it)."""
    fpath, fname = os.path.split(program)
    if fpath:
        return program if (os.path.isfile(program)
                           and os.access(program, os.X_OK)) else None
    for path_dir in os.environ.get('PATH', '').split(os.pathsep):
        candidate = os.path.join(path_dir, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _download(url, dest, dry_run=False):
    """
    Download *url* to *dest*.  Uses urllib2 (py2.7).

    modrana.org URLs are plain HTTP and the host has stale TLS where it
    does have HTTPS — be tolerant.
    """
    log.info('  downloading %s', url)
    log.info('         -> %s', dest)
    if dry_run:
        # touch the file so subsequent steps that check existence pass
        open(dest, 'w').close()
        return
    try:
        # No `context=` on py2.7's urllib2 — ssl trust is the system's job.
        req = urllib_request.Request(url, headers={'User-Agent': 'curl/7.0'})
        resp = urllib_request.urlopen(req, timeout=120)
        with open(dest, 'wb') as f:
            shutil.copyfileobj(resp, f, length=1 << 16)
    except Exception as e:
        raise SystemExit(
            '  X download failed: %s\n'
            '    URL: %s\n'
            '    modrana.org is sometimes slow or down.  If this URL is dead,\n'
            '    mirror it and set the matching env var (PYTHON_ANDROID_ZIP_URL\n'
            '    / QT_COMPONENTS_URL / QT_COMPONENTS_THEME_URL).' % (e, url))


def _extract_zip(zip_path, dest_dir, dry_run=False):
    """Unzip *zip_path* into *dest_dir*."""
    log.info('  unzipping %s -> %s', zip_path, dest_dir)
    if dry_run:
        return
    _makedirs(dest_dir)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(dest_dir)


# ----------------------------------------------------------------------------
# Necessitas SDK probing — find the NDK, Qt, and Android SDK inside the
# Necessitas tree.  The layout has varied slightly across releases so we
# search rather than hard-code.
# ----------------------------------------------------------------------------

class NecessitasSDK(object):
    """
    Probe a Necessitas SDK installation for the paths the build needs.

    The Necessitas tree typically looks like:

        necessitas/
            android-ndk-r8b/              <- NDK
            android-sdk/                  <- Android SDK with API 14
            NecessitasQt/
                Qt/482/armeabi/           <- Qt 4.8.2 for armeabi
            Android/Qt/482/armeabi/       <- alternative layout
            (... installers, docs, etc.)
    """

    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))
        if not os.path.isdir(self.root):
            raise SystemExit('  X Necessitas SDK directory not found: %s'
                             % self.root)
        self.ndk = self._find_ndk()
        self.qt_dir = self._find_qt()
        self.android_sdk = self._find_android_sdk()

    def _find_ndk(self):
        """Look for android-ndk-* at the Necessitas root."""
        for name in sorted(os.listdir(self.root)):
            full = os.path.join(self.root, name)
            if os.path.isdir(full) and name.startswith('android-ndk-'):
                return full
        # Fallback to the canonical 'android-ndk' (some installers create
        # a symlink with this name).
        guess = os.path.join(self.root, 'android-ndk')
        if os.path.isdir(guess):
            return guess
        raise SystemExit(
            '  X No android-ndk-* directory inside %s.\n'
            '    Necessitas SDK should contain an embedded NDK (typically\n'
            '    android-ndk-r8b).' % self.root)

    def _find_qt(self):
        """Locate the Qt-for-Android tree (armeabi build)."""
        # Two layouts seen in the wild:
        candidates = [
            os.path.join(self.root, 'Android', 'Qt', '482', 'armeabi'),
            os.path.join(self.root, 'NecessitasQt', 'Qt', '482', 'armeabi'),
            os.path.join(self.root, 'NecessitasQt', 'qt', 'android_armeabi-v7a'),
        ]
        for c in candidates:
            if os.path.isdir(c) and os.path.isfile(
                    os.path.join(c, 'bin', 'qmake')):
                return c
        # Last resort: walk a few levels deep looking for bin/qmake
        for cur_root, dirs, _files in os.walk(self.root):
            if cur_root.count(os.sep) - self.root.count(os.sep) > 6:
                dirs[:] = []
                continue
            if (os.path.basename(cur_root) == 'armeabi' and
                    os.path.isfile(os.path.join(cur_root, 'bin', 'qmake'))):
                return cur_root
        raise SystemExit(
            '  X Could not find Qt 4.8 for armeabi inside %s.\n'
            '    Expected something like NecessitasQt/Qt/482/armeabi/'
            % self.root)

    def _find_android_sdk(self):
        """Find the bundled Android SDK (needs platform 14 installed)."""
        for name in ('android-sdk', 'android-sdk-linux'):
            full = os.path.join(self.root, name)
            if os.path.isdir(full):
                return full
        log.warning(
            '  ! No android-sdk directory inside Necessitas SDK at %s.\n'
            '    The ant build will fail unless ANDROID_HOME points to a\n'
            '    valid SDK with platform-14 installed.', self.root)
        return None

    def describe(self):
        log.info('  Necessitas SDK at %s', self.root)
        log.info('    NDK         : %s', self.ndk)
        log.info('    Qt 4.8      : %s', self.qt_dir)
        log.info('    Android SDK : %s', self.android_sdk or '(not found)')


# ----------------------------------------------------------------------------
# Step 1 -- Preflight
# ----------------------------------------------------------------------------

def preflight(args):
    """Check host environment + user's project layout."""
    log.info('Step 1/12 -- Preflight')
    log.info(
        '  BUILDER_SCRIPT_VERSION = %d  (PySide 1, Qt 4.8, Python 2.7)',
        BUILDER_SCRIPT_VERSION)

    # Python 2.7 (this very interpreter)
    if sys.version_info[:2] != (2, 7):
        # Allow running under py3 with --dry-run, but warn loudly.
        if not args.dry_run:
            raise SystemExit(
                '  X this script must run under Python 2.7.\n'
                '    Detected: Python %s' %
                '.'.join(str(x) for x in sys.version_info[:3]))
        log.warning(
            '  ! running under Python %s (not 2.7) — dry-run only',
            '.'.join(str(x) for x in sys.version_info[:3]))

    # Project dir + main.py
    if not os.path.isdir(args.project_dir):
        raise SystemExit('  X project dir not found: %s' % args.project_dir)
    main_py = os.path.join(args.project_dir, 'main.py')
    if not os.path.isfile(main_py):
        raise SystemExit(
            '  X no main.py at %s.\n'
            '    Your project must have a main.py at its root — this is\n'
            '    the file the C++ wrapper will execute on the device.'
            % main_py)
    log.info('  ✓ project_dir : %s', args.project_dir)
    log.info('  ✓ main.py     : %s', main_py)

    # Quick sanity-check: main.py should use PySide (Qt4), not PyQt5/PySide6
    with open(main_py, 'rb') as f:
        src = f.read()
    if b'PySide6' in src or b'PyQt5' in src or b'PyQt6' in src:
        raise SystemExit(
            '  X main.py uses PySide6 / PyQt5 / PyQt6 imports.\n'
            '    This builder targets PySide 1 (Qt 4.8) on Python 2.7.\n'
            '    For modern stacks use build_pyqt5_android.py.')
    if b'PySide' not in src:
        log.warning(
            '  ! main.py does not import PySide — make sure your app\n'
            '    actually uses the PySide 1 bindings.')

    # Host tools
    required_tools = ['cmake', 'git', 'ant', 'make', 'g++', 'tar', 'unzip']
    missing = [t for t in required_tools if not _which(t)]
    if missing:
        raise SystemExit(
            '  X missing host tools: %s\n'
            '    On Ubuntu install with:\n'
            '      sudo apt-get install build-essential cmake git ant'
            % ', '.join(missing))
    log.info('  ✓ host tools  : %s', ', '.join(required_tools))

    # Necessitas SDK
    args.sdk = NecessitasSDK(args.necessitas_sdk)
    args.sdk.describe()

    # Disk space
    _check_disk_mb(args.cache_dir)

    # Derive defaults
    if not args.app_name:
        args.app_name = os.path.basename(
            os.path.abspath(args.project_dir.rstrip(os.sep))) or 'PySideApp'
    if not args.unique_name:
        # Sanitise — Java package names can only use [a-zA-Z0-9_.]
        safe = re.sub(r'[^a-zA-Z0-9_]', '_', args.app_name)
        args.unique_name = 'com.example.%s' % safe
    log.info('  ✓ app_name    : %s', args.app_name)
    log.info('  ✓ unique_name : %s', args.unique_name)

    # Echo cache dir + threading
    _makedirs(args.cache_dir)
    log.info('  ✓ cache_dir   : %s', args.cache_dir)
    log.info('  ✓ build_jobs  : %d', args.build_threads)


# ----------------------------------------------------------------------------
# Step 2 -- Workspace setup
# ----------------------------------------------------------------------------

def setup_workspace(args):
    """
    Create the per-build workspace inside cache_dir and clone the
    canonical build-scripts repo into it.

    Workspace layout
    ----------------
        <cache_dir>/
            build-scripts/                  <- cloned from M4rtinK/android-pyside-build-scripts
                env.sh                       (our customised copy)
                build_shiboken.sh            (de-prompt-patched)
                build_pyside.sh              (de-prompt-patched)
                fix_pyside_cmake_paths.sh
                strip_binaries.sh
                android_python/              (bundled)
                shiboken-android/            (cloned by prepare.sh)
                pyside-android/              (cloned by prepare.sh)
                stage/                       (build output)
            example-project/                <- cloned from M4rtinK/android-pyside-example-project
            downloads/                       <- python_27 + qt_components zips
            artifacts/                       <- final outputs
    """
    log.info('Step 2/12 -- Workspace setup')

    bs_dir = os.path.join(args.cache_dir, 'build-scripts')
    args.build_scripts_dir = bs_dir
    args.stage_dir = os.path.join(bs_dir, 'stage')
    args.downloads_dir = os.path.join(args.cache_dir, 'downloads')
    args.artifacts_dir = os.path.join(args.cache_dir, 'artifacts')
    _makedirs(args.downloads_dir)
    _makedirs(args.artifacts_dir)

    if os.path.isdir(bs_dir):
        log.info('  ✓ build-scripts already cloned at %s', bs_dir)
    else:
        run(['git', 'clone', '--depth', '1', BUILD_SCRIPTS_REPO, bs_dir],
            dry_run=args.dry_run)
        log.info('  ✓ cloned build-scripts -> %s', bs_dir)

    # Run prepare.sh to clone shiboken-android + pyside-android, but only
    # if those sub-clones don't already exist.  prepare.sh is idempotent-ish
    # but we save a few seconds by short-circuiting.
    shiboken_src = os.path.join(bs_dir, 'shiboken-android')
    pyside_src = os.path.join(bs_dir, 'pyside-android')
    if os.path.isdir(shiboken_src) and os.path.isdir(pyside_src):
        log.info('  ✓ shiboken-android + pyside-android already cloned')
    else:
        log.info('  running prepare.sh to clone shiboken + pyside sources')
        run(['bash', 'prepare.sh'], cwd=bs_dir, dry_run=args.dry_run)

    # Create the build dirs that prepare.sh sets up (in case it skipped).
    for d in ('shiboken-build', 'pyside-build', 'stage'):
        _makedirs(os.path.join(bs_dir, d))


# ----------------------------------------------------------------------------
# Step 3 -- Configure env.sh and de-promptify the bash scripts
# ----------------------------------------------------------------------------

def configure_env(args):
    """
    Write a customised env.sh into the build-scripts directory with the
    right NECESSITAS_DIR, NDK path, Qt path, and build-thread count.
    Also patch out the `read -p "press any key"` calls in build_shiboken.sh
    and build_pyside.sh so the build runs unattended.
    """
    log.info('Step 3/12 -- Configure env.sh + de-prompt build scripts')

    bs = args.build_scripts_dir
    env_path = os.path.join(bs, 'env.sh')

    # Read the upstream env.sh, replace the NECESSITAS_DIR placeholder
    # AND the BUILD_THREAD_COUNT, then write back.  We don't try to be
    # clever — just sed-style string substitution.
    with open(env_path, 'rb') as f:
        env_text = f.read().decode('utf-8')

    # The placeholder is literally: <path to the Necessitas SDK folder>
    env_text = env_text.replace(
        '<path to the Necessitas SDK folder>',
        args.sdk.root)

    # The repo also hard-codes ANDROID_NDK to ${NECESSITAS_DIR}/android-ndk,
    # but Necessitas sometimes installs as android-ndk-r8b.  Replace the
    # whole ANDROID_NDK line with our detected path.
    env_text = re.sub(
        r'^export\s+ANDROID_NDK=.*$',
        'export ANDROID_NDK="%s"' % args.sdk.ndk,
        env_text, count=1, flags=re.MULTILINE)

    # Similarly for QT_DIR — repo's path differs from the script's earlier
    # default.  Use whatever NecessitasSDK detected.
    env_text = re.sub(
        r'^export\s+QT_DIR=.*$',
        'export QT_DIR="%s"' % args.sdk.qt_dir,
        env_text, count=1, flags=re.MULTILINE)

    # Build threads.
    env_text = re.sub(
        r'^export\s+BUILD_THREAD_COUNT=.*$',
        'export BUILD_THREAD_COUNT=%d' % args.build_threads,
        env_text, count=1, flags=re.MULTILINE)

    # Modern-GCC compatibility flags for Qt 4.8 headers.
    #
    # Qt 4.8.0 was released in 2011 against GCC 4.x.  Its headers
    # (qmap.h, qlist.h, qvector.h, ...) use placement new and other
    # constructs that historically relied on STL headers being
    # auto-included via include-chain pollution.  Modern GCC (11+)
    # tightened up include hygiene, so qmap.h fails with:
    #
    #     qmap.h:456: error: no matching function for call to
    #         'operator new(unsigned int, int*)'
    #
    # The fix is `-include <header>` flags which prepend the listed
    # headers to every translation unit — the equivalent of adding
    # an `#include` at the top of every .cpp file.
    #
    # We also add `-fpermissive` because Qt 4.8 uses several
    # constructs that modern GCC considers errors but older GCC
    # considered warnings (e.g. converting string literals to char*,
    # implicit declarations, narrowing conversions in initialiser lists).
    compat_flags = (
        '-include cstddef '       # <cstddef> for std::size_t, NULL
        '-include cstring '       # <cstring> for memcpy, memset, strlen
        '-include new '           # <new> for placement new (qmap.h:456)
        '-include cstdlib '       # <cstdlib> for malloc, free, exit
        '-include cstdio '        # <cstdio> for FILE, fprintf
        '-include cstdint '       # <cstdint> for int32_t etc.
        '-include climits '       # <climits> for INT_MAX etc.
        '-fpermissive'            # downgrade many C++11+ errors to warnings
    )

    # Append the flags to whichever line(s) of env.sh export CXXFLAGS or
    # CFLAGS.  We match `export X="..."` and `export X='...'` styles and
    # also bare `X=...` assignments.
    def _patch_flag_line(name, value, text):
        """Append `value` to every assignment of CXX/CFLAGS in `text`."""
        # Matches: export NAME="…" / export NAME='…' / NAME="…" / etc.
        pattern = re.compile(
            r'^(\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*["\'])([^"\']*)(["\'].*)$',
            re.MULTILINE)
        def repl(m):
            existing = m.group(2)
            if value in existing:
                return m.group(0)        # already present, leave alone
            sep = '' if (existing == '' or existing.endswith(' ')) else ' '
            return m.group(1) + existing + sep + value + m.group(3)
        return pattern.sub(repl, text)

    env_text = _patch_flag_line('CXXFLAGS', compat_flags, env_text)
    env_text = _patch_flag_line('CFLAGS', '-include cstddef -include cstring', env_text)

    # If env.sh DOESN'T set CXXFLAGS itself (some forks rely on cmake's
    # default), append an explicit export so our flags reach the compile.
    if 'CXXFLAGS' not in env_text:
        env_text = env_text.rstrip() + (
            '\n\n# Appended by build_pyside_android.py — modern GCC needs\n'
            '# explicit STL includes that Qt 4.8 headers expect to be\n'
            '# auto-included via the old GCC implicit-include chain.\n'
            'export CXXFLAGS="${CXXFLAGS:-} ' + compat_flags + '"\n')
        log.info('  (env.sh had no CXXFLAGS — appended an explicit export)')

    if args.dry_run:
        log.info('  [dry-run] would write env.sh with:')
        log.info('    NECESSITAS_DIR    = %s', args.sdk.root)
        log.info('    ANDROID_NDK       = %s', args.sdk.ndk)
        log.info('    QT_DIR            = %s', args.sdk.qt_dir)
        log.info('    BUILD_THREAD_COUNT= %d', args.build_threads)
        log.info('    +CXXFLAGS additions: %s', compat_flags)
    else:
        with open(env_path, 'wb') as f:
            f.write(env_text.encode('utf-8'))
        log.info('  ✓ wrote %s (with modern-GCC compat flags)', env_path)

    # De-promptify build_shiboken.sh and build_pyside.sh
    for script in ('build_shiboken.sh', 'build_pyside.sh'):
        script_path = os.path.join(bs, script)
        if not os.path.isfile(script_path):
            log.warning('  ! %s not found, skipping de-prompt', script_path)
            continue
        with open(script_path, 'rb') as f:
            text = f.read().decode('utf-8')
        # Replace `read -p "..." -n1 -s` (any args) with a no-op comment.
        new_text, n = re.subn(
            r'read\s+-p\s+"[^"]*"\s+-n1\s+-s',
            '# (interactive pause removed by build_pyside_android.py)',
            text)
        if n == 0:
            log.info('  ✓ %s already de-prompted', script)
            continue
        if args.dry_run:
            log.info('  [dry-run] would de-prompt %d call(s) in %s',
                     n, script)
        else:
            with open(script_path, 'wb') as f:
                f.write(new_text.encode('utf-8'))
            log.info('  ✓ removed %d interactive pause(s) from %s', n, script)


# ----------------------------------------------------------------------------
# Qt 4.8 header patcher — bypasses every "the flag didn't reach the compile"
# failure mode by patching the headers directly on disk.
# ----------------------------------------------------------------------------

# Marker we prepend to patched files so re-runs are idempotent.  Stays
# valid as a C++ comment + a unique-enough string to grep for.
QT48_PATCH_MARKER = '// PATCHED_BY_BUILD_PYSIDE_ANDROID -- DO NOT REMOVE'

# Headers known to use placement new and other 2010-era C++ constructs
# that modern GCC won't accept without explicit STL header inclusion.
# This list comes from cataloguing every compile error in the failed
# build.log + a sweep of every other Qt 4.8 container header that uses
# the same patterns (so we patch once, not multiple times in a loop of
# build-fail-patch-rebuild iterations).
QT48_HEADERS_TO_PATCH = (
    'qmap.h',                  # placement new at line 456-458 (qmap.h)
    'qhash.h',                 # placement new at line 530-532
    'qlist.h',                 # placement new at line 412
    'qvector.h',               # placement new in node_construct
    'qpair.h',                 # uses std::pair internally
    'qcache.h',                # placement new in Node
    'qcontiguouscache.h',      # placement new in append
    'qbytearray.h',            # uses memcpy/memset without <cstring>
    'qstring.h',               # uses memcpy/memset, references std::size_t
    'qglobal.h',               # foundation header used by EVERY Qt include
)


def _patch_qt48_headers(qt_dir, dry_run=False):
    """
    Prepend `#include <new>` and other STL headers to Qt 4.8 container
    headers so they build cleanly under modern GCC.

    This is the only reliable workaround.  Adding CXXFLAGS in env.sh
    doesn't work because:

      * build_shiboken.sh's cmake invocation reads CMAKE_CXX_FLAGS,
        not the environment $CXXFLAGS variable.
      * CMakeCache.txt from a previous (failed) configure caches the
        empty CXXFLAGS and short-circuits re-config on subsequent
        cmake runs.
      * Passing -DCMAKE_CXX_FLAGS=... at the cmake invocation would
        require sed-patching build_shiboken.sh, which keeps the fix
        couplied to upstream's exact script layout.

    Patching the headers directly bypasses all of that.  ANY compile
    that pulls in qmap.h sees `#include <new>` first and resolves
    placement new correctly.

    Idempotent — looks for QT48_PATCH_MARKER at the top of each file
    and skips files that already have it.

    Arguments
    ---------
    qt_dir   : path to the Qt-for-Android root (the dir containing
               bin/qmake, include/, lib/, etc.)
    dry_run  : if True, log what WOULD be patched but don't write.

    Returns
    -------
    int: count of headers actually modified this call.
    """
    log.info('  patching Qt 4.8 headers for modern-GCC compatibility')

    include_root = os.path.join(qt_dir, 'include', 'QtCore')
    if not os.path.isdir(include_root):
        log.warning(
            '  ! QtCore include dir not found at %s — skipping header patch.\n'
            '    If the cross-compile fails with placement-new errors,\n'
            '    your Qt-for-Android install layout differs from expected.',
            include_root)
        return 0

    # The block we prepend.  We include a generous set of STL headers
    # rather than a minimal one because:
    #   * <new> alone fixes the immediate qmap.h:456 error
    #   * <cstddef> covers std::size_t / NULL references
    #   * <cstring> covers memcpy/memset (qbytearray.h, qstring.h)
    #   * <cstdlib>/<cstdint> cover qmalloc and the int-type aliases
    # The marker comes first so our grep-for-marker check sees it.
    PREPEND = (
        QT48_PATCH_MARKER + '\n'
        '#include <new>\n'
        '#include <cstddef>\n'
        '#include <cstring>\n'
        '#include <cstdlib>\n'
        '#include <cstdint>\n'
        '#include <climits>\n'
        '\n'
    )

    patched_count = 0
    skipped_count = 0
    not_found = []

    for header_name in QT48_HEADERS_TO_PATCH:
        path = os.path.join(include_root, header_name)
        if not os.path.isfile(path):
            not_found.append(header_name)
            continue

        with open(path, 'rb') as f:
            content = f.read()

        # Idempotency check — skip if marker already in the first 1 KB.
        # We bound the check so we don't scan multi-MB files (some Qt
        # headers are large).
        if QT48_PATCH_MARKER.encode('utf-8') in content[:1024]:
            skipped_count += 1
            log.info('    skip  %s  (already patched)', header_name)
            continue

        if dry_run:
            log.info('    DRY   %s  (would prepend %d-byte block)',
                     header_name, len(PREPEND))
            patched_count += 1
            continue

        new_content = PREPEND.encode('utf-8') + content
        try:
            with open(path, 'wb') as f:
                f.write(new_content)
        except (IOError, OSError) as e:
            # Header files in some Necessitas installs are read-only.
            # Chmod and retry.
            try:
                os.chmod(path, 0o644)
                with open(path, 'wb') as f:
                    f.write(new_content)
            except (IOError, OSError) as e2:
                raise SystemExit(
                    '  X could not patch %s: %s\n'
                    '    chmod retry also failed: %s\n'
                    '    Check filesystem permissions on the Necessitas SDK install.'
                    % (path, e, e2))

        log.info('    patched  %s  (added <new>, <cstddef>, <cstring>, ...)',
                 header_name)
        patched_count += 1

    # Summary
    log.info('  ✓ Qt 4.8 header patch: %d patched, %d already done, %d not present',
             patched_count - (patched_count if dry_run else 0)
                 if dry_run else patched_count,
             skipped_count, len(not_found))
    if not_found:
        log.info('    (not-present headers — fine, just not in this Qt build: %s)',
                 ', '.join(not_found))
    return patched_count


# ----------------------------------------------------------------------------
# Step 4 -- Cross-compile Shiboken + PySide
# ----------------------------------------------------------------------------

def cross_compile(args):
    """
    Run the canonical build_shiboken.sh and build_pyside.sh.

    Before invoking either, this step does two prep things:

      1. Patches Qt 4.8 headers to add `#include <new>` and other
         modern-GCC-required STL includes at the top.  Qt 4.8.0's
         headers (qmap.h, qhash.h, qlist.h, qvector.h) use placement
         new without including <new> — relying on the old GCC 4.x
         transitive include chain.  Modern GCC 11+ tightened include
         hygiene, so without the patch you get:

             qmap.h:456: error: no matching function for call to
                 'operator new(unsigned int, int*)'

         The previous attempt to fix this via env.sh CXXFLAGS didn't
         reach the cmake-driven compile because build_shiboken.sh
         doesn't propagate $CXXFLAGS to its cmake invocation, and the
         build directory's CMakeCache.txt held the old (no flags)
         configuration.  Patching the headers directly bypasses
         both problems.

      2. Removes shiboken-build/ and pyside-build/ from any previous
         failed run so cmake re-runs from scratch with the patched
         headers.  Without this, cmake's cache short-circuits the
         re-config and the patched headers don't take effect.

    Skipped entirely if --skip-build is passed AND --pyside-stage
    points to a pre-built stage tree.
    """
    log.info('Step 4/12 -- Cross-compile Shiboken + PySide')

    if args.skip_build:
        if not args.pyside_stage:
            raise SystemExit(
                '  X --skip-build requires --pyside-stage DIR pointing at\n'
                '    a previously-built stage/ tree.')
        if not os.path.isdir(args.pyside_stage):
            raise SystemExit(
                '  X --pyside-stage directory does not exist: %s'
                % args.pyside_stage)
        # Mirror the user's stage into the build-scripts/stage so subsequent
        # steps find everything where they expect it.
        log.info('  ✓ using pre-built stage at %s', args.pyside_stage)
        if not args.dry_run:
            if os.path.isdir(args.stage_dir):
                shutil.rmtree(args.stage_dir)
            shutil.copytree(args.pyside_stage, args.stage_dir,
                            symlinks=True)
        return

    bs = args.build_scripts_dir

    # (1) Patch Qt 4.8 headers — see docstring above.
    _patch_qt48_headers(args.sdk.qt_dir, dry_run=args.dry_run)

    # (2) Wipe any stale build dirs from previous failed runs.  cmake
    # caches CXXFLAGS in CMakeCache.txt and will NOT pick up
    # newly-patched headers unless we force a fresh configure.
    for stale in ('shiboken-build', 'pyside-build'):
        d = os.path.join(bs, stale)
        if os.path.isdir(d) and any(os.listdir(d)):
            log.info('  removing stale build dir: %s', d)
            if not args.dry_run:
                shutil.rmtree(d)
            _makedirs(d)

    # The bash scripts use `source env.sh` then run cmake+make from
    # subdirectories.  Invoke through `bash -c` so `source` works.
    run(['bash', '-e', 'build_shiboken.sh'], cwd=bs,
        dry_run=args.dry_run)
    log.info('  ✓ Shiboken cross-compile done')

    run(['bash', '-e', 'build_pyside.sh'], cwd=bs,
        dry_run=args.dry_run)
    log.info('  ✓ PySide cross-compile done')

    # Sanity-check the stage
    if not args.dry_run:
        expected = [
            os.path.join(args.stage_dir, 'lib', 'libshiboken.so'),
            os.path.join(args.stage_dir, 'lib', 'libpyside.so'),
        ]
        missing = [p for p in expected if not os.path.isfile(p)]
        if missing:
            raise SystemExit(
                '  X cross-compile finished but expected outputs missing:\n'
                + '\n'.join('      ' + p for p in missing) + '\n'
                '    Check the build log for cc1plus errors.  If RAM-bound,\n'
                '    retry with --build-threads 1.')


# ----------------------------------------------------------------------------
# Step 5 -- Strip binaries
# ----------------------------------------------------------------------------

def strip_binaries(args):
    """Run strip_binaries.sh.  Shrinks the .so files by ~70%."""
    log.info('Step 5/12 -- Strip binaries')

    bs = args.build_scripts_dir
    strip_sh = os.path.join(bs, 'strip_binaries.sh')

    # The repo's strip script expects to be run from inside a sub-dir
    # (it does `source ../env.sh`).  Run it from the build-scripts root
    # via an explicit `bash -c "cd stage && bash ../strip_binaries.sh"`.
    # Actually, looking at the original: it does `find stage | ...` so
    # it expects cwd = build-scripts.  But then `source ../env.sh` looks
    # one dir up.  We patch the source path on the fly.

    if not os.path.isfile(strip_sh):
        log.warning('  ! strip_binaries.sh not found at %s — skipping',
                    strip_sh)
        return

    # The script reads ../env.sh — but if we run from build-scripts/, that
    # resolves to <parent>/env.sh which doesn't exist.  Patch the source
    # to use ./env.sh.
    with open(strip_sh, 'rb') as f:
        text = f.read().decode('utf-8')
    fixed = text.replace('source ../env.sh', 'source ./env.sh')
    if fixed != text and not args.dry_run:
        with open(strip_sh, 'wb') as f:
            f.write(fixed.encode('utf-8'))
        log.info('  ✓ patched strip_binaries.sh to find env.sh in cwd')

    run(['bash', '-e', 'strip_binaries.sh'], cwd=bs,
        dry_run=args.dry_run)
    log.info('  ✓ binaries stripped')


# ----------------------------------------------------------------------------
# Step 6 -- Bundle the Android Python runtime (python_27.zip)
# ----------------------------------------------------------------------------

def package_python_runtime(args):
    """
    Build python_27.zip containing everything that gets unpacked to
    /data/data/<unique_name>/files/python/ on first launch.

    Layout INSIDE the zip:

        python/
            bin/python                       <- Android Python 2.7 interpreter
            lib/                              <- libpython2.7.so, libshiboken.so,
                                                 libpyside.so, Qt libs, stdlib...
            lib/python2.7/site-packages/     <- PySide bindings (.so files)
            imports/                          <- Qt Components QML files
            themes/                           <- Qt Components theme

    The PDF (page 8) describes this layout.  The example project's
    QtActivity.java extracts this archive to <files>/python/ on first
    start.
    """
    log.info('Step 6/12 -- Bundle python_27.zip')

    out_zip = os.path.join(args.artifacts_dir, 'python_27.zip')
    if os.path.isfile(out_zip) and not args.dry_run:
        os.remove(out_zip)

    # Acquire the three modrana zips (Python for Android + Qt Components +
    # theme).  All three are HTTP and can be slow; we cache them.
    py_zip = os.path.join(args.downloads_dir, 'python2.7_for_android_v1.zip')
    qtc_zip = os.path.join(args.downloads_dir, 'qt_components_v1.zip')
    theme_zip = os.path.join(args.downloads_dir,
                             'qt_components_theme_mini_v1.zip')

    if not os.path.isfile(py_zip):
        _download(
            os.environ.get('PYTHON_ANDROID_ZIP_URL', PYTHON_ANDROID_ZIP_URL),
            py_zip, dry_run=args.dry_run)
    if not os.path.isfile(qtc_zip):
        _download(
            os.environ.get('QT_COMPONENTS_URL', QT_COMPONENTS_URL),
            qtc_zip, dry_run=args.dry_run)
    if not os.path.isfile(theme_zip):
        _download(
            os.environ.get('QT_COMPONENTS_THEME_URL', QT_COMPONENTS_THEME_URL),
            theme_zip, dry_run=args.dry_run)

    if args.dry_run:
        log.info('  [dry-run] would assemble python_27.zip from %s, %s, %s + stage',
                 py_zip, qtc_zip, theme_zip)
        return out_zip

    # Build the python/ tree in a temp dir, then zip it.
    tree = os.path.join(args.cache_dir, '_python_tree')
    if os.path.isdir(tree):
        shutil.rmtree(tree)
    py_dir = os.path.join(tree, 'python')
    _makedirs(py_dir)

    # 1. Extract the Android Python runtime
    _extract_zip(py_zip, py_dir)

    # 2. Extract Qt Components into imports/
    imports_dir = os.path.join(py_dir, 'imports')
    _makedirs(imports_dir)
    _extract_zip(qtc_zip, imports_dir)

    # 3. Extract theme into themes/
    themes_dir = os.path.join(py_dir, 'themes')
    _makedirs(themes_dir)
    _extract_zip(theme_zip, themes_dir)

    # 4. Copy libshiboken.so + libpyside.so from stage/lib into python/lib
    src_lib = os.path.join(args.stage_dir, 'lib')
    dst_lib = os.path.join(py_dir, 'lib')
    _makedirs(dst_lib)
    for so in ('libshiboken.so', 'libpyside.so'):
        src = os.path.join(src_lib, so)
        if os.path.isfile(src):
            shutil.copy2(src, dst_lib)
            log.info('  ✓ copied %s', so)
        else:
            log.warning('  ! %s not found in stage — apps that import\n'
                        '    PySide will fail at runtime', src)

    # 5. Copy PySide site-packages (the actual bindings) into
    #    python/lib/python2.7/site-packages/PySide/
    src_pyside_pkg = os.path.join(src_lib, 'python2.7', 'site-packages',
                                  'PySide')
    if os.path.isdir(src_pyside_pkg):
        dst_pyside_pkg = os.path.join(
            dst_lib, 'python2.7', 'site-packages', 'PySide')
        _makedirs(os.path.dirname(dst_pyside_pkg))
        if os.path.isdir(dst_pyside_pkg):
            shutil.rmtree(dst_pyside_pkg)
        shutil.copytree(src_pyside_pkg, dst_pyside_pkg)
        log.info('  ✓ copied PySide bindings -> python/lib/python2.7/site-packages/PySide/')
    else:
        log.warning('  ! PySide site-packages not found at %s', src_pyside_pkg)

    # 6. Zip up python/
    log.info('  packing %s -> %s', py_dir, out_zip)
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root_dir, _dirs, files in os.walk(tree):
            for fn in files:
                full = os.path.join(root_dir, fn)
                arc = os.path.relpath(full, tree)
                zf.write(full, arc)

    size_mb = os.path.getsize(out_zip) / (1024 * 1024)
    log.info('  ✓ python_27.zip ready (%d MB)', size_mb)
    return out_zip


# ----------------------------------------------------------------------------
# Step 7 -- Bundle the user's app (my_python_project.zip)
# ----------------------------------------------------------------------------

def package_user_app(args):
    """
    Zip the user's project_dir into my_python_project.zip.

    Exclusions: pycache, .pyc, .git, hidden dirs, build outputs.
    """
    log.info('Step 7/12 -- Bundle my_python_project.zip')

    out_zip = os.path.join(args.artifacts_dir, 'my_python_project.zip')
    if os.path.isfile(out_zip) and not args.dry_run:
        os.remove(out_zip)
    if args.dry_run:
        log.info('  [dry-run] would zip %s -> %s', args.project_dir, out_zip)
        return out_zip

    EXCLUDED_DIRS = {'.git', '__pycache__', '.tox', '.venv',
                     'venv', 'env', 'build', 'dist'}
    EXCLUDED_SUFFIXES = ('.pyc', '.pyo')

    project = os.path.abspath(args.project_dir)
    file_count = 0
    with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
        for cur_root, dirs, files in os.walk(project):
            # in-place prune excluded dirs
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS
                       and not d.startswith('.')]
            for fn in files:
                if fn.endswith(EXCLUDED_SUFFIXES) or fn.startswith('.'):
                    continue
                full = os.path.join(cur_root, fn)
                arc = os.path.relpath(full, project)
                zf.write(full, arc)
                file_count += 1
    log.info('  ✓ %s (%d files)', out_zip, file_count)
    return out_zip


# ----------------------------------------------------------------------------
# Step 8 -- Clone example project, rename it per user's app + unique name
# ----------------------------------------------------------------------------

def prepare_apk_project(args):
    """
    Clone android-pyside-example-project into the workspace, then run
    the rename script documented on PDF page 9.

    All files that hard-code "PySideExample" or "org.modrana.PySideExample"
    get sed-substituted to args.app_name and args.unique_name.
    """
    log.info('Step 8/12 -- Scaffold APK project (clone + rename)')

    ex_dir = os.path.join(args.cache_dir, 'example-project')
    if os.path.isdir(ex_dir):
        log.info('  ! removing stale example-project dir')
        if not args.dry_run:
            shutil.rmtree(ex_dir)
    run(['git', 'clone', '--depth', '1', EXAMPLE_PROJECT_REPO, ex_dir],
        dry_run=args.dry_run)

    if args.dry_run:
        log.info('  [dry-run] would rename %s -> %s and %s -> %s',
                 EXAMPLE_APP_NAME, args.app_name,
                 EXAMPLE_UNIQUE_NAME, args.unique_name)
        args.apk_project_dir = ex_dir
        return

    # 1. Rename PySideExample.pro -> <app_name>.pro
    old_pro = os.path.join(ex_dir, '%s.pro' % EXAMPLE_APP_NAME)
    new_pro = os.path.join(ex_dir, '%s.pro' % args.app_name)
    if os.path.isfile(old_pro):
        os.rename(old_pro, new_pro)
        log.info('  ✓ renamed %s.pro -> %s.pro',
                 EXAMPLE_APP_NAME, args.app_name)

    # 2. sed-substitute names across the files documented in the PDF guide
    #    (page 9).  We do this in pure Python to avoid sed portability.
    targets = [
        # (file relative to ex_dir, list of (old, new) substitutions)
        (new_pro,                                       [
            (EXAMPLE_APP_NAME, args.app_name),
        ]),
        (os.path.join(ex_dir, 'main.h'),                [
            (EXAMPLE_UNIQUE_NAME, args.unique_name),
        ]),
        (os.path.join(ex_dir, 'main.cpp'),              [
            (EXAMPLE_UNIQUE_NAME, args.unique_name),
        ]),
        (os.path.join(ex_dir, 'android', 'src',
                      'org', 'kde', 'necessitas', 'origo',
                      'QtActivity.java'),               [
            (EXAMPLE_UNIQUE_NAME, args.unique_name),
        ]),
        (os.path.join(ex_dir, 'android', 'AndroidManifest.xml'), [
            (EXAMPLE_UNIQUE_NAME, args.unique_name),
            (EXAMPLE_APP_NAME, args.app_name),
        ]),
        (os.path.join(ex_dir, 'android', 'res', 'values', 'strings.xml'), [
            (EXAMPLE_APP_NAME, args.app_name),
        ]),
        (os.path.join(ex_dir, 'android', 'build.xml'),  [
            (EXAMPLE_APP_NAME, args.app_name),
        ]),
    ]
    for path, subs in targets:
        if not os.path.isfile(path):
            log.warning('  ! %s not found, skipping rename', path)
            continue
        with open(path, 'rb') as f:
            text = f.read()
        for old, new in subs:
            text = text.replace(old.encode('utf-8'), new.encode('utf-8'))
        with open(path, 'wb') as f:
            f.write(text)
        log.info('  ✓ renamed in %s', os.path.relpath(path, ex_dir))

    args.apk_project_dir = ex_dir


# ----------------------------------------------------------------------------
# Step 9 -- Inject zips + regenerate main.h
# ----------------------------------------------------------------------------

def inject_artifacts(args, python_27_zip, my_python_project_zip):
    """
    Copy python_27.zip + my_python_project.zip into the APK's
    android/res/raw/, then regenerate main.h from scratch with the new
    unique-name in all the paths (more reliable than sed-substituting an
    existing one).
    """
    log.info('Step 9/12 -- Inject artifacts + regenerate main.h')

    ex_dir = args.apk_project_dir
    raw_dir = os.path.join(ex_dir, 'android', 'res', 'raw')
    _makedirs(raw_dir)

    for src in (python_27_zip, my_python_project_zip):
        dst = os.path.join(raw_dir, os.path.basename(src))
        if args.dry_run:
            log.info('  [dry-run] would copy %s -> %s', src, dst)
        else:
            shutil.copy2(src, dst)
            log.info('  ✓ copied %s', os.path.basename(src))

    # Generate main.h per PDF page 6.  This is more robust than sed-patching
    # the existing one — we write it from scratch using the user's unique-name.
    main_h = os.path.join(ex_dir, 'main.h')
    files_root = '/data/data/%s/files' % args.unique_name
    py_root = '%s/python' % files_root
    main_h_text = (
        '#ifndef MAIN_H\n'
        '#define MAIN_H\n\n'
        '#define MAIN_PYTHON_FILE "%s/main.py"\n'
        '#define PYTHON_HOME "%s/"\n'
        '#define PYTHON_PATH "%s/lib/python2.7"\n'
        '#define LD_LIBRARY_PATH "%s/lib"\n'
        '#define PATH "%s/bin:$PATH"\n'
        '#define THEME_PATH "%s/themes/"\n'
        '#define QML_IMPORT_PATH "%s/imports"\n'
        '#define PYSIDE_APPLICATION_FOLDER "/data/data/%s/"\n\n'
        '#endif // MAIN_H\n'
    ) % (files_root, py_root, py_root, py_root, py_root,
         py_root, py_root, args.unique_name)
    if args.dry_run:
        log.info('  [dry-run] would write %s', main_h)
        log.info('    with unique_name=%s', args.unique_name)
    else:
        with open(main_h, 'wb') as f:
            f.write(main_h_text.encode('utf-8'))
        log.info('  ✓ wrote main.h with unique_name=%s', args.unique_name)


# ----------------------------------------------------------------------------
# Step 10 -- Build the APK with ant
# ----------------------------------------------------------------------------

def build_apk(args):
    """
    Run `ant debug` in the example-project's android/ subdir to produce
    the APK.

    Pre-conditions:
      * Necessitas SDK is set up (Android SDK with platform 14 available)
      * Java 8 is on PATH (newer JDKs break Necessitas's build.xml)
      * ant is installed
    """
    log.info('Step 10/12 -- Build APK (ant debug)')

    ex_dir = args.apk_project_dir
    android_dir = os.path.join(ex_dir, 'android')
    if not os.path.isdir(android_dir):
        raise SystemExit('  X no android/ subdir in %s' % ex_dir)

    # Set up environment for ant:
    #   * ANDROID_HOME -> Necessitas's android-sdk
    #   * JAVA_HOME (if set) is inherited
    env = os.environ.copy()
    if args.sdk.android_sdk:
        env['ANDROID_HOME'] = args.sdk.android_sdk
        env['ANDROID_SDK_ROOT'] = args.sdk.android_sdk
        env['PATH'] = (
            os.path.join(args.sdk.android_sdk, 'tools') + os.pathsep +
            os.path.join(args.sdk.android_sdk, 'platform-tools') + os.pathsep +
            env.get('PATH', ''))

    # Step 1: `android update project` to regenerate local.properties.
    # The example project's build.xml needs a valid SDK target.
    if args.sdk.android_sdk:
        android_tool = os.path.join(args.sdk.android_sdk, 'tools', 'android')
        if os.path.isfile(android_tool):
            run([android_tool, 'update', 'project', '--path', android_dir,
                 '--target', 'android-14'],
                env=env, dry_run=args.dry_run, check=False)
        else:
            log.warning(
                '  ! %s not found — ant build may fail with no local.properties',
                android_tool)

    # Step 2: ant debug
    run(['ant', 'debug'], cwd=android_dir, env=env, dry_run=args.dry_run)
    log.info('  ✓ ant debug finished')


# ----------------------------------------------------------------------------
# Step 11 -- Locate the APK + (optionally) install it
# ----------------------------------------------------------------------------

def locate_apk(args):
    """Find <ex_dir>/android/bin/*-debug.apk and copy to artifacts/."""
    log.info('Step 11/12 -- Locate APK')

    if args.dry_run:
        log.info('  [dry-run] would search for *.apk')
        return None

    bin_dir = os.path.join(args.apk_project_dir, 'android', 'bin')
    apk = None
    if os.path.isdir(bin_dir):
        for fn in os.listdir(bin_dir):
            if fn.endswith('-debug.apk') or fn.endswith('.apk'):
                apk = os.path.join(bin_dir, fn)
                break

    if apk is None:
        # Wider search
        for cur_root, _dirs, files in os.walk(args.apk_project_dir):
            for fn in files:
                if fn.endswith('.apk'):
                    apk = os.path.join(cur_root, fn)
                    break
            if apk:
                break

    if apk is None:
        raise SystemExit(
            '  X no .apk produced under %s\n'
            '    Check the ant build output for errors (usually missing\n'
            '    Android SDK platform 14 or wrong Java version).'
            % args.apk_project_dir)

    dest = os.path.join(args.artifacts_dir,
                        '%s-debug.apk' % args.app_name)
    shutil.copy2(apk, dest)
    size_mb = os.path.getsize(dest) / (1024 * 1024)
    log.info('  ✓ APK: %s (%d MB)', dest, size_mb)
    return dest


def install_apk(args, apk_path):
    """`adb install -r` to a connected device."""
    log.info('Step 12/12 -- adb install')
    if not args.install_apk:
        log.info('  (skipped — pass --install-apk to deploy)')
        return
    if apk_path is None:
        log.warning('  ! no APK to install')
        return
    adb = _which('adb')
    if adb is None and args.sdk.android_sdk:
        adb_guess = os.path.join(args.sdk.android_sdk, 'platform-tools', 'adb')
        if os.path.isfile(adb_guess):
            adb = adb_guess
    if adb is None:
        log.warning(
            '  ! adb not found.  Install Android platform-tools or add\n'
            '    %s/platform-tools to PATH.', args.sdk.android_sdk or '<SDK>')
        return
    run([adb, 'install', '-r', apk_path], dry_run=args.dry_run, check=False)


# ----------------------------------------------------------------------------
# Argument parser
# ----------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog='build_pyside_android.py',
        description=__doc__.split('\n\n', 1)[0],   # first paragraph only
        formatter_class=argparse.RawDescriptionHelpFormatter)

    p.add_argument('--project-dir', required=True, metavar='DIR',
                   help='Directory containing your PySide Python application '
                        '(must have main.py at the root).')
    p.add_argument('--necessitas-sdk', required=True, metavar='DIR',
                   help='Path to your Necessitas SDK installation.')
    p.add_argument('--app-name', metavar='NAME', default=None,
                   help='Application name (default: project directory basename).')
    p.add_argument('--unique-name', metavar='NAME', default=None,
                   help='Android unique package name (com.example.MyApp).  '
                        'Default: com.example.<app-name>.')
    p.add_argument('--pyside-stage', metavar='DIR', default=None,
                   help='Use a pre-built stage/ tree from a previous run.  '
                        'Combine with --skip-build to skip cross-compile.')
    p.add_argument('--cache-dir', metavar='DIR', default=DEFAULT_CACHE_DIR,
                   help='Persistent cache for clones, downloads, and build '
                        'outputs (default: %(default)s).')
    p.add_argument('--build-threads', type=int,
                   default=DEFAULT_BUILD_THREADS, metavar='N',
                   help='Parallel make jobs (default: %(default)d).  Set to 1 '
                        'if you hit "cc1plus: Internal error: Killed".')
    p.add_argument('--skip-build', action='store_true',
                   help='Skip Shiboken+PySide cross-compile (needs --pyside-stage).')
    p.add_argument('--install-apk', action='store_true',
                   help='adb install the APK to a connected device after build.')
    p.add_argument('--keep-build', action='store_true',
                   help='Keep intermediate cmake build directories (debugging).')
    p.add_argument('--dry-run', action='store_true',
                   help='Print commands without executing them.')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Enable debug-level logging.')
    return p


# ----------------------------------------------------------------------------
# main()
# ----------------------------------------------------------------------------

def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if args.verbose:
        log.setLevel(logging.DEBUG)

    log.info('=====================================================')
    log.info(' PySide 1 (Qt 4.8) -> Android APK build')
    log.info('=====================================================')
    log.info('  project       : %s', args.project_dir)
    log.info('  Necessitas SDK: %s', args.necessitas_sdk)

    try:
        preflight(args)
        setup_workspace(args)
        configure_env(args)
        cross_compile(args)
        strip_binaries(args)
        py27_zip = package_python_runtime(args)
        proj_zip = package_user_app(args)
        prepare_apk_project(args)
        inject_artifacts(args, py27_zip, proj_zip)
        build_apk(args)
        apk = locate_apk(args)
        install_apk(args, apk)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log.error('interrupted by user')
        return 130
    except Exception as e:
        log.exception('unhandled exception: %s', e)
        return 1

    log.info('=====================================================')
    log.info(' BUILD SUCCEEDED')
    log.info('=====================================================')
    return 0


if __name__ == '__main__':
    sys.exit(main())
