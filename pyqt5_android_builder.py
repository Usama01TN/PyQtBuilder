#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyQt5 -> Android APK Builder  (pyqtdeploy pipeline)
====================================================
Automates the complete cross-compilation pipeline for packaging a PyQt5
application as an Android APK using pyqtdeploy, SIP, and Qt 5.

References / Sources:
--------------------
- https://plashless.wordpress.com/2014/08/14/using-qtcreator-on-linux-to-cross-compile-for-android/
- https://plashless.wordpress.com/2014/08/19/using-pyqtdeploy0-5-on-linux-to-cross-compile-a-pyqt-app-for-android/
- https://plashless.wordpress.com/2014/08/26/using-pyqtdeploy0-5-on-linux-to-cross-compile-a-pyqt-app-for-android-part-2
- https://github.com/kviktor/pyqtdeploy-android-build
- https://github.com/achille-martin/pyqt-crom

Architecture of the build pipeline:
------------------------------------
  1. Preflight  - validate host OS, tools, disk space, project layout
  2. Env Setup  - environment variables, working directories
  3. Toolchain  - install Android SDK (API 28), NDK (r21e), JDK 11, Qt 5.15.2
  4. Sources    - download Python, SIP, PyQt5 source tarballs
  5. Sysroot    - cross-compile static Python -> SIP -> PyQt5 into SYSROOT
  6. pyqtdeploy - configure .pdt project, generate Qt Creator .pro
  7. APK Build  - qmake + make + androiddeployqt -> .apk
  8. ADB Deploy - optional install to connected device

Usage:
-----
    python pyqt5_android_builder.py --project-dir /path/to/myapp [OPTIONS]
    # Full automated build (arm64):
    python pyqt5_android_builder.py --project-dir ./myapp --arch android-64
    # Environment + sysroot only (no APK yet):
    python pyqt5_android_builder.py --project-dir ./myapp --only-sysroot
    # Use pre-installed Qt and NDK:
    python pyqt5_android_builder.py --project-dir ./myapp --qt-dir ~/Qt5.15.2/5.15.2/android
        --ndk-path ~/Android/Sdk/ndk/21.4.7075529 --sdk-path ~/Android/Sdk
    # Verbose output + keep intermediate files:
    python pyqt5_android_builder.py --project-dir ./myapp --verbose --keep-build
    # Install APK after build:
    python pyqt5_android_builder.py --project-dir ./myapp --install-apk

Requirements (host machine):
----------------------------
    - Ubuntu 22.04 (LTS recommended) or compatible Linux.
    - Python 3.10.x  (host interpreter).
    - ~40-50 GB free disk space.
    - Internet access (first run only).
"""
from os.path import join, basename, exists, dirname, isdir, getsize, getmtime, expanduser, realpath, normpath
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from logging import getLogger, DEBUG, basicConfig, INFO
from os import environ, statvfs, walk, makedirs
from subprocess import Popen, PIPE, check_call
from platform import system, release
from collections import namedtuple
from sys import version_info, path
from shutil import rmtree, copy2
from textwrap import dedent
from fnmatch import filter
from sys import exit
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
try:
    FileNotFoundError
except:
    FileNotFoundError = IOError

try:
    from shutil import disk_usage
except:
    _DiskUsage = namedtuple('DiskUsage', ['total', 'used', 'free'])


    def disk_usage(path):
        """
        :param path: str
        :return: _DiskUsage
        """
        st = statvfs(path)
        return _DiskUsage(
            st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)

try:
    from os import cpu_count
except:
    def cpu_count():
        """
        Fallback cpu_count using /proc/cpuinfo.
        :return: int | None
        """
        try:
            with open('/proc/cpuinfo') as fh:
                return sum(1 for line in fh if line.strip().startswith('processor'))
        except:
            return None

try:
    from venv import create
except:
    def create(venv_dir, with_pip=True, clear=True):
        """
        :param venv_dir: str
        :param with_pip: bool
        :param clear: bool
        :return:
        """
        check_call(['virtualenv', venv_dir])


def _makedirs(path):
    """
    Create *path* and all missing parents; silently ignore if it exists.
    :param path: str
    :return:
    """
    if not isdir(path):
        try:
            makedirs(path)
        except OSError:
            if not isdir(path):
                raise


def _rglob(directory, pattern):
    """
    Recursively yield file paths under *directory* whose names match *pattern*.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []  # type: list[str]
    for root, _dirs, files in walk(directory):
        for filename in filter(files, pattern):
            matches.append(join(root, filename))
    return matches


def _read_text(pth, encoding='utf-8'):
    """
    Read and return the entire contents of *path* as a Unicode string.
    :param pth: str
    :param encoding: str
    :return:
    """
    with io.open(pth, 'r', encoding=encoding) as fh:
        return fh.read()


def _write_text(pth, text, encoding='utf-8'):
    """
    Write *text* to *path*, overwriting any existing content.
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
        self.args = args  # type: list[str]
        self.returncode = returncode  # type: int
        self.stdout = stdout or ''  # type: str
        self.stderr = stderr or ''  # type: str


# ------------------------------------------------------------------------------
# Versioned constants — adjust to match Qt + PyQt releases.
# ------------------------------------------------------------------------------
QT_VERSION = '5.15.2'
PYQT_VERSION = '5.15.10'  # Latest GPL PyQt5 stable.
SIP_VERSION = '6.8.3'  # Must match PyQt5 requirement.
PYTHON_VERSION = '3.10.14'  # Cross-compiled into sysroot.
# Android SDK / NDK versions tested with Qt 5.15 (achille-martin/pyqt-crom).
ANDROID_API = '28'  # Android 9.0
NDK_VERSION = '21.4.7075529'  # r21e
NDK_TAG = 'android-ndk-r21e'
BUILD_TOOLS_VER = '28.0.3'
SDK_PLATFORM_PKG = 'platforms;android-{}'.format(ANDROID_API)
BUILD_TOOLS_PKG = 'build-tools;{}'.format(BUILD_TOOLS_VER)
# Download URLs.
PYTHON_URL = 'https://www.python.org/ftp/python/{}/Python-{}.tgz'.format(PYTHON_VERSION, PYTHON_VERSION)
PYQT5_URL = 'https://pypi.riverbank.computing.com/packages/PyQt5_gpl-{}.tar.gz'.format(PYQT_VERSION)
SIP_URL = 'https://pypi.riverbank.computing.com/packages/sip-{}.tar.gz'.format(SIP_VERSION)
# pyqtdeploy - installed via pip into host venv.
PYQTDEPLOY_VERSION = '3.3.0'
# Qt installer script (offline approach; users may already have Qt).
QT_INSTALL_SCRIPT = 'qt-unified-linux-x64-online.run'
QT_DOWNLOAD_URL = 'https://d13lb3tujbc8s0.cloudfront.net/onlineinstallers/qt-unified-linux-x64-4.6.1-online.run'
# Architecture targets recognized by pyqtdeploycli.
ARCH_MAP = {
    'android-32': 'android_armv7',  # armeabi-v7a (32-bit ARM).
    'android-64': "android_arm64_v8a",  # arm64-v8a  (64-bit ARM, recommended).
    'android-x86': 'android_x86',
    'android-x86_64': 'android_x86_64'}
# Required disk space (GB).
MIN_DISK_GB = 40
# Resolved home directory.
_HOME = expanduser('~')
# ------------------------------------------------------------------------------
# Logging.
# ------------------------------------------------------------------------------
basicConfig(format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S', level=INFO)
log = getLogger('pyqt5-android-builder')


def _step(title):
    """
    Print a visually distinct step header.
    :param title: str
    """
    bar = '=' * 64  # type: str
    log.info('\n%s\n  %s\n%s', bar, title, bar)


# ------------------------------------------------------------------------------
# Configuration class.
# ------------------------------------------------------------------------------

class BuildConfig(object):
    """
    All resolved paths and options for one build run.
    """

    def __init__(self,
                 project_dir, app_name, arch, qt_version=QT_VERSION, pyqt_version=PYQT_VERSION, sip_version=SIP_VERSION,
                 python_version=PYTHON_VERSION, qt_dir=None, ndk_path=None, sdk_path=None, verbose=False, dry_run=False,
                 keep_build=False, only_sysroot=False, install_apk=False):
        """
        :param project_dir: str
        :param app_name: str
        :param arch: (str) One of ARCH_MAP keys.
        :param qt_version: str
        :param pyqt_version: str
        :param sip_version: str
        :param python_version: str
        :param qt_dir: str | None
        :param ndk_path: str | None
        :param sdk_path: str | None
        :param verbose: bool
        :param dry_run: bool
        :param keep_build: bool
        :param only_sysroot: bool
        :param install_apk: bool
        """
        self.project_dir = project_dir
        self.app_name = app_name
        self.arch = arch
        self.qt_version = qt_version
        self.pyqt_version = pyqt_version
        self.sip_version = sip_version
        self.python_version = python_version
        self.qt_dir = qt_dir
        self.ndk_path = ndk_path
        self.sdk_path = sdk_path
        self.verbose = verbose
        self.dry_run = dry_run
        self.keep_build = keep_build
        self.only_sysroot = only_sysroot
        self.install_apk = install_apk
        # Derived paths  (replaces __post_init__).
        base = join(_HOME, '.pyqt5_android_build')
        self.work_dir = base
        self.sysroot_dir = join(base, 'sysroot')
        self.sources_dir = join(base, 'sources')
        self.venv_dir = join(base, 'venv')
        self.build_dir = join(self.project_dir, 'build-{}'.format(self.arch))

    # -- Derived paths --------------------------------------------------------

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
    def pyqtdeploycli(self):
        """
        :return: str
        """
        return join(self.venv_dir, 'bin', 'pyqtdeploycli')

    @property
    def qt_arch_dir(self):
        """
        Qt directory for the target Android arch.
        :return: str
        """
        if self.qt_dir:
            return self.qt_dir
        return join(_HOME, 'Qt5.15.2', '5.15.2', ARCH_MAP.get(self.arch, 'android_arm64_v8a'))

    @property
    def qmake(self):
        """
        :return: str
        """
        return join(self.qt_arch_dir, 'bin', 'qmake')

    @property
    def androiddeployqt(self):
        """
        :return: str
        """
        return join(self.qt_arch_dir, 'bin', 'androiddeployqt')

    @property
    def python_src_dir(self):
        """
        :return: str
        """
        return join(self.sources_dir, 'Python-{}'.format(self.python_version))

    @property
    def sip_src_dir(self):
        """
        :return: str
        """
        return join(self.sources_dir, 'sip-{}'.format(self.sip_version))

    @property
    def pyqt5_src_dir(self):
        """
        :return: str
        """
        return join(self.sources_dir, 'PyQt5_gpl-{}'.format(self.pyqt_version))

    @property
    def ndk_root(self):
        """
        :return: str
        """
        return self.ndk_path if self.ndk_path else join(_HOME, 'Android', 'Sdk', 'ndk', NDK_VERSION)

    @property
    def sdk_root(self):
        """
        :return: str
        """
        return self.sdk_path if self.sdk_path else join(_HOME, 'Android', 'Sdk')

    @property
    def adb_exe(self):
        """
        :return: str
        """
        return join(self.sdk_root, 'platform-tools', 'adb')


# ------------------------------------------------------------------------------
# Subprocess helpers.
# ------------------------------------------------------------------------------

def _run(cmd, cwd=None, env=None, check=True, dry_run=False, capture=False):
    """
    Run a subprocess, printing the command and handling errors uniformly.
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
    # Build merged environment.
    # Fixes original bug: merged_env.update(dict(environ), **(env or {}))
    # dict.update() only accepts ONE positional argument.
    merged_env = {}
    merged_env.update(environ)
    if env:
        merged_env.update(env)
    try:
        if capture:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, env=merged_env, stdout=PIPE, stderr=PIPE,
                         universal_newlines=True)
        else:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, env=merged_env, universal_newlines=True)
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
    Assert that an external tool is on PATH and return its full path as a str.
    :param name: str
    :return:     str
    """
    path = which(name)
    if not path:
        raise EnvironmentError('Required tool "{}" not found on PATH.\nInstall it before re-running.'.format(name))
    return path  # Plain str; original returned Path(path) which is removed.


def _download(url, dest):
    """
    Download *url* -> *dest* with simple progress indication.
    :param url:  str
    :param dest: str
    :return:
    """
    _makedirs(dirname(dest))
    if exists(dest):
        log.info('Cached: %s', basename(dest))
        return
    log.info('Downloading  %s', url)
    try:
        urlretrieve(url, dest)
        log.info('Saved to     %s', dest)
    except URLError as exc:
        raise RuntimeError('Download failed: {}\n{}'.format(url, exc))


def _extract(archive, dest_dir):
    """
    Extract a .tgz / .tar.gz archive into *dest_dir*.
    :param archive:  str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info('Extracting %s -> %s', basename(archive), dest_dir)
    with tarfile.open(archive) as tf:
        tf.extractall(path=dest_dir)


def _check_disk_space(path, required_gb=MIN_DISK_GB):
    """
    Warn if free disk space at *path* is below *required_gb* gigabytes.
    :param path:        str
    :param required_gb: int
    :return:
    """
    check_path = path if exists(path) else dirname(path)
    stat = disk_usage(check_path)
    # 1024.0 ** 3 forces float division.
    free_gb = stat.free / 1024.0 ** 3
    if free_gb < required_gb:
        log.warning('Low disk space: %.1f GB free at %s (recommended >= %d GB).', free_gb, path, required_gb)
    else:
        log.info('Disk space: %.1f GB free :)', free_gb)


# ------------------------------------------------------------------------------
# Step 1 - Preflight checks.
# ------------------------------------------------------------------------------

def preflight_checks(cfg):
    """
    Validate host environment.
    Requirements (from plashless blogs + pyqt-crom):
      - Linux host (Ubuntu 22.04 recommended)
      - Python 3.10.x host interpreter
      - JDK 11 (Gradle/Qt requirement for Android SDK 28)
      - Essential build tools: git, make, zip, unzip, gcc
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 1/8 - Preflight checks')
    # OS
    if system() != 'Linux':
        raise EnvironmentError('The pyqtdeploy Android pipeline requires a Linux host. Detected: {}.'.format(system()))
    log.info('Host OS : %s %s', system(), release())
    # Python version (host).
    major, minor = version_info[:2]
    if (major, minor) < (3, 9):
        raise EnvironmentError('Host Python >= 3.9 required (found {}.{}).'.format(major, minor))
    log.info('Python  : %d.%d', major, minor)
    # Essential system tools.
    for tool in ('git', 'make', 'gcc', 'g++', 'zip', 'unzip', 'java', 'javac'):
        p = _require_tool(tool)
        log.info('Found   : %-10s -> %s', tool, p)
    # JDK version - must be 11 for Android SDK 28 + Qt 5.15.2.
    java_res = _run([getJavaExecutable(), '-version'], capture=True, check=False)
    raw_output = java_res.stderr or java_res.stdout or ''
    output_lines = raw_output.splitlines()
    version_str = output_lines[0] if output_lines else ''
    log.info('JDK     : %s', version_str)
    if '11' not in version_str:
        log.warning(
            'JDK 11 strongly recommended for Android API 28 / Qt 5.15.2. '
            'Other versions may break Gradle. Install with: sudo apt install openjdk-11-jdk openjdk-11-jre')
    # Disk space.
    _check_disk_space(dirname(cfg.work_dir))
    # Project structure.
    if not isdir(cfg.project_dir):
        raise FileNotFoundError('Project directory not found: {}'.format(cfg.project_dir))
    # Look for a pyqtdeploy .pdt file or Python entry point.
    pdts = _rglob(cfg.project_dir, '*.pdt')
    if not pdts:
        log.warning(
            'No .pdt pyqtdeploy project file found under %s.\n'
            'You will need to create one with: pyqtdeploy <n>.pdt\n'
            'See: https://github.com/achille-martin/pyqt-crom for examples.', cfg.project_dir)
    else:
        log.info('Found .pdt: %s', pdts[0])
    # Architecture.
    if cfg.arch not in ARCH_MAP:
        raise ValueError('Unknown architecture "{}". Choose from: {}'.format(cfg.arch, ', '.join(ARCH_MAP)))
    log.info('Arch    : %s -> Qt sub-dir "%s"', cfg.arch, ARCH_MAP[cfg.arch])
    log.info('Preflight passed :)')


# ------------------------------------------------------------------------------
# Step 2 - Work directories + environment variables.
# ------------------------------------------------------------------------------

def setup_directories(cfg):
    """
    Create all working directories and print the environment variables that
    shell commands later in this script will rely on.
    From plashless Part 1:
      export ANDROID_NDK_ROOT=...
      export SYSROOT=...
      export PYTHONPATH=...
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 2/8 - Directories & environment')
    for d in (cfg.work_dir, cfg.sysroot_dir, cfg.sources_dir, cfg.build_dir):
        _makedirs(d)
        log.info('Dir: %s', d)
    log.info(
        '\n  Key environment variables used during build:\n'
        '    ANDROID_NDK_ROOT = %s\n'
        '    SYSROOT          = %s\n'
        '    PYTHONPATH       = %s/lib/python3.10/site-packages\n'
        '    JAVA_HOME        = (auto from javac)',
        cfg.ndk_root, cfg.sysroot_dir, cfg.sysroot_dir)
    log.info('Directories ready :)')


def _build_env(cfg):
    """
    Return a dict of extra env vars to pass to cross-compile subprocesses.
    :param cfg: BuildConfig
    :return: dict[str, str]
    """
    return {'ANDROID_NDK_ROOT': cfg.ndk_root, 'SYSROOT': cfg.sysroot_dir,
            'PYTHONPATH': join(cfg.sysroot_dir, 'lib', 'python3.10', 'site-packages')}


# ------------------------------------------------------------------------------
# Step 3 - Android SDK / NDK + Qt
# ------------------------------------------------------------------------------

def setup_toolchain(cfg):
    """
    Validate or guide the user to install:
      - Android SDK (API 28 platform + build-tools 28.0.3)
      - Android NDK r21e  (required by Qt 5.15.x)
      - Qt 5.15.2 with Android arm64 component
    From plashless Qt/Creator blog + pyqt-crom README (sections 1.5.4-1.5.7):
      android-28, build-tools;28.0.3, NDK 21.4.7075529, Qt5.15.2
    NOTE: Full SDK/NDK download is interactive (Android Studio or sdkmanager).
    This function validates paths and prints actionable instructions when
    anything is missing.
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 3/8 - Toolchain validation (SDK / NDK / Qt)')
    issues = []  # type: list[str]
    # -- Android NDK ----------------------------------------------------------
    if exists(cfg.ndk_root):
        log.info('NDK found: %s :)', cfg.ndk_root)
    else:
        issues.append(
            'Android NDK r21e not found at {}.\n'
            '  Install via Android Studio -> SDK Manager -> SDK Tools -> NDK (Side by side) v21.4.7075529\n'
            '  Or set --ndk-path to an existing NDK.'.format(cfg.ndk_root))
    # -- Android SDK ----------------------------------------------------------
    platform_dir = join(cfg.sdk_root, 'platforms', 'android-{}'.format(ANDROID_API))
    build_tools = join(cfg.sdk_root, 'build-tools', BUILD_TOOLS_VER)
    if exists(cfg.sdk_root) and exists(platform_dir):
        log.info('SDK found: %s :)', cfg.sdk_root)
        log.info('Platform android-%s :)', ANDROID_API)
    else:
        issues.append(
            'Android SDK (API {}) not found at {}.\n'
            '  Install Android Studio and run its SDK Manager.\n'
            '  Required packages:\n'
            '    SDK Platforms -> Android {} (android-{})\n'
            '    SDK Tools     -> Build-Tools {}\n'
            '    SDK Tools     -> NDK (Side by side) {}\n'
            '  Or set --sdk-path to an existing SDK.'.format(
                ANDROID_API, cfg.sdk_root, ANDROID_API, ANDROID_API, BUILD_TOOLS_VER, NDK_VERSION))
    if not exists(build_tools):
        issues.append("Build-tools {} not found at {}.\n  Install it via Android Studio SDK Manager.".format(
            BUILD_TOOLS_VER, build_tools))
    # -- Qt 5.15.2 Android ----------------------------------------------------
    if exists(cfg.qmake):
        log.info("qmake found: %s :)", cfg.qmake)
    else:
        issues.append(
            'Qt {} Android qmake not found at {}.\n'
            '  Download the Qt online installer:\n'
            '    {}\n'
            '  Install Qt {} with the Android arm64-v8a component.\n'
            '  Default install location: ~/Qt5.15.2\n'
            '  Or set --qt-dir to the android sub-directory.'.format(
                QT_VERSION, cfg.qmake, QT_DOWNLOAD_URL, QT_VERSION))
    if issues:
        raise EnvironmentError('Toolchain validation failed - fix the following:\n\n{}\n'.format(
            '\n\n'.join('  [!]  {}'.format(i) for i in issues)))
    log.info('Toolchain validated :)')


# ------------------------------------------------------------------------------
# Step 4 - Host virtual environment + pyqtdeploy.
# ------------------------------------------------------------------------------

def setup_venv(cfg):
    """
    Create a Python virtual environment for the build host and install pyqtdeploy + PyQt5 (host) into it.
    pyqtdeploy requires PyQt5 to be importable on the host in order to show its GUI and run its CLI.
    :param cfg: BuildConfig
    :return
    """
    _step('Step 4/8 - Host virtual environment')
    if exists(cfg.venv_dir):
        log.info('Reusing venv: %s', cfg.venv_dir)
    else:
        log.info('Creating venv at %s', cfg.venv_dir)
        create(cfg.venv_dir, with_pip=True, clear=True)
    _run([cfg.python_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'], dry_run=cfg.dry_run)
    # Install host PyQt5 (needed so pyqtdeploy can import it).
    _run([cfg.pip_exe, 'install', 'PyQt5=={}'.format(PYQT_VERSION), '--quiet', '--no-warn-script-location'],
         dry_run=cfg.dry_run)
    # Install pyqtdeploy
    _run([cfg.pip_exe, 'install', 'pyqtdeploy=={}'.format(PYQTDEPLOY_VERSION), '--quiet',
          '--no-warn-script-location'], dry_run=cfg.dry_run)
    # Verify CLI is usable.
    if not cfg.dry_run:
        res = _run([cfg.pyqtdeploycli, '--version'], capture=True, check=False)
        log.info('pyqtdeploycli: %s', (res.stdout or res.stderr).strip())
    log.info('Virtual environment ready :)')


# ------------------------------------------------------------------------------
# Step 5 - Download source tarballs
# ------------------------------------------------------------------------------

def download_sources(cfg):
    """
    Download Python, SIP, and PyQt5 GPL source tarballs.
    From plashless Part 1:
      "Navigate to python.org/downloads, download the gzipped source tarball"
      "Download SIP ... Download PyQt5"
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 5/8 - Downloading source tarballs')
    _makedirs(cfg.sources_dir)
    downloads = [
        (PYTHON_URL, join(cfg.sources_dir, 'Python-{}.tgz'.format(cfg.python_version))),
        (SIP_URL, join(cfg.sources_dir, 'sip-{}.tar.gz'.format(cfg.sip_version))),
        (PYQT5_URL, join(cfg.sources_dir, 'PyQt5_gpl-{}.tar.gz'.format(cfg.pyqt_version)))]
    for url, dest in downloads:
        if not cfg.dry_run:
            _download(url, dest)
            # Extract if not already done.
            base_name = basename(dest).replace('.tgz', '').replace('.tar.gz', '')
            stem_dir = join(dirname(dest), base_name)
            if not exists(stem_dir):
                _extract(dest, dirname(dest))
        else:
            log.info('[DRY-RUN] Would download: %s', url)
    log.info('Sources ready :)')


# ------------------------------------------------------------------------------
# Step 6 - Build sysroot (Python -> SIP -> PyQt5, all static, cross-compiled).
# ------------------------------------------------------------------------------

def _build_static_python(cfg):
    """
    Cross-compile Python statically for Android into SYSROOT.
    From plashless Part 1:
      pyqtdeploycli --package python --target android-32 configure
      ~/Qt/5.x/android_armv7/bin/qmake SYSROOT=~/aRoot
      make && make install
    :param cfg: BuildConfig
    :return:
    """
    log.info('-- Building static Python %s for %s --', cfg.python_version, cfg.arch)
    src = cfg.python_src_dir
    if not exists(src):
        raise FileNotFoundError('Python source not found: {}'.format(src))
    env = _build_env(cfg)
    # 1. pyqtdeploycli configure (patches Python source, creates python.pro).
    _run([cfg.pyqtdeploycli, '--package', 'python', '--target', cfg.arch, 'configure'], cwd=src, env=env,
         dry_run=cfg.dry_run)
    # 2. qmake.
    _run([cfg.qmake, 'SYSROOT={}'.format(cfg.sysroot_dir)], cwd=src, env=env, dry_run=cfg.dry_run)
    # 3. make && make install.
    _run([getMakeExecutable(), '-j{}'.format(cpu_count() or 4)], cwd=src, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'install'], cwd=src, env=env, dry_run=cfg.dry_run)
    log.info('Static Python built :)')


def _build_static_sip(cfg):
    """
    Cross-compile SIP statically for Android into SYSROOT.
    From plashless Part 1:
      pyqtdeploycli --package sip --target android-32 configure
      python3 configure.py --static --sysroot=... --no-tools --use-qmake --configuration=sip-android.cfg
      qmake && make && make install
    :param cfg: BuildConfig
    :return:
    """
    log.info('-- Building static SIP %s for %s --', cfg.sip_version, cfg.arch)
    src = cfg.sip_src_dir
    if not exists(src):
        raise FileNotFoundError('SIP source not found: {}'.format(src))
    env = _build_env(cfg)
    cfg_file = join(src, 'sip-{}.cfg'.format(cfg.arch))
    # 1. Generate configuration file.
    _run([cfg.pyqtdeploycli, '--package', 'sip', '--target', cfg.arch, 'configure'], cwd=src, env=env,
         dry_run=cfg.dry_run)
    # 2. Run configure.py
    _run([cfg.python_exe, 'configure.py', '--static', '--sysroot={}'.format(cfg.sysroot_dir), '--no-tools',
          '--use-qmake', '--configuration={}'.format(cfg_file)], cwd=src, env=env, dry_run=cfg.dry_run)
    # 3. qmake.
    _run([cfg.qmake], cwd=src, env=env, dry_run=cfg.dry_run)
    # 4. make && make install.
    _run([getMakeExecutable(), '-j{}'.format(cpu_count() or 4)], cwd=src, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'install'], cwd=src, env=env, dry_run=cfg.dry_run)
    log.info('Static SIP built :)')


# Default Qt modules for PyQt5 Android (from plashless Part 1).
DEFAULT_PYQT5_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtPrintSupport', 'QtSvg', 'QtNetwork']


def _build_static_pyqt5(cfg, extra_modules=None):
    """
    Cross-compile PyQt5 statically for Android into SYSROOT.
    From plashless Part 1:
      pyqtdeploycli --package pyqt5 --target android-32 configure
      python3 configure.py --static --verbose --sysroot=...
                           --no-tools --no-qsci-api
                           --no-designer-plugin --no-qml-plugin
                           --configuration=pyqt5-android.cfg
                           --qmake=.../qmake
      make && make install
    NOTE: The pyqt5-android.cfg is edited to remove unused Qt modules
    (avoids OpenSSL / WebSockets link errors on Android).
    :param cfg:           BuildConfig
    :param extra_modules: list[str] | None
    :return:
    """
    log.info('-- Building static PyQt5 %s for %s --', cfg.pyqt_version, cfg.arch)
    src = cfg.pyqt5_src_dir
    if not exists(src):
        raise FileNotFoundError('PyQt5 source not found: {}'.format(src))
    env = _build_env(cfg)
    cfg_file = join(src, 'pyqt5-{}.cfg'.format(cfg.arch))
    # 1. Generate configuration file
    _run([cfg.pyqtdeploycli, '--package', 'pyqt5', '--target', cfg.arch, 'configure'], cwd=src, env=env,
         dry_run=cfg.dry_run)
    # 2. Prune unused Qt modules from the config file to avoid link errors.
    #    (e.g. QSslConfiguration undefined on Android without OpenSSL)
    modules = DEFAULT_PYQT5_MODULES + (extra_modules or [])
    if not cfg.dry_run and exists(cfg_file):
        _prune_pyqt5_config(cfg_file, modules)
    # 3. Run configure.py
    _run([cfg.python_exe, 'configure.py', '--static', '--verbose', '--sysroot={}'.format(cfg.sysroot_dir),
          '--no-tools', '--no-qsci-api', '--no-designer-plugin', '--no-qml-plugin',
          '--configuration={}'.format(cfg_file), '--qmake={}'.format(cfg.qmake)], cwd=src, env=env, dry_run=cfg.dry_run)
    # 4. make && make install.
    _run([getMakeExecutable(), '-j{}'.format(cpu_count() or 4)], cwd=src, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'install'], cwd=src, env=env, dry_run=cfg.dry_run)
    log.info('Static PyQt5 built :)')


def _prune_pyqt5_config(cfg_file, keep_modules):
    """
    Edit pyqt5-android.cfg to include only *keep_modules*.
    plashless Part 1: "I edit the pyqt5-android.cfg file, removing all Qt
    modules that I don't use ... down to QtCore, QtGui, QtWidgets, ..."
    :param cfg_file:     str
    :param keep_modules: list[str]
    :return:
    """
    log.info('Pruning pyqt5 config to modules: %s', keep_modules)
    text = _read_text(cfg_file)
    lines = text.splitlines()
    out = []
    in_mods = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[Qt]'):
            in_mods = True
            out.append(line)
            continue
        if in_mods and stripped.startswith('[') and not stripped.startswith('[Qt]'):
            in_mods = False
        if in_mods and stripped.startswith('Qt'):
            module_name = stripped.split('=')[0].strip()
            if module_name in keep_modules:
                out.append(line)
            else:
                log.debug('Removed module from config: %s', module_name)
        else:
            out.append(line)
    _write_text(cfg_file, '\n'.join(out) + '\n')
    log.info('Config pruned: %s', cfg_file)


def build_sysroot(cfg, extra_pyqt_modules=None):
    """
    Orchestrate the three-stage static build:
      Python -> SIP -> PyQt5  (all into cfg.sysroot_dir)
    From plashless Part 1 + pyqt-crom section 1.7.
    :param cfg:               BuildConfig
    :param extra_pyqt_modules: list[str] | None
    :return:
    """
    _step('Step 6/8 - Building sysroot (Python + SIP + PyQt5, cross-compiled)')
    _build_static_python(cfg)
    _build_static_sip(cfg)
    _build_static_pyqt5(cfg, extra_modules=extra_pyqt_modules)
    log.info('Sysroot complete: %s :)', cfg.sysroot_dir)


# ------------------------------------------------------------------------------
# Step 7 - pyqtdeploy: freeze app code -> Qt Creator project -> APK.
# ------------------------------------------------------------------------------

def build_apk(cfg):
    """
    Use pyqtdeploy to freeze the application and produce an APK.
    From plashless Part 2:
      1. pyqtdeploy <app>.pdt   (configure / GUI, or use existing .pdt)
         -> produces a Qt Creator .pro file
      2. qmake (ARM qmake)
      3. make
      4. androiddeployqt -> .apk
    pyqt-crom uses build_app.py which does:
      python3 build_app.py --pdt config.pdt --jobs 1 --target android-64 --qmake $QT_DIR/android/bin/qmake --verbose
    :param cfg: BuildConfig
    :return:    str  (path to the produced APK)
    """
    _step('Step 7/8 - Building APK (pyqtdeploy -> qmake -> make)')
    # Locate the .pdt project file.
    pdts = _rglob(cfg.project_dir, '*.pdt')
    if not pdts:
        raise FileNotFoundError(
            'No pyqtdeploy .pdt file found under {}.\n'
            'Create one with: pyqtdeploy <appname>.pdt\n'
            'See https://github.com/achille-martin/pyqt-crom for a full example.'.format(cfg.project_dir))
    pdt_file = pdts[0]
    log.info('Using .pdt: %s', pdt_file)
    _makedirs(cfg.build_dir)
    env = _build_env(cfg)
    # -- 1. pyqtdeploy build (generates .pro + frozen sources) ---------------
    log.info('Generating Qt Creator project from .pdt...')
    _run([cfg.pyqtdeploycli, 'build', '--target', cfg.arch, '--build-dir', cfg.build_dir, pdt_file],
         cwd=cfg.project_dir, env=env, dry_run=cfg.dry_run)
    # Find the generated .pro file.
    pros = _rglob(cfg.build_dir, '*.pro')
    if not pros and not cfg.dry_run:
        raise FileNotFoundError('pyqtdeploy did not generate a .pro file in {}.'.format(cfg.build_dir))
    pro_file = pros[0] if pros else join(cfg.build_dir, '{}.pro'.format(cfg.app_name))
    log.info('.pro file: %s', pro_file)
    # -- 2. qmake ------------------------------------------------------------
    log.info('Running qmake...')
    _run([cfg.qmake, pro_file, 'ANDROID_NDK_ROOT={}'.format(cfg.ndk_root),
          'ANDROID_SDK_ROOT={}'.format(cfg.sdk_root)], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # -- 3. make -------------------------------------------------------------
    log.info('Running make...')
    _run([getMakeExecutable(), '-j{}'.format(cpu_count() or 4)], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # -- 4. androiddeployqt -> APK --------------------------------------------
    log.info('Packaging APK with androiddeployqt...')
    android_build = join(cfg.build_dir, 'android-build')
    _makedirs(android_build)
    _run([cfg.androiddeployqt, "--input", join(cfg.build_dir, 'android_deployment_settings.json'), '--output',
          android_build, '--android-platform', 'android-{}'.format(ANDROID_API), '--gradle'], cwd=cfg.build_dir,
         env=env, dry_run=cfg.dry_run)
    # Locate the .apk
    apks = sorted(_rglob(android_build, '*.apk'), key=lambda p: getmtime(p) if exists(p) else 0)
    if not apks:
        if cfg.dry_run:
            log.info('[DRY-RUN] Build skipped; no APK produced.')
            return join(cfg.build_dir, '{}-debug.apk'.format(cfg.app_name))
        raise FileNotFoundError('Build completed but no .apk found under {}.'.format(android_build))
    apk = apks[-1]
    log.info('APK: %s (%.1f MB) :)', apk, getsize(apk) / 1024.0 ** 2)
    # Optionally copy to project releases dir (pyqt-crom convention).
    releases_dir = join(cfg.project_dir, 'releases')
    _makedirs(releases_dir)
    dest_apk = join(releases_dir, basename(apk))
    copy2(apk, dest_apk)
    log.info('Copied to: %s', dest_apk)
    # Clean up intermediate build directory unless --keep-build.
    if not cfg.keep_build:
        log.info('Removing intermediate build dir: %s', cfg.build_dir)
        rmtree(cfg.build_dir, ignore_errors=True)
    return dest_apk


# ------------------------------------------------------------------------------
# Step 8 - Optional ADB install.
# ------------------------------------------------------------------------------

def install_via_adb(cfg, apk_path):
    """
    Install the APK on the first ADB-connected Android device.
    From plashless QtCreator blog:
      - Enable Developer Options -> USB debugging on the device
      - adb devices  (confirm the device appears)
      - adb install /path/to/app.apk
    From pyqt-crom section 1.8:
      "Copy, install and run the .apk onto your phone (>=Android v9.0)"
    :param cfg:      BuildConfig
    :param apk_path: str
    :return: None
    """
    _step('Step 8/8 - Installing APK via ADB')
    adb = cfg.adb_exe if exists(cfg.adb_exe) else (which('adb') or '')
    if not adb or not exists(adb):
        log.warning(
            'adb not found. Install android-tools-adb and retry.'
            '\n  Ubuntu: sudo apt install android-tools-adb android-tools-fastboot')
        return
    res = _run([adb, 'devices'], capture=True, check=False)
    device_lines = [l for l in res.stdout.splitlines() if l.strip() and 'List of devices' not in l]
    if not device_lines:
        log.warning(
            'No ADB devices detected.\n'
            '  1. Enable Developer Options on your Android device.\n'
            '  2. Enable USB Debugging.\n'
            '  3. Accept the RSA fingerprint dialog, then retry.')
        return
    log.info('Connected device(s):\n%s', '\n'.join('  {}'.format(l) for l in device_lines))
    _run([adb, 'install', '-r', apk_path], dry_run=cfg.dry_run)
    log.info('Installation complete :)')
    log.info('Stream device logs with:\n  %s logcat | grep -i "%s"', adb, cfg.app_name.lower())


# ------------------------------------------------------------------------------
# Summary & error FAQ.
# ------------------------------------------------------------------------------

def print_summary(cfg, artifact):
    """
    :param cfg:      BuildConfig
    :param artifact: str | None
    :return:
    """
    _step('Build summary')
    lines = [
        '  App name        : {}'.format(cfg.app_name),
        '  Architecture    : {}'.format(cfg.arch),
        '  Qt version      : {}'.format(cfg.qt_version),
        '  PyQt5 version   : {}'.format(cfg.pyqt_version),
        '  Python version  : {}'.format(cfg.python_version),
        '  Project dir     : {}'.format(cfg.project_dir),
        '  Sysroot         : {}'.format(cfg.sysroot_dir),
        '  NDK             : {}'.format(cfg.ndk_root),
        '  SDK             : {}'.format(cfg.sdk_root)]
    if artifact:
        lines.append('  Output APK      : {}'.format(artifact))
    log.info('\n'.join(lines))
    if artifact and (exists(artifact) or cfg.dry_run):
        log.info('\n  Build succeeded!')
        log.info('   Install:  adb install %s', artifact)
        log.info('   Logs:     adb logcat | grep -i %s', cfg.app_name.lower())
    else:
        log.warning('\n  Build may not have completed successfully.')
    log.info(dedent("""
        ------------------------------------------------
        Common errors & fixes (from plashless / pyqt-crom)
        ------------------------------------------------
        * "skipping incompatible ... libQtGui.a"
          -> Architecture mismatch. Rebuild sysroot with the correct --arch.
            Check library arch: objdump -f libfoo.a | grep architecture

        * "undefined reference to 'PyInit__posixsubprocess'"
          -> Optional Python C extension missing from python.pro.
            Add the .c source file to MOD_SOURCES in Python-x.x/python.pro
            and re-run qmake + make + make install.

        * "undefined SYS_getdents64"
          -> Apply the patch from https://bugs.python.org/issue20307

        * "undefined EPOLL_CLOEXEC" or "undefined epoll_create1"
          -> Comment out in pyconfig.h:  /* #define HAVE_EPOLL_CREATE1 1 */

        * "undefined log2"
          -> In pyconfig.h: /* #define HAVE_LOG2 1 */  +  #undef HAVE_LOG2

        * "QSslConfiguration is undefined"
          -> Remove QtWebSockets / SSL-dependent modules from pyqt5-android.cfg

        * "mkdir: cannot create directory '/libs': Permission denied"
          -> Use latest PyQt5 snapshot; see QTBUG-39300.

        * Architecture undefined in objdump output
          -> Likely ARM. Verify with: arm-linux-androideabi-nm -a libfoo.a
        ------------------------------------------------
    """))


# ------------------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------------------

def build_arg_parser():
    """
    :return: ArgumentParser
    """
    parser = ArgumentParser(
        prog='pyqt5_android_builder', formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PyQt5 -> Android APK Builder (pyqtdeploy pipeline)
            ==================================================
            Cross-compiles Python + SIP + PyQt5 statically, then uses
            pyqtdeploy + Qt's qmake + androiddeployqt to produce an APK.
        """),
        epilog=dedent("""\
            Examples
            --------
            # Full build for arm64 (most modern Android devices):
              python pyqt5_android_builder.py --project-dir ./myapp --arch android-64

            # Build for 32-bit ARM (older devices):
              python pyqt5_android_builder.py --project-dir ./myapp --arch android-32

            # Use pre-installed Qt / SDK / NDK:
              python pyqt5_android_builder.py --project-dir ./myapp \\
                  --qt-dir ~/Qt5.15.2/5.15.2/android_arm64_v8a \\
                  --ndk-path ~/Android/Sdk/ndk/21.4.7075529 \\
                  --sdk-path ~/Android/Sdk

            # Sysroot only (skip APK build - useful for CI caching):
              python pyqt5_android_builder.py --project-dir ./myapp --only-sysroot

            # Build + install on connected device:
              python pyqt5_android_builder.py --project-dir ./myapp --install-apk

            # Include extra Qt modules (e.g. QtSql, QtBluetooth):
              python pyqt5_android_builder.py --project-dir ./myapp \\
                  --extra-pyqt-modules QtSql,QtBluetooth
        """))
    # Paths: use str instead of pathlib.Path – resolved manually in main()
    parser.add_argument('--project-dir', required=True, type=str,
                        help='Path to your PyQt5 project directory (must contain a .pdt file).')
    parser.add_argument(
        '--app-name', type=str, default=None, help='Application name. Defaults to the project directory name.')
    parser.add_argument('--arch', choices=list(ARCH_MAP), default='android-64',
                        help='Target Android architecture (default: android-64 = arm64-v8a).')
    # Version overrides.
    parser.add_argument('--qt-version', default=QT_VERSION)
    parser.add_argument('--pyqt-version', default=PYQT_VERSION)
    parser.add_argument('--sip-version', default=SIP_VERSION)
    parser.add_argument(
        '--python-version', default=PYTHON_VERSION, help='Cross-compiled Python version (default: %(default)s).')
    # Pre-existing toolchain paths.
    parser.add_argument('--qt-dir', type=str, default=None,
                        help='Qt 5.15 Android arch directory (e.g. ~/Qt5.15.2/5.15.2/android_arm64_v8a).')
    parser.add_argument('--ndk-path', type=str, default=None,
                        help='Android NDK root (e.g. ~/Android/Sdk/ndk/21.4.7075529).')
    parser.add_argument('--sdk-path', type=str, default=None, help='Android SDK root (e.g. ~/Android/Sdk).')
    # Module selection.
    parser.add_argument('--extra-pyqt-modules', type=str, default='',
                        help='Comma-separated extra Qt modules to include (e.g. QtSql,QtBluetooth).')
    # Build control flags.
    parser.add_argument('--only-sysroot', action='store_true', help='Build sysroot only; skip APK packaging.')
    parser.add_argument(
        '--install-apk', action="store_true", help='Install the produced APK on the first ADB-connected device.')
    parser.add_argument(
        '--keep-build', action='store_true', help='Keep intermediate build directory after APK is produced.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug-level output.')
    return parser


# ------------------------------------------------------------------------------
# Main entry point.
# ------------------------------------------------------------------------------

def main(argv=None):
    """
    :param argv: list[str] | None
    :return:     int
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        getLogger().setLevel(DEBUG)
    # -- Resolve project directory --------------------------------------------
    project_dir = realpath(args.project_dir)
    app_name = args.app_name or basename(normpath(project_dir))
    extra_mods = [m.strip() for m in args.extra_pyqt_modules.split(
        ',') if m.strip()] if args.extra_pyqt_modules else None

    # -- Resolve optional toolchain paths: expanduser + realpath --------------
    def _resolve_optional(p):
        """
        Return realpath(expanduser(p)) when p is given, else None.
        :param p: str
        :return: str | None
        """
        return None if not p else realpath(expanduser(p))

    cfg = BuildConfig(
        project_dir=project_dir,
        app_name=app_name,
        arch=args.arch,
        qt_version=args.qt_version,
        pyqt_version=args.pyqt_version,
        sip_version=args.sip_version,
        python_version=args.python_version,
        qt_dir=_resolve_optional(args.qt_dir),
        ndk_path=_resolve_optional(args.ndk_path),
        sdk_path=_resolve_optional(args.sdk_path),
        verbose=args.verbose,
        dry_run=args.dry_run,
        keep_build=args.keep_build,
        only_sysroot=args.only_sysroot,
        install_apk=args.install_apk)
    artifact = None  # type: str | None
    try:
        preflight_checks(cfg)
        setup_directories(cfg)
        setup_toolchain(cfg)
        setup_venv(cfg)
        download_sources(cfg)
        build_sysroot(cfg, extra_pyqt_modules=extra_mods)
        if not args.only_sysroot:
            artifact = build_apk(cfg)
            if args.install_apk and artifact:
                install_via_adb(cfg, artifact)
        print_summary(cfg, artifact)
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
        log.warning('Interrupted by user.')
        return 130


if __name__ == '__main__':
    exit(main())
