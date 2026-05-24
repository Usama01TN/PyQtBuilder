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

BUILDER_SCRIPT_VERSION = 11  # 2026-05: fix v10's libpyside patch causing
                             # double-definition errors in shiboken-generated
                             # qtcore_module_wrapper.cpp (which includes
                             # pyside.h via two different paths in the same
                             # translation unit).
                             #
                             # Root cause: v9/v10 appended the new function
                             # AFTER pyside.h's own `#endif` include guard,
                             # leaving the function unprotected.  When the
                             # header is included twice in one TU, the
                             # original (guarded) code is suppressed but
                             # our patch runs twice -> redefinition error.
                             # C++ `inline` only relaxes ODR across
                             # different TUs, not within one.
                             #
                             # v11 fixes by:
                             #   (a) wrapping the patch snippet in its own
                             #       include guard
                             #       (LIBPYSIDE_BACKPORT_GETWRAPPERFORQOBJECT)
                             #   (b) detecting older patch markers and
                             #       TRUNCATING the file at the start of
                             #       the old block before appending the
                             #       new one, so the patcher self-heals
                             #       caches that v9/v10 polluted.

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

    # Sanity-check main.py imports.
    #
    # The OLD check was `if b'PySide6' in src` — a substring match — which
    # had false positives for any file containing the string "PySide6" in
    # a comment, docstring, or non-import line.  This new check uses a
    # regex that matches actual import STATEMENTS only:
    #
    #   from PySide2 import ...       → REJECTED
    #   from PySide6 import ...       → REJECTED
    #   from PyQt5 import ...         → REJECTED
    #   from PyQt6 import ...         → REJECTED
    #   import PySide2                → REJECTED
    #   import PySide6                → REJECTED
    #   from PySide import ...        → ACCEPTED (this is PySide 1)
    #   # TODO: port to PySide6        → ACCEPTED (comment, not import)
    #   "documentation about PySide6"  → ACCEPTED (string, not import)
    with open(main_py, 'rb') as f:
        src = f.read()

    forbidden_re = re.compile(
        br'^\s*(?:from\s+(?P<from_mod>PySide2|PySide6|PyQt5|PyQt6)\b'
        br'|import\s+(?P<imp_mod>PySide2|PySide6|PyQt5|PyQt6)\b)',
        re.MULTILINE)

    matches = list(forbidden_re.finditer(src))
    if matches:
        offending_lines = []
        for m in matches:
            # Figure out which line number the match is on
            line_num = src[:m.start()].count(b'\n') + 1
            # Extract the full source line for display
            line_start = src.rfind(b'\n', 0, m.start()) + 1
            line_end = src.find(b'\n', m.start())
            if line_end == -1:
                line_end = len(src)
            line_text = src[line_start:line_end].decode('utf-8', 'replace').rstrip()
            offending_lines.append('      line %d: %s' % (line_num, line_text))
        raise SystemExit(
            '  X main.py imports from PySide2/PySide6/PyQt5/PyQt6.\n'
            '    This builder targets PySide 1 (Qt 4.8) on Python 2.7.\n'
            '    Offending import line(s):\n'
            + '\n'.join(offending_lines) + '\n'
            '    For modern stacks use build_pyqt5_android.py.\n'
            '    For PySide 1, replace these imports with:\n'
            '      from PySide import QtGui      (instead of PyQt5/PySide6 QtWidgets)\n'
            '      from PySide import QtCore     (instead of PyQt5/PySide6 QtCore)')

    # Positive check: confirm PySide 1 imports are present.  PySide 1
    # is just `PySide` (no version).  Match `from PySide ` or `import PySide `
    # NOT followed by a digit (so PySide2/PySide6 don't satisfy this).
    pyside1_re = re.compile(
        br'^\s*(?:from|import)\s+PySide(?!\d)\b',
        re.MULTILINE)
    if not pyside1_re.search(src):
        log.warning(
            '  ! main.py does not appear to import PySide (Qt 4) — your app\n'
            '    may not actually use the PySide 1 bindings.  Continuing anyway,\n'
            '    but the resulting APK may not work as expected.')
    else:
        # Log the first PySide 1 import line so the user can confirm we
        # detected the right import.
        #
        # IMPORTANT: under Python 2.7 we must NOT mix `unicode` arguments
        # with the byte-literal format string (which contains the UTF-8
        # `✓` glyph as 3 raw bytes).  When logging gets unicode + bytes
        # it tries to upcast the format string via ASCII codec and the
        # 0xE2 in '✓' breaks with UnicodeDecodeError.
        #
        # Fix: keep `line_text` as bytes/str (no .decode()).  In Python 2,
        # bytes IS str, so the %s substitution is bytes-to-bytes and
        # everything stays consistent.  Under Python 3 (where bytes != str)
        # we explicitly decode for the %s slot.
        m = pyside1_re.search(src)
        line_num = src[:m.start()].count(b'\n') + 1
        line_start = src.rfind(b'\n', 0, m.start()) + 1
        line_end = src.find(b'\n', m.start())
        if line_end == -1:
            line_end = len(src)
        line_text_bytes = src[line_start:line_end].rstrip()
        # In Py3 bytes != str so we need to decode for the %s slot.
        # In Py2 str == bytes and decoding to unicode breaks the format
        # mix described above, so we stay as bytes.
        if str is bytes:
            line_text = line_text_bytes
        else:
            line_text = line_text_bytes.decode('utf-8', 'replace')
        log.info('  PySide 1 import found at line %d: %s', line_num, line_text)

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
# NDK acquisition + path probing
# ----------------------------------------------------------------------------
#
# Necessitas's offline NDK ship was version r6b from 2011.  Two problems
# with relying on it in 2026:
#
#   1. Necessitas's online installer often fails to actually download
#      the NDK component (its component server has been unmaintained
#      since ~2014).  necessitas-install.log shows many "Could not delete
#      ...android-ndk-r6b-linux-x86.7z" errors that mean the file was
#      never present to begin with.
#
#   2. NDK r6b was Linux-x86 (32-bit host) only.  Modern Ubuntu runners
#      are x86_64; running 32-bit binaries needs multilib (we have it)
#      but the NDK was never tested on 2020s glibc.
#
# We sidestep both by downloading NDK r8b from Google's archive (still
# hosted as of late 2025) which has 64-bit host binaries AND ships
# multiple GCC versions (4.4.3, 4.6, 4.7).  4.4.3 matches what env.sh
# expects.

# NDK download URL list — ordered by preference.  Google's CDN at
# dl.google.com/android/repository/ is the canonical permanent home for
# NDKs and has been stable for ~10 years.  NDK r8b (what M4rtinK's
# scripts originally targeted) was REMOVED from Google's CDN in ~2017
# when they restructured /android/ndk/ → /android/repository/.  The
# oldest NDK still hosted is r10e (released March 2015, last with GCC
# 4.x), which is what we target now.  Our v4 NDK-path probing logic
# substitutes whatever STL/toolchain version is found into env.sh, so
# it doesn't matter that r10e is 4.6/4.8/4.9 instead of r8b's 4.4.3.
#
# The user can override the list entirely via the NDK_DOWNLOAD_URL
# environment variable — if both lists below fail, set that to a
# self-hosted URL (e.g. a GitHub Release asset).
NDK_DOWNLOAD_URLS = [
    # Primary: NDK r10e from Google CDN (.zip, ~400MB).
    # Stable, still hosted as of 2026.  This is the file Google itself
    # links from developer.android.com/ndk/downloads/older_releases.
    'https://dl.google.com/android/repository/android-ndk-r10e-linux-x86_64.zip',

    # Secondary: NDK r12b (similar era, also still hosted).  Some
    # people find r12b builds cleaner than r10e.
    'https://dl.google.com/android/repository/android-ndk-r12b-linux-x86_64.zip',

    # Tertiary: NDK r13b
    'https://dl.google.com/android/repository/android-ndk-r13b-linux-x86_64.zip',

    # Quaternary: NDK r17c — the last NDK with GCC 4.9 at all.
    # If r10e/r12b/r13b are gone, this is the fallback.
    'https://dl.google.com/android/repository/android-ndk-r17c-linux-x86_64.zip',
]


def _probe_ndk(ndk_dir):
    """
    Inspect the NDK at *ndk_dir* and return a dict with the actual paths
    the build needs.  These are HARDCODED in upstream env.sh to versions
    that don't exist on most installs:

        gnu-libstdc++/4.4.3/include
        toolchains/arm-linux-androideabi-4.4.3/prebuilt/linux-x86/bin/
        platforms/android-8/arch-arm

    We probe what's actually there and substitute correct paths.

    Returns dict with keys: stl_version, stl_include, stl_libs,
    toolchain_dir, toolchain_bin, platform_dir, host_prebuilt.

    Raises SystemExit with a clear diagnostic if the NDK is empty or
    has none of the expected subdirectories.
    """
    if not os.path.isdir(ndk_dir):
        raise SystemExit('  X NDK directory does not exist: %s' % ndk_dir)

    info = {}

    # 1. Find STL version.  NDK r6b ships 4.4.3 only; r7+ adds 4.6;
    # r8+ adds 4.7 and 4.8.  Prefer the lowest (4.4.3) because that's
    # what env.sh and the shiboken code were written for.
    stl_root = os.path.join(ndk_dir, 'sources', 'cxx-stl', 'gnu-libstdc++')
    if not os.path.isdir(stl_root):
        raise SystemExit(
            '  X No gnu-libstdc++ STL in NDK at %s\n'
            '    Expected: %s/sources/cxx-stl/gnu-libstdc++/\n'
            '    This NDK install is incomplete.  Pass --download-ndk to\n'
            '    fetch a working NDK r8b from Google\'s archive.'
            % (ndk_dir, ndk_dir))
    stl_versions = sorted([
        d for d in os.listdir(stl_root)
        if os.path.isdir(os.path.join(stl_root, d, 'include'))
    ])
    if not stl_versions:
        raise SystemExit(
            '  X No usable STL versions found under %s\n'
            '    Expected one or more of: 4.4.3, 4.6, 4.7, 4.8' % stl_root)
    info['stl_version'] = stl_versions[0]   # prefer lowest = oldest = matches env.sh
    info['stl_include'] = os.path.join(stl_root, info['stl_version'], 'include')
    info['stl_libs'] = os.path.join(stl_root, info['stl_version'],
                                    'libs', 'armeabi')
    if not os.path.isfile(os.path.join(info['stl_include'], 'cstddef')):
        raise SystemExit(
            '  X STL include path %s missing cstddef.\n'
            '    The NDK install is corrupted.' % info['stl_include'])

    # 2. Find toolchain.  NDK r6b: arm-linux-androideabi-4.4.3 only.
    #    NDK r8b: 4.4.3 + 4.6 + 4.7.  Prefer 4.4.3 to match shiboken's
    #    expectations and the env.sh defaults.
    tc_root = os.path.join(ndk_dir, 'toolchains')
    if not os.path.isdir(tc_root):
        raise SystemExit('  X No toolchains/ dir in NDK at %s' % ndk_dir)
    candidates = sorted([
        d for d in os.listdir(tc_root)
        if d.startswith('arm-linux-androideabi-')
    ])
    if not candidates:
        raise SystemExit(
            '  X No arm-linux-androideabi-* toolchain in %s' % tc_root)
    # Match STL version to toolchain version when possible (avoid ABI mismatch).
    matched = [c for c in candidates if c.endswith('-' + info['stl_version'])]
    info['toolchain_dir'] = os.path.join(
        tc_root, matched[0] if matched else candidates[0])

    # 3. Find host prebuilt arch.  NDK r6b: linux-x86 only.
    #    NDK r8b: linux-x86 + linux-x86_64.  Prefer x86_64 on 64-bit hosts.
    prebuilt_root = os.path.join(info['toolchain_dir'], 'prebuilt')
    if not os.path.isdir(prebuilt_root):
        raise SystemExit('  X No prebuilt/ in %s' % info['toolchain_dir'])
    host_dirs = sorted(os.listdir(prebuilt_root))
    # Prefer x86_64, fall back to x86
    if 'linux-x86_64' in host_dirs:
        info['host_prebuilt'] = 'linux-x86_64'
    elif 'linux-x86' in host_dirs:
        info['host_prebuilt'] = 'linux-x86'
    elif host_dirs:
        info['host_prebuilt'] = host_dirs[0]   # whatever's there
    else:
        raise SystemExit('  X No host prebuilt dirs in %s' % prebuilt_root)
    info['toolchain_bin'] = os.path.join(prebuilt_root, info['host_prebuilt'], 'bin')

    # Sanity-check the compiler binary exists
    gxx = os.path.join(info['toolchain_bin'], 'arm-linux-androideabi-g++')
    if not os.path.isfile(gxx):
        raise SystemExit(
            '  X arm-linux-androideabi-g++ not found at %s\n'
            '    Toolchain install is incomplete.' % gxx)

    # 4. Find Android platform dir.  env.sh defaults to api 8.
    plats_root = os.path.join(ndk_dir, 'platforms')
    if not os.path.isdir(plats_root):
        raise SystemExit('  X No platforms/ in NDK at %s' % ndk_dir)
    plats = sorted([
        d for d in os.listdir(plats_root)
        if d.startswith('android-')
        and os.path.isdir(os.path.join(plats_root, d, 'arch-arm', 'usr', 'include'))
    ])
    if not plats:
        raise SystemExit(
            '  X No android-* platform dirs in %s' % plats_root)
    # Prefer android-9 if available (works for most things), else android-8
    # (env.sh default), else lowest available.
    preferred = ['android-9', 'android-8', 'android-14']
    chosen = next((p for p in preferred if p in plats), plats[0])
    info['platform_dir'] = os.path.join(plats_root, chosen, 'arch-arm')

    return info


def _download_ndk(dest_parent_dir, dry_run=False):
    """
    Download a working Android NDK from Google's CDN and extract it to
    *dest_parent_dir*.  Returns the path to the extracted android-ndk-*/
    subdirectory.

    Tries URLs in this order:
      1. The user's override URL ($NDK_DOWNLOAD_URL env var, if set)
      2. Each URL in NDK_DOWNLOAD_URLS (r10e → r12b → r13b → r17c)

    Handles both .zip (NDK r10e and newer) and .tar.bz2 (NDK r9 and
    older) by sniffing the file's content rather than trusting the URL
    extension.

    File size sanity check: any download under 50 MB is rejected as a
    likely HTML error page or partial transfer.
    """
    log.info('  acquiring Android NDK (one-time, ~400-500 MB download)')
    _makedirs(dest_parent_dir)

    # User-provided override via env var takes priority.  Useful when
    # the user has mirrored an NDK to their own GitHub Release etc.
    user_url = os.environ.get('NDK_DOWNLOAD_URL', '').strip()
    urls = [user_url] if user_url else []
    urls += NDK_DOWNLOAD_URLS

    archive_path = os.path.join(dest_parent_dir, 'ndk-download.archive')
    chosen_url = None
    MIN_NDK_BYTES = 50 * 1024 * 1024   # 50 MB — well under any real NDK

    for url in urls:
        log.info('  trying %s', url)
        if dry_run:
            open(archive_path, 'w').close()
            chosen_url = url
            break

        # Clean any partial file from a previous attempt
        if os.path.exists(archive_path):
            os.remove(archive_path)

        rc = run([
            'curl', '-L', '--fail', '--retry', '3',
            '--retry-delay', '5', '--max-time', '900',
            '-A', 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0.0.0',
            '-o', archive_path, url,
        ], check=False, dry_run=False)

        if rc != 0:
            log.warning('  ! curl failed for this URL (exit %d)', rc)
            continue

        size = os.path.getsize(archive_path) if os.path.isfile(archive_path) else 0
        if size < MIN_NDK_BYTES:
            log.warning('  ! got only %d bytes (need ≥ %d) — likely error page',
                        size, MIN_NDK_BYTES)
            try:
                # Peek the first few bytes to log what we got
                with open(archive_path, 'rb') as f:
                    head = f.read(200)
                log.warning('    file head: %r', head[:150])
            except (IOError, OSError):
                pass
            continue

        log.info('  ✓ downloaded %d bytes (%.1f MB)', size, size / (1024.0 * 1024.0))
        chosen_url = url
        break

    if chosen_url is None:
        # Last resort error message — tell the user exactly what to do.
        raise SystemExit(
            '  X All NDK download URLs failed.\n'
            '\n'
            '    Tried in order:\n'
            + ''.join('      %s\n' % u for u in urls) +
            '\n'
            '    Workarounds:\n'
            '\n'
            '    A) Set NDK_DOWNLOAD_URL env var to a self-hosted mirror.\n'
            '       Upload an NDK r8e/r9d/r10e .tar.bz2 or .zip to a\n'
            '       GitHub Release on your repo, then in the workflow:\n'
            '\n'
            '         env:\n'
            '           NDK_DOWNLOAD_URL: "https://github.com/USER/REPO/releases/download/v1/android-ndk-r10e.zip"\n'
            '\n'
            '    B) Pre-build the Shiboken+PySide stage tarball locally\n'
            '       on a machine with a working NDK install, then pass\n'
            '       --pyside-stage URL to skip the cross-compile entirely.\n'
            '\n'
            '    C) Switch to the PyQt5/Qt5 builder (build_pyqt5_android.py).\n'
            '       The PyQt5 build chain uses Qt 5 and modern toolchains\n'
            '       that aren\'t bit-rotting like the Necessitas stack.')

    if dry_run:
        # Return a plausible extracted path so downstream steps see something
        return os.path.join(dest_parent_dir, 'android-ndk-r10e')

    # Sniff the archive type by reading magic bytes rather than trusting
    # the URL extension.  Real archives:
    #   .zip          : starts with "PK\x03\x04" or "PK\x05\x06"
    #   .tar.bz2      : starts with "BZh"
    #   .tar.gz       : starts with "\x1f\x8b"
    #   .tar.xz       : starts with "\xfd7zXZ"
    with open(archive_path, 'rb') as f:
        magic = f.read(6)

    log.info('  extracting NDK (this is the slow part — ~500MB of small files)')
    if magic.startswith(b'PK'):
        log.info('  detected ZIP archive')
        run(['unzip', '-q', archive_path, '-d', dest_parent_dir], dry_run=False)
    elif magic.startswith(b'BZh'):
        log.info('  detected bzip2 tarball')
        run(['tar', '-xjf', archive_path, '-C', dest_parent_dir], dry_run=False)
    elif magic.startswith(b'\x1f\x8b'):
        log.info('  detected gzip tarball')
        run(['tar', '-xzf', archive_path, '-C', dest_parent_dir], dry_run=False)
    elif magic.startswith(b'\xfd7zXZ'):
        log.info('  detected xz tarball')
        run(['tar', '-xJf', archive_path, '-C', dest_parent_dir], dry_run=False)
    else:
        raise SystemExit(
            '  X downloaded file at %s is not a recognized archive.\n'
            '    First 6 bytes: %r\n'
            '    URL was: %s'
            % (archive_path, magic, chosen_url))

    os.remove(archive_path)

    # The extracted dir name varies by NDK version: android-ndk-r10e/,
    # android-ndk-r12b/, etc.  Find whichever one landed.
    candidates = [
        d for d in os.listdir(dest_parent_dir)
        if d.startswith('android-ndk-')
        and os.path.isdir(os.path.join(dest_parent_dir, d))
    ]
    if not candidates:
        raise SystemExit(
            '  X extraction finished but no android-ndk-*/ dir at %s.\n'
            '    Contents: %s' % (dest_parent_dir, os.listdir(dest_parent_dir)))

    real_ndk = os.path.join(dest_parent_dir, candidates[0])
    log.info('  ✓ NDK extracted to %s', real_ndk)
    return real_ndk


def _ensure_working_ndk(args):
    """
    Verify the Necessitas NDK has all the bits we need (toolchain, STL,
    platform headers).  If anything's missing, download a replacement
    NDK from the still-hosted Google CDN URLs.

    Updates args.sdk.ndk to point at the working NDK.  Returns the
    probed-paths dict from _probe_ndk().
    """
    log.info('  validating NDK at %s', args.sdk.ndk)
    try:
        info = _probe_ndk(args.sdk.ndk)
        log.info('  ✓ existing NDK is usable')
        log.info('    STL version    : %s', info['stl_version'])
        log.info('    Host prebuilt  : %s', info['host_prebuilt'])
        log.info('    Toolchain bin  : %s', info['toolchain_bin'])
        log.info('    Platform dir   : %s', info['platform_dir'])
        return info
    except SystemExit as e:
        # Squash the nested SystemExit into a single-line warning so
        # the log stays readable.
        msg = str(e).strip().replace('\n', ' ')
        # Trim the multi-space gaps left by stripped newlines
        msg = re.sub(r'\s{2,}', ' ', msg)
        log.warning('  ! Necessitas NDK is incomplete: %s', msg)

    # Necessitas's NDK didn't work — download a fresh one.
    log.info('  downloading replacement NDK from Google CDN')
    new_ndk_parent = os.path.join(args.cache_dir, 'extra-ndk')
    _makedirs(new_ndk_parent)

    # Skip download if a working NDK is already cached.  We look for any
    # android-ndk-*/ subdirectory that probes successfully.
    cached_ndk = None
    if os.path.isdir(new_ndk_parent):
        for name in os.listdir(new_ndk_parent):
            candidate = os.path.join(new_ndk_parent, name)
            if name.startswith('android-ndk-') and os.path.isdir(candidate):
                try:
                    _probe_ndk(candidate)
                    cached_ndk = candidate
                    log.info('  ✓ NDK already cached at %s', cached_ndk)
                    break
                except SystemExit:
                    # Partial / corrupted cache — fall through and re-download
                    continue

    if cached_ndk is None:
        cached_ndk = _download_ndk(new_ndk_parent, dry_run=args.dry_run)

    # Override the SDK's ndk path with the new one
    args.sdk.ndk = cached_ndk
    log.info('  ✓ using NDK at %s', cached_ndk)
    return _probe_ndk(cached_ndk)


# ----------------------------------------------------------------------------
# Step 3 -- Configure env.sh and de-promptify the bash scripts
# ----------------------------------------------------------------------------

def configure_env(args):
    """
    Write a customised env.sh into the build-scripts directory with the
    right NECESSITAS_DIR, NDK path, Qt path, build-thread count, AND
    correctly-discovered NDK toolchain/STL versions.

    The upstream env.sh hardcodes paths to NDK r6b:

        STL_INCLUDES="-I${STL_PATH}/4.4.3/include -I${STL_PATH}/4.4.3/libs/armeabi/include"
        ANDROID_BIN="${ANDROID_NDK}/toolchains/arm-linux-androideabi-4.4.3/prebuilt/linux-x86/bin/"

    Those paths are wrong for any NDK other than the original r6b 32-bit
    host build.  Modern runners are x86_64 and the bundled Necessitas
    NDK install often fails (its component server is dead).  This
    function probes the actual NDK on disk via _probe_ndk() and
    substitutes the discovered paths.

    Also patches out the `read -p "press any key"` calls in
    build_shiboken.sh and build_pyside.sh so the build runs unattended.
    """
    log.info('Step 3/12 -- Configure env.sh + de-prompt build scripts')

    # FIRST: validate the NDK and download a working one if necessary.
    # This may update args.sdk.ndk to point at a fresh r8b install.
    ndk_info = _ensure_working_ndk(args)

    bs = args.build_scripts_dir
    env_path = os.path.join(bs, 'env.sh')

    with open(env_path, 'rb') as f:
        env_text = f.read().decode('utf-8')

    # NECESSITAS_DIR placeholder.
    env_text = env_text.replace(
        '<path to the Necessitas SDK folder>',
        args.sdk.root)

    # ANDROID_NDK — point at the working NDK (Necessitas's or downloaded).
    env_text = re.sub(
        r'^export\s+ANDROID_NDK=.*$',
        'export ANDROID_NDK="%s"' % args.sdk.ndk,
        env_text, count=1, flags=re.MULTILINE)

    # QT_DIR — Necessitas's Qt 4.8 install.
    env_text = re.sub(
        r'^export\s+QT_DIR=.*$',
        'export QT_DIR="%s"' % args.sdk.qt_dir,
        env_text, count=1, flags=re.MULTILINE)

    # BUILD_THREAD_COUNT.
    env_text = re.sub(
        r'^export\s+BUILD_THREAD_COUNT=.*$',
        'export BUILD_THREAD_COUNT=%d' % args.build_threads,
        env_text, count=1, flags=re.MULTILINE)

    # NDK STL version — upstream hardcodes 4.4.3 in two places.
    # Replace with the version we actually found in the NDK.
    log.info('  substituting STL version 4.4.3 -> %s in env.sh',
             ndk_info['stl_version'])
    env_text = env_text.replace('/4.4.3/', '/%s/' % ndk_info['stl_version'])

    # NDK toolchain version — same hardcoded 4.4.3.
    env_text = env_text.replace(
        'arm-linux-androideabi-4.4.3',
        os.path.basename(ndk_info['toolchain_dir']))

    # Host prebuilt — upstream hardcodes linux-x86.  Modern runners need
    # linux-x86_64 for the cross-compiler to even run (NDK r8b has
    # 64-bit prebuilts but r6b doesn't).
    if ndk_info['host_prebuilt'] != 'linux-x86':
        log.info('  substituting prebuilt linux-x86 -> %s in env.sh',
                 ndk_info['host_prebuilt'])
        env_text = env_text.replace(
            'prebuilt/linux-x86/',
            'prebuilt/%s/' % ndk_info['host_prebuilt'])

    # ANDROID_API_LEVEL — make sure the platform we discovered is used.
    discovered_api = re.search(r'android-(\d+)',
                               os.path.basename(os.path.dirname(
                                   ndk_info['platform_dir'])))
    if discovered_api:
        env_text = re.sub(
            r'^export\s+ANDROID_API_LEVEL=.*$',
            'export ANDROID_API_LEVEL="%s"' % discovered_api.group(1),
            env_text, count=1, flags=re.MULTILINE)

    # Modern-GCC compatibility flags for Qt 4.8 headers AND shiboken
    # source files (which also miss `#include <list>` etc.).
    #
    # The expanded -include list covers everything we've seen the
    # 2011-era code reference without an explicit include:
    #
    #   * Containers   : list, vector, map, set, string, utility
    #   * Algorithms   : algorithm  (std::max, std::min)
    #   * C compat     : cstddef, cstring, cstdlib, cstdio, cstdint, climits
    #   * Memory       : new  (placement new)
    #
    # We also add `-fpermissive` to downgrade C++11-strictness errors
    # to warnings (string-literal-to-char*, narrowing in initializers).
    compat_flags = (
        '-include cstddef '       # <cstddef> for std::size_t, NULL
        '-include cstring '       # <cstring> for memcpy, memset, strlen
        '-include new '           # <new> for placement new (qmap.h:456)
        '-include cstdlib '       # <cstdlib> for malloc, free, exit
        '-include cstdio '        # <cstdio> for FILE, fprintf
        # NOTE: we deliberately use the C-style header <stdint.h>
        # rather than the C++ one <cstdint>.  NDK r10e's libstdc++
        # 4.8 guards <cstdint> behind `__cplusplus >= 201103L` and
        # #errors with "this file requires C++11" if it's pulled in
        # without -std=c++11.  M4rtinK's shiboken-android code is
        # C++98, so we must keep the compiler in default mode.
        # <stdint.h> is the C99 form, available since GCC 3.x in any
        # mode, and provides the same int32_t / uint64_t etc.
        '-include stdint.h '      # int32_t, uint64_t — C99-style
        '-include climits '       # <climits> for INT_MAX etc. (C++98)
        # C++ STL containers/algos that shiboken's source uses without
        # `#include` directives (basewrapper.cpp, conversions.h, etc.):
        '-include list '          # std::list
        '-include vector '        # std::vector
        '-include map '           # std::map
        '-include set '           # std::set
        '-include string '        # std::string
        '-include utility '       # std::pair, std::make_pair
        '-include algorithm '     # std::max, std::min, std::find
        '-include sstream '       # std::stringstream
        # ── Code-generation flags for ARM shared-library link ──
        # Without -fPIC, the linker rejects the .o files with:
        #   "ld: error: requires unsupported dynamic reloc
        #    R_ARM_REL32; recompile with -fPIC"
        # CMake's standard "build .so" rules SHOULD imply -fPIC, but
        # M4rtinK's shiboken CMakeLists.txt uses CMAKE_FORCE_CXX_COMPILER
        # which bypasses CMake's normal compiler-flag plumbing.  Adding
        # -fPIC to CXXFLAGS reaches the compile unconditionally.
        '-fPIC '
        # NDK r10e's libgcc doesn't export __dso_handle as a public
        # symbol — only the runtime does, but we're statically linking
        # libgcc.  -fno-use-cxa-atexit tells GCC not to generate calls
        # to __cxa_atexit() (which need __dso_handle), and to fall back
        # to old-style atexit() instead.  This is the standard fix for:
        #   "ld: error: hidden symbol '__dso_handle' is not defined
        #    locally"
        '-fno-use-cxa-atexit '
        # -fpermissive downgrades many C++11-strictness errors to
        # warnings (string-literal-to-char*, narrowing in initializers).
        '-fpermissive'
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
    env_text = _patch_flag_line(
        'CFLAGS',
        '-include cstddef -include cstring -fPIC -fno-use-cxa-atexit',
        env_text)

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
QT48_PATCH_MARKER = '// PATCHED_BY_BUILD_PYSIDE_ANDROID v6 -- DO NOT REMOVE'

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
    #   * <cstdlib> covers malloc/free/qmalloc
    # The marker comes first so our grep-for-marker check sees it.
    #
    # NOTE: we deliberately do NOT prepend <cstdint> here.  That header
    # requires C++11 in libstdc++ 4.8 and triggers a `#error` if
    # __cplusplus < 201103L.  Qt 4.8 code (C++98) and shiboken-android
    # (C++98) don't actually need cstdint — the int32_t/uint64_t types
    # they reference come from <stdint.h> via our CXXFLAGS instead.
    PREPEND = (
        QT48_PATCH_MARKER + '\n'
        '#include <new>\n'
        '#include <cstddef>\n'
        '#include <cstring>\n'
        '#include <cstdlib>\n'
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

        # Idempotency check — skip if THIS version's marker is present.
        if QT48_PATCH_MARKER.encode('utf-8') in content[:1024]:
            skipped_count += 1
            log.info('    skip  %s  (already patched)', header_name)
            continue

        # Old-version-marker detection: a previous version of this
        # script may have patched this file with a DIFFERENT (older)
        # prepend block — e.g. v5 prepended `#include <cstdint>` which
        # triggers C++11 errors in libstdc++ 4.8.  Strip the old block
        # before applying the new one.
        OLD_MARKER_PREFIX = b'// PATCHED_BY_BUILD_PYSIDE_ANDROID'
        if OLD_MARKER_PREFIX in content[:1024]:
            log.info('    stripping old patch block from %s', header_name)
            # The old block runs from the marker through the blank line
            # after the includes.  Find the first non-#include line
            # after the marker.
            lines = content.split(b'\n')
            strip_until = 0
            saw_marker = False
            for i, line in enumerate(lines):
                if not saw_marker:
                    if OLD_MARKER_PREFIX in line:
                        saw_marker = True
                    continue
                # After marker — keep stripping #include lines and blanks
                stripped = line.strip()
                if stripped.startswith(b'#include') or stripped == b'':
                    strip_until = i + 1
                else:
                    break
            content = b'\n'.join(lines[strip_until:])

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
# libpyside source patcher — adds functions missing in M4rtinK's 1.1-era fork
# ----------------------------------------------------------------------------

LIBPYSIDE_PATCH_MARKER = '// LIBPYSIDE_PATCH_v11 -- DO NOT REMOVE'

# Old-version marker prefix.  When _patch_libpyside_source runs, it
# detects any line containing this prefix and TRUNCATES the file
# at that point before appending the new snippet.  This handles the
# case where a previous (broken) version of this patcher already
# wrote a now-incompatible snippet to disk, which would otherwise
# stay there and cause double-definition errors.
LIBPYSIDE_PATCH_MARKER_PREFIX = '// LIBPYSIDE_PATCH_'

# The chunk we inject into libpyside/pyside.h.  This is a back-port of
# PySide 1.2's `getWrapperForQObject()` helper.  Shiboken's generator
# emits calls to this function in the QObject CppToPython converters
# it generates.  M4rtinK's pyside-android fork (PySide 1.1.x era) is
# missing the function entirely, causing:
#
#     qabstracteventdispatcher_wrapper.cpp:1476:12: error:
#     'getWrapperForQObject' is not a member of 'PySide'
#
# IMPORTANT: this snippet appears AFTER pyside.h's own `#endif` of the
# PYSIDE_H include guard.  Without ITS OWN include guard, the function
# would be re-declared every time pyside.h is included.  Some shiboken-
# generated translation units include pyside.h more than once via
# different paths (e.g. qtcore_module_wrapper.cpp includes it at lines
# 28 and 210), and even an `inline` function cannot be defined twice
# in the SAME translation unit -- the C++ standard's "inline" relaxation
# only covers DIFFERENT translation units.  So we add our own guard.
LIBPYSIDE_PATCH_SNIPPET = r'''

// ============================================================
// %s
// ============================================================
// Back-ported from PySide 1.2.4 -- required by newer shiboken
// generators which emit `PySide::getWrapperForQObject(...)` calls
// in the QObject CppToPython converter blocks.  M4rtinK's PySide
// 1.1.x-era fork didn't include this helper.

#ifndef LIBPYSIDE_BACKPORT_GETWRAPPERFORQOBJECT
#define LIBPYSIDE_BACKPORT_GETWRAPPERFORQOBJECT

#include <shiboken.h>

namespace PySide
{
    inline PyObject* getWrapperForQObject(QObject* cppSelf, SbkObjectType* sbk_type)
    {
        PyObject* pyOut = (PyObject*) Shiboken::BindingManager::instance().retrieveWrapper(cppSelf);
        if (pyOut) {
            Py_INCREF(pyOut);
            return pyOut;
        }
        // Create a new Python wrapper for this QObject.  We pass
        // hasOwnership=false because Qt owns the QObject's lifetime,
        // and isExactType=false to allow shiboken's type-resolution
        // machinery to find subclass wrappers if appropriate.
        pyOut = Shiboken::Object::newObject(sbk_type,
                                            cppSelf,
                                            /* hasOwnership */ false,
                                            /* isExactType  */ false);
        return pyOut;
    }
}

#endif // LIBPYSIDE_BACKPORT_GETWRAPPERFORQOBJECT
''' % LIBPYSIDE_PATCH_MARKER


def _patch_libpyside_source(build_scripts_dir, dry_run=False):
    """
    Inject `PySide::getWrapperForQObject` into M4rtinK's libpyside/pyside.h.

    Shiboken's generator (a 1.2-era shiboken) emits wrapper code that
    references `PySide::getWrapperForQObject(QObject*, SbkObjectType*)`.
    M4rtinK's pyside-android fork is based on PySide 1.1.x and doesn't
    declare this function — every generated QObject wrapper fails to
    compile with:

        'getWrapperForQObject' is not a member of 'PySide'

    We patch it in by appending the function (inside the PySide
    namespace) to libpyside/pyside.h.  The append goes AFTER the
    existing header's `#endif` guard so we don't disturb its include
    structure; we add our own include for shiboken.h.

    Idempotent — looks for LIBPYSIDE_PATCH_MARKER and skips if present.

    Args:
      build_scripts_dir : the dir containing pyside-android/ (cloned
                          by prepare.sh in Step 2).
      dry_run           : if True, log but don't write.

    Returns:
      int: 1 if patched, 0 if already-patched, raises SystemExit if
      the expected file isn't present.
    """
    log.info('  patching M4rtinK libpyside for shiboken-generated calls')

    pyside_h = os.path.join(build_scripts_dir, 'pyside-android',
                            'libpyside', 'pyside.h')
    if not os.path.isfile(pyside_h):
        # Be tolerant: maybe the file moved or the user's tree is
        # different.  Log a warning and continue — the compile will
        # fail later with a clear error if the symbol's still missing.
        log.warning('  ! libpyside/pyside.h not at expected path: %s', pyside_h)
        log.warning('    (skipping libpyside patch; build may fail with')
        log.warning('    "PySide::getWrapperForQObject is not a member")')
        return 0

    with open(pyside_h, 'rb') as f:
        content = f.read()

    # Helper: convert a string constant to bytes, safely under both
    # Python 2 and 3.  The trick is that Python 2's str.encode('utf-8')
    # actually does an IMPLICIT decode-via-ASCII FIRST (str -> unicode)
    # before re-encoding, and breaks on any 0x80-0xFF byte.  In Python 2,
    # str IS bytes, so we just return it as-is.  In Python 3, str is
    # text, so encode it.  This way the snippet/marker stay defined as
    # plain string literals (works in both py2 and py3 source) but the
    # runtime conversion to bytes never tries an ASCII decode.
    def _as_bytes(s):
        if isinstance(s, bytes):
            return s                # Py2 str == bytes, no conversion
        return s.encode('utf-8')    # Py3 str -> bytes

    # Idempotency + old-version cleanup.
    #
    # If the file already has THIS version's marker, leave alone.
    # If it has any OLDER version's marker (e.g. v9 or v10), TRUNCATE
    # the file at the start of the old patch block, then append the
    # new one.  Without this, repeat runs would either skip (leaving
    # the broken old patch in place) OR stack the new patch on top
    # of the old one, which would multiply the double-definition
    # error rather than fix it.
    marker_bytes = _as_bytes(LIBPYSIDE_PATCH_MARKER)
    if marker_bytes in content:
        log.info('    skip pyside.h (already patched at current version)')
        return 0

    old_marker_prefix = _as_bytes(LIBPYSIDE_PATCH_MARKER_PREFIX)
    if old_marker_prefix in content:
        # Find the position of the FIRST line containing the old marker
        # and truncate everything from there onward.  We also strip any
        # preceding blank lines for a clean append.
        marker_pos = content.find(old_marker_prefix)
        # Walk back to the start of the line containing the marker
        line_start = content.rfind(b'\n', 0, marker_pos) + 1
        # Also strip any preceding lines that look like our patch
        # decorations (the `// ====` separator we put above the marker).
        # We do a simple loop: while the previous line is a `// ====`
        # divider or blank, eat it too.
        cut = line_start
        while cut > 0:
            prev_end = cut - 1   # the \n we just crossed
            prev_start = content.rfind(b'\n', 0, prev_end) + 1
            prev_line = content[prev_start:prev_end].strip()
            if prev_line == b'' or prev_line.startswith(b'// ====='):
                cut = prev_start
            else:
                break
        log.info('    stripping old patch block (saw %r at offset %d)',
                 old_marker_prefix, marker_pos)
        content = content[:cut]

    if dry_run:
        log.info('    [dry-run] would append %d bytes to %s',
                 len(LIBPYSIDE_PATCH_SNIPPET), pyside_h)
        return 1

    # Append our patch block.  Going at the END of the file (after
    # the existing #endif) is safest -- we don't risk messing up
    # namespace nesting or include order in the original code.
    # The snippet has its OWN include guard so multi-inclusion of
    # pyside.h within one translation unit doesn't redefine the function.
    new_content = content + _as_bytes(LIBPYSIDE_PATCH_SNIPPET)

    try:
        with open(pyside_h, 'wb') as f:
            f.write(new_content)
    except (IOError, OSError) as e:
        # Retry with chmod in case the file is read-only
        try:
            os.chmod(pyside_h, 0o644)
            with open(pyside_h, 'wb') as f:
                f.write(new_content)
        except (IOError, OSError) as e2:
            raise SystemExit(
                '  X could not patch %s: %s (chmod retry: %s)'
                % (pyside_h, e, e2))

    log.info('    patched %s (added PySide::getWrapperForQObject)', pyside_h)
    return 1


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

    # (1b) Patch M4rtinK's pyside-android source to add missing functions
    # that newer Shiboken generators reference.  Specifically:
    # `PySide::getWrapperForQObject` — present in PySide 1.2.x, missing
    # in M4rtinK's 1.1.x-era fork.  See _patch_libpyside_source docstring.
    _patch_libpyside_source(bs, dry_run=args.dry_run)

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
