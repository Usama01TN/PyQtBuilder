# -*- coding: utf-8 -*-
# ============================================================================
#  PyQt5 -> Android APK Builder
#  ===========================
#
#  Inspired by github.com/kviktor/pyqtdeploy-android-build.  Uses
#  pyqtdeploy 2.5.1 + Qt 5.13.2 (Android arm64) + NDK r20b to compile a
#  PyQt5 project into an Android APK.
#
#  Written in Python 2/3 dual-compatible syntax (no f-strings, no type
#  hints, no dataclasses, no pathlib).  In production it is invoked via
#  Python 3.7 because pyqtdeploy 2.5.1 itself requires Python 3, but the
#  source code style follows the conventions of the original kviktor
#  reference repo.
#
#  Pipeline (12 stages, each idempotent and re-runnable):
#       1.  Validate environment (Python 3.7, Qt, NDK, SDK, venv)
#       2.  Apply quirks (source.properties stub, python3 symlink)
#       3.  Scan project + DETECT PyQt5 imports automatically
#       4.  Generate sysroot.json (lowercase keys, dependency order)
#       5.  Generate app.pdy (Project version=7 schema)
#       6.  Generate android_source/ (AndroidManifest, Activity.java)
#       7.  Download source tarballs (with mirror fallback)
#       8.  Run pyqtdeploy-sysroot   (~25-40 min on first run)
#       9.  Run pyqtdeploy-build
#      10.  Compile (qmake + make + make install)
#      11.  Package APK (androiddeployqt --gradle)
#      12.  Locate + verify APK
#
#  Re-running is safe: each stage checks if its output already exists.
#  Pass --force to clean and rebuild from scratch.
# ============================================================================

from __future__ import absolute_import, division, print_function

import argparse
import ast
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import time
import zipfile
from os.path import join, exists, isdir, isfile, dirname, abspath, basename
from xml.dom import minidom
from xml.etree import ElementTree as ET


# Cross-compat string type
try:
    string_types = (str, unicode)   # Python 2
except NameError:
    string_types = (str,)            # Python 3


# ----------------------------------------------------------------- Versions
PYTHON_VERSION      = '3.7.7'
OPENSSL_VERSION     = '1.0.2r'
SIP_VERSION         = '4.19.24'
PYQT5_VERSION       = '5.15.1'
QT_VERSION          = '5.13.2'
PYQTDEPLOY_VERSION  = '2.5.1'

# Android targets
ANDROID_API_PLATFORM = 29              # used in AndroidManifest, androiddeployqt
ANDROID_ABI          = 'arm64-v8a'
QT_ARCH_SUBDIR       = 'android_arm64_v8a'
PYQTDEPLOY_TARGET    = 'android-64'
MIN_SDK_VERSION      = 21
TARGET_SDK_VERSION   = 28

# Defaults; overridable via CLI args.  Match the layout of the Dockerfile
# we ship (build venv at /root/build-venv, Qt under /root/Qt/5.13.2, etc.).
DEFAULT_VENV_DIR = '/root/build-venv'
DEFAULT_QT_DIR   = '/root/Qt/{0}'.format(QT_VERSION)
DEFAULT_NDK_DIR  = '/root/Android/android-ndk-r20b'
DEFAULT_SDK_DIR  = '/root/Android/tools'


# ----------------------------------------------------------------- Source URLs
# Multi-mirror with fallback.  The canonical hosts (pypi.org/packages/
# source/, openssl.org/source/old/) have removed several legacy tarballs.
SOURCE_URLS = {
    'Python-{0}.tgz'.format(PYTHON_VERSION): [
        'https://www.python.org/ftp/python/{0}/Python-{0}.tgz'.format(PYTHON_VERSION),
    ],
    'sip-{0}.tar.gz'.format(SIP_VERSION): [
        'https://distfiles.macports.org/py-sip/sip-{0}.tar.gz'.format(SIP_VERSION),
        'https://sourceforge.net/projects/pyqt/files/sip/sip-{0}/sip-{0}.tar.gz/download'.format(SIP_VERSION),
    ],
    'PyQt5-{0}.tar.gz'.format(PYQT5_VERSION): [
        'https://distfiles.macports.org/py-pyqt5/PyQt5-{0}.tar.gz'.format(PYQT5_VERSION),
    ],
    'openssl-{0}.tar.gz'.format(OPENSSL_VERSION): [
        'https://github.com/openssl/openssl/releases/download/OpenSSL_{0}/openssl-{1}.tar.gz'.format(
            OPENSSL_VERSION.replace('.', '_'), OPENSSL_VERSION),
        'https://distfiles.macports.org/openssl/openssl-{0}.tar.gz'.format(OPENSSL_VERSION),
    ],
}

# Minimum size (bytes) for each downloaded source.  Catches truncated downloads.
SOURCE_MIN_SIZE = {
    'Python-{0}.tgz'.format(PYTHON_VERSION):         15000000,   # ~22 MB
    'sip-{0}.tar.gz'.format(SIP_VERSION):              900000,   # ~1 MB
    'PyQt5-{0}.tar.gz'.format(PYQT5_VERSION):        2500000,    # ~3 MB
    'openssl-{0}.tar.gz'.format(OPENSSL_VERSION):    4000000,    # ~5 MB
}


# ----------------------------------------------------------------- Module defaults

# Default PyQt5 modules: covers a basic Widgets app.  Override with
# --pyqt5-modules.  Note: the builder also auto-detects modules actually
# imported in the user's source code (see Builder._detect_pyqt5_imports).
DEFAULT_PYQT5_MODULES = [
    'QtCore', 'QtGui', 'QtWidgets', 'QtNetwork',
    'QtPrintSupport', 'QtSvg',
]

# Default Python stdlib modules to freeze into the APK.  Comprehensive list
# to reduce ModuleNotFoundError surface at runtime -- pyqtdeploy only bundles
# modules listed here, so unlisted ones crash the app on first import.
#
# IMPORTANT: only modules whose C extensions either (a) have no external
# dependency or (b) depend on libraries we already cross-compile (OpenSSL,
# zlib).  Modules that need OTHER native libs MUST NOT be added here unless
# the lib is also cross-compiled and added to sysroot.json.
#
# Intentionally NOT in this list (need libs we don't provide):
#   sqlite3   -> needs libsqlite3      (header sqlite3.h missing from NDK)
#   _curses   -> needs ncurses
#   _tkinter  -> needs Tcl/Tk
#   _dbm      -> needs gdbm / ndbm
#   _gdbm     -> needs gdbm
#   _lzma     -> needs liblzma
#   _bz2      -> needs libbz2
#   readline  -> needs libreadline
#   nis       -> needs libnsl
#   ossaudiodev -> Linux-only audio (no Android equivalent)
#   spwd      -> needs Linux shadow passwd db (no Android equivalent)
#
# To enable any of the above, you would need to (1) add a cross-compile
# step to sysroot.json for the dependent library, and (2) add the module
# here.  This is beyond the scope of the default build.
DEFAULT_STDLIB_MODULES = [
    'abc', 'argparse', 'ast', 'atexit', 'base64', 'binascii', 'bisect',
    'calendar', 'cmath', 'cmd', 'code', 'codecs', 'codeop',
    'collections', 'collections.abc',
    'concurrent', 'concurrent.futures', 'configparser', 'contextlib',
    'contextvars', 'copy', 'copyreg', 'csv', 'ctypes',
    'datetime', 'decimal', 'difflib', 'dis', 'doctest',
    'email', 'email.charset', 'email.encoders', 'email.errors',
    'email.feedparser', 'email.generator', 'email.header',
    'email.iterators', 'email.message', 'email.mime', 'email.mime.text',
    'email.parser', 'email.policy', 'email.utils',
    'encodings', 'encodings.aliases', 'encodings.ascii', 'encodings.cp437',
    'encodings.latin_1', 'encodings.utf_8',
    'enum', 'errno',
    'fcntl', 'fnmatch', 'fractions', 'functools',
    'gc', 'getopt', 'getpass', 'gettext', 'glob', 'gzip',
    'hashlib', 'heapq', 'hmac', 'html', 'html.entities', 'html.parser',
    'http', 'http.client', 'http.cookies', 'http.server',
    'imghdr', 'imp', 'importlib', 'importlib.abc',
    'importlib.machinery', 'importlib.resources', 'importlib.util',
    'inspect', 'io', 'ipaddress', 'itertools',
    'json', 'json.decoder', 'json.encoder',
    'keyword', 'linecache', 'locale',
    'logging', 'logging.config', 'logging.handlers',
    'math', 'mimetypes',
    'numbers', 'operator', 'os', 'os.path',
    'pathlib', 'pickle', 'pickletools', 'pkgutil', 'platform',
    'posixpath', 'pprint', 'pty',
    'queue', 'quopri',
    'random', 're', 'reprlib', 'runpy',
    'secrets', 'select', 'selectors', 'shlex', 'shutil', 'signal',
    'site', 'smtplib', 'socket', 'socketserver', 'ssl',
    'stat', 'statistics', 'string', 'stringprep', 'struct', 'subprocess',
    'sys', 'sysconfig',
    'tarfile', 'telnetlib', 'tempfile', 'textwrap', 'threading', 'time',
    'timeit', 'token', 'tokenize', 'traceback', 'tty', 'types', 'typing',
    'unicodedata', 'unittest', 'unittest.mock',
    'urllib', 'urllib.error', 'urllib.parse', 'urllib.request',
    'urllib.response',
    'uu', 'uuid',
    'warnings', 'weakref', 'webbrowser', 'wsgiref',
    'xml', 'xml.dom', 'xml.dom.minidom', 'xml.etree',
    'xml.etree.ElementTree', 'xml.parsers.expat', 'xml.sax',
    'xmlrpc',
    'zipapp', 'zipfile', 'zipimport', 'zlib',
]

# Stdlib modules whose C extensions need external libraries not provided by
# our sysroot.  If the user's code imports any of these we fail at scan
# time with a clear actionable message, rather than letting them hit a
# confusing C compile error 25 minutes into the build.
PROBLEMATIC_STDLIB_MODULES = {
    'sqlite3':   'requires libsqlite3 (header sqlite3.h not in NDK)',
    '_sqlite3':  'requires libsqlite3 (header sqlite3.h not in NDK)',
    'curses':    'requires ncurses (not in NDK)',
    '_curses':   'requires ncurses (not in NDK)',
    'tkinter':   'requires Tcl/Tk (not in NDK)',
    '_tkinter':  'requires Tcl/Tk (not in NDK)',
    'dbm':       'requires gdbm/ndbm (not in NDK)',
    '_dbm':      'requires gdbm/ndbm (not in NDK)',
    '_gdbm':     'requires gdbm (not in NDK)',
    'lzma':      'requires liblzma (not in NDK)',
    '_lzma':     'requires liblzma (not in NDK)',
    'bz2':       'requires libbz2 (not in NDK)',
    '_bz2':      'requires libbz2 (not in NDK)',
    'readline':  'requires libreadline (not in NDK)',
    'nis':       'requires libnsl (not in NDK)',
    'ossaudiodev': 'Linux-only audio (no Android equivalent)',
    'spwd':      'requires Linux shadow passwd db (no Android equivalent)',
}

# Common package-content exclusions
PACKAGE_EXCLUDES = [
    '*.pyc', '*.pyd', '*.pyo', '*.pyx', '*.pxi',
    '__pycache__', '*-info', 'EGG_INFO', '*.so',
]


# ----------------------------------------------------------------- Stage list
# Plain list of (number, code, title) tuples.  We avoid enum.Enum here
# because the original kviktor reference does not use it either.

STAGES = [
    (1,  'VALIDATE',       'Validate environment'),
    (2,  'QUIRKS',         'Apply quirks (SDK source.properties, python3 symlink)'),
    (3,  'SCAN',           'Scan project + detect PyQt5 imports'),
    (4,  'SYSROOT_JSON',   'Generate sysroot.json'),
    (5,  'APP_PDY',        'Generate app.pdy'),
    (6,  'ANDROID_SOURCE', 'Generate android_source (manifest, activity)'),
    (7,  'SOURCES',        'Verify source tarballs (download if missing)'),
    (8,  'PYQTDEPLOY_SR',  'Build sysroot (pyqtdeploy-sysroot)'),
    (9,  'PYQTDEPLOY_B',   'Generate Qt sources (pyqtdeploy-build)'),
    (10, 'COMPILE',        'Compile (qmake + make + make install)'),
    (11, 'PACKAGE',        'Package APK (androiddeployqt --gradle)'),
    (12, 'LOCATE',         'Locate + verify APK'),
]
STAGE_BY_CODE = dict((code, (num, title)) for (num, code, title) in STAGES)


# ----------------------------------------------------------------- Embedded templates

ANDROID_MANIFEST_TEMPLATE = """\
<?xml version='1.0' encoding='utf-8'?>
<manifest package="{package_name}"
          xmlns:android="http://schemas.android.com/apk/res/android"
          android:versionName="1.0"
          android:versionCode="1"
          android:installLocation="auto">

    <uses-sdk android:minSdkVersion="{min_sdk}" android:targetSdkVersion="{target_sdk}"/>

    <!-- %%INSERT_PERMISSIONS -->
    <!-- %%INSERT_FEATURES -->

    <supports-screens android:largeScreens="true"
                      android:normalScreens="true"
                      android:anyDensity="true"
                      android:smallScreens="true"/>

    <application android:hardwareAccelerated="true"
                 android:name="org.qtproject.qt5.android.bindings.QtApplication"
                 android:label="{app_name}">
        <activity android:configChanges="orientation|uiMode|screenLayout|screenSize|smallestScreenSize|layoutDirection|locale|fontScale|keyboard|keyboardHidden|navigation|mcc|mnc|density"
                  android:name="{package_name}.{app_name}Activity"
                  android:label="{app_name}"
                  android:screenOrientation="unspecified"
                  android:launchMode="singleTop">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>

            <meta-data android:name="android.app.lib_name" android:value="-- %%INSERT_APP_LIB_NAME%% --"/>
            <meta-data android:name="android.app.qt_sources_resource_id" android:resource="@array/qt_sources"/>
            <meta-data android:name="android.app.repository" android:value="default"/>
            <meta-data android:name="android.app.qt_libs_resource_id" android:resource="@array/qt_libs"/>
            <meta-data android:name="android.app.bundled_libs_resource_id" android:resource="@array/bundled_libs"/>
            <meta-data android:name="android.app.bundle_local_qt_libs" android:value="-- %%BUNDLE_LOCAL_QT_LIBS%% --"/>
            <meta-data android:name="android.app.bundled_in_lib_resource_id" android:resource="@array/bundled_in_lib"/>
            <meta-data android:name="android.app.bundled_in_assets_resource_id" android:resource="@array/bundled_in_assets"/>
            <meta-data android:name="android.app.use_local_qt_libs" android:value="-- %%USE_LOCAL_QT_LIBS%% --"/>
            <meta-data android:name="android.app.libs_prefix" android:value="/data/local/tmp/qt/"/>
            <meta-data android:name="android.app.load_local_libs" android:value="-- %%INSERT_LOCAL_LIBS%% --"/>
            <meta-data android:name="android.app.load_local_jars" android:value="-- %%INSERT_LOCAL_JARS%% --"/>
            <meta-data android:name="android.app.static_init_classes" android:value="-- %%INSERT_INIT_CLASSES%% --"/>
            <meta-data android:name="android.app.background_running" android:value="false"/>
            <meta-data android:name="android.app.auto_screen_scale_factor" android:value="false"/>
            <meta-data android:name="android.app.extract_android_style" android:value="default"/>
        </activity>
    </application>
</manifest>
"""

ACTIVITY_JAVA_TEMPLATE = """\
package {package_name};

import android.os.Bundle;
import android.util.Log;

public class {app_name}Activity extends org.qtproject.qt5.android.bindings.QtActivity
{{
    private static final String TAG = "{app_name}Activity";

    public {app_name}Activity()
    {{
        super();
        Log.i(TAG, "{app_name}Activity ctor");
    }}

    @Override
    public void onCreate(Bundle savedInstanceState)
    {{
        Log.i(TAG, "onCreate");
        super.onCreate(savedInstanceState);
    }}
}}
"""

DEFAULT_MAIN_PY_TEMPLATE = '''\
# -*- coding: utf-8 -*-
"""
{app_name} - PyQt5 Hello World for Android
==========================================

Auto-generated by pyqt5_android_kviktor.py.  Replace this file with your
actual app code.  All output goes to stderr -- watch it on device with:
    adb logcat -v time python.stderr:V python.stdout:V *:S
"""
from __future__ import absolute_import, division, print_function

import sys
import traceback


def main():
    print("[{app_name}] starting", file=sys.stderr)
    try:
        from PyQt5.QtCore import Qt, QT_VERSION_STR
        from PyQt5.QtGui import QFont
        from PyQt5.QtWidgets import (
            QApplication, QLabel, QVBoxLayout, QWidget
        )

        print("[{app_name}] Qt {{0}} imported OK".format(QT_VERSION_STR),
              file=sys.stderr)

        app = QApplication(sys.argv)
        win = QWidget()
        win.setWindowTitle("{app_name}")

        layout = QVBoxLayout(win)

        title = QLabel("Hello from PyQt5!")
        title.setAlignment(Qt.AlignCenter)
        f = QFont()
        f.setPointSize(28)
        f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)

        info = QLabel(
            "App: {app_name}\\n"
            "Python: {{0}}\\n"
            "Qt: {{1}}".format(sys.version.split()[0], QT_VERSION_STR)
        )
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        win.show()
        print("[{app_name}] entering event loop", file=sys.stderr)
        sys.exit(app.exec_())

    except Exception as e:
        print("[{app_name}] FATAL: {{0}}: {{1}}".format(type(e).__name__, e),
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
'''


# ----------------------------------------------------------------- Logging

_log = logging.getLogger('pyqt5-builder')


def setup_logging(verbose):
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        '%(asctime)s  %(levelname)-7s  %(message)s', datefmt='%H:%M:%S'))
    _log.handlers = [handler]
    _log.setLevel(logging.DEBUG if verbose else logging.INFO)


def banner(stage_code):
    num, title = STAGE_BY_CODE[stage_code]
    line = '=' * 72
    _log.info('')
    _log.info(line)
    _log.info('  Step %d/12 - %s', num, title)
    _log.info(line)


# ----------------------------------------------------------------- Subprocess

class BuildError(RuntimeError):
    """Raised when a build step fails fatally."""
    pass


def _run(cmd, cwd=None, env=None, check=True, capture=False):
    """Run a subprocess.  cmd is a list (no shell=True nonsense).

    On non-zero exit (when check=True), raises BuildError with the command
    line and a tail of any captured output.

    Returns a SimpleProcess(returncode, stdout, stderr) for the caller's
    convenience (mirrors subprocess.CompletedProcess without requiring
    subprocess.run, which is Python 3.5+ only).
    """
    cmd_str = [str(x) for x in cmd]
    _log.debug('$ %s%s', ' '.join(cmd_str),
               ('  [cwd={0}]'.format(cwd)) if cwd else '')

    stdout_arg = subprocess.PIPE if capture else None
    stderr_arg = subprocess.PIPE if capture else None

    try:
        p = subprocess.Popen(
            cmd_str, cwd=cwd, env=env,
            stdout=stdout_arg, stderr=stderr_arg,
        )
    except OSError as e:
        raise BuildError(
            'Failed to start subprocess: {0}\n'
            '  Command: {1}\n'
            '  Error:   {2}'.format(cmd_str[0], ' '.join(cmd_str), e))

    stdout, stderr = p.communicate()

    # Decode bytes -> str for Python 3
    if stdout is not None and not isinstance(stdout, string_types):
        try:
            stdout = stdout.decode('utf-8', errors='replace')
        except AttributeError:
            pass
    if stderr is not None and not isinstance(stderr, string_types):
        try:
            stderr = stderr.decode('utf-8', errors='replace')
        except AttributeError:
            pass

    if check and p.returncode != 0:
        msg = ['Command failed (exit {0}):'.format(p.returncode),
               '  $ ' + ' '.join(cmd_str)]
        if cwd:
            msg.append('  cwd: {0}'.format(cwd))
        if stdout:
            msg.append('  stdout tail:\n' + stdout[-2000:])
        if stderr:
            msg.append('  stderr tail:\n' + stderr[-2000:])
        raise BuildError('\n'.join(msg))

    return SimpleProcess(p.returncode, stdout, stderr)


class SimpleProcess(object):
    """Stand-in for subprocess.CompletedProcess (which is Python 3.5+)."""
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout     = stdout
        self.stderr     = stderr


def _download(url, dest_path, min_size=1024, timeout=300):
    """Download `url` to `dest_path` using curl.  Returns True on success."""
    _log.info('  trying: %s', url)
    try:
        _run(
            ['curl', '-fL', '--retry', '3', '--retry-delay', '5',
             '--max-time', str(timeout), '-A', 'Mozilla/5.0',
             '-o', dest_path, url],
            check=True, capture=True,
        )
    except BuildError as e:
        _log.warning('  failed: %s', str(e).splitlines()[0])
        if exists(dest_path):
            os.remove(dest_path)
        return False

    if not exists(dest_path):
        return False
    sz = os.path.getsize(dest_path)
    if sz < min_size:
        _log.warning('  too small: %d bytes (need >= %d)', sz, min_size)
        os.remove(dest_path)
        return False
    with open(dest_path, 'rb') as f:
        sha = hashlib.sha256(f.read()).hexdigest()[:12]
    _log.info('  ok: %s (%d bytes, sha256=%s)', basename(dest_path), sz, sha)
    return True


def _download_with_fallbacks(urls, dest_path, min_size):
    """Try each URL until one works.  Raise BuildError if all fail."""
    if exists(dest_path) and os.path.getsize(dest_path) >= min_size:
        _log.info('  cached: %s (%d bytes)',
                  basename(dest_path), os.path.getsize(dest_path))
        return
    for url in urls:
        if _download(url, dest_path, min_size=min_size):
            return
    raise BuildError(
        'Failed to download {0} from any mirror:\n  {1}'.format(
            basename(dest_path), '\n  '.join('- ' + u for u in urls)))


def _makedirs(path):
    """mkdir -p compatible with Python 2.7+."""
    if not exists(path):
        os.makedirs(path)


def _write_text(path, content):
    """Write text to a file, parent dirs created automatically."""
    _makedirs(dirname(path))
    f = open(path, 'w')
    try:
        f.write(content)
    finally:
        f.close()


def _read_text(path):
    """Read text from a file (Python 2/3 compatible)."""
    f = open(path, 'r')
    try:
        return f.read()
    finally:
        f.close()


# ----------------------------------------------------------------- Config

class BuildConfig(object):
    """All build-time configuration in one place.  No dataclasses (Py 2 compat)."""

    def __init__(self,
                 project_dir, app_name, package_name,
                 jobs=2,
                 venv_dir=DEFAULT_VENV_DIR,
                 qt_dir=DEFAULT_QT_DIR,
                 ndk_dir=DEFAULT_NDK_DIR,
                 sdk_dir=DEFAULT_SDK_DIR,
                 output_dir=None,
                 pyqt5_modules=None,
                 stdlib_modules=None,
                 force=False,
                 verbose=False):
        self.project_dir   = abspath(project_dir)
        self.app_name      = app_name
        self.package_name  = package_name
        self.jobs          = jobs
        self.venv_dir      = venv_dir
        self.qt_dir        = qt_dir
        self.ndk_dir       = ndk_dir
        self.sdk_dir       = sdk_dir
        self.output_dir    = abspath(output_dir) if output_dir else join(self.project_dir, 'output')
        self.pyqt5_modules = list(pyqt5_modules) if pyqt5_modules else list(DEFAULT_PYQT5_MODULES)
        self.stdlib_modules = list(stdlib_modules) if stdlib_modules else list(DEFAULT_STDLIB_MODULES)
        self.force         = force
        self.verbose       = verbose

    # ----- derived paths -----
    @property
    def sources_dir(self):
        return join(self.project_dir, 'sources')

    @property
    def android_source_dir(self):
        return join(self.project_dir, 'android_source')

    @property
    def sysroot_json(self):
        return join(self.project_dir, 'sysroot.json')

    @property
    def app_pdy(self):
        return join(self.project_dir, 'app.pdy')

    @property
    def sysroot_dir(self):
        return join(self.project_dir, 'sysroot-{0}'.format(PYQTDEPLOY_TARGET))

    @property
    def build_dir(self):
        return join(self.project_dir, 'build-{0}'.format(PYQTDEPLOY_TARGET))

    @property
    def qt_arch_dir(self):
        return join(self.qt_dir, QT_ARCH_SUBDIR)

    @property
    def main_py(self):
        return join(self.project_dir, 'main.py')

    # ----- tools -----
    @property
    def qmake_exe(self):
        return join(self.qt_arch_dir, 'bin', 'qmake')

    @property
    def androiddeployqt_exe(self):
        return join(self.qt_arch_dir, 'bin', 'androiddeployqt')

    @property
    def pyqtdeploy_sysroot_exe(self):
        return join(self.venv_dir, 'bin', 'pyqtdeploy-sysroot')

    @property
    def pyqtdeploy_build_exe(self):
        return join(self.venv_dir, 'bin', 'pyqtdeploy-build')

    # ----- env -----
    def build_env(self):
        """Environment dict for spawned subprocesses (qmake, make, etc.)."""
        e = os.environ.copy()
        e['ANDROID_SDK_ROOT']     = self.sdk_dir
        e['ANDROID_NDK_ROOT']     = self.ndk_dir
        e['ANDROID_NDK_PLATFORM'] = 'android-{0}'.format(ANDROID_API_PLATFORM)
        e['APP_DIR']              = self.project_dir
        e['SYSROOT']              = self.sysroot_dir
        path_parts = [
            join(self.venv_dir, 'bin'),
            join(self.qt_arch_dir, 'bin'),
            join(self.sdk_dir, 'platform-tools'),
            join(self.ndk_dir, 'toolchains', 'llvm', 'prebuilt',
                 'linux-x86_64', 'bin'),
            e.get('PATH', ''),
        ]
        e['PATH'] = ':'.join(path_parts)
        return e


# ----------------------------------------------------------------- Builder

class Builder(object):
    """The build orchestrator.  Each stage is a separate method."""

    def __init__(self, cfg):
        self.cfg = cfg

    # ===== stage 1 ============================================ validate

    def stage_validate(self):
        banner('VALIDATE')
        cfg = self.cfg
        problems = []

        # Python version
        py_major, py_minor = sys.version_info[:2]
        if (py_major, py_minor) != (3, 7):
            problems.append(
                'Builder runs on Python {0}.{1}, but pyqtdeploy 2.5.1 '
                'needs 3.7.  Run via python3.7.'.format(py_major, py_minor))
        _log.info('  Python: %s', sys.version.split()[0])

        # Check tool paths
        checks = [
            ('venv',               cfg.venv_dir),
            ('Qt',                 cfg.qt_dir),
            ('Qt arch dir',        cfg.qt_arch_dir),
            ('qmake',              cfg.qmake_exe),
            ('androiddeployqt',    cfg.androiddeployqt_exe),
            ('NDK',                cfg.ndk_dir),
            ('SDK',                cfg.sdk_dir),
            ('pyqtdeploy-sysroot', cfg.pyqtdeploy_sysroot_exe),
            ('pyqtdeploy-build',   cfg.pyqtdeploy_build_exe),
        ]
        for label, path in checks:
            ok = exists(path)
            _log.info('  %-22s: %s  %s',
                      label, 'OK ' if ok else 'MISSING',
                      '' if ok else path)
            if not ok:
                problems.append('{0} not found at {1}'.format(label, path))

        # Disk space
        try:
            check_dir = dirname(cfg.project_dir) if exists(cfg.project_dir) else '/tmp'
            try:
                # Python 3.3+
                ds = shutil.disk_usage(check_dir)
                free_gb = ds.free / (1024.0 ** 3)
            except AttributeError:
                # Python 2 fallback
                st = os.statvfs(check_dir)
                free_gb = (st.f_bavail * st.f_frsize) / (1024.0 ** 3)
            _log.info('  Disk free: %.1f GB', free_gb)
            if free_gb < 5:
                problems.append(
                    'Only {0:.1f} GB free disk -- need >= 5 GB.'.format(free_gb))
        except Exception as e:
            _log.warning('  Could not check disk space: %s', e)

        # NDK quirk: warn if API-platform sysroot dir is missing
        ndk_platform_dir = join(
            cfg.ndk_dir, 'platforms',
            'android-{0}'.format(ANDROID_API_PLATFORM))
        if not exists(ndk_platform_dir):
            _log.warning('  NDK platform dir not found at %s '
                         '(may be OK with unified sysroot)', ndk_platform_dir)

        if problems:
            raise BuildError(
                'Environment validation failed:\n  - ' +
                '\n  - '.join(problems))
        _log.info('  ok: environment validated')

    # ===== stage 2 ============================================ quirks

    def stage_apply_quirks(self):
        """Fix known pyqtdeploy 2.5.1 + Android-SDK-layout incompatibilities."""
        banner('QUIRKS')
        cfg = self.cfg

        # Quirk 1: pyqtdeploy 2.5.1 (platforms.py:508) expects
        #     $ANDROID_SDK_ROOT/tools/source.properties
        # The legacy "Android SDK Tools" package was removed by Google in
        # 2021 and replaced with cmdline-tools, so modern SDKs don't ship
        # the file pyqtdeploy reads for its SDK version check.  Stub it.
        legacy_tools = join(cfg.sdk_dir, 'tools')
        source_props = join(legacy_tools, 'source.properties')
        if not exists(source_props):
            _makedirs(legacy_tools)
            _write_text(source_props,
                'Pkg.UserSrc=false\n'
                'Pkg.Revision=26.1.1\n'
                'Pkg.Path=tools\n'
                'Pkg.Desc=Android SDK Tools\n')
            _log.info('  created stub %s', source_props)
        else:
            _log.info('  ok: %s exists', source_props)

        # Quirk 2: pyqtdeploy expects `python3` on PATH (without version
        # suffix) when build_host_from_source=false.  Our image may only
        # have python3.7 (compiled from source) without a python3 symlink.
        python3_target = '/usr/local/bin/python3'
        python37       = '/usr/local/bin/python3.7'
        if exists(python37) and not exists(python3_target):
            try:
                os.symlink(python37, python3_target)
                _log.info('  symlinked %s -> %s', python3_target, python37)
            except OSError as e:
                _log.warning('  could not create python3 symlink: %s', e)
        else:
            which_python3 = self._which('python3')
            _log.info('  ok: python3 at %s', which_python3 or '(not on PATH)')

    @staticmethod
    def _which(name):
        """shutil.which() backport for Python 2.  Returns None if not found."""
        path_env = os.environ.get('PATH', '')
        for d in path_env.split(os.pathsep):
            candidate = join(d, name)
            if exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    # ===== stage 3 ============================================ scan project

    def _detect_pyqt5_imports(self, files):
        """Parse all .py files in the project to detect PyQt5 submodule
        imports.  Returns a set of submodule names actually used
        (e.g. {'QtCore', 'QtWidgets', 'QtQml'}).

        This is the #1 win for crash prevention: if the user's code does
            from PyQt5.QtQml import QQmlEngine
        but QtQml isn't in --pyqt5-modules, the APK is missing the binding
        and crashes on import.  We catch that BEFORE the 30-minute sysroot
        build, not when the user is staring at adb logcat at 2am.
        """
        cfg = self.cfg
        found = set()
        for relpath, isdir_ in files:
            if isdir_ or not relpath.endswith('.py'):
                continue
            fpath = join(cfg.project_dir, relpath)
            try:
                source_text = _read_text(fpath)
                tree = ast.parse(source_text, filename=fpath)
            except SyntaxError as e:
                _log.warning('    [import-scan] syntax error in %s: %s',
                             relpath, e)
                continue
            except (IOError, OSError) as e:
                _log.warning('    [import-scan] could not read %s: %s',
                             relpath, e)
                continue
            for node in ast.walk(tree):
                # `from PyQt5.QtCore import ...`
                if isinstance(node, ast.ImportFrom):
                    mod = node.module or ''
                    if mod == 'PyQt5':
                        # `from PyQt5 import QtCore, QtGui`
                        for alias in node.names:
                            if alias.name.startswith('Qt'):
                                found.add(alias.name)
                    elif mod.startswith('PyQt5.'):
                        parts = mod.split('.')
                        if len(parts) >= 2 and parts[1].startswith('Qt'):
                            found.add(parts[1])
                # `import PyQt5.QtCore`
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('PyQt5.'):
                            parts = alias.name.split('.')
                            if len(parts) >= 2 and parts[1].startswith('Qt'):
                                found.add(parts[1])
        return found

    def _detect_problematic_stdlib_imports(self, files):
        """Scan .py files for imports of stdlib modules that won't build on
        Android (need libs the NDK doesn't provide).  Returns dict mapping
        module name -> list of (relpath, lineno) tuples.
        """
        cfg = self.cfg
        problems = {}   # module_name -> [(relpath, lineno), ...]
        for relpath, isdir_ in files:
            if isdir_ or not relpath.endswith('.py'):
                continue
            fpath = join(cfg.project_dir, relpath)
            try:
                source_text = _read_text(fpath)
                tree = ast.parse(source_text, filename=fpath)
            except (SyntaxError, IOError, OSError):
                continue
            for node in ast.walk(tree):
                # Resolve every import to its top-level module name
                names_to_check = []
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        names_to_check.append(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        names_to_check.append(node.module.split('.')[0])
                for n in names_to_check:
                    if n in PROBLEMATIC_STDLIB_MODULES:
                        problems.setdefault(n, []).append(
                            (relpath, getattr(node, 'lineno', '?')))
        return problems

    def stage_scan(self):
        """Scan project_dir for source files.  Returns metadata used by
        subsequent stages.  Also auto-detects which PyQt5 submodules the
        user's code actually imports -- catches the #1 cause of on-device
        crashes (missing module bundled in the APK)."""
        banner('SCAN')
        cfg = self.cfg

        if not exists(cfg.project_dir):
            _makedirs(cfg.project_dir)
            _log.info('  created project dir: %s', cfg.project_dir)

        # Find main.py (required for entry point).  If absent, write a
        # Hello-World template.
        if not exists(cfg.main_py):
            _log.warning('  no main.py found -- creating a Hello-World template')
            _write_text(cfg.main_py,
                DEFAULT_MAIN_PY_TEMPLATE.format(app_name=cfg.app_name))
            _log.info('  wrote %s', cfg.main_py)
        else:
            _log.info('  main.py: %s', cfg.main_py)

        SKIP_DIRS = set([
            'sources', 'android_source', '__pycache__', '.git',
            '.github', '.idea', '.vscode', 'venv', '.venv', 'output',
            'build-{0}'.format(PYQTDEPLOY_TARGET),
            'sysroot-{0}'.format(PYQTDEPLOY_TARGET),
        ])
        WANTED_EXTS = set(['.py', '.qml', '.ui', '.json', '.txt',
                           '.qrc', '.cfg', '.ini'])
        SKIPPED_EXTS = set(['.pyc', '.pyo', '.pdy'])

        files = []   # list of (relpath, is_dir)

        def _scan(dirpath, rel):
            try:
                items = sorted(os.listdir(dirpath))
            except OSError:
                return
            for name in items:
                if name.startswith('.'):
                    continue
                if name in SKIP_DIRS:
                    continue
                full = join(dirpath, name)
                rel_name = (rel + '/' + name) if rel else name
                if isfile(full):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in SKIPPED_EXTS:
                        continue
                    if ext in WANTED_EXTS:
                        files.append((rel_name, False))
                elif isdir(full):
                    files.append((rel_name, True))
                    _scan(full, rel_name)

        _scan(cfg.project_dir, '')

        _log.info('  found %d source paths under %s:', len(files), cfg.project_dir)
        for path, is_dir in files[:20]:
            _log.info('    %s%s', '[d] ' if is_dir else '    ', path)
        if len(files) > 20:
            _log.info('    ... and %d more', len(files) - 20)

        # ---- detect problematic stdlib imports (fail fast) ----
        bad_stdlib = self._detect_problematic_stdlib_imports(files)
        if bad_stdlib:
            msg_lines = [
                '',
                'Your code imports stdlib modules that cannot be cross-',
                'compiled for Android with the default sysroot (NDK does',
                'not ship the required headers / libraries):',
                '',
            ]
            for mod in sorted(bad_stdlib.keys()):
                reason = PROBLEMATIC_STDLIB_MODULES[mod]
                msg_lines.append('  - {0:12s}  {1}'.format(mod, reason))
                for relpath, lineno in bad_stdlib[mod][:5]:
                    msg_lines.append('      imported in {0}:{1}'.format(
                        relpath, lineno))
            msg_lines.append('')
            msg_lines.append('To proceed, either:')
            msg_lines.append('  (a) remove these imports from your code')
            msg_lines.append('  (b) add cross-compile steps for the required')
            msg_lines.append('      native libs to sysroot.json (advanced).')
            raise BuildError('\n'.join(msg_lines))

        # ---- auto-detect PyQt5 imports ----
        detected = self._detect_pyqt5_imports(files)
        if detected:
            _log.info('  detected PyQt5 modules in source: %s',
                      sorted(detected))
            current = set(cfg.pyqt5_modules)
            missing = detected - current
            extra   = current - detected
            if missing:
                _log.warning('  ! these PyQt5 modules are USED but not in '
                             '--pyqt5-modules: %s', sorted(missing))
                _log.warning('  ! auto-adding them now to prevent on-device '
                             'ImportError crashes')
                cfg.pyqt5_modules = sorted(current | missing)
                _log.warning('  ! updated --pyqt5-modules to: %s',
                             cfg.pyqt5_modules)
                _log.warning('  ! NOTE: if the sysroot was previously built '
                             'without these, --force is needed to rebuild it')
            if extra:
                _log.info('  (note: %s in --pyqt5-modules but unused in source '
                          '-- harmless, just extra APK size)', sorted(extra))
        else:
            _log.info('  no PyQt5 imports detected (may be intentional)')

        return {'files': files, 'detected_pyqt5_modules': detected}

    # ===== stage 4 ============================================ sysroot.json

    def stage_sysroot_json(self):
        banner('SYSROOT_JSON')
        cfg = self.cfg

        if exists(cfg.sysroot_json) and not cfg.force:
            _log.info('  exists: %s (use --force to regenerate)',
                      cfg.sysroot_json)
            return

        # Order matters!  pyqtdeploy iterates components in JSON-key order
        # and components must come AFTER their dependencies:
        #     openssl  -- independent
        #     qt5      -- independent
        #     python   -- depends on openssl (for ssl module)
        #     sip      -- depends on python
        #     pyqt5    -- depends on sip + qt5
        # We use OrderedDict to make insertion order explicit (works in both
        # Python 2.7 and Python 3.7+).
        from collections import OrderedDict
        sysroot = OrderedDict()
        sysroot['Description'] = (
            'PyQt5 {0} sysroot for Android arm64, built by '
            'pyqt5_android_kviktor.py'.format(PYQT5_VERSION))
        sysroot['android|macos|win#openssl'] = OrderedDict([
            ('android#source', 'openssl-{0}.tar.gz'.format(OPENSSL_VERSION)),
            ('no_asm',         True),
        ])
        sysroot['qt5'] = OrderedDict([
            ('android-32#qt_dir', 'android_armv7'),
            ('android-64#qt_dir', QT_ARCH_SUBDIR),
            ('edition',           'opensource'),
            ('android|linux#ssl', 'openssl-runtime'),
        ])
        sysroot['python'] = OrderedDict([
            ('build_host_from_source',   False),
            ('build_target_from_source', True),
            ('source',                   'Python-{0}.tgz'.format(PYTHON_VERSION)),
        ])
        sysroot['sip'] = OrderedDict([
            ('module_name', 'PyQt5.sip'),
            ('source',      'sip-{0}.tar.gz'.format(SIP_VERSION)),
        ])
        sysroot['pyqt5'] = OrderedDict([
            ('android#disabled_features', [
                'PyQt_Desktop_OpenGL', 'PyQt_Printer', 'PyQt_PrintDialog',
                'PyQt_PrintPreviewDialog', 'PyQt_PrintPreviewWidget',
            ]),
            ('android#modules', list(cfg.pyqt5_modules)),
            ('source',          'PyQt5-{0}.tar.gz'.format(PYQT5_VERSION)),
        ])

        f = open(cfg.sysroot_json, 'w')
        try:
            json.dump(sysroot, f, indent=4, sort_keys=False)
        finally:
            f.close()
        _log.info('  wrote %s', cfg.sysroot_json)
        _log.info('  components: %s',
                  [k for k in sysroot.keys() if k != 'Description'])

    # ===== stage 5 ============================================ app.pdy

    def _build_package_contents(self, parent_el, files, parent_path=''):
        """Convert a flat list of (relpath, is_dir) tuples into nested
        <PackageContent> XML elements."""
        children = {}   # name -> {'isdir': bool, 'sub': [tuple, ...]}
        for path, isdir_ in files:
            if not path.startswith(parent_path):
                continue
            rel = path[len(parent_path):].lstrip('/')
            if not rel:
                continue
            first = rel.split('/', 1)[0]
            if first not in children:
                children[first] = {'isdir': False, 'sub': []}
            if '/' in rel:
                children[first]['isdir'] = True
                children[first]['sub'].append((path, isdir_))
            else:
                children[first]['isdir'] = isdir_
        for name in sorted(children.keys()):
            info = children[name]
            el = ET.SubElement(
                parent_el, 'PackageContent',
                included='1',
                isdirectory='1' if info['isdir'] else '0',
                name=name,
            )
            if info['isdir']:
                full = (parent_path.rstrip('/') + '/' + name) if parent_path else name
                self._build_package_contents(el, info['sub'], full)

    def stage_app_pdy(self, scan_result):
        banner('APP_PDY')
        cfg = self.cfg

        if exists(cfg.app_pdy) and not cfg.force:
            _log.info('  exists: %s (use --force to regenerate)', cfg.app_pdy)
            return

        root = ET.Element('Project', usingdefaultlocations='1', version='7')

        ET.SubElement(root, 'Python',
                      major='3', minor='7', patch='7', platformpython='')

        app_el = ET.SubElement(
            root, 'Application',
            entrypoint='', isbundle='0', isconsole='0', ispyqt5='1',
            name='', script='$APP_DIR/main.py', syspath='',
        )

        qm = ET.SubElement(app_el, 'QMakeConfiguration')
        # Flat layout: main.py and android_source/ both inside the project
        # dir, so APP_DIR == project_dir.  No '..' needed.
        qm.text = 'ANDROID_PACKAGE_SOURCE_DIR="$$(APP_DIR)/android_source"'

        app_pkg = ET.SubElement(app_el, 'Package', name='')
        for excl in PACKAGE_EXCLUDES:
            ET.SubElement(app_pkg, 'Exclude', name=excl)

        for mod in cfg.pyqt5_modules:
            ET.SubElement(root, 'PyQtModule', name=mod)

        for mod in cfg.stdlib_modules:
            ET.SubElement(root, 'StdlibModule', name=mod)

        ET.SubElement(root, 'ExternalLib', target='android', name='zlib',
                      defines='', includepath='', libs='-lz')
        ET.SubElement(root, 'ExternalLib', target='ios', name='ssl',
                      defines='', includepath='', libs='')

        other_pkg = ET.SubElement(root, 'Package', name='$APP_DIR')
        files = scan_result.get('files', [])
        self._build_package_contents(other_pkg, files)
        for excl in PACKAGE_EXCLUDES:
            ET.SubElement(other_pkg, 'Exclude', name=excl)

        # Serialise pretty-printed.  minidom.parseString() accepts bytes
        # without an XML declaration just fine; toprettyxml(encoding=...)
        # adds the declaration in the output.
        raw = ET.tostring(root, encoding='utf-8')
        pretty = minidom.parseString(raw).toprettyxml(
            indent='    ', encoding='utf-8')
        f = open(cfg.app_pdy, 'wb')
        try:
            f.write(pretty)
        finally:
            f.close()
        _log.info('  wrote %s', cfg.app_pdy)

        # Self-validate: re-parse and check root tag
        parsed = ET.parse(cfg.app_pdy).getroot()
        if parsed.tag != 'Project':
            raise BuildError(
                'app.pdy validation failed: root tag is {0!r}, expected '
                "'Project'".format(parsed.tag))
        if parsed.get('version') != '7':
            raise BuildError(
                'app.pdy validation failed: version is {0!r}, expected '
                "'7'".format(parsed.get('version')))
        _log.info('  validated: root=Project version=7')

    # ===== stage 6 ============================================ android_source

    def stage_android_source(self):
        banner('ANDROID_SOURCE')
        cfg = self.cfg

        _makedirs(cfg.android_source_dir)

        # AndroidManifest.xml
        manifest_dest = join(cfg.android_source_dir, 'AndroidManifest.xml')
        if not exists(manifest_dest) or cfg.force:
            _write_text(manifest_dest, ANDROID_MANIFEST_TEMPLATE.format(
                package_name=cfg.package_name,
                app_name=cfg.app_name,
                min_sdk=MIN_SDK_VERSION,
                target_sdk=TARGET_SDK_VERSION,
            ))
            _log.info('  wrote %s', manifest_dest)
        else:
            _log.info('  exists: %s', manifest_dest)

        # Activity.java -- nested under src/<package_path>/<App>Activity.java
        pkg_path = cfg.package_name.replace('.', '/')
        java_dir = join(cfg.android_source_dir, 'src', pkg_path)
        _makedirs(java_dir)
        activity_dest = join(java_dir, '{0}Activity.java'.format(cfg.app_name))
        if not exists(activity_dest) or cfg.force:
            _write_text(activity_dest, ACTIVITY_JAVA_TEMPLATE.format(
                package_name=cfg.package_name,
                app_name=cfg.app_name,
            ))
            _log.info('  wrote %s', activity_dest)
        else:
            _log.info('  exists: %s', activity_dest)

        # res/values dir (resources auto-generated by androiddeployqt)
        _makedirs(join(cfg.android_source_dir, 'res', 'values'))

    # ===== stage 7 ============================================ source tarballs

    def stage_sources(self):
        banner('SOURCES')
        cfg = self.cfg
        _makedirs(cfg.sources_dir)

        for filename in sorted(SOURCE_URLS.keys()):
            urls   = SOURCE_URLS[filename]
            dest   = join(cfg.sources_dir, filename)
            min_sz = SOURCE_MIN_SIZE.get(filename, 1024)
            _log.info('  %s ...', filename)
            _download_with_fallbacks(urls, dest, min_sz)

    # ===== stage 8 ============================================ pyqtdeploy-sysroot

    @staticmethod
    def _find_libpython(sysroot_dir):
        """Return the path to a libpython*.{a,so} in the sysroot, or None.

        pyqtdeploy 2.5.1's Python plugin may produce any of:
            lib/libpython3.7m.a   (traditional, with pymalloc 'm' suffix)
            lib/libpython3.7.a    (when pymalloc disabled)
            lib/libpython3.7m.so  (shared variant)
            lib/libpython3.7.so   (shared, no 'm')
        We check all of them.  Also searches the host/ subdir as a sanity
        backstop (some pyqtdeploy variants stage there).
        """
        import glob as _glob
        candidates = []
        for base in (sysroot_dir, join(sysroot_dir, 'host')):
            for sub in ('lib', 'lib64'):
                lib_dir = join(base, sub)
                if not exists(lib_dir):
                    continue
                for pat in ('libpython3.7m.a', 'libpython3.7.a',
                            'libpython3.7m.so', 'libpython3.7.so',
                            'libpython3.7*.a', 'libpython3.7*.so',
                            'libpython3*.a',   'libpython3*.so'):
                    matches = sorted(_glob.glob(join(lib_dir, pat)))
                    for m in matches:
                        if m not in candidates:
                            candidates.append(m)
        return candidates[0] if candidates else None

    def stage_pyqtdeploy_sysroot(self):
        banner('PYQTDEPLOY_SR')
        cfg = self.cfg

        # Sysroot is huge (~600 MB).  If it already exists and looks built
        # (has any libpython static or shared lib), skip.
        existing = self._find_libpython(cfg.sysroot_dir)
        if existing and not cfg.force:
            _log.info('  sysroot exists with libpython at %s -- skipping',
                      existing)
            _log.info('  (use --force to rebuild)')
            return

        cmd = [
            cfg.pyqtdeploy_sysroot_exe,
            '--target',     PYQTDEPLOY_TARGET,
            '--source-dir', cfg.sources_dir,
            '--source-dir', cfg.qt_dir,
            '--sysroot',    cfg.sysroot_dir,
            '--verbose',
            cfg.sysroot_json,
        ]
        _log.info('  this typically takes 20-40 minutes on first run')
        _run(cmd, cwd=cfg.project_dir, env=cfg.build_env())

        # Post-condition: look for ANY libpython variant.
        libpython = self._find_libpython(cfg.sysroot_dir)
        if not libpython:
            # Detailed diagnostic so the user can see what actually got built.
            msg = ['pyqtdeploy-sysroot reported success but no libpython*.{a,so} '
                   'was found.']
            for base, label in [(cfg.sysroot_dir, 'sysroot'),
                                (join(cfg.sysroot_dir, 'host'), 'sysroot/host')]:
                if not exists(base):
                    msg.append('  {0} ({1}): does not exist'.format(label, base))
                    continue
                msg.append('  {0} ({1}):'.format(label, base))
                for sub in ('lib', 'lib64', 'bin', 'include'):
                    sub_dir = join(base, sub)
                    if exists(sub_dir):
                        try:
                            items = sorted(os.listdir(sub_dir))[:30]
                        except OSError as e:
                            items = ['(could not list: {0})'.format(e)]
                        msg.append('    {0}/  ({1} items)'.format(
                            sub, len(items)))
                        for it in items[:15]:
                            msg.append('      ' + it)
                        if len(items) >= 30:
                            msg.append('      ... (truncated)')
            msg.append('')
            msg.append('Note: this often indicates the cross-compile of Python '
                       'failed but pyqtdeploy did not propagate the error.  Try '
                       '--force --verbose for a clean rebuild with full logs.')
            raise BuildError('\n'.join(msg))
        _log.info('  ok: sysroot built (libpython at %s)', libpython)

    # ===== stage 9 ============================================ pyqtdeploy-build

    @staticmethod
    def _find_pro_file(build_dir):
        """Return the path to any *.pro file in the build dir, or None.

        pyqtdeploy 2.5.1 names the .pro after the Application's name
        attribute in the .pdy (or 'app', 'pyqtdeploy', or some other
        default when name is empty).  The actual name doesn't matter --
        qmake picks up whatever .pro is in cwd.  We just need to verify
        that pyqtdeploy-build actually produced one.
        """
        import glob as _glob
        if not exists(build_dir):
            return None
        matches = sorted(_glob.glob(join(build_dir, '*.pro')))
        return matches[0] if matches else None

    def stage_pyqtdeploy_build(self):
        banner('PYQTDEPLOY_B')
        cfg = self.cfg

        cmd = [
            cfg.pyqtdeploy_build_exe,
            '--target',    PYQTDEPLOY_TARGET,
            '--build-dir', cfg.build_dir,
            '--no-clean',
            '--verbose',
            cfg.app_pdy,
        ]
        _run(cmd, cwd=cfg.project_dir, env=cfg.build_env())

        # Post-condition: pyqtdeploy-build should have produced a .pro file
        # in the build dir.  The actual filename varies (depends on the
        # Application name in the .pdy), so we glob for any .pro file.
        pro_file = self._find_pro_file(cfg.build_dir)
        if not pro_file:
            # Diagnostic dump
            msg = ['pyqtdeploy-build reported success but no .pro file was '
                   'found in {0}.'.format(cfg.build_dir)]
            if exists(cfg.build_dir):
                try:
                    items = sorted(os.listdir(cfg.build_dir))[:40]
                except OSError as e:
                    items = ['(could not list: {0})'.format(e)]
                msg.append('  Contents of build dir ({0} items):'.format(
                    len(items)))
                for it in items:
                    full = join(cfg.build_dir, it)
                    if isdir(full):
                        msg.append('    [d] ' + it + '/')
                    else:
                        msg.append('        ' + it)
            else:
                msg.append('  Build dir does not exist!')
            msg.append('')
            msg.append('Note: pyqtdeploy-build normally produces *.pro, '
                       'main.cpp, resources/, and similar files.  If they '
                       'are missing, the .pdy may be malformed or the '
                       'sysroot incomplete.  Try --force --verbose.')
            raise BuildError('\n'.join(msg))
        _log.info('  ok: build dir populated at %s', cfg.build_dir)
        _log.info('  ok: project file: %s', pro_file)

    # ===== stage 10 =========================================== compile

    @staticmethod
    def _find_libmain(build_dir):
        """Return the path to the main shared library in the build dir.

        qmake produces lib<TARGET>.so where TARGET is derived from the
        Application name in the .pdy.  With our empty name='' default,
        it is 'libmain.so', but a customised .pdy could produce something
        else.  We check for the conventional name first, then glob.
        """
        import glob as _glob
        canonical = join(build_dir, 'libmain.so')
        if exists(canonical):
            return canonical
        for pat in ('lib*.so',):
            matches = sorted(_glob.glob(join(build_dir, pat)))
            # Exclude obvious non-app libs (Qt copies, runtime, etc.)
            for m in matches:
                name = basename(m)
                if any(name.startswith(p) for p in ('libQt5', 'libc++',
                        'libssl', 'libcrypto', 'libgdbserver')):
                    continue
                return m
        return None

    @staticmethod
    def _find_deployment_settings(build_dir):
        """Return the path to androiddeployqt's input JSON, or None.

        Filename is android-<libname>.so-deployment-settings.json where
        libname matches the qmake TARGET.  We accept any variant.
        """
        import glob as _glob
        if not exists(build_dir):
            return None
        # Most specific first
        for pat in ('android-libmain.so-deployment-settings.json',
                    'android-lib*.so-deployment-settings.json',
                    'android-*deployment-settings.json',
                    '*deployment-settings.json'):
            matches = sorted(_glob.glob(join(build_dir, pat)))
            if matches:
                return matches[0]
        return None

    def stage_compile(self):
        banner('COMPILE')
        cfg = self.cfg
        env = cfg.build_env()

        # qmake
        _log.info('  [qmake] configuring native build ...')
        _run([cfg.qmake_exe], cwd=cfg.build_dir, env=env)
        if not exists(join(cfg.build_dir, 'Makefile')):
            raise BuildError('qmake did not produce a Makefile')

        # make
        _log.info('  [make -j%d] compiling app shared library ...', cfg.jobs)
        _run(['make', '-j{0}'.format(cfg.jobs)], cwd=cfg.build_dir, env=env)
        libmain = self._find_libmain(cfg.build_dir)
        if not libmain:
            # Diagnostic
            try:
                so_files = sorted([f for f in os.listdir(cfg.build_dir)
                                   if f.endswith('.so')])
            except OSError:
                so_files = []
            raise BuildError(
                'make completed but no app shared library (lib*.so) was '
                'found in {0}.  Found .so files: {1}'.format(
                    cfg.build_dir, so_files or '(none)'))
        sz = os.path.getsize(libmain)
        _log.info('  ok: %s produced (%.1f MB)',
                  basename(libmain), sz / (1024.0 * 1024.0))

        # make install INSTALL_ROOT=app
        install_root = join(cfg.build_dir, 'app')
        _log.info('  [make install] staging into %s ...', install_root)
        _run(['make', 'install', 'INSTALL_ROOT={0}'.format(install_root)],
             cwd=cfg.build_dir, env=env)

    # ===== stage 11 =========================================== package APK

    def stage_package(self):
        banner('PACKAGE')
        cfg = self.cfg

        deployment_json = self._find_deployment_settings(cfg.build_dir)
        if not deployment_json:
            # Diagnostic
            try:
                json_files = sorted([f for f in os.listdir(cfg.build_dir)
                                     if f.endswith('.json')])
            except OSError:
                json_files = []
            raise BuildError(
                'deployment settings JSON not found in {0}.  Found .json '
                'files: {1}'.format(cfg.build_dir, json_files or '(none)'))
        _log.info('  using deployment settings: %s', deployment_json)

        cmd = [
            cfg.androiddeployqt_exe,
            '--gradle',
            '--android-platform', str(ANDROID_API_PLATFORM),
            '--input',  deployment_json,
            '--output', join(cfg.build_dir, 'app'),
            '--verbose',
        ]
        _run(cmd, cwd=cfg.build_dir, env=cfg.build_env())

    # ===== stage 12 =========================================== locate APK

    def stage_locate_apk(self):
        banner('LOCATE')
        cfg = self.cfg

        # Conventional path produced by Gradle assembleDebug
        canonical = join(
            cfg.build_dir, 'app', 'build', 'outputs', 'apk',
            'debug', 'app-debug.apk')
        candidates = []
        if exists(canonical):
            candidates.append(canonical)
        # Fallback: walk the build dir for any .apk
        for root_dir, dirs, names in os.walk(cfg.build_dir):
            for name in names:
                if name.lower().endswith('.apk'):
                    p = join(root_dir, name)
                    if p not in candidates:
                        candidates.append(p)

        if not candidates:
            outputs_dir = join(cfg.build_dir, 'app', 'build', 'outputs')
            if exists(outputs_dir):
                _log.error('Contents of %s:', outputs_dir)
                for root_dir, dirs, names in os.walk(outputs_dir):
                    for n in names:
                        _log.error('  %s', join(root_dir, n))
            raise BuildError(
                'no APK found anywhere under {0}'.format(cfg.build_dir))

        apk = candidates[0]
        sz  = os.path.getsize(apk)
        with open(apk, 'rb') as f:
            sha = hashlib.sha256(f.read()).hexdigest()

        # Validate it's a real ZIP
        try:
            zf = zipfile.ZipFile(apk, 'r')
            try:
                names = zf.namelist()
            finally:
                zf.close()
            has_dex      = any(n == 'classes.dex' for n in names)
            has_manifest = any(n == 'AndroidManifest.xml' for n in names)
            has_libmain  = any('libmain.so' in n for n in names)
            if not (has_dex and has_manifest and has_libmain):
                _log.warning(
                    'APK content check: dex=%s manifest=%s libmain=%s',
                    has_dex, has_manifest, has_libmain)
        except zipfile.BadZipfile:
            raise BuildError('{0} is not a valid ZIP/APK file'.format(apk))

        # Copy to output dir under a friendly name
        _makedirs(cfg.output_dir)
        dest = join(cfg.output_dir, '{0}-debug.apk'.format(cfg.app_name))
        shutil.copy2(apk, dest)
        _log.info('  source APK:  %s (%.2f MB)', apk, sz / (1024.0 * 1024.0))
        _log.info('  copied to:   %s', dest)
        _log.info('  sha256:      %s', sha)
        if len(candidates) > 1:
            _log.info('  (also found %d additional APK candidates)',
                      len(candidates) - 1)
        return dest

    # ----- driver -----

    def run_all(self):
        t0 = time.time()
        self.stage_validate()
        self.stage_apply_quirks()
        scan = self.stage_scan()
        self.stage_sysroot_json()
        self.stage_app_pdy(scan)
        self.stage_android_source()
        self.stage_sources()
        self.stage_pyqtdeploy_sysroot()
        self.stage_pyqtdeploy_build()
        self.stage_compile()
        self.stage_package()
        apk = self.stage_locate_apk()
        dt = time.time() - t0
        _log.info('')
        _log.info('=' * 72)
        _log.info('  BUILD SUCCEEDED in %d min %d sec',
                  int(dt // 60), int(dt % 60))
        _log.info('=' * 72)
        _log.info('  APK:     %s', apk)
        _log.info('  Install: adb install %s', apk)
        _log.info('  Logs:    adb logcat -v time *:S python.stderr:V '
                  'python.stdout:V Qt:V AndroidRuntime:E')
        return apk


# ----------------------------------------------------------------- CLI

def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog='pyqt5_android_kviktor.py',
        description=('Build a PyQt5 app into an Android APK using '
                     'pyqtdeploy 2.5.1.'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            EXAMPLES

              Basic build using defaults (paths match the Docker image):
                python3 pyqt5_android_kviktor.py \\
                    --project-dir /work/myapp \\
                    --app-name MyApp \\
                    --package-name com.example.myapp

              Override Qt/NDK/SDK paths:
                python3 pyqt5_android_kviktor.py \\
                    --project-dir ./demo \\
                    --app-name Demo \\
                    --package-name com.demo \\
                    --qt-dir   $HOME/Qt/5.13.2 \\
                    --ndk-dir  $HOME/Android/android-ndk-r20b \\
                    --sdk-dir  $HOME/Android/tools

              Include extra PyQt5 modules:
                python3 pyqt5_android_kviktor.py \\
                    --project-dir ./qml-app \\
                    --app-name QmlApp \\
                    --package-name com.example.qml \\
                    --pyqt5-modules QtCore,QtGui,QtWidgets,QtQml,QtQuick,QtQuickWidgets

              Force full clean rebuild (slow, regenerates the entire sysroot):
                python3 pyqt5_android_kviktor.py \\
                    --project-dir ./demo --app-name Demo \\
                    --package-name com.demo --force
        """),
    )
    p.add_argument('--project-dir', required=True,
                   help='The directory containing your PyQt5 project '
                        '(main.py at root).')
    p.add_argument('--app-name', required=True,
                   help='App name (also used as Java class prefix). '
                        'Use a valid Java identifier, e.g. MyApp.')
    p.add_argument('--package-name', required=True,
                   help='Android package name in reverse DNS form, '
                        'e.g. com.example.myapp.')
    p.add_argument('--jobs', type=int, default=2,
                   help='Parallel `make -jN` jobs (default: 2).')
    p.add_argument('--venv-dir', default=DEFAULT_VENV_DIR,
                   help='Python venv with pyqtdeploy {0} installed '
                        '(default: {1}).'.format(PYQTDEPLOY_VERSION,
                                                 DEFAULT_VENV_DIR))
    p.add_argument('--qt-dir', default=DEFAULT_QT_DIR,
                   help='Qt installation root (default: {0}).'.format(
                       DEFAULT_QT_DIR))
    p.add_argument('--ndk-dir', default=DEFAULT_NDK_DIR,
                   help='Android NDK r20b (default: {0}).'.format(
                       DEFAULT_NDK_DIR))
    p.add_argument('--sdk-dir', default=DEFAULT_SDK_DIR,
                   help='Android SDK root (default: {0}).'.format(
                       DEFAULT_SDK_DIR))
    p.add_argument('--output-dir', default=None,
                   help='Where to copy the final APK '
                        '(default: <project-dir>/output).')
    p.add_argument('--pyqt5-modules', default=None,
                   help='Comma-separated PyQt5 modules to compile in (e.g. '
                        '\'QtCore,QtGui,QtWidgets,QtQml\').  Default is a '
                        'minimal Widgets set.  The builder also auto-detects '
                        'PyQt5 imports in your source code.')
    p.add_argument('--stdlib-modules', default=None,
                   help='Comma-separated stdlib modules to bundle.  Default '
                        'is a comprehensive set (~140 modules).')
    p.add_argument('--force', action='store_true',
                   help='Regenerate sysroot.json/app.pdy and rebuild the '
                        'sysroot (slow; ~30 min).')
    p.add_argument('--verbose', action='store_true',
                   help='Show DEBUG-level logs.')
    return p.parse_args(argv)


def _cli_to_config(args):
    pyqt5_mods = None
    if args.pyqt5_modules:
        pyqt5_mods = [m.strip() for m in args.pyqt5_modules.split(',')
                      if m.strip()]
    stdlib_mods = None
    if args.stdlib_modules:
        stdlib_mods = [m.strip() for m in args.stdlib_modules.split(',')
                       if m.strip()]
    return BuildConfig(
        project_dir    = args.project_dir,
        app_name       = args.app_name,
        package_name   = args.package_name,
        jobs           = args.jobs,
        venv_dir       = args.venv_dir,
        qt_dir         = args.qt_dir,
        ndk_dir        = args.ndk_dir,
        sdk_dir        = args.sdk_dir,
        output_dir     = args.output_dir,
        pyqt5_modules  = pyqt5_mods,
        stdlib_modules = stdlib_mods,
        force          = args.force,
        verbose        = args.verbose,
    )


def main(argv=None):
    args = _parse_args(argv)
    setup_logging(args.verbose)
    try:
        cfg = _cli_to_config(args)
        builder = Builder(cfg)
        builder.run_all()
        return 0
    except BuildError as e:
        _log.error('')
        _log.error('=' * 72)
        _log.error('  BUILD FAILED')
        _log.error('=' * 72)
        for line in str(e).splitlines():
            _log.error('  %s', line)
        return 1
    except KeyboardInterrupt:
        _log.error('Interrupted by user')
        return 130


if __name__ == '__main__':
    sys.exit(main())
