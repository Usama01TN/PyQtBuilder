#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyQt5 5.15.1 -> Android APK Builder
=====================================
Automates the complete pipeline described in
  https://github.com/kviktor/pyqtdeploy-android-build

Exact versions used (mirrors the kviktor guide exactly):
  Python       3.7.7
  Qt           5.13.2  (Android arm64-v8a component)
  PyQt5        5.15.1
  SIP          4.19.24
  pyqtdeploy   2.5.1
  OpenSSL      1.0.2r
  Android NDK  r20b
  Android API  28 (platform) / 29 (deploy target)
  Host OS      Ubuntu 20.04 LTS (64-bit)
  JDK          OpenJDK 8

Pipeline:
--------
  1.  Preflight        - host OS, tools, JDK 8, disk space
  2.  Virtualenv       - isolated Python 3.7 env with pyqtdeploy 2.5.1
  3.  Qt validation    - verify Qt 5.13.2 Android arm64 installation
  4.  SDK/NDK check    - verify Android SDK API-28 + NDK r20b
  5.  Source download  - Python 3.7.7, SIP 4.19.24, PyQt5 5.15.1, OpenSSL 1.0.2r
  6.  sysroot.json     - generate the pyqtdeploy 2.x sysroot spec
  7.  app.pdy          - generate the pyqtdeploy project file
  8.  Android assets   - generate AndroidManifest.xml + CustomActivity.java
  9.  Build sysroot    - pyqtdeploy-sysroot --target android-64 ...
  10. Build app        - pyqtdeploy-build -> qmake -> make -> make install
  11. Package APK      - androiddeployqt --gradle
  12. ADB install      - optional adb install to connected device
  13. Summary          - artifact paths + full error FAQ

Usage (see bottom of file for detailed examples):
-------------------------------------------------
  python pyqt5_android_kviktor.py --project-dir ./myapp [OPTIONS]

Requirements:
------------
  Ubuntu 20.04 LTS (64-bit)
  Python 3.7.7  (this script must be run with Python >= 3.6)
  Qt 5.13.2 installed under ~/Qt  (Android arm64-v8a component required)
  Android Studio installed under ~/Android
  Android NDK r20b extracted to ~/Android/android-ndk-r20b
  Android SDK API 28 installed
  OpenJDK 8  (NOT 11 or 17 - kviktor guide specifies openjdk-8-jdk)
  ~15 GB free disk space
"""
from os.path import join, exists, getmtime, getsize, realpath, expanduser, basename, normpath, dirname, pathsep, \
    splitext, isdir
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from os import environ, listdir, statvfs, makedirs, walk
from logging import basicConfig, INFO, getLogger, DEBUG
from subprocess import Popen, PIPE, check_call
from sys import version_info, exit, path
from platform import system, release
from textwrap import dedent
from zipfile import ZipFile
from fnmatch import filter
from shutil import copy2
from json import dumps
import tarfile
import io

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import which, getMakeExecutable, getJavaExecutable
except:
    from builders import which, getMakeExecutable, getJavaExecutable
try:
    from urllib import urlretrieve  # noqa: F401
    from urllib2 import URLError  # noqa: F401
except:
    from urllib.request import urlretrieve  # noqa: F401
    from urllib.error import URLError  # noqa: F401

# FileNotFoundError does not exist; alias to IOError
try:
    FileNotFoundError
except:
    FileNotFoundError = IOError

try:
    from shutil import disk_usage
except ImportError:
    from collections import namedtuple

    _DiskUsage = namedtuple('DiskUsage', ['total', 'used', 'free'])


    def disk_usage(pth):
        """
        os.statvfs-based replacement for shutil.disk_usage (Python 2 / Linux).
        :param pth: str
        :return: _DiskUsage
        """
        st = statvfs(pth)
        return _DiskUsage(
            st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)

try:
    from venv import create
except ImportError:
    def create(venv_dir, with_pip=True, clear=True):
        """
        Delegate to the 'virtualenv' command.
        :param venv_dir: str
        :param with_pip: bool
        :param clear: bool
        :return:
        """
        check_call(['virtualenv', venv_dir])


def _makedirs(pth):
    """
    Create *path* and all missing parents; silently ignore if it exists.
    :param pth: str
    :return:
    """
    if not isdir(pth):
        try:
            makedirs(pth)
        except OSError:
            if not isdir(pth):
                raise


def _rglob(directory, pattern):
    """
    Recursively yield file paths under *directory* whose names match *pattern*.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []
    for root, _dirs, files in walk(directory):
        for filename in filter(files, pattern):
            matches.append(join(root, filename))
    return matches


def _glob_dir(directory, pattern):
    """
    List direct children of *directory* whose names match *pattern*.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    results = []
    try:
        entries = listdir(directory)
    except OSError:
        return results
    for entry in filter(entries, pattern):
        results.append(join(directory, entry))
    return results


def _read_text(pth, encoding='utf-8'):
    """
    Read and return the entire contents of *path* as a unicode string.
    :param pth: str
    :param encoding: str
    :return: str
    """
    with io.open(pth, 'r', encoding=encoding) as fh:
        return fh.read()


def _write_text(pth, text, encoding='utf-8'):
    """
    Write *text* (unicode string) to *path*, overwriting any existing content.
    :param pth: str
    :param text: str
    :param encoding: str
    :return:
    """
    with io.open(pth, 'w', encoding=encoding) as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# subprocess helper  (replacing subprocess.run + CompletedProcess).
# ---------------------------------------------------------------------------

class SimpleProcess(object):
    """
    Minimal stand-in for subprocess.CompletedProcess.
    """

    def __init__(self, args, returncode, stdout='', stderr=''):
        """
        :param args: list[str]
        :param returncode: int
        :param stdout: str
        :param stderr: str
        """
        self.args = args
        self.returncode = returncode
        self.stdout = stdout or ''
        self.stderr = stderr or ''


# -------------------------------------------------------------------------------
# Pinned versions (exact versions from kviktor guide).
# -------------------------------------------------------------------------------
PYTHON_VERSION = '3.7.7'
QT_VERSION = '5.13.2'
PYQT5_VERSION = '5.15.1'
SIP_VERSION = '4.19.24'
PYQTDEPLOY_VER = '2.5.1'
OPENSSL_VERSION = '1.0.2r'
NDK_VERSION = 'r20b'
NDK_DIR_NAME = 'android-ndk-{}'.format(NDK_VERSION)
ANDROID_API_PLATFORM = '28'  # SDK platform to install.
ANDROID_API_DEPLOY = '29'  # API level passed to androiddeployqt.
TARGET = 'android-64'  # pyqtdeploy target string (arm64-v8a).
QT_ARCH_SUBDIR = 'android_arm64_v8a'
# -- Download URLs --------------------------------------------------------------
PYTHON_URL = 'https://www.python.org/ftp/python/{}/Python-{}.tgz'.format(PYTHON_VERSION, PYTHON_VERSION)
SIP_URL = 'https://pypi.org/packages/source/s/sip/sip-{}.tar.gz'.format(SIP_VERSION)
PYQT5_URL = 'https://pypi.org/packages/source/P/PyQt5/PyQt5-{}.tar.gz'.format(PYQT5_VERSION)
OPENSSL_URL = 'https://www.openssl.org/source/old/1.0.2/openssl-{}.tar.gz'.format(OPENSSL_VERSION)
NDK_URL = 'https://dl.google.com/android/repository/android-ndk-{}-linux-x86_64.zip'.format(NDK_VERSION)
# -- Default install locations (mirrors kviktor guide) -------------------------
_HOME = expanduser('~')
DEFAULT_QT_DIR = join(_HOME, 'Qt', QT_VERSION)
DEFAULT_ANDROID = join(_HOME, 'Android')
DEFAULT_NDK = join(_HOME, 'Android', NDK_DIR_NAME)
DEFAULT_SDK = join(_HOME, 'Android', 'tools')
MIN_DISK_GB = 15
# -------------------------------------------------------------------------------
# Logging.
# -------------------------------------------------------------------------------
basicConfig(format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S', level=INFO)
log = getLogger('pyqt5-android-kviktor')


def _step(title):
    """
    :param title: str
    :return:
    """
    bar = '=' * 66  # type: str
    log.info('\n%s\n  %s\n%s', bar, title, bar)


# -------------------------------------------------------------------------------
# Config class.
# -------------------------------------------------------------------------------

class Config(object):
    """
    All resolved paths and options for a single build run.
    """

    def __init__(self, project_dir, app_name, package_name, qt_dir, ndk_path, sdk_path, jobs=2, verbose=False,
                 dry_run=False, skip_sysroot=False, install_apk=False, keep_build=False, extra_stdlib=None):
        """
        :param project_dir: str
        :param app_name: str
        :param package_name: (str) e.g. com.example.MyApp
        :param qt_dir: (str) ~/Qt/5.13.2
        :param ndk_path: (str) ~/Android/android-ndk-r20b
        :param sdk_path: (str) ~/Android/tools
        :param jobs: int
        :param verbose: bool
        :param dry_run: bool
        :param skip_sysroot: bool
        :param install_apk: bool
        :param keep_build: bool
        :param extra_stdlib: list[str] | None
        """
        self.project_dir = project_dir
        self.app_name = app_name
        self.package_name = package_name
        self.qt_dir = qt_dir
        self.ndk_path = ndk_path
        self.sdk_path = sdk_path
        self.jobs = jobs
        self.verbose = verbose
        self.dry_run = dry_run
        self.skip_sysroot = skip_sysroot
        self.install_apk = install_apk
        self.keep_build = keep_build
        self.extra_stdlib = extra_stdlib or []
        # Derived paths  (replaces field(init=False) + __post_init__)
        base = join(_HOME, '.pyqt5_android_kviktor')
        self.work_dir = base
        self.venv_dir = join(base, 'venv')
        self.sources_dir = join(self.project_dir, 'sources')
        self.build_dir = join(self.project_dir, 'build-{}'.format(TARGET))
        self.sysroot_dir = join(self.project_dir, 'sysroot-{}'.format(TARGET))

    # -- Derived paths ----------------------------------------------------------

    @property
    def python_exe(self):
        """
        :return: str
        """
        return join(self.venv_dir, 'bin', 'python3')

    @property
    def pip_exe(self):
        """
        :return: str
        """
        return join(self.venv_dir, 'bin', 'pip')

    @property
    def pyqtdeploy_sysroot(self):
        """
        :return: str
        """
        return join(self.venv_dir, 'bin', 'pyqtdeploy-sysroot')

    @property
    def pyqtdeploy_build(self):
        """
        :return: str
        """
        return join(self.venv_dir, 'bin', 'pyqtdeploy-build')

    @property
    def qt_android_dir(self):
        """
        ~/Qt/5.13.2/android_arm64_v8a
        :return: str
        """
        return join(self.qt_dir, QT_ARCH_SUBDIR)

    @property
    def qmake(self):
        """
        :return: str
        """
        return join(self.qt_android_dir, 'bin', 'qmake')

    @property
    def androiddeployqt(self):
        """
        :return: str
        """
        return join(self.qt_android_dir, 'bin', 'androiddeployqt')

    @property
    def sdkmanager(self):
        """
        :return: str
        """
        return join(self.sdk_path, 'tools', 'bin', 'sdkmanager')

    @property
    def adb(self):
        """
        :return: str
        """
        return join(self.sdk_path, 'platform-tools', 'adb')

    @property
    def sysroot_json(self):
        """
        :return: str
        """
        return join(self.project_dir, 'sysroot.json')

    @property
    def app_pdy(self):
        """
        :return: str
        """
        return join(self.project_dir, 'app.pdy')

    @property
    def android_source_dir(self):
        """
        :return: str
        """
        return join(self.project_dir, 'android_source')

    def build_env(self):
        """
        Return the environment dict required by pyqtdeploy and qmake.
        :return: dict[str, str]
        """
        e = dict(environ)
        e['ANDROID_SDK_ROOT'] = self.sdk_path
        e['ANDROID_NDK_ROOT'] = self.ndk_path
        e['ANDROID_NDK_PLATFORM'] = 'android-{}'.format(ANDROID_API_DEPLOY)
        e['QT_DIR'] = self.qt_dir
        e['APP_DIR'] = self.project_dir
        # adb + androiddeployqt on PATH.
        e['PATH'] = (join(self.sdk_path, 'platform-tools') + pathsep + join(
            self.qt_android_dir, 'bin') + pathsep + e.get('PATH', ''))
        return e


# -------------------------------------------------------------------------------
# Subprocess helpers.
# -------------------------------------------------------------------------------

def _run(cmd, cwd=None, env=None, check=True, dry_run=False, capture=False):
    """
    Run a subprocess with unified error handling.
    :param cmd:     list[str]
    :param cwd:     str | None
    :param env:     dict[str, str] | None
    :param check:   bool
    :param dry_run: bool
    :param capture: bool
    :return:        SimpleProcess
    """
    cmd_strs = [c for c in cmd]
    display = ' '.join(cmd_strs)
    log.debug('$ %s  [cwd=%s]', display, cwd or '.')
    if dry_run:
        log.info('[DRY-RUN] %s', display)
        return SimpleProcess(cmd, 0, '', '')
    try:
        if capture:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, env=env, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        else:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, env=env, universal_newlines=True)
        stdout, stderr = proc.communicate()
    except OSError as exc:
        raise RuntimeError('Failed to start subprocess: {}\n{}'.format(display, exc))
    result = SimpleProcess(cmd, proc.returncode, stdout or '', stderr or '')
    if check and result.returncode != 0:
        log.error('Command failed (exit %d):\n  %s', result.returncode, display)
        if result.stdout:
            log.error('stdout:\n%s', result.stdout[-3000:])
        if result.stderr:
            log.error('stderr:\n%s', result.stderr[-3000:])
        raise RuntimeError('Subprocess exited with code {}'.format(result.returncode))
    return result


def _require_tool(name):
    """
    Assert that an external tool is on PATH and return its full path.
    :param name: str
    :return:     str
    """
    pth = which(name)
    if not pth:
        raise EnvironmentError('Required tool "{}" not found on PATH.\nInstall it and re-run.'.format(name))
    return pth


def _download(url, dest):
    """
    Download *url* to *dest*, showing progress.
    :param url:  str
    :param dest: str
    :return:     None
    """
    _makedirs(dirname(dest))
    if exists(dest):
        log.info('Cached: %s', basename(dest))
        return
    log.info('Downloading  %s', url)
    try:
        urlretrieve(url, dest)
        log.info('Saved:       %s', dest)
    except URLError as exc:
        raise RuntimeError('Download failed: {}\n{}'.format(url, exc))


def _extract(archive, dest):
    """
    Extract a tar or zip archive to *dest*.
    :param archive: str
    :param dest:    str
    :return:
    """
    _makedirs(dest)
    log.info('Extracting %s', basename(archive))
    if splitext(archive)[1] == '.zip':
        with ZipFile(archive) as zf:
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(dest)


def _check_disk(pth, required_gb=MIN_DISK_GB):
    """
    Warn if free disk space at *path* is below *required_gb* gigabytes.
    :param pth:        str
    :param required_gb: int
    :return:
    """
    free_gb = disk_usage(pth if exists(pth) else dirname(pth)).free / 1024.0 ** 3
    if free_gb < required_gb:
        log.warning('Low disk space: %.1f GB free (recommended >= %d GB).', free_gb, required_gb)
    else:
        log.info('Disk: %.1f GB free OK', free_gb)


# -------------------------------------------------------------------------------
# Step 1 - Preflight checks.
# -------------------------------------------------------------------------------

def preflight(cfg):
    """
    From kviktor README:
      Ubuntu 20.04
      sudo apt-get install clang make zlib1g zlib1g-dev libbz2-dev libssl-dev openjdk-8-jdk build-essential git
    :param cfg: Config
    :return:
    """
    _step('Step 1/13 - Preflight checks')
    if system() != 'Linux':
        raise EnvironmentError('This pipeline requires Linux (Ubuntu 20.04 recommended). Detected: {}'.format(system()))
    log.info('Host OS:  %s %s', system(), release())
    # Python version of this script (must be >= 3.6)
    major, minor = version_info[:2]
    if (major, minor) < (3, 6):
        raise EnvironmentError('This script requires Python >= 3.6 (found {}.{}).'.format(major, minor))
    log.info('Script Python: %d.%d', major, minor)
    # Required system tools (from kviktor README apt-get install)
    required = {
        'clang': 'sudo apt-get install clang',
        'make': 'sudo apt-get install build-essential',
        'git': 'sudo apt-get install git',
        'javac': 'sudo apt-get install openjdk-8-jdk',
        'java': 'sudo apt-get install openjdk-8-jdk',
        'zip': 'sudo apt-get install zip',
        'unzip': 'sudo apt-get install unzip'}
    for tool, hint in required.items():
        try:
            p = _require_tool(tool)
            log.info('Found: %-10s -> %s', tool, p)
        except EnvironmentError:
            raise EnvironmentError('"{}" not found. Install with:\n  {}'.format(tool, hint))
    # JDK must be version 8 (kviktor specifies openjdk-8-jdk explicitly).
    res = _run([getJavaExecutable(), '-version'], capture=True, check=False)
    ver_line = (res.stderr or res.stdout or '').splitlines()[0] if (res.stderr or res.stdout) else ''
    log.info('JDK: %s', ver_line)
    if '1.8' not in ver_line and 'openjdk 8' not in ver_line.lower():
        log.warning(
            'JDK 8 is required (kviktor guide specifies openjdk-8-jdk). '
            'Found: %s\n'
            'Install JDK 8:  sudo apt-get install openjdk-8-jdk\n'
            'Switch default: sudo update-alternatives --config java', ver_line)
    # Project directory.
    if not isdir(cfg.project_dir):
        raise FileNotFoundError('Project directory not found: {}'.format(cfg.project_dir))
    log.info('Project dir: %s', cfg.project_dir)
    _check_disk(dirname(cfg.work_dir))
    log.info('Preflight passed :)')


# -------------------------------------------------------------------------------
# Step 2 - Virtualenv with pyqtdeploy 2.5.1
# -------------------------------------------------------------------------------

def setup_venv(cfg):
    """
    From kviktor README:
      pip install -U pip
      pip install pyqtdeploy==2.5.1 pyqt5
    NOTE: pyqtdeploy 2.5.1 is the correct version - version 3+ has
    breaking changes (TOML-based config instead of JSON).
    :param cfg: Config
    :return:
    """
    _step('Step 2/13 - Virtualenv (pyqtdeploy 2.5.1)')
    _makedirs(cfg.work_dir)
    if exists(cfg.venv_dir):
        log.info('Reusing venv: %s', cfg.venv_dir)
    else:
        log.info('Creating venv: %s', cfg.venv_dir)
        create(cfg.venv_dir, with_pip=True, clear=True)
    _run([cfg.python_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'], dry_run=cfg.dry_run)
    _run([cfg.pip_exe, 'install', 'pyqtdeploy=={}'.format(PYQTDEPLOY_VER), 'PyQt5=={}'.format(PYQT5_VERSION),
          '--quiet', '--no-warn-script-location'], dry_run=cfg.dry_run)
    if not cfg.dry_run:
        res = _run(
            [cfg.python_exe, '-c', 'import pyqtdeploy; print(pyqtdeploy.__version__)'], capture=True, check=False)
        log.info('pyqtdeploy installed: %s :)', res.stdout.strip())
    log.info('Virtualenv ready :)')


# -------------------------------------------------------------------------------
# Step 3 - Qt 5.13.2 Android validation.
# -------------------------------------------------------------------------------

def validate_qt(cfg):
    """
    From kviktor README:
      Download Qt online installer -> install Qt 5.13.2 -> Android packages only.
      export QT_DIR=$HOME/Qt/5.13.2
      export PATH=$HOME/Qt/5.13.2/android_arm64_v8a/bin:$PATH
    :param cfg: Config
    :return:
    """
    _step('Step 3/13 - Qt 5.13.2 Android validation')
    if not exists(cfg.qmake):
        raise EnvironmentError(
            'Qt Android qmake not found: {}\n\n'
            'Install Qt {} with the Android arm64-v8a component:\n'
            '  1. Download from https://www.qt.io/download-qt-installer\n'
            '  2. Install to ~/Qt\n'
            '  3. In Qt Maintenance Tool -> Add/Remove Components\n'
            '     -> Qt {} -> Android -> arm64-v8a  :)\n'
            '  4. Re-run with --qt-dir ~/Qt/{}'.format(
                cfg.qmake, QT_VERSION, QT_VERSION, QT_VERSION))
    res = _run([cfg.qmake, '--version'], capture=True, check=False)
    qmake_lines = (res.stdout or '').splitlines()
    log.info('qmake: %s :)', qmake_lines[0] if qmake_lines else 'unknown')
    if not exists(cfg.androiddeployqt):
        raise EnvironmentError(
            'androiddeployqt not found at: {}\n'
            'The Qt Android component may be incomplete. Re-run the Qt installer.'.format(cfg.androiddeployqt))
    log.info('androiddeployqt: %s :)', cfg.androiddeployqt)
    # Check src/android/templates (needed for AndroidManifest.xml copy)
    templates_dir = join(cfg.qt_android_dir, 'src', 'android', 'templates')
    if exists(templates_dir):
        log.info('Android templates: %s :)', templates_dir)
    else:
        log.warning('Android templates dir not found at %s', templates_dir)
    log.info('Qt validation passed :)')


# -------------------------------------------------------------------------------
# Step 4 - Android SDK / NDK validation.
# -------------------------------------------------------------------------------

def validate_sdk_ndk(cfg):
    """
    From kviktor README:
      Download Android Studio -> ~/Android
      ~/Android/tools/tools/bin/sdkmanager --update
      ~/Android/tools/tools/bin/sdkmanager "platforms;android-28"
      Download NDK r20b -> extract to ~/Android/android-ndk-r20b
      export ANDROID_SDK_ROOT=$HOME/Android/tools
      export ANDROID_NDK_ROOT=$HOME/Android/android-ndk-r20b
      export ANDROID_NDK_PLATFORM=android-29
    :param cfg: Config
    :return:
    """
    _step('Step 4/13 - Android SDK / NDK validation')
    # NDK
    ndk_props = join(cfg.ndk_path, 'source.properties')
    if exists(cfg.ndk_path) and exists(ndk_props):
        log.info('NDK found: %s :)', cfg.ndk_path)
        props = _read_text(ndk_props)
        for line in props.splitlines():
            if 'Pkg.Revision' in line:
                log.info('NDK revision: %s', line.split('=')[-1].strip())
    else:
        raise EnvironmentError(
            'Android NDK r20b not found at: {}\n\n'
            'Download NDK r20b from:\n'
            '  https://developer.android.com/ndk/downloads/older_releases\n'
            'Extract to: ~/Android/android-ndk-r20b\n'
            'Verify:     cat ~/Android/android-ndk-r20b/source.properties\n'
            'Re-run with --ndk-path ~/Android/android-ndk-r20b\n\n'
            'NOTE: NDK r20b is required. Newer versions may break pyqtdeploy 2.5.1.'.format(cfg.ndk_path))
    # SDK platform-tools (adb).
    if exists(cfg.adb):
        log.info('adb: %s :)', cfg.adb)
    else:
        log.warning(
            'adb not found at %s.\nInstall Android Studio from https://developer.android.com/studio\n'
            'then run: ~/Android/tools/tools/bin/sdkmanager --update', cfg.adb)
    # API 28 platform.
    platform_dir = join(cfg.sdk_path, 'platforms', 'android-{}'.format(ANDROID_API_PLATFORM))
    if exists(platform_dir):
        log.info('Android API %s: %s :)', ANDROID_API_PLATFORM, platform_dir)
    else:
        log.warning("Android platform API %s not found at %s.\nInstall with:\n  %s 'platforms;android-%s'",
                    ANDROID_API_PLATFORM, platform_dir, cfg.sdkmanager, ANDROID_API_PLATFORM)
    log.info('SDK/NDK validation passed :)')


# -------------------------------------------------------------------------------
# Step 5 - Download source tarballs.
# -------------------------------------------------------------------------------

def download_sources(cfg):
    """
    From kviktor README:
      Download the following files and put them in example/sources/
        sip-4.19.24.tar.gz
        PyQt5-5.15.1.tar.gz
        Python-3.7.7.tgz
        openssl-1.0.2r.tar.gz
    The sysroot.json references sources by filename, so names must match exactly.
    :param cfg: Config
    :return:
    """
    _step('Step 5/13 - Downloading source tarballs')
    _makedirs(cfg.sources_dir)
    sources = [
        (PYTHON_URL, join(cfg.sources_dir, 'Python-{}.tgz'.format(PYTHON_VERSION))),
        (SIP_URL, join(cfg.sources_dir, 'sip-{}.tar.gz'.format(SIP_VERSION))),
        (PYQT5_URL, join(cfg.sources_dir, 'PyQt5-{}.tar.gz'.format(PYQT5_VERSION))),
        (OPENSSL_URL, join(cfg.sources_dir, 'openssl-{}.tar.gz'.format(OPENSSL_VERSION)))]
    for url, dest in sources:
        if cfg.dry_run:
            log.info('[DRY-RUN] Would download: %s -> %s', url, basename(dest))
        else:
            _download(url, dest)
    log.info('Source tarballs ready :)  (in %s)', cfg.sources_dir)


# -------------------------------------------------------------------------------
# Step 6 - Generate sysroot.json
# -------------------------------------------------------------------------------

def generate_sysroot_json(cfg):
    """
    Generate the sysroot.json for pyqtdeploy 2.5.1 android-64 target.
    From kviktor example/sysroot.json pattern:
      - OpenSSL with Android-specific shared_lib settings
      - Python 3.7.7 with android-64 target SSL includes
      - SIP 4.19.24
      - PyQt5 5.15.1 with pruned module list
      - Qt 5.13.2 installed_copy from $QT_DIR
    NOTE: pyqtdeploy 2.x uses JSON (not TOML). Version 3+ changed to TOML.
    :param cfg: Config
    :return:    None
    """
    _step('Step 6/13 - Generating sysroot.json')
    if exists(cfg.sysroot_json):
        log.info('sysroot.json already exists: %s', cfg.sysroot_json)
        log.info('  Delete it to regenerate, or edit manually.')
        return
    # Standard library modules - matches kviktor example.
    stdlib_modules = [
                         'contextlib', 'copy', 'copyreg', 'enum', 'fnmatch', 'functools',
                         'heapq', 'importlib', 'io', 'keyword', 'linecache', 'locale',
                         'logging', 'os', 'os.path', 'pickle', 'pkgutil', 'posix',
                         'posixpath', 're', 'reprlib', 'signal', 'site', 'stat',
                         'string', 'struct', 'tokenize', 'traceback', 'types',
                         'typing', 'unittest', 'warnings', 'weakref'] + cfg.extra_stdlib
    # PyQt5 modules - minimal set for a Qt Widgets app.
    pyqt5_modules = ['QtCore', 'QtGui', 'QtWidgets', 'QtNetwork', 'QtPrintSupport', 'QtSvg']
    sysroot = {
        'Description': (
            'pyqtdeploy 2.5.1 sysroot for PyQt5 {} Android arm64 - generated by pyqt5_android_kviktor.py'.format(
                PYQT5_VERSION)
        ),
        'OpenSSL': {
            'android_api': ANDROID_API_PLATFORM,
            'install_from_source': True,
            'no_asm': True,
            'python_version': PYTHON_VERSION,
            'source': 'openssl-{}.tar.gz'.format(OPENSSL_VERSION),
            'target_arch_abi': 'arm64-v8a',
        },
        'Python': {
            'build_host_from_source': True,
            'build_target_from_source': True,
            'dynamic_loading': False,
            'install_host_from_source': True,
            'source': 'Python-{}.tgz'.format(PYTHON_VERSION),
            'ssl': {
                'android': {
                    'include_dirs': ['%(sysroot)s/include/openssl'],
                    'libs': ['-lssl', '-lcrypto'],
                    'lib_dirs': ['%(sysroot)s/lib'],
                }
            },
            'standard_library': sorted(set(stdlib_modules)),
        },
        'PyQt5': {
            'android_abis': ['arm64-v8a'],
            'installed_qt_dir': '%(QT_DIR)s',
            'modules': pyqt5_modules,
            'source': 'PyQt5-{}.tar.gz'.format(PYQT5_VERSION),
        },
        'Qt': {
            'android_abis': ['arm64-v8a'],
            'edition': "open-source",
            'installed_copy': {
                'android': {
                    'dir': '%(QT_DIR)s/{}'.format(QT_ARCH_SUBDIR),
                    'modules': pyqt5_modules,
                }
            },
        },
        'SIP': {
            'module_name': 'PyQt5.sip',
            'source': 'sip-{}.tar.gz'.format(SIP_VERSION),
        },
    }
    _write_text(cfg.sysroot_json, dumps(sysroot, indent=4, sort_keys=True))
    log.info('sysroot.json written: %s :)', cfg.sysroot_json)


# -------------------------------------------------------------------------------
# Step 7 - Generate app.pdy
# -------------------------------------------------------------------------------

def generate_app_pdy(cfg):
    """
    Generate the pyqtdeploy 2.5.1 project file (.pdy).
    From kviktor README:
      pyqtdeploy-build --target android-64 --verbose --no-clean app.pdy
    The .pdy is an XML file. Key fields:
      - Application/MainScript: your app's entry point
      - Application/Name: app name (also becomes libmain entrypoint)
      - Python/TargetVersion: 3.7
      - Qmake/ExtraVariables: ANDROID_PACKAGE_SOURCE_DIR (for custom assets)
    From kviktor: "libmain comes from the entrypoint name in the .pdy file"
    :param cfg: Config
    :return:    None
    """
    _step('Step 7/13 - Generating app.pdy')
    # Check if a .pdy already exists in the project dir.
    existing = _glob_dir(cfg.project_dir, '*.pdy')
    if existing:
        log.info('Using existing .pdy: %s', existing[0])
        return
    # Check if main.py exists (pyqtdeploy requires an entry point).
    main_py = join(cfg.project_dir, 'main.py')
    if not exists(main_py):
        _create_main_py_template(cfg)
    # Build extra qmake ANDROID_PACKAGE_SOURCE_DIR variable.
    android_src = cfg.android_source_dir.replace(cfg.project_dir + '/', '')
    py_target_ver = '.'.join(PYTHON_VERSION.split('.')[:2])  # "3.7"
    pdy_xml = dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE pyqtdeploy>
        <!--
            pyqtdeploy 2.5.1 project file
            Generated by pyqt5_android_kviktor.py
            Source: https://github.com/kviktor/pyqtdeploy-android-build
        -->
        <pyqtdeploy version="2.5">
            <Application>
                <n>{app_name}</n>
                <EntryPoint>main:main</EntryPoint>
                <IsPackage>0</IsPackage>
                <AndroidPackageName>{package_name}</AndroidPackageName>
                <SysPath/>
                <Modules/>
                <ExternalLibs/>
                <ExternalLibDirs/>
                <Defines/>
                <IncludePath/>
                <MainScript>main.py</MainScript>
            </Application>
            <Python>
                <TargetVersion>{py_ver}</TargetVersion>
                <SupportedTargetVersion/>
                <StandardLibrary>
                    <Module name="contextlib"/>
                    <Module name="copy"/>
                    <Module name="copyreg"/>
                    <Module name="enum"/>
                    <Module name="fnmatch"/>
                    <Module name="functools"/>
                    <Module name="io"/>
                    <Module name="logging"/>
                    <Module name="os"/>
                    <Module name="os.path"/>
                    <Module name="re"/>
                    <Module name="site"/>
                    <Module name="string"/>
                    <Module name="traceback"/>
                    <Module name="types"/>
                    <Module name="warnings"/>
                    <Module name="weakref"/>
                </StandardLibrary>
                <ExcludedModules/>
            </Python>
            <Qmake>
                <ExtraVariables>
                    ANDROID_PACKAGE_SOURCE_DIR = $$PWD/{android_src}
                </ExtraVariables>
                <Defines/>
                <IncludePath/>
                <Libs/>
                <ExternalLibs/>
            </Qmake>
            <PyQt5>
                <Modules>
                    <Module name="QtCore"/>
                    <Module name="QtGui"/>
                    <Module name="QtWidgets"/>
                    <Module name="QtPrintSupport"/>
                    <Module name="QtNetwork"/>
                    <Module name="QtSvg"/>
                </Modules>
            </PyQt5>
        </pyqtdeploy>
    """.format(app_name=cfg.app_name, package_name=cfg.package_name, py_ver=py_target_ver, android_src=android_src))
    _write_text(cfg.app_pdy, pdy_xml)
    log.info('app.pdy written: %s :)', cfg.app_pdy)


def _create_main_py_template(cfg):
    """
    Create a minimal PyQt5 main.py if the project has none.
    :param cfg: Config
    :return:
    """
    # Note: the template string below is the content written to main.py.
    # The outer .format() fills in app_name and PYQT5_VERSION placeholders.
    # The literal string 'f"..."' inside the template is valid Python 3
    # syntax that will be in the generated file — it is not an f-string
    # evaluated here.
    template = dedent("""\
        #!/usr/bin/env python3
        # -*- coding: utf-8 -*-
        # Minimal PyQt5 Android app - generated by pyqt5_android_kviktor.py
        # Based on: https://github.com/kviktor/pyqtdeploy-android-build

        import sys
        from PyQt5.QtWidgets import QApplication, QLabel
        from PyQt5.QtCore import Qt


        def main():
            app = QApplication(sys.argv)
            label = QLabel(
                "<center><h2>{app_name}</h2>"
                "<p>PyQt5 {pyqt_ver} on Android</p></center>"
            )
            label.setAlignment(Qt.AlignCenter)
            label.setWindowTitle("{app_name}")
            label.resize(480, 320)
            label.show()
            sys.exit(app.exec_())


        if __name__ == '__main__':
            main()
    """.format(app_name=cfg.app_name, pyqt_ver=PYQT5_VERSION))
    _write_text(join(cfg.project_dir, 'main.py'), template)
    log.info('main.py template created :)')


# -------------------------------------------------------------------------------
# Step 8 - Android assets (AndroidManifest.xml + CustomActivity.java).
# -------------------------------------------------------------------------------

def generate_android_assets(cfg):
    """
    From kviktor README:
      Copy $QT_DIR/android_arm64_v8a/src/android/templates/AndroidManifest.xml
           -> example/android_source/
      Create ExampleActivity.java in
           example/android_source/src/org/kviktor/example/
    We derive the Java package path from cfg.package_name.
    :param cfg: Config
    :return:
    """
    _step('Step 8/13 - Android assets (manifest + activity)')
    _makedirs(cfg.android_source_dir)
    # -- AndroidManifest.xml --------------------------------------------------
    manifest_dest = join(cfg.android_source_dir, 'AndroidManifest.xml')
    qt_manifest = join(cfg.qt_android_dir, 'src', 'android', 'templates', 'AndroidManifest.xml')
    if exists(qt_manifest) and not exists(manifest_dest):
        copy2(qt_manifest, manifest_dest)
        log.info('Copied Qt AndroidManifest.xml -> %s', manifest_dest)
        # Patch the activity class reference.
        text = _read_text(manifest_dest)
        text = text.replace('org.qtproject.qt5.android.bindings.QtActivity', '{}.{}Activity'.format(
            cfg.package_name, cfg.app_name))
        _write_text(manifest_dest, text)
        log.info('Patched activity class in AndroidManifest.xml :)')
    elif not exists(manifest_dest):
        # Generate a minimal manifest.
        _write_minimal_manifest(cfg, manifest_dest)
    # -- CustomActivity.java --------------------------------------------------
    # Derive directory from package_name: com.example.MyApp -> com/example/MyApp
    pkg_path = join(*cfg.package_name.split('.'))
    java_dir = join(cfg.android_source_dir, 'src', pkg_path)
    _makedirs(java_dir)
    activity_file = join(java_dir, '{}Activity.java'.format(cfg.app_name))
    if not exists(activity_file):
        n = cfg.app_name
        activity_content = dedent("""\
            // {n}Activity.java
            // Generated by pyqt5_android_kviktor.py
            // Based on: https://github.com/kviktor/pyqtdeploy-android-build
            //
            // From kviktor README:
            //   "This one just extends the original QtActivity class and saves a
            //    reference to itself in a static variable."
            package {pkg};


            public class {n}Activity
                    extends org.qtproject.qt5.android.bindings.QtActivity
            {{
                private static {n}Activity m_instance;

                public {n}Activity()
                {{
                    m_instance = this;
                }}

                /**
                 * Returns the singleton Activity instance.
                 * Useful when calling Android APIs from Python via QPython bridge.
                 */
                public static {n}Activity getInstance()
                {{
                    return m_instance;
                }}
            }}
        """.format(n=n, pkg=cfg.package_name))
        _write_text(activity_file, activity_content)
        log.info('Activity written: %s :)', activity_file)
    else:
        log.info('Activity already exists: %s', activity_file)
    log.info('Android assets ready :)')


def _write_minimal_manifest(cfg, dest):
    """
    Write a minimal AndroidManifest.xml when the Qt template is not available.
    :param cfg:  Config
    :param dest: str
    :return:
    """
    manifest = dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <!-- Generated by pyqt5_android_kviktor.py (Qt template not found) -->
        <manifest xmlns:android="http://schemas.android.com/apk/res/android"
                  package="{pkg}"
                  android:versionCode="1"
                  android:versionName="1.0"
                  android:installLocation="auto">

            <uses-sdk android:minSdkVersion="21"
                      android:targetSdkVersion="{api}"/>

            <supports-screens android:anyDensity="true"
                              android:normalScreens="true"
                              android:largeScreens="true"
                              android:xlargeScreens="true"/>

            <uses-permission android:name="android.permission.INTERNET"/>

            <application android:name="org.qtproject.qt5.android.bindings.QtApplication"
                         android:label="{app}"
                         android:hardwareAccelerated="true">
                <activity android:configChanges="orientation|uiMode|screenLayout|screenSize|smallestScreenSize|locale|fontScale|keyboard|keyboardHidden|navigation|mcc|mnc|density"
                          android:name="{pkg}.{app}Activity"
                          android:label="{app}"
                          android:launchMode="singleTop"
                          android:screenOrientation="unspecified"
                          android:exported="true">
                    <intent-filter>
                        <action android:name="android.intent.action.MAIN"/>
                        <category android:name="android.intent.category.LAUNCHER"/>
                    </intent-filter>
                </activity>
            </application>
        </manifest>
    """.format(pkg=cfg.package_name, api=ANDROID_API_DEPLOY, app=cfg.app_name))
    _write_text(dest, manifest)
    log.info('Minimal AndroidManifest.xml written: %s :)', dest)


# -------------------------------------------------------------------------------
# Step 9 - Build sysroot.
# -------------------------------------------------------------------------------

def build_sysroot(cfg):
    """
    From kviktor README:
      Go to example/ directory and run:
        pyqtdeploy-sysroot --target android-64 --source-dir sources/ --source-dir $QT_DIR --verbose sysroot.json
    This cross-compiles OpenSSL + Python 3.7.7 + SIP 4.19.24 + PyQt5 5.15.1
    statically into the sysroot directory. Takes 30-90 minutes on first run.
    :param cfg: Config
    :return:    None
    """
    _step('Step 9/13 - Building sysroot (Python + SIP + PyQt5, static)')
    if cfg.skip_sysroot:
        log.info('--skip-sysroot set; skipping sysroot build.')
        return
    if exists(cfg.sysroot_dir):
        log.info('Sysroot already exists: %s', cfg.sysroot_dir)
        log.info('  Delete it to force rebuild, or use --skip-sysroot.')
        return
    cmd = [
        cfg.pyqtdeploy_sysroot,
        '--target', TARGET,
        '--source-dir', cfg.sources_dir,
        '--source-dir', cfg.qt_dir,  # Qt dir as additional source.
        '--sysroot', cfg.sysroot_dir]
    if cfg.verbose:
        cmd.append('--verbose')
    cmd.append(cfg.sysroot_json)
    log.info('Building sysroot - this compiles OpenSSL, Python, SIP, PyQt5.\n  Expect 30-90 minutes on first run.')
    _run(cmd, cwd=cfg.project_dir, env=cfg.build_env(), dry_run=cfg.dry_run)
    log.info('Sysroot built: %s :)', cfg.sysroot_dir)


# -------------------------------------------------------------------------------
# Step 10 - Build app (pyqtdeploy-build -> qmake -> make).
# -------------------------------------------------------------------------------

def build_app(cfg):
    """
    From kviktor README:
      pyqtdeploy-build --target android-64 --verbose --no-clean app.pdy
      cd build-android-64
      qmake
      make -j2
      make install INSTALL_ROOT=app
    From kviktor: "libmain comes from the entrypoint name in the .pdy file"
    The deployment settings JSON is named
    android-lib<entrypoint>.so-deployment-settings.json.
    :param cfg: Config
    :return:    str  (build directory path)
    """
    _step('Step 10/13 - Building app (pyqtdeploy-build -> qmake -> make)')
    # Find .pdy file.
    pdys = _glob_dir(cfg.project_dir, '*.pdy')
    if not pdys:
        raise FileNotFoundError(
            'No .pdy file found in {}.\nGenerate one with step 7 or create it with: pyqtdeploy app.pdy'.format(
                cfg.project_dir))
    pdy_file = pdys[0]
    log.info('Using .pdy: %s', pdy_file)
    env = cfg.build_env()
    # -- pyqtdeploy-build -----------------------------------------------------
    build_cmd = [cfg.pyqtdeploy_build, '--target', TARGET, '--build-dir', cfg.build_dir, '--no-clean']
    if cfg.verbose:
        build_cmd.append('--verbose')
    build_cmd.append(pdy_file)
    log.info('Running pyqtdeploy-build ...')
    _run(build_cmd, cwd=cfg.project_dir, env=env, dry_run=cfg.dry_run)
    if not exists(cfg.build_dir) and not cfg.dry_run:
        raise FileNotFoundError('pyqtdeploy-build did not create build dir: {}'.format(cfg.build_dir))
    # -- qmake ----------------------------------------------------------------
    log.info('Running qmake ...')
    _run([cfg.qmake], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # -- make -----------------------------------------------------------------
    log.info('Running make -j%d ...', cfg.jobs)
    _run([getMakeExecutable(), '-j{}'.format(cfg.jobs)], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # -- make install INSTALL_ROOT=app ----------------------------------------
    install_root = join(cfg.build_dir, 'app')
    log.info('Running make install INSTALL_ROOT=app ...')
    _run([getMakeExecutable(), 'install', 'INSTALL_ROOT={}'.format(install_root)], cwd=cfg.build_dir, env=env,
         dry_run=cfg.dry_run)
    log.info('App build complete :)  (build dir: %s)', cfg.build_dir)
    return cfg.build_dir


# -------------------------------------------------------------------------------
# Step 11 - Package APK with androiddeployqt
# -------------------------------------------------------------------------------

def package_apk(cfg, build_dir):
    """
    From kviktor README:
      # 29 as in android-29, libmain comes from the entrypoint name in the .pdy file
      androiddeployqt --gradle \\
          --android-platform 29 \\
          --input android-libmain.so-deployment-settings.json \\
          --output app
    The deployment settings file is named:
      android-lib<AppName>.so-deployment-settings.json
    androiddeployqt reads this JSON and uses Gradle to build the APK.
    :param cfg:       Config
    :param build_dir: str
    :return:          str  (APK path)
    """
    _step('Step 11/13 - Packaging APK (androiddeployqt --gradle)')
    env = cfg.build_env()
    # Find the deployment settings JSON (named after the .pdy entrypoint).
    settings_files = _glob_dir(build_dir, '*deployment-settings.json')
    if not settings_files and not cfg.dry_run:
        raise FileNotFoundError(
            'No *deployment-settings.json found in {}.\nThe make step may have failed. Check the make output.'.format(
                build_dir))
    settings_json = (
        settings_files[0]
        if settings_files
        else join(build_dir, 'android-lib{}.so-deployment-settings.json'.format(cfg.app_name)))
    log.info('Deployment settings: %s', settings_json)
    apk_output = join(build_dir, 'app')
    cmd = [cfg.androiddeployqt, '--gradle', '--android-platform', ANDROID_API_DEPLOY, '--input', settings_json,
           '--output', apk_output]
    if cfg.verbose:
        cmd.append('--verbose')
    _run(cmd, cwd=build_dir, env=env, dry_run=cfg.dry_run)
    # Locate the APK.
    # kviktor: app/build/outputs/apk/debug/app-debug.apk
    apk_path = join(apk_output, 'build', 'outputs', 'apk', 'debug', 'app-debug.apk')
    if exists(apk_path):
        log.info('APK: %s  (%.1f MB) :)', apk_path, getsize(apk_path) / 1024.0 ** 2)
    elif cfg.dry_run:
        log.info('[DRY-RUN] APK would be at: %s', apk_path)
    else:
        # Search for any .apk produced.
        candidates = sorted(_rglob(apk_output, '*.apk'), key=lambda p: getmtime(p))
        if candidates:
            apk_path = candidates[-1]
            log.info('APK found: %s :)', apk_path)
        else:
            raise FileNotFoundError('No .apk found under {}.\nCheck androiddeployqt output above.'.format(apk_output))
    return apk_path


# -------------------------------------------------------------------------------
# Step 12 - ADB install.
# -------------------------------------------------------------------------------

def install_via_adb(cfg, apk_path):
    """
    From kviktor README:
      adb devices
      adb install app/build/outputs/apk/debug/app-debug.apk
      "(might need to enable developer mode and USB debugging on your phone,
       after that it works via USB or over TCP too)"
    :param cfg:      Config
    :param apk_path: str
    :return:         None
    """
    _step('Step 12/13 - ADB install')
    adb = cfg.adb if exists(cfg.adb) else (which('adb') or '')
    if not adb or not exists(adb):
        log.warning(
            'adb not found. Add Android SDK platform-tools to PATH:\n  export PATH=~/Android/tools/platform-tools:$PATH'
        )
        return
    # adb devices.
    res = _run([adb, 'devices'], capture=True, check=False)
    device_lines = [l for l in (res.stdout or '').splitlines() if l.strip() and 'List of devices' not in l]
    if not device_lines:
        log.warning(
            'No ADB devices found.\n'
            '  1. Enable Developer Options on your Android phone.\n'
            '  2. Enable USB Debugging.\n'
            '  3. Accept the RSA key fingerprint prompt.\n'
            '  (kviktor: "it works via USB or over TCP too")')
        return
    log.info('Connected devices:\n%s', '\n'.join('  {}'.format(l) for l in device_lines))
    if not exists(apk_path) and not cfg.dry_run:
        log.warning('APK not found: %s', apk_path)
        return
    _run([adb, 'install', apk_path], dry_run=cfg.dry_run)
    log.info('Installed :)')
    log.info('Stream logs:  %s logcat', adb)


# -------------------------------------------------------------------------------
# Step 13 - Summary.
# -------------------------------------------------------------------------------

def print_summary(cfg, apk_path):
    """
    :param cfg:      Config
    :param apk_path: str | None
    :return:
    """
    _step('Step 13/13 - Summary')
    log.info(
        '\n'
        '  App name         : %s\n'
        '  Package          : %s\n'
        '  Python           : %s\n'
        '  PyQt5            : %s\n'
        '  SIP              : %s\n'
        '  pyqtdeploy       : %s\n'
        '  Qt               : %s  (%s)\n'
        '  NDK              : %s\n'
        '  Target           : %s\n'
        '  Sysroot          : %s\n'
        '  Build dir        : %s\n'
        '  APK              : %s',
        cfg.app_name,
        cfg.package_name,
        PYTHON_VERSION,
        PYQT5_VERSION,
        SIP_VERSION,
        PYQTDEPLOY_VER,
        QT_VERSION, QT_ARCH_SUBDIR,
        NDK_DIR_NAME,
        TARGET,
        cfg.sysroot_dir,
        cfg.build_dir, apk_path or 'N/A')
    if apk_path and (exists(apk_path) or cfg.dry_run):
        log.info('\n  Build succeeded!')
        log.info('   Install:  adb install %s', apk_path)
    else:
        log.warning('\n  Build did not produce an APK. Check errors above.')
    log.info(dedent("""
        ------------------------------------------------------------------
        Error FAQ  (kviktor guide + pyqtdeploy 2.5.1 known issues)
        ------------------------------------------------------------------

        * "No such file or directory: 'pyqtdeploy-sysroot'"
          Fix: ensure pyqtdeploy 2.5.1 is in your venv.
               The script installs it automatically in Step 2.
               Verify: ~/.pyqt5_android_kviktor/venv/bin/pyqtdeploy-sysroot

        * "pyqtdeploy-sysroot: error: unrecognised target 'android-64'"
          Fix: you are using pyqtdeploy 3+. This pipeline requires 2.5.1.
               pip install pyqtdeploy==2.5.1

        * "sysroot.json: No such file or directory"
          Fix: run the builder with --project-dir pointing to the dir
               that contains sysroot.json (generated in Step 6).

        * "OpenSSL: error: 'OPENSSL_VERSION_TEXT' undeclared"
          Fix: use OpenSSL 1.0.2r exactly (not 1.1.x).
               The kviktor guide specifies openssl-1.0.2r.tar.gz.

        * "Python: configure: error: C compiler cannot create executables"
          Fix: the NDK toolchain is not on PATH.
               Ensure ANDROID_NDK_ROOT is set and points to android-ndk-r20b.

        * "make: arm-linux-androideabi-gcc: command not found"
          Fix: NDK r20b toolchain not on PATH.
               Add to env: export PATH=$ANDROID_NDK_ROOT/toolchains/
               llvm/prebuilt/linux-x86_64/bin:$PATH

        * "androiddeployqt: JAVA_HOME is not set"
          Fix: export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
               sudo apt-get install openjdk-8-jdk

        * "Cannot find ELF information"
          This warning from androiddeployqt is safe to ignore (kviktor).

        * "Gradle: failed to find target with hash string 'android-29'"
          Fix: install the Android platform:
               ~/Android/tools/tools/bin/sdkmanager "platforms;android-29"

        * "libmain.so not found" in deployment settings JSON
          Fix: the .pdy entrypoint name must match the .so filename.
               If app name is 'MyApp', the file will be android-libMyApp.so.
               Pass this to androiddeployqt --input.

        * APK installed but crashes immediately on launch
          Fix: check adb logcat for Python import errors.
               Missing stdlib modules -> add to <StandardLibrary> in app.pdy.
               Missing PyQt5 module  -> add to <Modules> in app.pdy.

        * "note: it's recommended to always use the latest PyQt5 source"
          (kviktor README): PyQt5 5.15.x is backward-compatible with Qt 5.13.2.
        ------------------------------------------------------------------
    """))


# -------------------------------------------------------------------------------
# Argument parser.
# -------------------------------------------------------------------------------

def make_parser():
    """
    :return: ArgumentParser
    """
    p = ArgumentParser(
        prog='pyqt5_android_kviktor.py', formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PyQt5 {pyqt} -> Android APK Builder  (pyqtdeploy {pdep})
            ============================================================
            Replicates the exact pipeline from:
              https://github.com/kviktor/pyqtdeploy-android-build

            Pinned versions:
              Python {python}  |  Qt {qt}  |  PyQt5 {pyqt}
              SIP {sip}  |  pyqtdeploy {pdep}  |  NDK {ndk}
        """.format(
            pyqt=PYQT5_VERSION, pdep=PYQTDEPLOY_VER, python=PYTHON_VERSION,
            qt=QT_VERSION, sip=SIP_VERSION, ndk=NDK_VERSION)),
        epilog=dedent("""\
            -- Quick start ------------------------------------------------------
            # 1. Full automated build:
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --app-name    MyApp \\
                --package-name com.example.MyApp

            # 2. Skip sysroot rebuild (already built):
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --skip-sysroot

            # 3. Custom Qt / NDK / SDK paths:
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --qt-dir      ~/Qt/{qt} \\
                --ndk-path    ~/Android/{ndk} \\
                --sdk-path    ~/Android/tools

            # 4. Build + auto-install on connected device:
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --install-apk

            # 5. Use 4 parallel make jobs (faster on multi-core):
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --jobs 4

            # 6. Include extra stdlib modules:
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --extra-stdlib json,ssl,base64,xmlrpc

            # 7. Dry-run (print all commands, no execution):
            python pyqt5_android_kviktor.py \\
                --project-dir ./myapp \\
                --dry-run --verbose
            ---------------------------------------------------------------------
        """.format(qt=QT_VERSION, ndk=NDK_DIR_NAME)))
    p.add_argument('--project-dir', required=True, type=str,
                   help='Your app directory. Must contain (or will receive) main.py.')
    p.add_argument('--app-name', default=None, help='Application name (default: project dir basename).')
    p.add_argument('--package-name', default=None, dest='package_name',
                   help='Android package identifier, e.g. com.example.MyApp (default: com.example.<app-name>).')
    # Toolchain paths (default to kviktor guide locations).
    p.add_argument('--qt-dir', type=str, default=DEFAULT_QT_DIR,
                   help='Qt {} root directory (default: {}).'.format(QT_VERSION, DEFAULT_QT_DIR))
    p.add_argument('--ndk-path', type=str, default=DEFAULT_NDK,
                   help='Android NDK {} root (default: {}).'.format(NDK_VERSION, DEFAULT_NDK))
    p.add_argument('--sdk-path', type=str, default=DEFAULT_SDK,
                   help='Android SDK root (default: {}).'.format(DEFAULT_SDK))
    # Build control.
    p.add_argument('--jobs', type=int, default=2, help='Parallel make jobs (default: 2). kviktor uses -j2.')
    p.add_argument('--extra-stdlib', type=str, default='',
                   help='Comma-separated extra Python stdlib modules to include, e.g. json,ssl,base64,xmlrpc.')
    p.add_argument('--skip-sysroot', action='store_true',
                   help='Skip building the sysroot (use existing sysroot directory).')
    p.add_argument('--install-apk', action='store_true',
                   help='Install the produced APK on the first ADB-connected device.')
    p.add_argument('--keep-build', action='store_true', help='Keep intermediate build files after APK is produced.')
    p.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    p.add_argument('-v', '--verbose', action='store_true', help='Enable debug-level output.')
    return p


# -------------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------------

def main(argv=None):
    """
    :param argv: list[str] | None
    :return:     int
    """
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        getLogger().setLevel(DEBUG)
    # -- Resolve project directory --------------------------------------------
    project_dir = realpath(args.project_dir)
    app_name = args.app_name or basename(normpath(project_dir))
    package_name = args.package_name or 'com.example.{}'.format(app_name)
    extra_stdlib = [m.strip() for m in args.extra_stdlib.split(',') if m.strip()] if args.extra_stdlib else []
    # -- Resolve toolchain paths: expanduser + realpath -----------------------
    qt_dir = realpath(expanduser(args.qt_dir))
    ndk_path = realpath(expanduser(args.ndk_path))
    sdk_path = realpath(expanduser(args.sdk_path))
    # -- Build config ---------------------------------------------------------
    cfg = Config(
        project_dir=project_dir,
        app_name=app_name,
        package_name=package_name,
        qt_dir=qt_dir,
        ndk_path=ndk_path,
        sdk_path=sdk_path,
        jobs=args.jobs,
        verbose=args.verbose,
        dry_run=args.dry_run,
        skip_sysroot=args.skip_sysroot,
        install_apk=args.install_apk,
        keep_build=args.keep_build,
        extra_stdlib=extra_stdlib)
    try:
        preflight(cfg)
        setup_venv(cfg)
        validate_qt(cfg)
        validate_sdk_ndk(cfg)
        download_sources(cfg)
        generate_sysroot_json(cfg)
        generate_app_pdy(cfg)
        generate_android_assets(cfg)
        build_sysroot(cfg)
        build_dir = build_app(cfg)
        apk_path = package_apk(cfg, build_dir)
        if args.install_apk:
            install_via_adb(cfg, apk_path)
        print_summary(cfg, apk_path)
        return 0
    except EnvironmentError as exc:
        log.error('Environment error:\n%s', exc)
        return 2
    except FileNotFoundError as exc:
        log.error('File not found:\n%s', exc)
        return 3
    except RuntimeError as exc:
        log.error('Build error:\n%s', exc)
        return 4
    except KeyboardInterrupt:
        log.warning('Interrupted.')
        return 130


if __name__ == '__main__':
    exit(main())
