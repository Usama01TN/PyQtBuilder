#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyQt5 -> iOS App Builder  (pyqtdeploy pipeline)
================================================
Automates the complete pipeline for packaging a PyQt5 application as an
iOS .xcodeproj (simulator + device) using pyqtdeploy 3.3.x on macOS.

References / Sources:
--------------------
- https://oforoshima.medium.com/i-was-able-to-create-an-ios-app-using-pyqt-1dce4839bc38
- https://plashless.wordpress.com/2014/09/14/using-pyqtdeploy-on-macos-to-cross-compile-a-pyqt-app-for-ios-part-2/
- https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/demo.html
- https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/sysroot.html
- https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/building.html
- https://pypi.org/project/pyqtdeploy/

Pipeline overview:
-----------------
  1. Preflight       - macOS host, Xcode, Python 3.10-3.12, disk space.
  2. Host venv       - install pyqtdeploy + PyQt5 + pyqt-builder on the host.
  3. Toolchain check - Qt 5.15.x iOS qmake, Xcode SDK, Apple SDK version.
  4. Source download - Python, SIP, PyQt5 GPL tarballs.
  5. Bug patches     - patch three known pyqtdeploy issues:
                         a) platforms.py:  arm64 (Apple Silicon) not recognised.
                         b) python.pro:    getentropy() undeclared on iOS.
                         c) SIP.py:        case-sensitive glob misses pyqt5_sip.
  6. sysroot.toml    - generate or update the TOML configuration.
  7. Sysroot build   - pyqtdeploy-sysroot -> static Python + SIP + PyQt5.
  8. App build       - pyqtdeploy-build  -> Xcode project (.xcodeproj).
  9. Xcode           - open project in Xcode (or run xcodebuild for simulator).

Usage:
-----
    python pyqt5_ios_builder.py --project-dir /path/to/myapp [OPTIONS]
    # Full build (generates Xcode project):
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake
    # Build AND open in Xcode automatically:
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --open-xcode
    # Build AND run on the iOS Simulator:
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --run-simulator
    # Sysroot only (cache it; skip the app build):
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --only-sysroot
    # Use an existing sysroot (skip rebuild):
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --sysroot ~/ios/iRoot
    # Verbose + keep all build intermediates:
    python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --verbose --keep-build

Requirements (host machine):
----------------------------
    - macOS 13 Ventura or later (Apple Silicon M1/M2/M3 or Intel).
    - Xcode 15 or later  (with iOS SDK).
    - Xcode Command Line Tools  (xcode-select --install).
    - Qt 5.15.x installed from qt.io  -- select the iOS component.
    - Python 3.10 / 3.11 / 3.12  (Miniconda or system).
    - ~30 GB free disk space.
    - Internet access (first run only).
"""
from os.path import isdir, join, basename, dirname, exists, realpath, normpath, expanduser
from re import DOTALL, compile, finditer, MULTILINE, sub, search, IGNORECASE
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from logging import getLogger, DEBUG, basicConfig, INFO
from os import environ, makedirs, walk, listdir
from subprocess import check_call, Popen, PIPE
from platform import mac_ver, system, machine
from sys import exit, version_info, path
from textwrap import dedent
from fnmatch import filter
from json import loads
import tarfile
import io

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import which, getXcrunExecutable, getOpenExecutable, getXcodebuildExecutable, \
        getXcodeSelectExecutable
except:
    from builders import which, getXcrunExecutable, getOpenExecutable, getXcodebuildExecutable, getXcodeSelectExecutable

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
    from collections import namedtuple
    from os import statvfs

    _DiskUsage = namedtuple('DiskUsage', ['total', 'used', 'free'])


    def disk_usage(pth):
        """
        Minimal os.statvfs-based replacement for shutil.disk_usage (Python / macOS).
        :param pth: str
        :return: _DiskUsage
        """
        st = statvfs(pth)
        return _DiskUsage(
            st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)

try:
    from venv import create
except:
    def create(venv_dir, with_pip=True, clear=True):
        """
        Delegate to the 'virtualenv' command.
        :param venv_dir: str
        :param with_pip: bool
        :param clear: bool
        :return:
        """
        check_call(['virtualenv', venv_dir])


# ---------------------------------------------------------------------------
# os.path / io helpers  (replacing pathlib.Path throughout).
# ---------------------------------------------------------------------------

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
    Replaces Path.rglob() without pathlib.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []  # type: list[str]
    for root, _dirs, files in walk(directory):
        for filename in filter(files, pattern):
            matches.append(join(root, filename))
    return matches


def _glob_dir(directory, pattern):
    """
    List direct children of *directory* whose names match *pattern*.
    Replaces Path.glob() for a single directory level without pathlib.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    results = []  # type: list[str]
    try:
        entries = listdir(directory)  # type: list[str]
    except OSError:
        return results
    for entry in filter(entries, pattern):
        results.append(join(directory, entry))
    return results


def _read_text(pth, encoding="utf-8"):
    """
    Read and return the entire contents of *path* as a unicode string.
    Replaces Path.read_text() without pathlib.
    :param pth: str
    :param encoding: str
    :return: str
    """
    with io.open(pth, "r", encoding=encoding) as fh:
        return fh.read()


def _write_text(pth, text, encoding="utf-8"):
    """
    Write *text* (unicode string) to *path*, overwriting any existing content.
    Replaces Path.write_text() without pathlib.
    :param pth: str
    :param text: str
    :param encoding: str
    :return:
    """
    with io.open(pth, "w", encoding=encoding) as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# subprocess helper  (replacing subprocess.run + CompletedProcess)
# ---------------------------------------------------------------------------

class SimpleProcess(object):
    """
    Minimal stand-in for subprocess.CompletedProcess.
    """

    def __init__(self, args, returncode, stdout='', stderr=''):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout or ''  # type: str
        self.stderr = stderr or ''  # type: str


# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
# Tested combination that is known to work (OforOshima article).
QT_VERSION = '5.15.18'  # type: str
PYQT_VERSION = '5.15.11'  # type: str  # Only version available for PyQt5 GPL at time of writing.
SIP_ABI_MAJOR = '12'  # type: str  # Must align with PYQT_VERSION.
PYTHON_VERSION = '3.12.3'  # type: str  # Version embedded in iOS.
PYQTDEPLOY_VERSION = '3.3.0'  # type: str
PYQT_BUILDER_VERSION = '1.16.4'  # type: str
TARGET = 'ios-64'  # type: str  # Pyqtdeploy target string for iOS.
# Default Qt modules bundled (mirrors OforOshima's stripped sysroot.toml).
DEFAULT_PYQT5_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtPrintSupport', 'QtSvg', 'QtNetwork']  # type: list[str]
# Download URLs.
PYQT5_URL = 'https://files.pythonhosted.org/packages/source/P/PyQt5/PyQt5-{}.tar.gz'.format(PYQT_VERSION)  # type: str
PYTHON_URL = 'https://www.python.org/ftp/python/{}/Python-{}.tgz'.format(PYTHON_VERSION, PYTHON_VERSION)  # type: str
# Minimum free disk space (GB).
MIN_DISK_GB = 30  # type: int
# Resolved home directory  (replaces Path.home())
HOME_DIR = expanduser('~')  # type: str
DEFAULT_BASE_DIR = join(HOME_DIR, '.pyqt5_ios_build')  # type: str
# ------------------------------------------------------------------------------
# Logging.
# ------------------------------------------------------------------------------
basicConfig(format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S', level=INFO)
log = getLogger('pyqt5-ios-builder')


def _step(title):
    """
    Print a visually distinct step header.
    :param title: str
    """
    bar = '=' * 66  # type: str
    log.info('\n%s\n  %s\n%s', bar, title, bar)


# ------------------------------------------------------------------------------
# Configuration class.
# ------------------------------------------------------------------------------

class BuildConfig(object):
    """
    All resolved paths and options for a single build run.
    """

    def __init__(self, project_dir, app_name, qmake_path, qt_version=QT_VERSION, pyqt_version=PYQT_VERSION,
                 python_version=PYTHON_VERSION, sysroot_path=None, verbose=False, dry_run=False, keep_build=False,
                 only_sysroot=False, open_xcode=False, run_simulator=False, extra_modules=None):
        """
        :param project_dir: str
        :param app_name: str
        :param qmake_path: (str) ~/Qt/5.15.18/ios/bin/qmake
        :param qt_version: str
        :param pyqt_version: str
        :param python_version: str
        :param sysroot_path: (str | None) Skip sysroot build if given.
        :param verbose: bool
        :param dry_run: bool
        :param keep_build: bool
        :param only_sysroot: bool
        :param open_xcode: bool
        :param run_simulator: bool
        :param extra_modules: list[str] | None
        """
        self.project_dir = project_dir
        self.app_name = app_name
        self.qmake_path = qmake_path
        self.qt_version = qt_version
        self.pyqt_version = pyqt_version
        self.python_version = python_version
        self.sysroot_path = sysroot_path
        self.verbose = verbose
        self.dry_run = dry_run
        self.keep_build = keep_build
        self.only_sysroot = only_sysroot
        self.open_xcode = open_xcode
        self.run_simulator = run_simulator
        self.extra_modules = extra_modules or []
        # Derived paths (resolved immediately, like __post_init__)
        base = DEFAULT_BASE_DIR  # type: str
        self.work_dir = base  # type: str
        self.venv_dir = join(base, 'venv')  # type: str
        self.src_dir = join(base, 'sources')  # type: str
        self.sysroot_dir = sysroot_path if sysroot_path else join(base, 'sysroot-ios-64')  # type: str
        self.build_dir = join(self.project_dir, 'build-{}'.format(TARGET))  # type: str

    # -- Computed paths -------------------------------------------------------

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
    def qt_ios_dir(self):
        """
        Parent of the iOS qmake binary  (~/Qt/5.15.18/ios).
        :return: str
        """
        # qmake_path = ~/Qt/5.15.18/ios/bin/qmake
        # parent     = ~/Qt/5.15.18/ios/bin
        # parent.parent = ~/Qt/5.15.18/ios
        return dirname(dirname(self.qmake_path))

    @property
    def pyqt5_src_dir(self):
        """
        :return: str
        """
        return join(self.src_dir, 'PyQt5-{}'.format(self.pyqt_version))

    @property
    def python_src_dir(self):
        """
        :return: str
        """
        return join(self.src_dir, 'Python-{}'.format(self.python_version))

    @property
    def sysroot_toml(self):
        """
        :return: str
        """
        return join(self.project_dir, 'sysroot.toml')

    @property
    def xcodeproj(self):
        """
        :return: str | None
        """
        hits = _rglob(self.build_dir, '*.xcodeproj')
        return hits[0] if hits else None

    # -- pyqtdeploy package paths (inside the venv site-packages) ------------

    @property
    def _pyqtdeploy_pkg(self):
        """
        :return: str | None
        """
        lib_dir = join(self.venv_dir, 'lib')
        for pyver_dir in _glob_dir(lib_dir, 'python3.*'):
            candidate = join(pyver_dir, 'site-packages', 'pyqtdeploy')
            if isdir(candidate):
                return candidate
        return None

    @property
    def platforms_py(self):
        """
        :return: str | None
        """
        pkg = self._pyqtdeploy_pkg
        return join(pkg, 'platforms.py') if pkg else None

    @property
    def python_pro(self):
        """
        :return: str | None
        """
        pkg = self._pyqtdeploy_pkg
        if pkg is None:
            return None
        candidate = join(pkg, 'sysroot', 'plugins', 'Python', 'configurations', 'python.pro')
        return candidate if exists(candidate) else None

    @property
    def sip_plugin_py(self):
        """
        :return: str | None
        """
        pkg = self._pyqtdeploy_pkg
        if pkg is None:
            return None
        candidate = join(pkg, 'sysroot', 'plugins', 'SIP.py')
        return candidate if exists(candidate) else None


# ------------------------------------------------------------------------------
# Subprocess helpers
# ------------------------------------------------------------------------------

def _run(cmd, cwd=None, env=None, check=True, dry_run=False, capture=False):
    """
    Run a subprocess with unified error handling.
    :param cmd:     list[str]
    :param cwd:     str | None
    :param env:     dict | None
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
    merged = {}
    merged.update(environ)
    merged.update(env or {})
    try:
        if capture:
            proc = Popen(
                cmd_strs, cwd=cwd if cwd else None, env=merged, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        else:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, env=merged, universal_newlines=True)
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
        raise EnvironmentError('Required tool "{}" not found on PATH. Install it and re-run.'.format(name))
    return pth


def _download(url, dest):
    """
    Download *url* to *dest*, showing progress.
    :param url:  str
    :param dest: str
    :return: None
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


def _extract(archive, dest_dir):
    """
    Extract a tar archive to *dest_dir*.
    :param archive:  str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info('Extracting %s', basename(archive))
    with tarfile.open(archive) as tf:
        tf.extractall(path=dest_dir)


def _check_disk(pth, required_gb=MIN_DISK_GB):
    """
    Warn if free disk space at *path* is below *required_gb* gigabytes.
    :param pth:        str
    :param required_gb: int
    :return:
    """
    check_path = pth if exists(pth) else dirname(pth)  # type: str
    free_gb = disk_usage(check_path).free / 1024.0 ** 3  # type: float
    if free_gb < required_gb:
        log.warning('Low disk space: %.1f GB free at %s (recommended >= %d GB).', free_gb, pth, required_gb)
    else:
        log.info('Disk space: %.1f GB free :)', free_gb)


# ------------------------------------------------------------------------------
# Step 1 - Preflight checks.
# ------------------------------------------------------------------------------

def preflight_checks(cfg):
    """
    Validate the host environment.
    Requirements (OforOshima + plashless):
      - macOS host (iOS toolchain only exists on macOS)
      - Python 3.10-3.12
      - Xcode + Xcode Command Line Tools
      - xcode-select pointing to a valid developer dir
      - Qt 5.15.x iOS qmake reachable at --qmake path
      - >=30 GB free disk space
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 1/9 - Preflight checks')
    # macOS is mandatory (iOS cross-compilation requires Apple's toolchain).
    if system() != 'Darwin':
        raise EnvironmentError(
            'iOS builds require macOS. Detected host: {}. Run this script on a Mac with Xcode installed.'.format(
                system()))
    log.info('Host OS : macOS %s', mac_ver()[0])
    # Python version.
    major, minor = version_info[:2]
    if (major, minor) < (3, 10) or (major, minor) > (3, 12):
        raise EnvironmentError(
            'Python 3.10 - 3.12 required (found {}.{}).\n'
            'Install via Miniconda:  conda install python=3.12'.format(major, minor))
    log.info('Python  : %d.%d :)', major, minor)
    # Apple Silicon / Intel -- both are fine; arm64 patch handles Silicon.
    arch = machine()
    log.info('CPU arch: %s', arch)
    if arch == 'arm64':
        log.info('Apple Silicon detected - platforms.py patch will be applied (Step 5a).')
    # Xcode
    xcodebuild = _require_tool('xcodebuild')
    res = _run([xcodebuild, '-version'], capture=True, check=False)
    xcode_lines = (res.stdout or '').splitlines()
    log.info('Xcode   : %s', xcode_lines[0] if xcode_lines else 'unknown')
    # Xcode Command Line Tools.
    _require_tool('xcrun')
    res = _run([getXcodeSelectExecutable(), '--print-path'], capture=True, check=False)
    dev_dir = (res.stdout or '').strip()
    if not dev_dir or not exists(dev_dir):
        raise EnvironmentError(
            'Xcode developer path is not set.\nRun:  xcode-select --install\n'
            'Then: sudo xcode-select --switch /Applications/Xcode.app')
    log.info('Xcode dev dir: %s :)', dev_dir)
    # iOS SDK
    res = _run([getXcrunExecutable(), '--sdk', 'iphoneos', '--show-sdk-path'], capture=True, check=False)
    ios_sdk = (res.stdout or '').strip()
    if not ios_sdk:
        raise EnvironmentError('iOS SDK not found. Install Xcode from the App Store and accept the license agreement.')
    log.info('iOS SDK : %s :)', ios_sdk)
    # Qt iOS qmake.
    if not exists(cfg.qmake_path):
        raise FileNotFoundError(
            'Qt iOS qmake not found: {}\n'
            'Install Qt {} from https://www.qt.io/ and select the iOS component.\n'
            'Then pass --qmake ~/Qt/5.15.18/ios/bin/qmake'.format(cfg.qmake_path, QT_VERSION))
    res = _run([cfg.qmake_path, '--version'], capture=True, check=False)
    qmake_lines = (res.stdout or '').splitlines()
    log.info('qmake   : %s', qmake_lines[0] if qmake_lines else "unknown")
    # Project directory.
    if not isdir(cfg.project_dir):
        raise FileNotFoundError('Project directory not found: {}'.format(cfg.project_dir))
    # Look for a .pdt file (pyqtdeploy project descriptor).
    pdts = _rglob(cfg.project_dir, '*.pdt')
    if not pdts:
        log.warning(
            'No pyqtdeploy .pdt project file found in %s.\n'
            '  You can create one with:  pyqtdeploy <appname>.pdt\n'
            '  Or use the demo:  https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/demo.html',
            cfg.project_dir)
    else:
        log.info('.pdt    : %s :)', pdts[0])
    # Git (optional but expected).
    git = which('git')
    log.info('git     : %s', git or 'NOT FOUND (optional)')
    # Disk space.
    _check_disk(dirname(cfg.work_dir))
    log.info('Preflight passed :)')


# ------------------------------------------------------------------------------
# Step 2 - Host virtual environment
# ------------------------------------------------------------------------------

def setup_venv(cfg):
    """
    Create a host venv and install pyqtdeploy + host PyQt5 into it.
    From OforOshima:
      pip install PyQt5 PyQt5_sip pyqtdeploy pyqt-builder
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 2/9 - Host virtual environment')
    _makedirs(cfg.work_dir)
    if exists(cfg.venv_dir):
        log.info('Reusing venv: %s', cfg.venv_dir)
    else:
        log.info('Creating venv: %s', cfg.venv_dir)
        create(cfg.venv_dir, with_pip=True, clear=True)
    _run([cfg.python_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'], dry_run=cfg.dry_run)
    packages = ['PyQt5=={}'.format(cfg.pyqt_version), 'PyQt5_sip', 'pyqtdeploy=={}'.format(PYQTDEPLOY_VERSION),
                'pyqt-builder=={}'.format(PYQT_BUILDER_VERSION)]
    _run([cfg.pip_exe, 'install'] + packages + ['--quiet', '--no-warn-script-location'], dry_run=cfg.dry_run)
    if not cfg.dry_run:
        res = _run(
            [cfg.python_exe, '-c', 'import pyqtdeploy; print(pyqtdeploy.__version__)'], capture=True, check=False)
        log.info('pyqtdeploy version: %s :)', res.stdout.strip())
    log.info('Virtual environment ready :)')


# ------------------------------------------------------------------------------
# Step 3 - Toolchain validation
# ------------------------------------------------------------------------------

def validate_toolchain(cfg):
    """
    Confirm that the Qt iOS kit is complete.
    From plashless Part 2:
      "Note that iOS does not allow dynamic libraries.
       Hence, Qt for iOS by default is built as static libraries."
    We verify the key binaries and frameworks are present.
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 3/9 - iOS toolchain validation')
    # iOS qmake (already checked in preflight, just log details).
    qt_ios = cfg.qt_ios_dir
    log.info('Qt iOS dir: %s', qt_ios)
    # Key frameworks' directory.
    frameworks = join(qt_ios, 'lib')
    if not exists(frameworks):
        raise EnvironmentError(
            'Qt iOS frameworks directory not found: {}\n'
            'Re-run the Qt installer and include the iOS component.'.format(frameworks))
    # Spot-check for QtCore.
    qtcore_candidates = _glob_dir(frameworks, 'QtCore.*')
    if not qtcore_candidates:
        raise EnvironmentError(
            'QtCore not found under {}.\nThe Qt iOS installation appears incomplete.'.format(frameworks))
    log.info('Qt iOS lib dir: %s :)', frameworks)
    log.info('QtCore: %s :)', basename(qtcore_candidates[0]))
    # Verify Apple SDK version for compatibility notes.
    res = _run([getXcrunExecutable(), '--sdk', 'iphoneos', '--show-sdk-version'], capture=True, check=False)
    sdk_ver = (res.stdout or '').strip()
    log.info('iOS SDK version: %s', sdk_ver)
    try:
        if sdk_ver and float(sdk_ver.split(".")[0]) < 16:
            log.warning('iOS SDK %s detected. Qt 5.15 + pyqtdeploy 3.3 work best with iOS SDK 16 or later.', sdk_ver)
    except ValueError:
        pass
    log.info('Toolchain validated :)')


# ------------------------------------------------------------------------------
# Step 4 - Download source tarballs
# ------------------------------------------------------------------------------

def download_sources(cfg):
    """
    Download Python and PyQt5 GPL source tarballs.
    From OforOshima:
      "Download the PyQt source code from pypi.org/project/PyQt5/#PyQt5-5.15.11.tar.gz
       Navigate to python.org/downloads and download the gzipped source tarball."
    SIP itself is handled by pyqtdeploy-sysroot via sysroot.toml (no tarball needed
    separately when using pyqtdeploy 3.3 - the SIP module version is declared in TOML).
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 4/9 - Downloading source tarballs')
    _makedirs(cfg.src_dir)
    downloads = [
        (PYQT5_URL, join(cfg.src_dir, 'PyQt5-{}.tar.gz'.format(cfg.pyqt_version))),
        (PYTHON_URL, join(cfg.src_dir, 'Python-{}.tgz'.format(cfg.python_version)))]
    for url, dest in downloads:
        if cfg.dry_run:
            log.info("[DRY-RUN] Would download: %s -> %s", url, basename(dest))
        else:
            _download(url, dest)
            # Strip both .tgz and .tar.gz to get the stem directory name.
            stem = basename(dest).replace('.tgz', '').replace('.tar.gz', '')
            extracted = join(cfg.src_dir, stem)
            if not exists(extracted):
                _extract(dest, cfg.src_dir)
    log.info('Sources ready :)')


# ------------------------------------------------------------------------------
# Step 5 - Apply the three known pyqtdeploy bug patches.
# ------------------------------------------------------------------------------
# -- 5a: platforms.py - arm64 (Apple Silicon) not recognised -----------------
_ARM64_PATCH_MARKER = '# [pyqt5-ios-builder] arm64 patch applied'


def patch_platforms_py(cfg):
    """
    Patch pyqtdeploy/platforms.py to recognise Apple Silicon arm64.
    From OforOshima:
      "pyqtdeploy-sysroot: 'macos-32' is not a supported architecture"
      "There is no handling for Apple Silicon's 'arm64'."
      "To fix this, I added support for arm64."
    The relevant code in platforms.py (around line 180) maps the host
    machine string to an internal architecture name.  We inject an entry
    for 'arm64' -> 'macos-64' so Apple Silicon is treated identically to
    Intel x86-64 for the purposes of building the sysroot on the host.
    :param cfg: BuildConfig
    :return:    None
    """
    _step('Step 5a/9 - Patch: platforms.py (Apple Silicon arm64)')
    pf = cfg.platforms_py
    if pf is None or not exists(pf):
        log.warning('platforms.py not found - skipping patch (may not be needed).')
        return
    text = _read_text(pf)
    if _ARM64_PATCH_MARKER in text:
        log.info('platforms.py already patched :)')
        return
    # Strategy: find the dict / if-elif block that maps machine() strings to
    # arch names and insert 'arm64' -> 'macos-64' before the first 'else' or
    # 'raise' that signals an unrecognised architecture.
    # Pattern 1: explicit dict (pyqtdeploy >= 3.2)
    dict_pattern = compile(
        r'(_MACHINE_ARCH\s*=\s*\{[^}]*?)'  # dict body.
        r'(\})',  # closing brace.
        DOTALL)
    dict_match = dict_pattern.search(text)
    # Pattern 2: if/elif chain (older pyqtdeploy).
    elif_pattern = compile(
        r"(machine\(\)\s*==\s*['\"]x86_64['\"][^:]*:[^\n]*\n)"  # x86_64 branch
        r'(\s*)(elif|else|raise)',  # next clause.
    )
    elif_match = elif_pattern.search(text)
    patched = False
    if dict_match:
        # Insert 'arm64': 'macos-64' into the mapping dict
        new_entry = "    'arm64': 'macos-64',\n    " + _ARM64_PATCH_MARKER + "\n"
        new_text = text[:dict_match.start(2)] + new_entry + text[dict_match.start(2):]
        _write_text(pf, new_text)
        patched = True
        log.info('Patched _MACHINE_ARCH dict in platforms.py :)')
    elif elif_match:
        # Inject elif arm64 before the next clause.
        indent = elif_match.group(2)
        insertion = ("{indent}elif machine() == 'arm64':\n{indent}    arch = 'macos-64'    {marker}\n{indent}{next_kw}"
                     ).format(indent=indent, marker=_ARM64_PATCH_MARKER, next_kw=elif_match.group(3))
        new_text = (text[:elif_match.start(2)] + elif_match.group(1) + insertion + text[elif_match.end(3):])
        _write_text(pf, new_text)
        patched = True
        log.info('Injected arm64 elif branch in platforms.py :)')
    if not patched:
        log.warning('Could not auto-patch platforms.py.\n  File: %s\n'
                    "  Manual fix: add  'arm64': 'macos-64'  to the architecture mapping dict (around line 180).", pf)


# -- 5b: python.pro - getentropy() undeclared on iOS -------------------------
_GETENTROPY_PATCH_MARKER = '# [pyqt5-ios-builder] getentropy patch applied'


def patch_python_pro(cfg):
    """
    Patch python.pro to suppress the getentropy() implicit-declaration error.
    From OforOshima:
      "Python/bootstrap_hash.c:225:19: error: call to undeclared function
       'getentropy'; ISO C99 and later do not support implicit function
       declarations"
      "Adding the -Wno-implicit-function-declaration flag resolves the issue."
      "The Makefile used is generated by qmake from:
       .../pyqtdeploy/sysroot/plugins/Python/configurations/python.pro"
    We add  QMAKE_CFLAGS += -Wno-implicit-function-declaration
    near the macOS/iOS-specific compilation options.
    :param cfg: BuildConfig
    :return:    None
    """
    _step('Step 5b/9 - Patch: python.pro (getentropy undeclared)')
    pp = cfg.python_pro
    if pp is None or not exists(pp):
        log.warning('python.pro not found - skipping patch.')
        return
    text = _read_text(pp)
    if _GETENTROPY_PATCH_MARKER in text:
        log.info('python.pro already patched :)')
        return
    # We add the flag at the very end of the file (safest location; qmake
    # processes all QMAKE_CFLAGS += assignments cumulatively).
    append_block = dedent("""

        # {marker}
        # Suppress 'getentropy' undeclared-function error on iOS 15 and earlier.
        # See: https://oforoshima.medium.com/i-was-able-to-create-an-ios-app-using-pyqt-1dce4839bc38
        ios {{
            QMAKE_CFLAGS += -Wno-implicit-function-declaration
        }}
    """.format(marker=_GETENTROPY_PATCH_MARKER))
    _write_text(pp, text + append_block)
    log.info('Appended -Wno-implicit-function-declaration to python.pro :)')
    log.info('Patched: %s', pp)


# -- 5c: SIP.py - case-sensitive glob misses pyqt5_sip tarball ---------------
_SIP_PATCH_MARKER = '# [pyqt5-ios-builder] case-insensitive glob patch applied'


def patch_sip_plugin(cfg):
    """
    Patch SIP.py to use a case-insensitive glob when finding the sip sdist.
    From OforOshima:
      "pyqtdeploy-sysroot: SIP: sip-module didn't create an sdist."
      "It should be finding a working file like pyqt5_sip-12.13.0.tar.gz,
       but due to a case sensitivity mismatch it fails to locate the file."
      "I created a custom method that allows glob.glob() to match filenames
       regardless of uppercase or lowercase differences."
    Implementation: we inject a helper function _iglob() that translates each
    character of the pattern into a case-insensitive character class, then
    replace all glob.glob() calls in SIP.py with _iglob().
    :param cfg: BuildConfig
    """
    _step('Step 5c/9 - Patch: SIP.py (case-insensitive glob)')
    sp = cfg.sip_plugin_py
    if sp is None or not exists(sp):
        log.warning('SIP.py not found - skipping patch.')
        return
    text = _read_text(sp)
    if _SIP_PATCH_MARKER in text:
        log.info('SIP.py already patched :)')
        return
    # Build the helper we want to inject.
    # Note: double-braces {{ }} produce literal braces in the formatted string.
    helper = dedent("""

        # {marker}
        # Case-insensitive glob helper injected by pyqt5-ios-builder.
        # Fixes: pyqtdeploy-sysroot: SIP: sip-module didn't create an sdist.
        # Source: https://oforoshima.medium.com/i-was-able-to-create-an-ios-app-using-pyqt-1dce4839bc38
        import glob as _glob_module
        def _iglob(pattern):
            \"\"\"glob.glob() wrapper that is case-insensitive on all platforms.\"\"\"
            def _ci(c):
                return '[{{0}}{{1}}]'.format(c.lower(), c.upper()) if c.isalpha() else c
            ci_pattern = ''.join(_ci(c) for c in pattern)
            return _glob_module.glob(ci_pattern)

    """.format(marker=_SIP_PATCH_MARKER))
    # Inject the helper right after the last import statement at the top of the file.
    import_end = 0
    for m in finditer(r"^import |^from ", text, MULTILINE):
        nl = text.find("\n", m.start())
        if nl > import_end:
            import_end = nl
    if import_end == 0:
        import_end = len(text)
    patched_text = text[:import_end + 1] + helper + text[import_end + 1:]

    # Replace glob.glob( -> _iglob( specifically for sip-related pattern lines
    # (only lines whose pattern string contains 'sip' case-insensitively).
    def _replace_sip_glob(m):
        """
        :param m: Match
        :return:  str
        """
        call = m.group(0)
        return call.replace('glob.glob(', '_iglob(') if search(r'sip', call, IGNORECASE) else call

    patched_text = sub(r'glob\.glob\([^)]+\)', _replace_sip_glob, patched_text)
    _write_text(sp, patched_text)
    log.info('SIP.py patched with case-insensitive glob :)')
    log.info('Patched: %s', sp)


def apply_all_patches(cfg):
    """
    Apply all three bug patches discovered by OforOshima.
    :param cfg: BuildConfig
    :return: None
    """
    if cfg.dry_run:
        log.info('[DRY-RUN] Skipping patches in dry-run mode.')
        return
    patch_platforms_py(cfg)
    patch_python_pro(cfg)
    patch_sip_plugin(cfg)
    log.info('All patches applied :)')


# ------------------------------------------------------------------------------
# Step 6 - Generate / update sysroot.toml
# ------------------------------------------------------------------------------

def generate_sysroot_toml(cfg):
    """
    Write a sysroot.toml tuned for iOS.
    From OforOshima:
      - Changed PyQt version to 5.15.11
      - Commented out all modules from PyQt3D to QScintilla
      - Changed Qt version to 5.15.18
      - SIP ABI major version 12
    From pyqtdeploy docs (sysroot.html):
      [SIP] abi_major_version = 12  module_name = "PyQt5.sip"
    :param cfg: BuildConfig
    :return: None
    """
    _step('Step 6/9 - sysroot.toml')
    if exists(cfg.sysroot_toml):
        log.info("sysroot.toml already exists: %s", cfg.sysroot_toml)
        log.info("Tip: edit it to change Qt modules or version pins, then re-run.")
        _patch_sysroot_toml_versions(cfg)
        return
    modules_block = "\n".join('    "{}",'.format(m) for m in (DEFAULT_PYQT5_MODULES + cfg.extra_modules))
    toml_content = dedent("""\
        # sysroot.toml - generated by pyqt5-ios-builder
        # Edit as needed, then re-run the builder.
        # Reference: https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/sysroot.html

        [python]
        version = "{python_version}"

        [sip]
        abi_major_version = {sip_abi}
        module_name = "PyQt5.sip"

        [PyQt5]
        version = "{pyqt_version}"
        # Qt modules to include (keep this list minimal to avoid link errors)
        modules = [
        {modules_block}
        ]
        # Disable extras that often fail on iOS:
        disabled_features = [
            "PyQt_Desktop_OpenGL",
            "PyQt_qreal_double",
        ]

        [PyQt5.qt]
        version = "{qt_version}"
        # Tell pyqtdeploy to use the pre-installed Qt rather than building from source.
        # Set this to the parent of the iOS arch directory, e.g. ~/Qt/5.15.18/ios
        install_from_source = false

        [zlib]
        install_from_source = false
    """.format(
        python_version=cfg.python_version,
        sip_abi=SIP_ABI_MAJOR,
        pyqt_version=cfg.pyqt_version,
        modules_block=modules_block,
        qt_version=cfg.qt_version))
    _write_text(cfg.sysroot_toml, toml_content)
    log.info('sysroot.toml written: %s :)', cfg.sysroot_toml)


def _patch_sysroot_toml_versions(cfg):
    """
    If sysroot.toml already exists, make sure the version pins match cfg.
    From OforOshima: "Changed PyQt version to 5.15.11 ... Qt version to 5.15.18"
    :param cfg: BuildConfig
    :return:
    """
    text = _read_text(cfg.sysroot_toml)
    original = text
    # Update PyQt5 version.
    text = sub(
        r'(\[PyQt5\][^\[]*?version\s*=\s*")[^"]+(")', r'\g<1>{}\g<2>'.format(cfg.pyqt_version), text, flags=DOTALL)
    # Update Python version.
    text = sub(
        r'(\[python\][^\[]*?version\s*=\s*")[^"]+(")', r'\g<1>{}\g<2>'.format(cfg.python_version), text, flags=DOTALL)
    # Update Qt version.
    text = sub(
        r'(\[PyQt5\.qt\][^\[]*?version\s*=\s*")[^"]+(")', r'\g<1>{}\g<2>'.format(cfg.qt_version), text, flags=DOTALL)
    if text != original:
        _write_text(cfg.sysroot_toml, text)
        log.info('sysroot.toml version pins updated :)')
    else:
        log.info('sysroot.toml version pins unchanged.')


# ------------------------------------------------------------------------------
# Step 7 - Build the sysroot.
# ------------------------------------------------------------------------------

def build_sysroot(cfg):
    """
    Run pyqtdeploy-sysroot to cross-compile Python + SIP + PyQt5 statically.
    From OforOshima build command (adapted for pyqtdeploy 3.3):
      pyqtdeploy-sysroot --target ios-64 --verbose sysroot.toml
    From plashless Part 2:
      pyqtdeploycli --package python --target ios-64 configure
      ~/Qt/5.x/ios/bin/qmake sysroot=~/ios/iRoot
      make && make install  (Python)
      [repeat for SIP and PyQt5]
    In pyqtdeploy 3.3 the sysroot tool handles all three in sequence.
    :param cfg: BuildConfig
    :return:    None
    """
    _step('Step 7/9 - Building sysroot (Python + SIP + PyQt5, static)')
    if cfg.sysroot_path and exists(cfg.sysroot_path):
        log.info('Using existing sysroot: %s (skipping rebuild)', cfg.sysroot_path)
        return
    _makedirs(cfg.sysroot_dir)
    cmd = [cfg.pyqtdeploy_sysroot, '--target', TARGET, '--sysroot', cfg.sysroot_dir, '--source-dir', cfg.src_dir]
    if cfg.verbose:
        cmd.append('--verbose')
    # Point pyqtdeploy-sysroot to the pre-installed Qt iOS kit.
    cmd += ['--qmake', cfg.qmake_path]
    cmd.append(cfg.sysroot_toml)
    log.info('This step cross-compiles Python, SIP, and PyQt5 statically.\nExpect 20-60 minutes on first run.')
    _run(cmd, cwd=cfg.project_dir, env={'SYSROOT': cfg.sysroot_dir}, dry_run=cfg.dry_run)
    log.info('Sysroot built: %s :)', cfg.sysroot_dir)


# ------------------------------------------------------------------------------
# Step 8 - Build the app (pyqtdeploy-build -> Xcode project).
# ------------------------------------------------------------------------------

def build_xcodeproj(cfg):
    """
    Run pyqtdeploy-build to generate the Xcode project, then run qmake.
    From pyqtdeploy docs (building.html):
      "pyqtdeploy-build ... generates the target-specific source code,
       including the qmake .pro files."
      "For an iOS target qmake generates an Xcode project file."
    From OforOshima build command:
      python build-demo.py --target ios-64
                           --qmake /Users/xxxx/Qt/5.15.18/ios/bin/qmake
                           --verbose
    The build-demo.py script calls pyqtdeploy-build then qmake internally;
    we replicate those steps here for any .pdt file.
    :param cfg: BuildConfig
    :return:    str  (path to .xcodeproj)
    """
    _step('Step 8/9 - Building Xcode project')
    # Locate the .pdt file.
    pdts = _rglob(cfg.project_dir, '*.pdt')
    if not pdts:
        raise FileNotFoundError(
            'No .pdt file in {}.\nCreate one with:  pyqtdeploy <appname>.pdt\n'
            'See: https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/demo.html'.format(cfg.project_dir))
    pdt_file = pdts[0]
    log.info('Using .pdt: %s', pdt_file)
    _makedirs(cfg.build_dir)
    # -- pyqtdeploy-build -----------------------------------------------------
    build_cmd = [cfg.pyqtdeploy_build, '--target', TARGET, '--build-dir', cfg.build_dir, '--qmake',
                 cfg.qmake_path, '--sysroot', cfg.sysroot_dir]
    if cfg.verbose:
        build_cmd.append('--verbose')
    build_cmd.append(pdt_file)
    log.info('Running pyqtdeploy-build...')
    _run(build_cmd, cwd=cfg.project_dir, dry_run=cfg.dry_run)
    # -- qmake (generates .xcodeproj) -----------------------------------------
    pro_files = _rglob(cfg.build_dir, '*.pro')
    if not pro_files and not cfg.dry_run:
        raise FileNotFoundError("pyqtdeploy-build didn't create a .pro file in {}.".format(cfg.build_dir))
    pro_file = (pro_files[0] if pro_files else join(cfg.build_dir, "{}.pro".format(cfg.app_name)))
    log.info('Running qmake on: %s', pro_file)
    _run([cfg.qmake_path, pro_file], cwd=cfg.build_dir, dry_run=cfg.dry_run)
    # Locate the .xcodeproj
    xcode_hits = _rglob(cfg.build_dir, '*.xcodeproj')
    if not xcode_hits:
        if cfg.dry_run:
            log.info('[DRY-RUN] No .xcodeproj produced (expected in dry-run).')
            return join(cfg.build_dir, '{}.xcodeproj'.format(cfg.app_name))
        raise FileNotFoundError(
            'qmake did not generate a .xcodeproj under {}.\nCheck the qmake output above for errors.'.format(
                cfg.build_dir))
    xcodeproj = xcode_hits[0]
    log.info('.xcodeproj generated: %s :)', xcodeproj)
    return xcodeproj


# ------------------------------------------------------------------------------
# Step 9 - Xcode / Simulator.
# ------------------------------------------------------------------------------

def open_or_build_xcode(cfg, xcodeproj):
    """
    Either open the project in Xcode or run xcodebuild for the simulator.
    From OforOshima:
      "The pyqt-demo.xcodeproj file can be found in the 'build-ios-64' directory.
       Run Xcode to build the app and run it in the simulator or deploy it to a device."
    :param cfg:       BuildConfig
    :param xcodeproj: str
    :return:
    """
    _step('Step 9/9 - Xcode')
    if cfg.run_simulator:
        _run_in_simulator(cfg, xcodeproj)
    elif cfg.open_xcode:
        log.info('Opening in Xcode: %s', xcodeproj)
        _run([getOpenExecutable(), xcodeproj], dry_run=cfg.dry_run)
    else:
        log.info(
            '\n'
            '  .xcodeproj is ready - open it in Xcode to continue.\n'
            '\n'
            '  To open manually:\n'
            '    open %s\n'
            '\n'
            '  Inside Xcode:\n'
            '    1. Select your Signing Team in project settings.\n'
            '    2. Choose iPhone Simulator from the device selector.\n'
            '    3. Press Run (Cmd+R).\n'
            '\n'
            '  To install on a real device:\n'
            '    1. Connect an iPhone/iPad with Developer mode enabled.\n'
            '    2. Select the device in Xcode and press Run.\n'
            '    3. Trust the developer profile:  Settings -> VPN & Device Management.', xcodeproj)


def _run_in_simulator(cfg, xcodeproj):
    """
    Build for the iOS Simulator via xcodebuild and launch the app.
    Requires:
      - The .xcodeproj to be open and fully configured.
      - At least one iOS Simulator runtime installed (Xcode -> Settings -> Platforms).
    :param cfg:       BuildConfig
    :param xcodeproj: str
    :return:
    """
    log.info('Building for iOS Simulator via xcodebuild...')
    # Find any available simulator device.
    res = _run([getXcrunExecutable(), 'simctl', 'list', 'devices', '--json'], capture=True, check=False)
    sim_device_udid = None  # type: str | None
    try:
        devices_data = loads(res.stdout or '{}')
        for runtime, devices in devices_data.get('devices', {}).items():
            if 'iOS' not in runtime:
                continue
            for dev in devices:
                if dev.get('isAvailable') and 'iPhone' in dev.get('name', ''):
                    sim_device_udid = dev['udid']
                    sim_name = dev['name']
                    log.info('Simulator: %s (%s)', sim_name, sim_device_udid)
                    break
            if sim_device_udid:
                break
    except (ValueError, KeyError):
        # ValueError covers json.JSONDecodeError
        pass
    if not sim_device_udid:
        log.warning(
            'No available iPhone simulator found.\nInstall iOS Simulator runtimes via:  Xcode -> Settings -> Platforms')
        # Fall back to a named destination
        sim_device_udid = 'platform=iOS Simulator,name=iPhone 16'
    xcodebuild_cmd = [getXcodebuildExecutable(), '-project', xcodeproj, '-scheme', cfg.app_name, '-destination',
                      "id={}".format(sim_device_udid), '-configuration', 'Debug', 'build']
    if not cfg.verbose:
        xcodebuild_cmd.append('-quiet')
    log.info('Running xcodebuild...')
    _run(xcodebuild_cmd, dry_run=cfg.dry_run)
    log.info(
        'Simulator build complete.\n'
        'Boot and launch with:\n'
        '  xcrun simctl boot %s\n'
        '  xcrun simctl install %s <path/to/app.app>\n'
        '  xcrun simctl launch %s <bundle-id>',
        sim_device_udid, sim_device_udid, sim_device_udid)


# ------------------------------------------------------------------------------
# Summary.
# ------------------------------------------------------------------------------

def print_summary(cfg, xcodeproj):
    """
    :param cfg:       BuildConfig
    :param xcodeproj: str | None
    :return:
    """
    _step('Build summary')
    log.info(
        '\n'
        '  App name      : %s\n'
        '  Qt version    : %s\n'
        '  PyQt5 version : %s\n'
        '  Python (iOS)  : %s\n'
        '  Target        : %s\n'
        '  Project dir   : %s\n'
        '  Sysroot       : %s\n'
        '  Xcode project : %s',
        cfg.app_name, cfg.qt_version, cfg.pyqt_version, cfg.python_version, TARGET, cfg.project_dir,
        cfg.sysroot_dir, xcodeproj or 'N/A')
    if xcodeproj and (exists(xcodeproj) or cfg.dry_run):
        log.info('\n  Build succeeded!  Open %s in Xcode.', xcodeproj)
    elif cfg.only_sysroot:
        log.info('\n  Sysroot built.  Re-run without --only-sysroot to build the app.')
    else:
        log.warning('\n  Build may not have completed. Check errors above.')
    log.info(
        dedent("""
        -------------------------------------------------------------
        Known errors & fixes  (from OforOshima + plashless)
        -------------------------------------------------------------
        * "macos-32 is not a supported architecture"
          -> platforms.py patch (Step 5a) - ARM64 not recognised.
            The patch adds 'arm64': 'macos-64' to the machine->arch map.

        * "call to undeclared function 'getentropy'"
          -> python.pro patch (Step 5b) - iOS SDK hides getentropy().
            Fix: QMAKE_CFLAGS += -Wno-implicit-function-declaration

        * "SIP: sip-module didn't create an sdist"
          -> SIP.py patch (Step 5c) - case-sensitive glob misses
            pyqt5_sip-*.tar.gz on case-insensitive macOS HFS+.
            Fix: case-insensitive _iglob() helper injected.

        * "QSslConfiguration is undefined"
          -> Remove QtNetwork or QtWebSockets from sysroot.toml [PyQt5] modules.

        * "You need a working sip on your PATH"
          -> Pass --sip=/path/to/sip to configure.py, or ensure the venv
            bin directory is on PATH.

        * Linking errors: "ld: framework 'QtFoo' not found"
          -> The Qt iOS kit may be incomplete. Re-run the Qt installer
            and select the iOS component explicitly.

        * Xcode "No signing certificate" error
          -> In Xcode: Project settings -> Signing & Capabilities ->
            Team: select your Apple Developer account.

        * App crashes immediately on simulator
          -> From OforOshima: "calling get_source_code() causes the app
            to crash." Replace with a placeholder like  view.setText("hello")
        -------------------------------------------------------------
        """))


# ------------------------------------------------------------------------------
# Argument parser
# ------------------------------------------------------------------------------

def build_arg_parser():
    """
    :return: ArgumentParser
    """
    parser = ArgumentParser(
        prog='pyqt5_ios_builder',
        formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PyQt5 -> iOS Builder  (pyqtdeploy 3.3 pipeline)
            ================================================
            Patches three known pyqtdeploy bugs, then cross-compiles
            Python + SIP + PyQt5 statically and generates an Xcode project.
        """),
        epilog=dedent("""\
            Examples
            --------
            # Full build (generates .xcodeproj):
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake

            # Full build + auto-open in Xcode:
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake --open-xcode

            # Full build + run on iOS Simulator:
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake --run-simulator

            # Sysroot only (cache for later):
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake --only-sysroot

            # Reuse an existing sysroot:
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake \\
                  --sysroot ~/.pyqt5_ios_build/sysroot-ios-64

            # Add extra Qt modules:
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake \\
                  --extra-modules QtSql,QtBluetooth

            # Dry-run to print all commands:
              python pyqt5_ios_builder.py --project-dir ./myapp \\
                  --qmake ~/Qt/5.15.18/ios/bin/qmake --dry-run
        """))
    # Paths: use str instead of pathlib.Path – resolved manually in main()
    parser.add_argument(
        '--project-dir', required=True, type=str, help='Path to your PyQt5 project (must contain a .pdt file).')
    parser.add_argument(
        '--app-name', type=str, default=None, help='Application name. Defaults to the project directory name.')
    parser.add_argument(
        '--qmake', required=True, type=str, dest='qmake_path',
        help='Path to the iOS qmake (e.g. ~/Qt/5.15.18/ios/bin/qmake).')
    # Version overrides
    parser.add_argument('--qt-version', default=QT_VERSION, help='Qt version (default: {}).'.format(QT_VERSION))
    parser.add_argument('--pyqt-version', default=PYQT_VERSION,
                        help='PyQt5 version (default: {}).'.format(PYQT_VERSION))
    parser.add_argument('--python-version', default=PYTHON_VERSION,
                        help='Python version embedded in iOS (default: {}).'.format(PYTHON_VERSION))
    # Qt modules
    parser.add_argument('--extra-modules', type=str, default='', dest='extra_modules',
                        help='Comma-separated extra Qt modules (e.g. QtSql,QtBluetooth).')
    # Pre-existing paths.
    parser.add_argument('--sysroot', type=str, default=None, dest="sysroot_path",
                        help='Reuse an existing sysroot directory (skip rebuild).')
    # Build control.
    parser.add_argument('--only-sysroot', action='store_true',
                        help='Build the sysroot only; skip the Xcode project generation.')
    parser.add_argument('--open-xcode', action='store_true',
                        help='Open the generated .xcodeproj in Xcode automatically.')
    parser.add_argument('--run-simulator', action='store_true',
                        help='Build for the iOS Simulator via xcodebuild and launch.')
    parser.add_argument('--keep-build', action='store_true', help='Retain the intermediate build directory.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug-level output.')
    return parser


# ------------------------------------------------------------------------------
# Main entry point
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
    # -- Resolve project directory -------------------------------------------
    # realpath is the Py2/3 equivalent of Path.resolve()
    project_dir = realpath(args.project_dir)
    # basename(normpath(...)) handles trailing slashes correctly
    app_name = args.app_name or basename(normpath(project_dir))
    # -- Resolve qmake path: expanduser + realpath ---------------------------
    # Equivalent to Path(args.qmake_path).expanduser().resolve()
    qmake_path = realpath(expanduser(args.qmake_path))
    # -- Parse optional sysroot path -----------------------------------------
    sysroot_path = (realpath(expanduser(args.sysroot_path)) if args.sysroot_path else None)
    # -- Parse extra modules -------------------------------------------------
    extra_mods = ([m.strip() for m in args.extra_modules.split(",") if m.strip()] if args.extra_modules else [])
    # -- Build config --------------------------------------------------------
    cfg = BuildConfig(
        project_dir=project_dir,
        app_name=app_name,
        qmake_path=qmake_path,
        qt_version=args.qt_version,
        pyqt_version=args.pyqt_version,
        python_version=args.python_version,
        sysroot_path=sysroot_path,
        verbose=args.verbose,
        dry_run=args.dry_run,
        keep_build=args.keep_build,
        only_sysroot=args.only_sysroot,
        open_xcode=args.open_xcode,
        run_simulator=args.run_simulator,
        extra_modules=extra_mods)
    xcodeproj = None  # type: str | None
    try:
        preflight_checks(cfg)
        setup_venv(cfg)
        validate_toolchain(cfg)
        download_sources(cfg)
        apply_all_patches(cfg)
        generate_sysroot_toml(cfg)
        build_sysroot(cfg)
        if not args.only_sysroot:
            xcodeproj = build_xcodeproj(cfg)
            open_or_build_xcode(cfg, xcodeproj)
        print_summary(cfg, xcodeproj)
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
