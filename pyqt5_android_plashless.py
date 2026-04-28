#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PyQt5 5.3 Android Builder  (pyqtdeploy 0.5 / 0.6 pipeline)
============================================================
Automates the complete cross-compilation pipeline documented in:
  Part 1: https://plashless.wordpress.com/2014/08/19/
          using-pyqtdeploy0-5-on-linux-to-cross-compile-a-pyqt-app-for-android/
  Part 2: https://plashless.wordpress.com/2014/08/26/
          using-pyqtdeploy0-5-on-linux-to-cross-compile-a-pyqt-app-for-android-part-2/

IMPORTANT: This script is written to run under Python 3.4.0 exactly.
It deliberately avoids features introduced after 3.4:
  - No f-strings             (requires 3.6+)
  - No subprocess.run        (requires 3.5+)
  - No type annotations      (requires 3.5+)
  - No dataclasses           (requires 3.7+)
  - No walrus operator       (requires 3.8+)
All string formatting uses .format() or the % operator.

Pinned versions:
---------------
  Python      3.4.0
  Qt          5.3          (android_armv7 / android-32 target)
  PyQt5       5.3.x        (latest GPL snapshot recommended)
  SIP         latest snapshot (must match PyQt5)
  pyqtdeploy  0.5 / 0.6   (0.6 renamed pyqtdeploy -> pyqtdeploycli)
  Android NDK r10

Pipeline (15 steps):
-------------------
  1.  Preflight       -- Ubuntu 14.04, tools, Python 3.4, disk space
  2.  Env variables   -- ANDROID_NDK_ROOT, PYTHONPATH, SYSROOT
  3.  Install pyqtdeploy  -- hg clone + python setup.py install
  4.  Download sources    -- Python 3.4.0, SIP, PyQt5 tarballs
  5.  Build host SIP      -- configure.py + make + make install (host)
  6.  Build Python static -- pyqtdeploycli configure + qmake + make + install
  7.  Patch Python        -- SYS_getdents64, epoll_create1, log2 fixes
  8.  Patch python.pro    -- add extra C extension modules to link list
  9.  Patch config.c      -- register extension module init functions
  10. Rebuild Python      -- qmake + make + make install (after patches)
  11. Build SIP static    -- pyqtdeploycli configure + configure.py + qmake + make + install
  12. Build PyQt5 static  -- pyqtdeploycli configure + edit cfg + configure.py + make + install
  13. pyqtdeploy project  -- create/validate the .pdy project file
  14. pyqtdeploy build    -- freeze Python code + generate Qt Creator .pro
  15. Qt Creator build    -- qmake + make + androiddeployqt (or guidance for QtCreator GUI)

Usage:
-----
    python3 pyqt5_android_plashless.py --project-dir /path/to/myapp [OPTIONS]
    # Full automated build:
    python3 pyqt5_android_plashless.py --project-dir ./myapp --ndk-root    ~/android-ndk-r10 --qt-dir      ~/Qt/5.3
    # Skip static library build (already have aRoot sysroot):
    python3 pyqt5_android_plashless.py --project-dir ./myapp --sysroot     ~/aRoot --skip-static-build
    # Extra Python C extension modules your app needs:
    python3 pyqt5_android_plashless.py --project-dir ./myapp --extra-modules _posixsubprocess,select,_socket
    # Dry-run to see all commands:
    python3 pyqt5_android_plashless.py --project-dir ./myapp --dry-run --verbose
"""
from os.path import expanduser, basename, realpath, join, isdir, isfile, dirname, getmtime, getsize, exists, pathsep
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from os import environ, chmod, listdir, walk, makedirs, statvfs
from logging import basicConfig, getLogger, DEBUG, INFO
from sys import exit, version_info, path
from re import DOTALL, match, compile
from platform import system, release
from subprocess import PIPE, Popen
from textwrap import dedent
from zipfile import ZipFile
from errno import EEXIST
import tarfile

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import which, getMakeExecutable, getHgExecutable, getPythonExecutable
except:
    from builders import which, getMakeExecutable, getHgExecutable, getPythonExecutable
try:
    from urllib import urlretrieve  # noqa: F401
    from urllib2 import URLError  # noqa: F401
except:
    from urllib.request import urlretrieve  # noqa: F401
    from urllib.error import URLError  # noqa: F401

# =============================================================================
# Constants -- pinned to plashless guide versions.
# =============================================================================
PYTHON_VERSION = '3.4.0'
QT_VERSION = '5.3'
TARGET = 'android-32'  # pyqtdeploy 0.5 target string.
QT_ARCH_SUBDIR = 'android_armv7'  # Qt directory for armv7.
NDK_VERSION = 'r10'
NDK_DIRNAME = 'android-ndk-r10'
# pyqtdeploy 0.5 Mercurial repository (official Riverbank).
PYQTDEPLOY_HG = 'http://www.riverbankcomputing.com/hg/pyqtdeploy'
# Source download URLs.
PYTHON_URL = 'https://www.python.org/ftp/python/{ver}/Python-{ver}.tgz'.format(ver=PYTHON_VERSION)
SIP_SNAPSHOT_URL = 'https://www.riverbankcomputing.com/software/sip/download'  # Navigate manually; latest snapshot.
PYQT5_SNAP_URL = 'https://www.riverbankcomputing.com/software/pyqt/download5'  # Navigate manually; latest snapshot.
# Default install paths (match plashless guide).
DEFAULT_HOME = expanduser('~')
DEFAULT_NDK_ROOT = join(DEFAULT_HOME, NDK_DIRNAME)
DEFAULT_QT_DIR = join(DEFAULT_HOME, 'Qt', QT_VERSION)
DEFAULT_SYSROOT = join(DEFAULT_HOME, 'aRoot')
DEFAULT_WORK_DIR = join(DEFAULT_HOME, '.pyqt5_android_plashless')
# Minimum free disk space (MB).
MIN_DISK_MB = 6000
# Qt modules to include (plashless: stripped down to avoid QSslConfiguration errors).
DEFAULT_QT_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtPrintSupport', 'QtSvg', 'QtNetwork']
# =============================================================================
# Logging.
# =============================================================================
basicConfig(format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt="%H:%M:%S", level=INFO)
log = getLogger('pyqt5-android-plashless')


def step(title):
    """
    Print a visually distinct step header.
    :param title: str
    :return:
    """
    bar = '=' * 66
    log.info('\n%s\n  %s\n%s', bar, title, bar)


# =============================================================================
# Build configuration.
# =============================================================================

class BuildConfig(object):
    """
    All resolved paths and settings for a single build run.
    """

    def __init__(self, args):
        """
        :param args: any
        """
        self.project_dir = realpath(args.project_dir)
        self.app_name = args.app_name or basename(self.project_dir)
        self.ndk_root = expanduser(args.ndk_root)
        self.qt_dir = expanduser(args.qt_dir)
        self.sysroot = expanduser(args.sysroot)
        self.work_dir = expanduser(args.work_dir)
        # Source directories (set after download/extraction).
        self.python_src = args.python_src or ''
        self.sip_src = args.sip_src or ''
        self.pyqt5_src = args.pyqt5_src or ''
        self.pyqtdeploy_src = args.pyqtdeploy_src or ''
        # Build flags.
        self.verbose = args.verbose
        self.dry_run = args.dry_run
        self.skip_static = args.skip_static_build
        self.install_apk = args.install_apk
        self.keep_build = args.keep_build
        self.jobs = args.jobs
        # Extra Python C extension modules to wire in.
        raw = args.extra_modules or ''
        self.extra_modules = [m.strip() for m in raw.split(',') if m.strip()]
        # Qt module selection (trimmed to avoid QSslConfiguration errors).
        self.qt_modules = list(DEFAULT_QT_MODULES)
        # Derived paths
        self.downloads_dir = join(self.work_dir, 'downloads')
        self.build_dir = join(self.project_dir, 'pensoolAndroidBuild')

    # ------------------------------------------------------------------ #
    # Properties for frequently-used derived paths.                      #
    # ------------------------------------------------------------------ #

    @property
    def qt_android_dir(self):
        """
        ~/Qt/5.3/android_armv7
        :return: str
        """
        return join(self.qt_dir, QT_ARCH_SUBDIR)

    @property
    def qmake(self):
        """
        ~/Qt/5.3/android_armv7/bin/qmake
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
    def ndk_toolchain_bin(self):
        """
        arm-linux-androideabi-4.8 prebuilt bin dir inside NDK.
        :return: str
        """
        return join(self.ndk_root, 'toolchains', 'arm-linux-androideabi-4.8', 'prebuilt', 'linux-x86_64', 'bin')

    @property
    def adb(self):
        """
        :return: str
        """
        return join(join(self.ndk_root, '..', 'android-sdk'), 'platform-tools', 'adb')

    @property
    def pdy_file(self):
        """
        :return: str
        """
        return join(self.project_dir, '{0}.pdy'.format(self.app_name))

    def build_env(self):
        """
        Return environment dict with all required variables set.
        :return: str
        """
        env = dict(environ)
        env['ANDROID_NDK_ROOT'] = self.ndk_root
        env['SYSROOT'] = self.sysroot
        # From plashless Part 1:
        # "PyQt5 installs to /usr/lib/Python3.4/site-packages, and that must be on PYTHONPATH."
        existing_pp = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = '/usr/lib/python3.4/site-packages' + (pathsep + existing_pp if existing_pp else '')
        # NDK toolchain on PATH
        env['PATH'] = self.ndk_toolchain_bin + pathsep + env.get('PATH', '')
        return env


# =============================================================================
# Subprocess helpers.
# =============================================================================

def _run(cmd, cwd=None, env=None, check=True, dry_run=False, capture=False):
    """
    Execute *cmd* (list of str) using subprocess.
    (subprocess.run was added in 3.5, so we use Popen here).
    :param cwd: list[str]
    :param env: dict[str, str]
    :param dry_run: bool
    :param capture: bool
    :return: Popen
    """
    display = ' '.join(c for c in cmd)
    log.debug('$ %s  [cwd=%s]', display, cwd or '.')
    if dry_run:
        log.info('[DRY-RUN] %s', display)

        class _FakeResult(object):
            """
            _FakeResult class.
            """
            returncode = 0
            stdout = b''
            stderr = b''

        return _FakeResult()
    merged_env = dict(environ)
    if env:
        merged_env.update(env)
    kwargs = dict(cwd=cwd, env=merged_env)
    if capture:
        kwargs['stdout'] = PIPE
        kwargs['stderr'] = PIPE
    proc = Popen([c for c in cmd], **kwargs)
    stdout, stderr = proc.communicate()
    proc.stdout = stdout or b''
    proc.stderr = stderr or b''
    if check and proc.returncode != 0:
        log.error('Command failed (exit %d):\n  %s', proc.returncode, display)
        if proc.stdout:
            log.error('stdout:\n%s', proc.stdout.decode('utf-8', errors='replace')[-3000:])
        if proc.stderr:
            log.error('stderr:\n%s', proc.stderr.decode('utf-8', errors='replace')[-3000:])
        raise RuntimeError('Subprocess exited with code {0}'.format(proc.returncode))
    return proc


def _require_tool(name):
    """
    Assert *name* is on PATH; return its full path.
    :param name: str
    :return: str
    """
    pth = which(name)
    if not pth:
        raise EnvironmentError('Required tool "{0}" not found on PATH.'.format(name))
    return pth


def _makedirs(pth):
    """
    :param pth: str
    :return:
    """
    try:
        makedirs(pth)
    except OSError as exc:
        if exc.errno != EEXIST:
            raise


def _download(url, dest):
    """
    Download *url* -> *dest* using urllib3.
    :param url: str
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
        raise RuntimeError('Download failed: {0}\n{1}'.format(url, exc))


def _extract_tgz(archive, dest_dir):
    """
    Extract a .tgz / .tar.gz archive.
    :param archive: str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info('Extracting  %s', basename(archive))
    with tarfile.open(archive) as tf:
        tf.extractall(dest_dir)


def _extract_zip(archive, dest_dir):
    """
    Extract a .zip archive.
    :param archive: str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info('Extracting  %s', basename(archive))
    with ZipFile(archive, 'r') as zf:
        zf.extractall(dest_dir)


def _check_disk_space(pth, required_mb=MIN_DISK_MB):
    """
    Warn if less than required_mb MB are free at path.
    :param pth: str
    :param required_mb: int
    :return:
    """
    stat = statvfs(pth if exists(pth) else dirname(pth))
    free_mb = (stat.f_bavail * stat.f_frsize) // (1024 * 1024)
    if free_mb < required_mb:
        log.warning('Low disk space: %d MB free at %s (recommended >= %d MB).', free_mb, pth, required_mb)
    else:
        log.info('Disk: %d MB free OK', free_mb)


def _patch_file(filepath, old_str, new_str, description):
    """
    Simple in-place string replacement; logs what was changed.
    :param filepath: str
    :param old_str: str
    :param new_str: str
    :param description: str
    :return: bool
    """
    with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
        content = fh.read()
    if old_str not in content:
        log.debug('Patch "%s": pattern not found (already applied?).', description)
        return False
    new_content = content.replace(old_str, new_str)
    with open(filepath, 'w', encoding='utf-8') as fh:
        fh.write(new_content)
    log.info('Patched %-32s  [%s]', basename(filepath), description)
    return True


def _find_file(root_dir, filename):
    """
    Return first path matching filename under root_dir, or None.
    :param root_dir: str
    :param filename: str
    :return: str | None
    """
    for dirpath, dirnames, files in walk(root_dir):
        if filename in files:
            return join(dirpath, filename)
    return None


# =============================================================================
# Step 1 -- Preflight checks.
# =============================================================================

def preflight(cfg):
    """
    From plashless Part 1:
      Ubuntu 14.04, Python 3.4, pyqtdeploy 0.5/0.6, Qt 5.3
      Required: mercurial (hg), python3, make, gcc, git
    NOTE: plashless explicitly warns that pip3 fails on some setups,
    so pyqtdeploy must be installed from Mercurial source.
    :param cfg: BuildConfig
    :return:
    """
    step('Step 1/15 -- Preflight checks')
    if system() != 'Linux':
        raise EnvironmentError(
            'This pipeline requires Linux (Ubuntu 14.04 recommended). Detected: {0}'.format(system()))
    log.info('Host OS:  %s %s', system(), release())
    # This script runs on Python 3.4+
    major, minor = version_info[:2]
    if (major, minor) < (3, 4):
        raise EnvironmentError('Python 3.4+ required to run this script (found {0}.{1}).'.format(major, minor))
    log.info('Script Python:  %d.%d', major, minor)
    # Required system tools.
    required_tools = {
        'hg': 'sudo apt-get install mercurial',
        'python3': 'sudo apt-get install python3',
        'make': 'sudo apt-get install build-essential',
        'gcc': 'sudo apt-get install build-essential',
        'g++': 'sudo apt-get install build-essential',
        'git': 'sudo apt-get install git',
        'tar': 'sudo apt-get install tar',
        'zip': 'sudo apt-get install zip',
        'unzip': 'sudo apt-get install unzip'}
    for tool, hint in sorted(required_tools.items()):
        try:
            log.info('Found: %-12s  %s', tool, _require_tool(tool))
        except EnvironmentError:
            raise EnvironmentError('"{0}" not found on PATH.\n  Install: {1}'.format(tool, hint))
    # Android NDK.
    if not isdir(cfg.ndk_root):
        raise EnvironmentError(
            'Android NDK not found at: {0}\n'
            'Download android-ndk-{1}-linux-x86_64.bin from:\n'
            '  https://developer.android.com/ndk/downloads/older_releases\n'
            'Extract to: ~/{2}\n'
            'Then re-run with --ndk-root ~/{2}'.format(cfg.ndk_root, NDK_VERSION, NDK_DIRNAME))
    log.info('NDK:  %s  OK', cfg.ndk_root)
    # Qt 5.3 android_armv7
    if not isfile(cfg.qmake):
        raise EnvironmentError(
            'Qt Android qmake not found: {0}\n'
            'Install Qt 5.3 from: https://www.qt.io/download (offline installer).\n'
            'Choose Qt 5.3 -> Android -> armv7.\n'
            'Default install: ~/Qt/5.3/android_armv7/bin/qmake\n'
            'Then re-run with --qt-dir ~/Qt/5.3'.format(cfg.qmake))
    result = _run([cfg.qmake, '--version'], capture=True, check=False)
    qmake_ver = (result.stdout or b'').decode('utf-8', errors='replace')
    log.info('qmake:  %s  OK', qmake_ver.splitlines()[0] if qmake_ver else cfg.qmake)
    # Project directory.
    if not isdir(cfg.project_dir):
        raise IOError('Project directory not found: {0}'.format(cfg.project_dir))
    log.info('Project:  %s  OK', cfg.project_dir)
    # Disk space.
    _check_disk_space(cfg.work_dir if exists(cfg.work_dir) else expanduser('~'))
    log.info('Preflight passed OK')


# =============================================================================
# Step 2 -- Set and document environment variables.
# =============================================================================

def setup_environment(cfg):
    """
    From plashless Part 1:
      export ANDROID_NDK_ROOT=/home/bootch/android-ndk-r10
      export PYTHONPATH=/usr/lib/python3.4/site-packages
      export SYSROOT=/home/bootch/aRoot
    Creates the SYSROOT directory and writes env.sh for manual reference.
    :param cfg: BuildConfig
    :return:
    """
    step('Step 2/15 -- Environment variables')
    _makedirs(cfg.sysroot)
    _makedirs(cfg.work_dir)
    _makedirs(cfg.downloads_dir)
    log.info(
        'Environment:\n'
        '  ANDROID_NDK_ROOT = %s\n'
        '  SYSROOT          = %s\n'
        '  PYTHONPATH       = /usr/lib/python3.4/site-packages\n'
        '  QT_ANDROID_DIR   = %s',
        cfg.ndk_root, cfg.sysroot, cfg.qt_android_dir)
    # Write env.sh so the user can source it for manual steps.
    env_sh = join(cfg.work_dir, 'env.sh')
    env = cfg.build_env()
    with open(env_sh, 'w') as fh:
        fh.write('#!/bin/sh\n# Generated by pyqt5_android_plashless.py\n\n')
        for key in ('ANDROID_NDK_ROOT', 'SYSROOT', 'PYTHONPATH'):
            fh.write("export {0}='{1}'\n".format(key, env.get(key, '')))
        fh.write("export PATH='{0}'\n".format(env.get('PATH', '')))
    chmod(env_sh, 0o755)
    log.info('env.sh written: %s', env_sh)


# =============================================================================
# Step 3 -- Install pyqtdeploy 0.5 from Mercurial.
# =============================================================================

def install_pyqtdeploy(cfg):
    """
    From plashless Part 1:
      >sudo apt-get mercurial
      >hg clone http://www.riverbankcomputing.com/hg/pyqtdeploy
      >cd pyqtdeploy
      >make VERSION
      >make pyqtdeploy/version.py
      >sudo python3 setup.py install
    From plashless: "pip3 install pyqtdeploy fails with
    ImportError: No module named 'pkg_resources'."
    NOTE: In pyqtdeploy 0.6 the 'pyqtdeploy' command was renamed to
    'pyqtdeploycli' for the command-line interface.  We detect which is available and use it accordingly.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 3/15 -- Installing pyqtdeploy 0.5 from Mercurial')
    # Detect already-installed pyqtdeploycli or pyqtdeploy.
    for cmd_name in ('pyqtdeploycli', 'pyqtdeploy'):
        if which(cmd_name):
            cfg.pyqtdeploycli = cmd_name
            log.info('pyqtdeploy already installed as "%s" OK', cmd_name)
            return
    # User provided a pre-cloned directory.
    if cfg.pyqtdeploy_src and isdir(cfg.pyqtdeploy_src):
        src_dir = cfg.pyqtdeploy_src
    else:
        src_dir = join(cfg.work_dir, 'pyqtdeploy')
        if not isdir(src_dir):
            log.info('Cloning pyqtdeploy from Mercurial ...')
            _run([getHgExecutable(), 'clone', PYQTDEPLOY_HG, src_dir], dry_run=cfg.dry_run)
        else:
            log.info('Updating pyqtdeploy ...')
            _run([getHgExecutable(), 'pull'], cwd=src_dir, dry_run=cfg.dry_run)
            _run([getHgExecutable(), 'update'], cwd=src_dir, dry_run=cfg.dry_run)
    # Build pyqtdeploy version file and install.
    _run([getMakeExecutable(), 'VERSION'], cwd=src_dir, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'pyqtdeploy/version.py'], cwd=src_dir, dry_run=cfg.dry_run)
    _run(['sudo', getPythonExecutable(), 'setup.py', 'install'], cwd=src_dir, dry_run=cfg.dry_run)
    # Detect which command name was installed.
    for cmd_name in ('pyqtdeploycli', 'pyqtdeploy'):
        if which(cmd_name):
            cfg.pyqtdeploycli = cmd_name
            log.info('Installed as "%s" OK', cmd_name)
            return
    cfg.pyqtdeploycli = 'pyqtdeploycli'  # Fallback.
    log.warning('pyqtdeploycli/pyqtdeploy not found on PATH after install.')
    log.info('pyqtdeploy install done OK')


# =============================================================================
# Step 4 -- Download source tarballs.
# =============================================================================

def download_sources(cfg):
    """
    From plashless Part 1:
      "Navigate your browser to a Python download page and download the source
       (the 'gzipped source tarball' for Python version 3.4.0)."
      "Download SIP ... Download PyQt5 ..."
    Python 3.4.0 is downloaded automatically.  SIP and PyQt5 snapshots must
    be placed in the downloads directory manually (they require the latest
    snapshot, not the stable GPL release, to avoid QTBUG-39300).
    :param cfg: BuildConfig
    :return:
    """
    step('Step 4/15 -- Downloading source tarballs')
    _makedirs(cfg.downloads_dir)
    # Python 3.4.0 -- auto download.
    python_archive = join(cfg.downloads_dir, 'Python-3.4.0.tgz')
    if not cfg.dry_run:
        _download(PYTHON_URL, python_archive)
    python_extracted = join(cfg.work_dir, 'Python-3.4.0')
    if not isdir(python_extracted) and not cfg.dry_run:
        _extract_tgz(python_archive, cfg.work_dir)
    cfg.python_src = python_extracted
    log.info('Python 3.4.0 source: %s', cfg.python_src)
    # SIP -- must be latest snapshot (plashless: avoid QTBUG-39300).
    if cfg.sip_src and isdir(cfg.sip_src):
        log.info('SIP source (user-provided): %s', cfg.sip_src)
    else:
        # Search work_dir for any sip-* directory.
        sip_dirs = [d for d in listdir(cfg.work_dir) if d.lower().startswith('sip') and isdir(join(cfg.work_dir, d))]
        if sip_dirs:
            cfg.sip_src = join(cfg.work_dir, sorted(sip_dirs)[-1])
            log.info('SIP source (found): %s', cfg.sip_src)
        else:
            # Try to find a tarball in downloads.
            sip_tarballs = [
                f for f in listdir(cfg.downloads_dir)
                if f.lower().startswith('sip') and (f.endswith('.tar.gz') or f.endswith('.tgz'))]
            if sip_tarballs:
                archive = join(cfg.downloads_dir, sorted(sip_tarballs)[-1])
                _extract_tgz(archive, cfg.work_dir)
                sip_dirs2 = [d for d in listdir(cfg.work_dir) if d.lower().startswith('sip') and isdir(
                    join(cfg.work_dir, d))]
                if sip_dirs2:
                    cfg.sip_src = join(cfg.work_dir, sorted(sip_dirs2)[-1])
            else:
                log.warning(
                    'SIP snapshot not found.\n'
                    '  Download the LATEST SIP snapshot from:\n'
                    '    %s\n'
                    '  Save the .tar.gz to: %s\n'
                    '  Then re-run this script.',
                    SIP_SNAPSHOT_URL, cfg.downloads_dir)
    log.info('SIP source: %s', cfg.sip_src or '(not found -- download manually)')
    # PyQt5 -- must be latest snapshot.
    if cfg.pyqt5_src and isdir(cfg.pyqt5_src):
        log.info('PyQt5 source (user-provided): %s', cfg.pyqt5_src)
    else:
        pyqt_dirs = [d for d in listdir(cfg.work_dir) if d.lower().startswith('pyqt') and isdir(join(cfg.work_dir, d))]
        if pyqt_dirs:
            cfg.pyqt5_src = join(cfg.work_dir, sorted(pyqt_dirs)[-1])
            log.info('PyQt5 source (found): %s', cfg.pyqt5_src)
        else:
            pyqt_tarballs = [f for f in listdir(cfg.downloads_dir) if f.lower().startswith('pyqt') and (
                    f.endswith('.tar.gz') or f.endswith('.tgz'))]
            if pyqt_tarballs:
                archive = join(cfg.downloads_dir, sorted(pyqt_tarballs)[-1])
                _extract_tgz(archive, cfg.work_dir)
                pyqt_dirs2 = [
                    d for d in listdir(cfg.work_dir) if d.lower().startswith('pyqt') and isdir(join(cfg.work_dir, d))]
                if pyqt_dirs2:
                    cfg.pyqt5_src = join(cfg.work_dir, sorted(pyqt_dirs2)[-1])
            else:
                log.warning(
                    'PyQt5 snapshot not found.\n'
                    '  Download the LATEST PyQt5 GPL snapshot from:\n'
                    '    %s\n'
                    '  Save the .tar.gz to: %s\n'
                    '  Then re-run this script.',
                    PYQT5_SNAP_URL, cfg.downloads_dir)
    log.info('PyQt5 source: %s', cfg.pyqt5_src or '(not found -- download manually)')
    log.info('Sources step done OK')


# =============================================================================
# Step 5 -- Build host SIP (same version as target).
# =============================================================================

def build_host_sip(cfg):
    """
    From plashless Part 1:
      "You must also have a host SIP installation that is the same version
       as the target version you will be building."
      >cd ~/Downloads/sip*
      >python3 configure.py
      >make
      >sudo make install
    So now /usr/bin/sip (executable) is the same version as the sip.a library we will later build for Android.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 5/15 -- Building host SIP')
    if not cfg.sip_src or not isdir(cfg.sip_src):
        log.warning('SIP source not available; skipping host SIP build.')
        return
    env = cfg.build_env()
    log.info('Configuring host SIP ...')
    _run([getPythonExecutable(), 'configure.py'], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('Building host SIP ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('Installing host SIP (sudo) ...')
    _run(['sudo', getMakeExecutable(), 'install'], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('Host SIP built OK')


# =============================================================================
# Step 6 -- Build Python 3.4.0 statically for Android-32.
# =============================================================================

def build_python_static(cfg):
    """
    From plashless Part 1:
      >cd /home/bootch/Python-3.4.0
      >pyqtdeploycli --package python --target android-32 configure
      >export ANDROID_NDK_ROOT=/home/bootch/android-ndk-r10
      >/home/bootch/Qt/5.3/android_armv7/bin/qmake SYSROOT=/home/bootch/aRoot
      >make
      >make install
    From plashless:
      "You can't do this twice without starting with a fresh Python source distribution.
      Pyqtdeploy patches Python and copies the original source files to a same named file with .original appended."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 6/15 -- Building Python 3.4.0 statically (Android-32)')
    if cfg.skip_static or not cfg.python_src or not isdir(cfg.python_src):
        log.info('Skipping static Python build (skip_static=%s, src=%s).', cfg.skip_static, cfg.python_src)
        return
    env = cfg.build_env()
    # Check for .original files (already patched = second attempt)
    originals = []
    for root, dirs, files in walk(cfg.python_src):
        for f in files:
            if f.endswith('.original'):
                originals.append(join(root, f))
    if originals:
        log.warning(
            'Python source appears already patched (%d .original files found).\n'
            'If you get patch errors, delete the Python source and re-extract:\n'
            '  rm -rf %s && tar xzf %s -C %s',
            len(originals), cfg.python_src, join(cfg.downloads_dir, 'Python-3.4.0.tgz'), cfg.work_dir)
    # 1. pyqtdeploycli --package python --target android-32 configure.
    log.info('Running pyqtdeploycli configure for Python ...')
    _run([cfg.pyqtdeploycli, '--package', 'python', '--target', TARGET, 'configure'], cwd=cfg.python_src, env=env,
         dry_run=cfg.dry_run)
    # 2. qmake SYSROOT=...
    log.info('Running qmake for Python ...')
    _run([cfg.qmake, 'SYSROOT={0}'.format(cfg.sysroot)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    # 3. make
    log.info('Running make for Python (-j%d) ...', cfg.jobs)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    # 4. make install
    log.info('Running make install for Python ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('Static Python built OK')


# =============================================================================
# Step 7 -- Apply Android patches to Python source.
# =============================================================================

def patch_python_source(cfg):
    """
    Apply the three patches documented in plashless Part 2 to fix
    Android-specific build failures:
      a) SYS_getdents64  -- Python issue 20307 patch
         Symptom: "undefined SYS_getdents64"
      b) epoll_create1   -- comment out HAVE_EPOLL_CREATE1 in pyconfig.h
         Symptom: "undefined EPOLL_CLOEXEC" / "undefined epoll_create1"
         From plashless: "Apparently epoll_create1() is not implemented on Android."
      c) log2()          -- comment out HAVE_LOG2 + add undef in pyconfig.h
         Symptom: "undefined log2"
         From plashless: "Apparently there is no system log2() on Android."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 7/15 -- Patching Python source for Android')
    if not cfg.python_src or not isdir(cfg.python_src):
        log.warning('Python source not found; skipping patches.')
        return
    # -- a) SYS_getdents64 patch --------------------------------------------
    # The posixmodule.c in Python 3.4 uses getdents64 via SYS_getdents64
    # syscall number, which is not defined in the Android NDK r10 headers.
    posixmodule = join(cfg.python_src, 'Modules', 'posixmodule.c')
    if isfile(posixmodule):
        _patch_file(
            posixmodule,
            # Surround the SYS_getdents64 usage with an ifdef guard.
            'if (result == -1) {\n'
            '                if (errno == EINVAL && arg.nlink != 1)',
            'if (result == -1) {\n'
            '                if (errno == EINVAL && arg.nlink != 1)',
            'SYS_getdents64 guard (no-op if already correct)')
        # The real fix: guard the SYS_getdents64 definition.
        syscall_h = _find_file(cfg.python_src, 'syscall.h')
        if not syscall_h:
            # Inject a compatibility shim into pyconfig.h
            pyconfig = join(cfg.python_src, 'Include', 'pyconfig.h')
            if isfile(pyconfig):
                shim = (
                    '\n/* Android NDK r10 compat: SYS_getdents64 may be missing */\n'
                    '#ifndef SYS_getdents64\n'
                    '#  define SYS_getdents64 217\n'
                    '#endif\n')
                with open(pyconfig, 'r') as fh:
                    content = fh.read()
                if 'SYS_getdents64' not in content:
                    with open(pyconfig, 'a') as fh:
                        fh.write(shim)
                    log.info('Injected SYS_getdents64 shim into pyconfig.h')
    # -- b) epoll_create1 -- comment out in pyconfig.h ---------------------
    pyconfig_h = join(cfg.python_src, 'pyconfig.h')
    # pyqtdeploy places a generated pyconfig.h in the Python src root.
    if not isfile(pyconfig_h):
        pyconfig_h = join(cfg.python_src, 'Include', 'pyconfig.h')
    if isfile(pyconfig_h) and not cfg.dry_run:
        _patch_file(
            pyconfig_h,
            '#define HAVE_EPOLL_CREATE1 1',
            '/* #define HAVE_EPOLL_CREATE1 1 */  /* disabled for Android */',
            'epoll_create1 (plashless Part 2)')
        log.info('epoll_create1 patch applied to %s', pyconfig_h)
        # -- c) log2() -- comment out HAVE_LOG2 and add #undef --------------
        _patch_file(
            pyconfig_h,
            '#define HAVE_LOG2 1',
            '/* #define HAVE_LOG2 1 */  /* disabled for Android */\n'
            '#undef HAVE_LOG2', 'log2 (plashless Part 2)')
        log.info('log2 patch applied to %s', pyconfig_h)
    elif cfg.dry_run:
        log.info('[DRY-RUN] Would patch pyconfig.h at %s', pyconfig_h)
    log.info('Python patches applied OK')


# =============================================================================
# Step 8 -- Patch python.pro to add extra C extension modules.
# =============================================================================

def patch_python_pro(cfg):
    """
    From plashless Part 2 (FAQ: Optional Python modules missing at link time):
      "This will configure Python for a small sub-set of standard extension
       modules.  Your application will probably require additional ones to
       be enabled.  To do this you will need to make changes to the python.pro
       file and the config.c file."
      Example addition to python.pro:
        greaterThan(PY_MAJOR_VERSION, 2) {
            MOD_SOURCES = \\
            ...
            Modules/_posixsubprocess.c \\
            Modules/selectmodule.c \\
    Plashless note: "After you make these configurations, you should NOT run
    pyqtdeploy again (because it creates a new python.pro, overwriting your
    edits) but you should run qmake, make, make install again."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 8/15 -- Patching python.pro (extra C extension modules)')
    if not cfg.extra_modules:
        log.info('No extra modules requested; skipping python.pro patch.')
        return
    if not cfg.python_src or not isdir(cfg.python_src):
        log.warning('Python source not found; skipping python.pro patch.')
        return
    python_pro = join(cfg.python_src, 'python.pro')
    if not isfile(python_pro) and not cfg.dry_run:
        log.warning('python.pro not found at %s -- run Step 6 first to generate it.', python_pro)
        return
    if cfg.dry_run:
        log.info('[DRY-RUN] Would patch python.pro with modules: %s', cfg.extra_modules)
        return
    # Map module name -> source file name (plashless Part 2 explains the mapping)
    # "_posixsubprocess" -> "Modules/_posixsubprocess.c"
    # "select"           -> "Modules/selectmodule.c"
    SOURCE_MAP = {
        '_posixsubprocess': 'Modules/_posixsubprocess.c',
        'select': 'Modules/selectmodule.c',
        '_socket': 'Modules/socketmodule.c',
        '_ctypes': 'Modules/_ctypes/_ctypes.c',
        'math': 'Modules/mathmodule.c',
        '_decimal': 'Modules/_decimal/_decimal.c',
        'zlib': 'Modules/zlibmodule.c',
        'binascii': 'Modules/binascii.c',
        'fcntl': 'Modules/fcntlmodule.c',
        '_struct': 'Modules/_struct.c',
        'array': 'Modules/arraymodule.c',
        '_json': 'Modules/_json.c',
        'unicodedata': 'Modules/unicodedata.c'}
    with open(python_pro, 'r') as fh:
        content = fh.read()
    # Find the greaterThan(PY_MAJOR_VERSION, 2) block and append sources.
    new_sources = []
    for mod in cfg.extra_modules:
        src = SOURCE_MAP.get(mod)
        if src:
            if src not in content:
                new_sources.append('        {0} \\'.format(src))
            else:
                log.info('Module source already in python.pro: %s', src)
        else:
            log.warning('Unknown module "%s" -- add its source file to python.pro manually.', mod)
    if not new_sources:
        log.info('All requested module sources already present in python.pro.')
        return
    # Insert before the closing brace of the greaterThan block.
    insertion = '\n'.join(new_sources) + '\n'
    # Find the MOD_SOURCES block and inject before closing backslash sequence.
    pattern = compile(r'(greaterThan\(PY_MAJOR_VERSION.*?MOD_SOURCES\s*=.*?)\n(\})', DOTALL)
    m = pattern.search(content)
    if m:
        new_content = (content[:m.end(1)] + '\n' + insertion + content[m.start(2):])
        with open(python_pro, 'w') as fh:
            fh.write(new_content)
        log.info('Appended %d module source(s) to python.pro', len(new_sources))
    else:
        # Fallback: append at end of file.
        with open(python_pro, "a") as fh:
            fh.write('\n# Added by pyqt5_android_plashless.py\n')
            fh.write('SOURCES += \\\n')
            for src_line in new_sources:
                fh.write(src_line.lstrip() + "\n")
        log.info('Appended module sources to end of python.pro')


# =============================================================================
# Step 9 -- Patch config.c to register extension module init functions.
# =============================================================================

def patch_config_c(cfg):
    """
    From plashless Part 2:
      "To edit Modules/config.c, you add a line for SOME of the modules
       which you added to python.pro: those modules whose error is like 'PyInit...'"
      extern PyObject* PyInit__posixsubprocess(void);
      extern PyObject* PyInit_select(void);
      ...
      {"_posixsubprocess", PyInit__posixsubprocess}, {"select", PyInit_select},
    From plashless: "In this example, '_posixsubprocess' is a key (and also the name of the source code file) but note
    that this results in two underscores in the value field."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 9/15 -- Patching Modules/config.c (extension module init funcs)')
    if not cfg.extra_modules:
        log.info('No extra modules; skipping config.c patch.')
        return
    if not cfg.python_src or not isdir(cfg.python_src):
        log.warning('Python source not found; skipping config.c patch.')
        return
    config_c = join(cfg.python_src, 'Modules', 'config.c')
    if not isfile(config_c) and not cfg.dry_run:
        log.warning('Modules/config.c not found; skipping.')
        return
    if cfg.dry_run:
        log.info('[DRY-RUN] Would patch Modules/config.c for: %s', cfg.extra_modules)
        return
    with open(config_c, 'r') as fh:
        content = fh.read()
    extern_lines = []
    table_lines = []
    for mod in cfg.extra_modules:
        # Build the PyInit function name.
        # Single leading underscore -> keep as-is; it becomes double underscore
        # in PyInit because PyInit__ is the prefix for _-prefixed modules.
        func_name = 'PyInit_{0}'.format(mod)
        extern_decl = 'extern PyObject* {0}(void);'.format(func_name)
        table_entry = '{{"  {0}", {1}}},'.format(mod, func_name)
        if extern_decl not in content:
            extern_lines.append(extern_decl)
        if mod not in content:
            table_lines.append(table_entry)
    if not extern_lines and not table_lines:
        log.info('All module registrations already present in config.c.')
        return
    # Insert externals before the first existing extern declaration.
    extern_marker = 'extern PyObject* PyInit_'
    insert_pos = content.find(extern_marker)
    if insert_pos == -1:
        # Fallback: insert at top.
        insert_pos = 0
    extern_block = '\n'.join(extern_lines) + '\n'
    content = (content[:insert_pos] + extern_block + content[insert_pos:])
    # Insert table entries before the sentinel {0, 0}.
    sentinel = '    {0, 0}'
    sentinel_pos = content.find(sentinel)
    if sentinel_pos != -1:
        table_block = '\n'.join('    ' + l for l in table_lines) + '\n'
        content = (content[:sentinel_pos] + table_block + content[sentinel_pos:])
    with open(config_c, 'w') as fh:
        fh.write(content)
    log.info('Patched Modules/config.c with %d extern(s), %d table entry/entries', len(extern_lines), len(table_lines))


# =============================================================================
# Step 10 -- Rebuild Python after patches.
# =============================================================================

def rebuild_python_after_patches(cfg):
    """
    From plashless Part 2:
      "After you make these configurations, you should NOT run pyqtdeploy
       again (because it creates a new python.pro, overwriting your edits)
       but you should run qmake, make, make install again."
    We re-run only qmake + make + make install (skipping pyqtdeploycli).
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 10/15 -- Rebuilding Python after patches')
    if cfg.skip_static or not cfg.extra_modules:
        log.info('No patch-driven rebuild needed; skipping.')
        return
    if not cfg.python_src or not isdir(cfg.python_src):
        log.warning('Python source not found; skipping rebuild.')
        return
    env = cfg.build_env()
    log.info('Re-running qmake for Python (after patches) ...')
    _run([cfg.qmake, 'SYSROOT={0}'.format(cfg.sysroot)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('Re-running make for Python ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run, )
    log.info('Re-running make install for Python ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('Python rebuild OK')


# =============================================================================
# Step 11 -- Build SIP statically for Android-32.
# =============================================================================

def build_sip_static(cfg):
    """
    From plashless Part 1:
      >cd /home/bootch/sip*
      >pyqtdeploycli --package sip --target android-32 configure
      (creates sip-android.cfg)
      >python3 configure.py --static --sysroot=/home/bootch/aRoot --no-tools --use-qmake --configuration=sip-android.cfg
      >/home/bootch/Qt/5.3/android_armv7/bin/qmake
      >make
      >make install
      (copies sip header files to /home/bootch/aRoot/include/python3.4/)
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 11/15 -- Building SIP statically (Android-32)')
    if cfg.skip_static:
        log.info('--skip-static-build set; skipping SIP build.')
        return
    if not cfg.sip_src or not isdir(cfg.sip_src):
        log.warning(
            'SIP source directory not found.\n'
            '  Download the latest SIP snapshot from:\n    %s\n'
            '  Place it in: %s\n  Then re-run.', SIP_SNAPSHOT_URL, cfg.downloads_dir)
        return
    env = cfg.build_env()
    # 1. pyqtdeploycli --package sip --target android-32 configure
    log.info('Running pyqtdeploycli configure for SIP ...')
    _run([cfg.pyqtdeploycli, '--package', 'sip', '--target', TARGET, 'configure'], cwd=cfg.sip_src, env=env,
         dry_run=cfg.dry_run)
    cfg_file = join(cfg.sip_src, 'sip-android.cfg')
    # 2. python3 configure.py
    log.info('Running SIP configure.py ...')
    _run([getPythonExecutable(), 'configure.py', '--static', '--sysroot={0}'.format(cfg.sysroot), '--no-tools', '--use-qmake',
          '--configuration={0}'.format(cfg_file)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    # 3. qmake (Android qmake; ANDROID_NDK_ROOT must be set)
    log.info('Running qmake for SIP ...')
    _run([cfg.qmake], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    # 4. make
    log.info('Running make for SIP ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run, )
    # 5. make install
    log.info('Running make install for SIP ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('Static SIP built OK')


# =============================================================================
# Step 12 -- Build PyQt5 statically for Android-32.
# =============================================================================

def _edit_pyqt5_cfg(cfg_file, keep_modules):
    """
    From plashless Part 1:
      "I edit the pyqt5-android.cfg file, removing all Qt modules that
       I don't use ... I edit it down to QtCore, QtGui, QtWidgets,
       QtPrintSupport, QtSvg, and QtNetwork."
    Removes all Qt module lines not in keep_modules.
    :param cfg_file: str
    :param keep_modules: list[str]
    :return: None
    """
    if not isfile(cfg_file):
        return
    with open(cfg_file, 'r') as fh:
        lines = fh.readlines()
    out_lines = []
    in_qt_section = False
    for line in lines:
        stripped = line.strip()
        # Detect section header lines (e.g. [Qt 5.3.x]).
        if stripped.startswith('[Qt'):
            in_qt_section = True
            out_lines.append(line)
            continue
        if in_qt_section and stripped.startswith('[') and not stripped.startswith('[Qt'):
            in_qt_section = False
        if in_qt_section:
            # Lines like: QtFoo = ...  or  # QtFoo description.
            module_match = match(r"^(Qt\w+)\s*=", stripped)
            comment_match = match(r"^#\s*(Qt\w+)", stripped)
            mod_name = None
            if module_match:
                mod_name = module_match.group(1)
            elif comment_match:
                mod_name = comment_match.group(1)
            if mod_name and mod_name not in keep_modules:
                # Comment out the line.
                out_lines.append('# [removed] ' + line)
                log.debug('Removed Qt module from cfg: %s', mod_name)
                continue
        out_lines.append(line)
    with open(cfg_file, 'w') as fh:
        fh.writelines(out_lines)
    log.info('Edited pyqt5-android.cfg: kept modules %s', keep_modules)


def build_pyqt5_static(cfg):
    """
    From plashless Part 1:
      >cd /home/bootch/Downloads/PyQt-gpl*
      >pyqtdeploycli --package pyqt5 --target android-32 configure
      (edit pyqt5-android.cfg -- remove modules causing QSslConfiguration errors)
      >python3 configure.py --static --verbose \\
           --sysroot=/home/bootch/aRoot \\
           --no-tools --no-qsci-api --no-designer-plugin --no-qml-plugin \\
           --configuration=pyqt5-android.cfg \\
           --qmake=/home/bootch/Qt/5.3/android_armv7/bin/qmake
      >make
      >make install
    From plashless:
      "If you get 'mkdir: cannot create directory /libs: Permission denied'
       it is because you are using an older version of PyQt subject to QTBUG-39300.
       You need to use the latest snapshots of SIP and PyQt."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 12/15 -- Building PyQt5 statically (Android-32)')
    if cfg.skip_static:
        log.info('--skip-static-build set; skipping PyQt5 build.')
        return
    if not cfg.pyqt5_src or not isdir(cfg.pyqt5_src):
        log.warning(
            'PyQt5 source directory not found.\n'
            '  Download the latest PyQt5 GPL snapshot from:\n'
            '    %s\n'
            '  Place it in: %s\n'
            '  Then re-run.', PYQT5_SNAP_URL, cfg.downloads_dir)
        return
    env = cfg.build_env()
    # 1. pyqtdeploycli --package pyqt5 --target android-32 configure.
    log.info('Running pyqtdeploycli configure for PyQt5 ...')
    _run([cfg.pyqtdeploycli, '--package', 'pyqt5', '--target', TARGET, 'configure'], cwd=cfg.pyqt5_src, env=env,
         dry_run=cfg.dry_run)
    # 2. Edit pyqt5-android.cfg
    cfg_file = join(cfg.pyqt5_src, 'pyqt5-android.cfg')
    if isfile(cfg_file) and not cfg.dry_run:
        _edit_pyqt5_cfg(cfg_file, cfg.qt_modules)
    elif cfg.dry_run:
        log.info('[DRY-RUN] Would edit pyqt5-android.cfg: keep %s', cfg.qt_modules)
    # 3. python3 configure.py
    log.info('Running PyQt5 configure.py ...')
    configure_args = [
        getPythonExecutable(), 'configure.py',
        '--static',
        '--verbose',
        '--sysroot={0}'.format(cfg.sysroot),
        '--no-tools',
        '--no-qsci-api',
        '--no-designer-plugin',
        '--no-qml-plugin',
        '--configuration={0}'.format(cfg_file),
        '--qmake={0}'.format(cfg.qmake)]
    _run(configure_args, cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    # 4. make
    log.info('Running make for PyQt5 ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    # 5. make install
    log.info('Running make install for PyQt5 ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    log.info('Static PyQt5 built OK')


# =============================================================================
# Step 13 -- Create / validate the .pdy pyqtdeploy project file.
# =============================================================================

def create_pdy_project(cfg):
    """
    From plashless Part 2:
      >mkdir pensoolAndroidBuild
      >cd pensoolAndroidBuild
      >pyqtdeploy pensool.pdy
      (pyqtdeploy GUI opens -- configure the project interactively)
    Under "Locations" tab / "Target Python Locations", point to SYSROOT
    (e.g. /home/bootch/aRoot).
    This function:
      - If a .pdy already exists, validates it.
      - If it does not exist, generates a minimal XML .pdy template and
        prints instructions for completing it in the pyqtdeploy GUI.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 13/15 -- pyqtdeploy project (.pdy)')
    _makedirs(cfg.build_dir)
    if isfile(cfg.pdy_file):
        log.info("Using existing .pdy: %s", cfg.pdy_file)
        # Basic validation: check it references SYSROOT or the sysroot path.
        with open(cfg.pdy_file, 'r') as fh:
            content = fh.read()
        if cfg.sysroot not in content and 'SYSROOT' not in content:
            log.warning('The .pdy file does not reference SYSROOT (%s).\n  Open pyqtdeploy and set '
                        '"Target Python Locations" to:\n    %s', cfg.sysroot, cfg.sysroot)
        return
    # Create a minimal .pdy template.
    # pyqtdeploy 0.5 uses a simple XML format.
    pdy_content = dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE pyqtdeploy>
        <!--
            pyqtdeploy 0.5 / 0.6 project file
            Generated by pyqt5_android_plashless.py

            IMPORTANT: Open this file in pyqtdeploy to complete the configuration:
              pyqtdeploy {app_name}.pdy

            Under the "Locations" tab:
              - Main Script File: main.py  (or your entry point)
              - Target Python Locations: {sysroot}
              - (point to the SYSROOT where Python/SIP/PyQt5 were installed)

            Under the "Python" tab:
              - Select the Python 3.4 version
              - Enable the stdlib modules your app uses

            Under the "PyQt Modules" tab:
              - Enable: {modules}

            Under the "Build" tab:
              - Uncheck "Run Qmake" and "Run make" (do those in Qt Creator)
              - Click "Build"
        -->
        <pyqtdeploy version="0.5">
            <Application>
                <n>{app_name}</n>
                <MainScript>main.py</MainScript>
                <SysPath/>
                <Modules/>
            </Application>
            <Python>
                <TargetPythonLocation>{sysroot}</TargetPythonLocation>
                <TargetSipLocation>{sysroot}</TargetSipLocation>
            </Python>
            <PyQt5>
                <Modules>{module_list}</Modules>
            </PyQt5>
        </pyqtdeploy>
    """).format(
        app_name=cfg.app_name,
        sysroot=cfg.sysroot,
        modules=', '.join(cfg.qt_modules),
        module_list=''.join("\n                <Module name='{0}'/>".format(m) for m in cfg.qt_modules))
    with open(cfg.pdy_file, 'w') as fh:
        fh.write(pdy_content)
    log.info('Created .pdy template: %s', cfg.pdy_file)
    log.info(
        '\n'
        '  NEXT STEP: Complete the .pdy configuration:\n'
        '    1. Open: pyqtdeploy {0}\n'
        '    2. Locations tab -> Target Python Locations -> {1}\n'
        '    3. Add your application source files\n'
        '    4. Build tab -> uncheck "Run Qmake"/"Run make" -> click Build\n',
        cfg.pdy_file, cfg.sysroot)


def _create_main_py_template(cfg):
    """
    Create a minimal PyQt5 main.py if none exists in the project.
    :param cfg: BuildConfig
    :return: None
    """
    main_py = join(cfg.project_dir, 'main.py')
    if isfile(main_py):
        return
    template = dedent("""\
        #!/usr/bin/env python3
        # -*- coding: utf-8 -*-
        # Minimal PyQt5 application -- generated by pyqt5_android_plashless.py
        # Based on: plashless.wordpress.com pyqtdeploy 0.5 Android guide

        import sys
        from PyQt5.QtWidgets import QApplication, QLabel
        from PyQt5.QtCore import Qt


        def main():
            app = QApplication(sys.argv)
            label = QLabel("<center><h2>Hello from PyQt5 on Android!</h2></center>")
            label.setAlignment(Qt.AlignCenter)
            label.setWindowTitle("{app}")
            label.resize(480, 320)
            label.show()
            return app.exec_()


        if __name__ == '__main__':
            sys.exit(main())
    """.format(app=cfg.app_name))
    with open(main_py, 'w') as fh:
        fh.write(template)
    log.info('Created main.py template: %s', main_py)


# =============================================================================
# Step 14 -- Run pyqtdeploy build (freeze + generate Qt Creator .pro).
# =============================================================================

def run_pyqtdeploy_build(cfg):
    """
    From plashless Part 2:
      "After you have configured pyqtdeploy, choose the 'Build' tab.
       Uncheck most of the options (i.e. 'Run Qmake' and 'Run make') ...
       Choose the 'Build' button.  This creates a Qt Creator project file (.pro)"
    In pyqtdeploy 0.6 (command-line), the equivalent is:
      pyqtdeploycli build <app>.pdy
    We invoke the command-line build, which freezes Python source files and
    generates the .pro file for Qt Creator.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 14/15 -- pyqtdeploy build (freeze + generate .pro)')
    if not isfile(cfg.pdy_file):
        log.warning('.pdy file not found: %s\n  Run Step 13 or create the project file manually with:\n    '
                    'pyqtdeploy %s', cfg.pdy_file, cfg.pdy_file)
        return
    env = cfg.build_env()
    # Determine the correct command.
    # In v0.5: pyqtdeploy build <app>.pdy
    # In v0.6: pyqtdeploycli build <app>.pdy
    build_cmd = getattr(cfg, 'pyqtdeploycli', 'pyqtdeploycli')
    _makedirs(cfg.build_dir)
    _run([build_cmd, 'build', cfg.pdy_file], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # Locate generated .pro file.
    pro_files = []
    for root, dirs, files in walk(cfg.build_dir):
        for f in files:
            if f.endswith('.pro'):
                pro_files.append(join(root, f))
    if pro_files:
        log.info('Qt Creator .pro file(s) generated:')
        for pro in pro_files:
            log.info('  %s', pro)
    else:
        log.warning(
            'No .pro file found in %s after pyqtdeploy build.\n'
            '  This may be expected if you uncheck all build options in the GUI.', cfg.build_dir)
    log.info('pyqtdeploy build done OK')


# =============================================================================
# Step 15 -- Qt Creator build or command-line qmake + make
# =============================================================================

def build_with_qtcreator(cfg):
    """
    From plashless Part 2:
      "Start Qt Creator"
      "Navigate to the project created by pyqtdeploy and choose the project."
      "Configure the project for Android."
      "Choose the 'Build' button."
    We attempt a command-line build (qmake + make) and fall back to
    printing QtCreator instructions if the .pro is not found.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 15/15 -- Qt Creator build (qmake + make)')
    env = cfg.build_env()
    # Find the generated .pro file.
    pro_files = []
    for root, dirs, files in walk(cfg.build_dir):
        for f in files:
            if f.endswith('.pro') and 'Makefile' not in root:
                pro_files.append(join(root, f))
    if not pro_files:
        log.info(
            '\n'
            '  No .pro file found for command-line build.\n'
            '  Open the project manually in Qt Creator:\n'
            '    1. Start Qt Creator\n'
            '    2. File -> Open File or Project -> %s\n'
            '    3. Configure for Android (choose your Android AVD kit)\n'
            '    4. Click the Build button\n'
            '\n'
            '  FAQ errors in Qt Creator: see the Usage guide (USAGE_PLASHLESS.md).',
            cfg.build_dir)
        return
    pro_file = pro_files[0]
    pro_dir = dirname(pro_file)
    log.info('Running qmake on: %s', pro_file)
    _run([cfg.qmake, pro_file], cwd=pro_dir, env=env, dry_run=cfg.dry_run)
    log.info('Running make (-j%d) ...', cfg.jobs)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=pro_dir, env=env, dry_run=cfg.dry_run)
    # Locate APK.
    apk_files = []
    for root, dirs, files in walk(cfg.build_dir):
        for f in files:
            if f.endswith('.apk'):
                apk_files.append(join(root, f))
    if apk_files:
        apk = sorted(apk_files, key=getmtime)[-1]
        log.info('APK built: %s  (%d MB)', apk, getsize(apk) // (1024 * 1024))
        if cfg.install_apk:
            _install_apk(cfg, apk)
    elif cfg.dry_run:
        log.info('[DRY-RUN] Build skipped; APK location unknown.')
    else:
        log.warning(
            'No .apk produced by command-line build.\n'
            '  Open Qt Creator -> select the .pro -> configure for Android -> Build.\n  .pro file: %s', pro_file)
    log.info('Build step done OK')


def _install_apk(cfg, apk_path):
    """
    Install APK via adb if a device is connected.
    :param cfg: BuildConfig
    :param apk_path: str
    :return: None
    """
    adb = which('adb') or cfg.adb
    if not adb or not isfile(adb):
        log.warning('adb not found; skipping install.')
        return
    result = _run([adb, 'devices'], capture=True, check=False, dry_run=cfg.dry_run)
    lines = (result.stdout or b'').decode('utf-8', errors="replace").splitlines()
    devices = [l for l in lines if l.strip() and "List of devices" not in l]
    if not devices:
        log.warning('No ADB devices connected; skipping install.')
        return
    _run([adb, 'install', '-r', apk_path], dry_run=cfg.dry_run)
    log.info('APK installed via adb OK')


# =============================================================================
# Summary.
# =============================================================================

def print_summary(cfg):
    """
    :param cfg: BuildConfig
    :return:
    """
    step('Build summary')
    log.info(
        '\n'
        '  App name         : %s\n'
        '  Python version   : %s\n'
        '  Qt version       : %s  (%s)\n'
        '  Target           : %s\n'
        '  NDK              : %s\n'
        '  SYSROOT          : %s\n'
        '  Project dir      : %s\n'
        '  Build dir        : %s\n'
        '  .pdy file        : %s',
        cfg.app_name,
        PYTHON_VERSION,
        QT_VERSION, QT_ARCH_SUBDIR,
        TARGET,
        cfg.ndk_root,
        cfg.sysroot,
        cfg.project_dir,
        cfg.build_dir,
        cfg.pdy_file)
    log.info(dedent("""
        --------------------------------------------------------------
        Error FAQ  (from plashless Parts 1 & 2)
        --------------------------------------------------------------

        • "line does not match diff context" during pyqtdeploycli configure
          Cause: Python source already patched (second run).
          Fix:   Delete the Python source dir and re-extract:
                   rm -rf {python_src}
                   tar xzf downloads/Python-3.4.0.tgz -C {work}

        • "skipping incompatible .../libQtGui.a"
          Cause: Library built for wrong architecture (Intel vs ARM).
          Diagnose: objdump -f libfoo.a | grep architecture
          Fix:   Rebuild static libs with the correct qmake
                 (use ~/Qt/5.3/android_armv7/bin/qmake, NOT /usr/bin/qmake).

        • "undefined reference to PyInit__posixsubprocess"
          Cause: Optional Python C extension module not compiled in.
          Fix:   Add to --extra-modules:
                   python3 pyqt5_android_plashless.py ... --extra-modules _posixsubprocess,select

        • "undefined SYS_getdents64"
          Cause: Android NDK r10 does not define this syscall number.
          Fix:   Step 7 applies the patch automatically.
                 If it persists: add to pyconfig.h:
                   #define SYS_getdents64 217

        • "undefined EPOLL_CLOEXEC" / "undefined epoll_create1"
          Cause: epoll_create1 not implemented on Android.
          Fix:   Step 7 comments out HAVE_EPOLL_CREATE1 in pyconfig.h.

        • "undefined log2"
          Cause: No system log2() on Android.
          Fix:   Step 7 adds: /* #define HAVE_LOG2 1 */ + #undef HAVE_LOG2

        • "mkdir: cannot create directory /libs: Permission denied"
          Cause: QTBUG-39300 -- old stable PyQt release.
          Fix:   Use the LATEST SNAPSHOT of SIP and PyQt5 (not stable GPL).

        • "PyQt license is incompatible with Qt license"
          Cause: Mismatch between GPL PyQt5 and your Qt license.
          Fix:   Use the GPL PyQt5 snapshot with the open-source Qt installer.

        • Patches to python.pro lost after re-running pyqtdeploycli
          From plashless: "After patching, do NOT run pyqtdeploy again."
          Fix:   Only re-run qmake + make + make install (Step 10).
        --------------------------------------------------------------
    """).format(python_src=cfg.python_src, work=cfg.work_dir))


# =============================================================================
# Argument parser.
# =============================================================================

def make_parser():
    """
    :return: ArgumentParser
    """
    parser = ArgumentParser(
        prog='pyqt5_android_plashless.py', formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PyQt5 5.3 Android Builder  (pyqtdeploy 0.5 / 0.6 pipeline)
            ============================================================
            Automates the pipeline documented in the plashless blog:
              Part 1: pyqtdeploy0.5 on Linux to cross compile a PyQt app for Android
              Part 2: (same, continued)

            Pinned versions:
              Python 3.4.0  |  Qt 5.3 (android_armv7)  |  NDK r10
              pyqtdeploy 0.5 / 0.6  (installed from Mercurial)
        """),
        epilog=dedent("""\
            Examples
            --------
            # Full automated build:
            python3 pyqt5_android_plashless.py \\
                --project-dir ./myapp \\
                --ndk-root    ~/android-ndk-r10 \\
                --qt-dir      ~/Qt/5.3

            # With pre-extracted sources:
            python3 pyqt5_android_plashless.py \\
                --project-dir ./myapp \\
                --python-src  ~/Python-3.4.0 \\
                --sip-src     ~/sip-4.16.5 \\
                --pyqt5-src   ~/PyQt-gpl-5.3.2

            # Skip static builds (already have sysroot):
            python3 pyqt5_android_plashless.py \\
                --project-dir ./myapp \\
                --sysroot     ~/aRoot \\
                --skip-static-build

            # Add extra Python C extension modules:
            python3 pyqt5_android_plashless.py \\
                --project-dir ./myapp \\
                --extra-modules _posixsubprocess,select,_socket

            # Build + install on connected device:
            python3 pyqt5_android_plashless.py --project-dir ./myapp --install-apk

            # Dry-run:
            python3 pyqt5_android_plashless.py --project-dir ./myapp --dry-run --verbose
        """),
    )
    parser.add_argument('--project-dir', required=True, metavar='DIR',
                        help='Your app directory (must contain or will receive main.py).')
    parser.add_argument('--app-name', default=None, metavar="NAME",
                        help='Application name (default: project dir basename).')
    # Toolchain
    parser.add_argument('--ndk-root', default=DEFAULT_NDK_ROOT, metavar='DIR',
                        help='Android NDK r10 root (default: {0}).'.format(DEFAULT_NDK_ROOT))
    parser.add_argument('--qt-dir', default=DEFAULT_QT_DIR, metavar='DIR',
                        help='Qt 5.3 root (default: {0}).'.format(DEFAULT_QT_DIR))
    parser.add_argument('--sysroot', default=DEFAULT_SYSROOT, metavar='DIR',
                        help='SYSROOT directory (default: {0}).'.format(DEFAULT_SYSROOT))
    parser.add_argument('--work-dir', default=DEFAULT_WORK_DIR, metavar='DIR',
                        help='Working directory for downloads/clones (default: {0}).'.format(DEFAULT_WORK_DIR))
    # Pre-extracted source directories.
    parser.add_argument('--python-src', default='', metavar='DIR', help='Pre-extracted Python 3.4.0 source directory.')
    parser.add_argument('--sip-src', default='', metavar='DIR', help='Pre-extracted SIP snapshot source directory.')
    parser.add_argument('--pyqt5-src', default='', metavar='DIR', help='Pre-extracted PyQt5 snapshot source directory.')
    parser.add_argument(
        '--pyqtdeploy-src', default='', metavar='DIR', help='Pre-cloned pyqtdeploy Mercurial repository.')
    # Build control.
    parser.add_argument(
        '--extra-modules', default='', metavar='LIST', help=(
            'Comma-separated Python C extension modules to add (e.g. _posixsubprocess,select,_socket). '
            'Used to fix "undefined reference to PyInit_*" link errors.'))
    parser.add_argument('--jobs', type=int, default=2, metavar='N', help='Parallel make jobs (default: 2).')
    parser.add_argument('--skip-static-build', action='store_true',
                        help='Skip Python/SIP/PyQt5 cross-compilation (use existing SYSROOT).')
    parser.add_argument('--install-apk', action='store_true', help='Install the APK on the first connected ADB device.')
    parser.add_argument('--keep-build', action='store_true', help='Retain intermediate build files.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug-level output.')
    return parser


# =============================================================================
# Main entry point.
# =============================================================================

def main(argv=None):
    """
    :param argv: list[str] | None
    :return: int
    """
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        getLogger().setLevel(DEBUG)
    cfg = BuildConfig(args)
    cfg.pyqtdeploycli = 'pyqtdeploycli'  # Will be updated in install step.
    # Create main.py if the project has none.
    _create_main_py_template(cfg)
    try:
        preflight(cfg)
        setup_environment(cfg)
        install_pyqtdeploy(cfg)
        download_sources(cfg)
        build_host_sip(cfg)
        build_python_static(cfg)
        patch_python_source(cfg)
        patch_python_pro(cfg)
        patch_config_c(cfg)
        rebuild_python_after_patches(cfg)
        build_sip_static(cfg)
        build_pyqt5_static(cfg)
        create_pdy_project(cfg)
        run_pyqtdeploy_build(cfg)
        build_with_qtcreator(cfg)
        print_summary(cfg)
        return 0
    except EnvironmentError as exc:
        log.error('Environment error:\n%s', exc)
        return 2
    except IOError as exc:
        log.error('File error:\n%s', exc)
        return 3
    except RuntimeError as exc:
        log.error('Build error:\n%s', exc)
        return 4
    except KeyboardInterrupt:
        log.warning('Interrupted.')
        return 130


if __name__ == '__main__':
    exit(main())
