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
from logging import basicConfig, getLogger, DEBUG, INFO
from os import environ, chmod, listdir, walk, statvfs
from sys import exit, version_info, path
from re import DOTALL, match, compile
from platform import system, release
from subprocess import PIPE, Popen
from textwrap import dedent
from zipfile import ZipFile
import tarfile

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import getMakeExecutable, getHgExecutable, getPythonExecutable
    from .build_utils import which, _makedirs, urlretrieve, URLError
except:
    from builders import getMakeExecutable, getHgExecutable, getPythonExecutable
    from build_utils import which, _makedirs, urlretrieve, URLError

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
# --- patched: direct download URLs for sources that the original script ---
# --- required to be downloaded "manually".  All mirrors verified working. ---
SIP_VERSION = '4.16.9'
PYQT5_VERSION = '5.3.2'
PYQTDEPLOY_VERSION = '0.5'
SIP_URL = 'https://distfiles.macports.org/py-sip/sip-{0}.tar.gz'.format(SIP_VERSION)
SIP_URL_FALLBACK = 'https://sourceforge.net/projects/pyqt/files/sip/sip-{0}/sip-{0}.tar.gz/download'.format(SIP_VERSION)
PYQT5_URL = 'https://distfiles.macports.org/py-pyqt5/PyQt-gpl-{0}.tar.gz'.format(PYQT5_VERSION)
PYQT5_URL_FALLBACK = ('https://sourceforge.net/projects/pyqt/files/PyQt5/PyQt-{0}/'
                     'PyQt-gpl-{0}.tar.gz/download').format(PYQT5_VERSION)
PYQTDEPLOY_URL = 'https://distfiles.macports.org/py-pyqtdeploy/pyqtdeploy-{0}.tar.gz'.format(PYQTDEPLOY_VERSION)
# Kept for log-message backward-compat:
SIP_SNAPSHOT_URL = SIP_URL
PYQT5_SNAP_URL = PYQT5_URL
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
        self.output_dir = realpath(args.output_dir) if getattr(args, 'output_dir', '') else join(self.project_dir, 'output')
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
        # Directory holding the pure-Python PyQt5 shim that lets pyqtdeploy 0.5
        # run head-lessly (no host PyQt5 needed).  Populated in Step 3.
        self.pyqt5_shim_dir = join(self.work_dir, '_pyqt5_headless_shim')

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
        # 'PyQt5 installs to /usr/lib/Python3.4/site-packages, and that must be on PYTHONPATH.'
        existing_pp = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = '/usr/lib/python3.4/site-packages' + (pathsep + existing_pp if existing_pp else '')
        # Put the pure-Python PyQt5 shim first so pyqtdeploy 0.5 can run without
        # a host PyQt5 installation (configure/build only need a few QtCore
        # filesystem classes, which the shim provides).
        if getattr(self, 'pyqt5_shim_dir', '') and isdir(self.pyqt5_shim_dir):
            env['PYTHONPATH'] = self.pyqt5_shim_dir + pathsep + env['PYTHONPATH']
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


def _download_with_fallback(urls, dest):
    """
    Try a list of URLs in order; first success wins.  Used when the primary
    download mirror may be flaky (e.g. SourceForge under load).
    :param urls: list[str]
    :param dest: str
    :return: None
    """
    _makedirs(dirname(dest))
    if exists(dest) and getsize(dest) > 0:
        log.info('Cached: %s', basename(dest))
        return
    for url in urls:
        log.info('Trying:     %s', url)
        try:
            urlretrieve(url, dest)
            if exists(dest) and getsize(dest) > 1024:
                log.info('Saved:       %s', dest)
                return
        except URLError as exc:
            log.warning('  failed: %s', exc)
            if exists(dest):
                try:
                    from os import remove
                    remove(dest)
                except OSError:
                    pass
    raise RuntimeError(
        'Failed to download {0} from any of: {1}'.format(basename(dest), urls))


def _maybe_sudo(cmd_list):
    """
    Prepend 'sudo' only if we are NOT already root (in Docker we run as root,
    so calling sudo is both unnecessary and often broken because sudo may
    not even be installed).
    :param cmd_list: list[str]
    :return: list[str]
    """
    try:
        from os import geteuid
        if geteuid() == 0:
            return list(cmd_list)
    except (AttributeError, ImportError):
        pass
    if which('sudo'):
        return ['sudo'] + list(cmd_list)
    return list(cmd_list)


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
            # --- patched: original Riverbank Mercurial server is dead.
            # Try a source-tarball download FIRST; only fall through to
            # `hg clone` if no tarball mirror works (very unlikely now).
            tarball_dest = join(cfg.downloads_dir, 'pyqtdeploy-{0}.tar.gz'.format(PYQTDEPLOY_VERSION))
            try:
                log.info('Fetching pyqtdeploy %s source tarball ...', PYQTDEPLOY_VERSION)
                _download_with_fallback([PYQTDEPLOY_URL], tarball_dest)
                _extract_tgz(tarball_dest, cfg.work_dir)
                # Locate extracted dir (may be pyqtdeploy-0.5 or just pyqtdeploy)
                for entry in listdir(cfg.work_dir):
                    full = join(cfg.work_dir, entry)
                    if entry.lower().startswith('pyqtdeploy') and isdir(full):
                        src_dir = full
                        break
            except RuntimeError as exc:
                log.warning('Tarball download failed (%s); trying hg clone ...', exc)
                hg = which('hg')
                if not hg:
                    raise RuntimeError(
                        'Cannot install pyqtdeploy: no working tarball mirror '
                        'and "hg" not on PATH for fallback clone.')
                _run([hg, 'clone', PYQTDEPLOY_HG, src_dir], dry_run=cfg.dry_run)
        else:
            log.info('Updating pyqtdeploy (existing checkout) ...')
            # Existing checkout -- try `hg pull` only if it's an hg repo.
            if isdir(join(src_dir, '.hg')):
                _run([getHgExecutable(), 'pull'], cwd=src_dir, dry_run=cfg.dry_run)
                _run([getHgExecutable(), 'update'], cwd=src_dir, dry_run=cfg.dry_run)
    # Some pyqtdeploy versions build a VERSION file via make; older tarballs
    # already ship one.  Make targets are best-effort.
    if isfile(join(src_dir, 'Makefile')):
        try:
            _run([getMakeExecutable(), 'VERSION'], cwd=src_dir, dry_run=cfg.dry_run, check=False)
            _run([getMakeExecutable(), 'pyqtdeploy/version.py'], cwd=src_dir, dry_run=cfg.dry_run, check=False)
        except RuntimeError as exc:
            log.warning('VERSION make targets failed (%s); continuing ...', exc)
    _run(_maybe_sudo([getPythonExecutable(), 'setup.py', 'install']), cwd=src_dir, dry_run=cfg.dry_run)
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
# Step 3b -- Make pyqtdeploy 0.5 run head-lessly (no host PyQt5).
# =============================================================================
#
# pyqtdeploy 0.5 is itself a PyQt5 application.  Importing the package runs
# pyqtdeploy/__init__.py, which eagerly imports the GUI (PyQt5.QtWidgets) and
# the Builder/Project (PyQt5.QtCore).  On this build host there is no host
# PyQt5, so even the command-line `configure`/`build` actions fail with:
#     ImportError: No module named 'PyQt5'
#
# Building a real host PyQt5 just to run pyqtdeploy would be enormous and
# pointless: the CLI path only uses a handful of QtCore *filesystem* classes
# (QDir, QFile, QFileInfo, QIODevice, QByteArray) plus inert QObject/pyqtSignal.
# So we:
#   1. Drop a tiny *pure-Python* PyQt5.QtCore shim onto PYTHONPATH (build_env()
#      puts cfg.pyqt5_shim_dir first), satisfying builder.py / project.py /
#      file_utilities.py with zero real Qt.
#   2. Make pyqtdeploy/__init__.py's GUI import non-fatal, so the head-less CLI
#      never tries to import PyQt5.QtWidgets.  (If a real PyQt5 is ever present,
#      the GUI still loads -- the patch is a try/except, not a deletion.)
#
# Both steps are idempotent and self-healing, so they work whether or not the
# Docker image was rebuilt.

# The shim source.  QtCore only -- it's all the CLI path touches.
_PYQT5_QTCORE_SHIM = r'''# -*- coding: utf-8 -*-
"""Auto-generated pure-Python stand-in for the subset of PyQt5.QtCore used by
pyqtdeploy 0.5's command-line (configure/build) path.  No real Qt; signals are
inert no-ops (the CLI has no event loop)."""
import os as _os
import shutil as _shutil


class QIODevice(object):
    ReadOnly = 0x0001; WriteOnly = 0x0002; ReadWrite = ReadOnly | WriteOnly
    Append = 0x0004; Truncate = 0x0008; Text = 0x0010; Unbuffered = 0x0020


class QByteArray(object):
    def __init__(self, data=b""):
        if isinstance(data, QByteArray): data = data._data
        elif isinstance(data, str): data = data.encode("utf-8")
        elif data is None: data = b""
        self._data = bytes(data)
    @staticmethod
    def _b(v):
        if isinstance(v, QByteArray): return v._data
        if isinstance(v, str): return v.encode("utf-8")
        return bytes(v)
    def replace(self, before, after):
        self._data = self._data.replace(self._b(before), self._b(after)); return self
    def endsWith(self, suffix): return self._data.endswith(self._b(suffix))
    def startsWith(self, prefix): return self._data.startswith(self._b(prefix))
    def chop(self, n):
        if n > 0: self._data = self._data[:-n]
    def isEmpty(self): return len(self._data) == 0
    def split(self, sep): return [QByteArray(p) for p in self._data.split(self._b(sep))]
    def data(self): return self._data
    def __bytes__(self): return self._data
    def __len__(self): return len(self._data)
    def __str__(self): return self._data.decode("utf-8", "replace")


class QFileInfo(object):
    def __init__(self, path): self._path = _os.path.abspath(str(path))
    def absoluteDir(self): return QDir(_os.path.dirname(self._path))
    def absoluteFilePath(self): return self._path


class QDir(object):
    Dirs = 0x001; Files = 0x002; Drives = 0x004; NoSymLinks = 0x008
    NoDotAndDotDot = 0x1000; AllEntries = Dirs | Files | Drives
    def __init__(self, path="."): self._path = _os.path.abspath(str(path))
    def cd(self, name):
        c = _os.path.abspath(_os.path.join(self._path, str(name)))
        if _os.path.isdir(c): self._path = c; return True
        return False
    def absolutePath(self): return self._path
    def exists(self, name=None):
        if name is None: return _os.path.isdir(self._path)
        return _os.path.exists(_os.path.join(self._path, str(name)))
    def absoluteFilePath(self, name):
        return _os.path.abspath(_os.path.join(self._path, str(name)))
    def entryList(self, filters=0):
        try: names = sorted(_os.listdir(self._path))
        except OSError: return []
        want_dirs = bool(filters & QDir.Dirs); want_files = bool(filters & QDir.Files)
        if not want_dirs and not want_files: want_dirs = want_files = True
        no_dots = bool(filters & QDir.NoDotAndDotDot); result = []
        for name in names:
            full = _os.path.join(self._path, name)
            if _os.path.isdir(full):
                if want_dirs: result.append(name)
            elif want_files: result.append(name)
        if want_dirs and not no_dots: result = [".", ".."] + result
        return result
    @staticmethod
    def fromNativeSeparators(path): return str(path).replace("\\", "/")
    @staticmethod
    def toNativeSeparators(path): return str(path)


class QFile(object):
    def __init__(self, name=""):
        self._name = str(name); self._fh = None; self._error = ""
    def fileName(self): return self._name
    def errorString(self): return self._error
    def open(self, mode):
        text = bool(mode & QIODevice.Text)
        base = ("a" if (mode & QIODevice.Append) else "w") if (mode & QIODevice.WriteOnly) else "r"
        try:
            self._fh = open(self._name, base, encoding="utf-8") if text else open(self._name, base + "b")
            return True
        except (IOError, OSError) as e:
            self._error = str(e); self._fh = None; return False
    def readAll(self):
        if self._fh is None: return QByteArray(b"")
        data = self._fh.read()
        if isinstance(data, str): data = data.encode("utf-8")
        return QByteArray(data)
    def write(self, data):
        if self._fh is None: self._error = "file not open"; return -1
        try:
            payload = data.data() if isinstance(data, QByteArray) else (data.encode("utf-8") if isinstance(data, str) else bytes(data))
            if "b" in getattr(self._fh, "mode", "wb"): self._fh.write(payload)
            else: self._fh.write(payload.decode("utf-8"))
            return len(payload)
        except (IOError, OSError) as e:
            self._error = str(e); return -1
    def close(self):
        if self._fh is not None:
            try: self._fh.close()
            finally: self._fh = None
    @staticmethod
    def exists(name): return _os.path.exists(str(name))
    @staticmethod
    def remove(name):
        try: _os.remove(str(name)); return True
        except OSError: return False
    @staticmethod
    def copy(src, dst):
        try:
            if _os.path.exists(str(dst)): return False
            _shutil.copyfile(str(src), str(dst)); return True
        except (IOError, OSError): return False


class _Signal(object):
    def __init__(self, *types): self._types = types
    def connect(self, *a, **k): return None
    def disconnect(self, *a, **k): return None
    def emit(self, *a, **k): return None


def pyqtSignal(*types, **kwargs): return _Signal(*types)


class QObject(object):
    def __init__(self, *args, **kwargs): pass
    def blockSignals(self, b): return False
    def objectName(self): return ""
    def setObjectName(self, name): pass
'''


def _write_pyqt5_shim(cfg):
    """ Write the pure-Python PyQt5 shim into cfg.pyqt5_shim_dir (idempotent).
    :param cfg: BuildConfig
    """
    pkg_dir = join(cfg.pyqt5_shim_dir, 'PyQt5')
    _makedirs(pkg_dir)
    init_py = join(pkg_dir, '__init__.py')
    qtcore_py = join(pkg_dir, 'QtCore.py')
    if not isfile(init_py):
        with open(init_py, 'w') as fh:
            fh.write('# Pure-Python head-less stand-in for PyQt5 (QtCore only).\n')
    with open(qtcore_py, 'w') as fh:
        fh.write(_PYQT5_QTCORE_SHIM)
    log.info('PyQt5 head-less shim ready: %s', pkg_dir)


def _locate_pyqtdeploy_init():
    """ Return the path to the installed pyqtdeploy package __init__.py without
    importing it (importing would trigger the very PyQt5 ImportError we fix).
    :return: str | None
    """
    # importlib.util.find_spec locates a package without executing __init__.
    try:
        import importlib.util
        spec = importlib.util.find_spec('pyqtdeploy')
        if spec is not None:
            if getattr(spec, 'origin', None) and basename(spec.origin) == '__init__.py':
                return spec.origin
            for loc in (getattr(spec, 'submodule_search_locations', None) or []):
                cand = join(loc, '__init__.py')
                if isfile(cand):
                    return cand
    except Exception as exc:
        log.debug('find_spec(pyqtdeploy) failed: %s', exc)
    # Fallback: scan sys.path entries (incl. eggs) for pyqtdeploy/__init__.py.
    candidates = []
    for entry in list(path):
        if not entry or not isdir(entry):
            continue
        direct = join(entry, 'pyqtdeploy', '__init__.py')
        if isfile(direct):
            candidates.append(direct)
        try:
            for name in listdir(entry):
                if name.lower().startswith('pyqtdeploy') and name.lower().endswith('.egg'):
                    cand = join(entry, name, 'pyqtdeploy', '__init__.py')
                    if isfile(cand):
                        candidates.append(cand)
        except OSError:
            pass
    return candidates[0] if candidates else None


def _patch_pyqtdeploy_gui_import(init_path):
    """ Make pyqtdeploy/__init__.py's GUI import non-fatal so the head-less CLI
    works without PyQt5.QtWidgets.  Idempotent.
    :param init_path: str
    :return: bool  -- True if patched/already-OK, False if anchor missing.
    """
    with open(init_path, 'r') as fh:
        src = fh.read()
    sentinel = 'except ImportError:\n    ProjectGUI = None'
    if sentinel in src:
        log.info('pyqtdeploy __init__ already head-less-patched OK')
        return True
    anchor = 'from .gui import ProjectGUI'
    if anchor not in src:
        log.warning('Could not find GUI import anchor in %s; skipping patch.', init_path)
        return False
    patched = src.replace(
        anchor,
        'try:\n    from .gui import ProjectGUI\nexcept ImportError:\n    ProjectGUI = None',
        1)
    try:
        with open(init_path, 'w') as fh:
            fh.write(patched)
        log.info('Patched pyqtdeploy __init__ for head-less use: %s', init_path)
        return True
    except (IOError, OSError) as exc:
        log.warning('Could not write patched %s (%s); relying on shim only.', init_path, exc)
        return False


def _patch_pyqtdeploy_freeze(init_path):
    """ Make pyqtdeploy's freeze.py read source files as bytes so non-ASCII
    Python sources freeze correctly under a POSIX/ASCII locale (Python 3.4's
    open() otherwise defaults to ASCII and raises UnicodeDecodeError).  Passing
    bytes to compile() lets it honour PEP 263 coding cookies / default to UTF-8,
    exactly like CPython's own import machinery.  Idempotent.
    :param init_path: str  -- path to pyqtdeploy/__init__.py (to locate the pkg)
    :return: bool
    """
    pkg_dir = dirname(init_path)
    freeze_path = join(pkg_dir, 'builder', 'lib', 'freeze.py')
    if not isfile(freeze_path):
        log.warning('freeze.py not found at %s; skipping freeze encoding patch.', freeze_path)
        return False
    with open(freeze_path, 'r') as fh:
        src = fh.read()
    if "open(py_filename, 'rb')" in src or 'open(py_filename, "rb")' in src:
        log.info('pyqtdeploy freeze.py already reads source as bytes OK')
        return True
    anchor = 'source_file = open(py_filename)'
    if anchor not in src:
        log.warning('Could not find open() anchor in %s; skipping freeze patch.', freeze_path)
        return False
    patched = src.replace(anchor, "source_file = open(py_filename, 'rb')", 1)
    try:
        with open(freeze_path, 'w') as fh:
            fh.write(patched)
        log.info('Patched pyqtdeploy freeze.py to read source as bytes: %s', freeze_path)
        return True
    except (IOError, OSError) as exc:
        log.warning('Could not write patched %s (%s).', freeze_path, exc)
        return False


def ensure_pyqtdeploy_headless(cfg):
    """ Step 3b: install the PyQt5 shim and patch pyqtdeploy so its command-line
    actions run without a host PyQt5.  Safe to call repeatedly.
    :param cfg: BuildConfig
    """
    step('Step 3b/15 -- Enabling head-less pyqtdeploy (PyQt5 shim)')
    if cfg.dry_run:
        log.info('[DRY-RUN] would write PyQt5 shim + patch pyqtdeploy __init__/freeze')
        return
    _write_pyqt5_shim(cfg)
    init_path = _locate_pyqtdeploy_init()
    if init_path:
        _patch_pyqtdeploy_gui_import(init_path)
        _patch_pyqtdeploy_freeze(init_path)
    else:
        log.warning('Installed pyqtdeploy package not located; the PyQt5 shim '
                    'should still satisfy the QtCore imports.')
    log.info('Head-less pyqtdeploy ready OK')


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
            # Try to find a tarball in downloads, or auto-download SIP 4.16.9.
            sip_tarballs = [
                f for f in listdir(cfg.downloads_dir)
                if f.lower().startswith('sip') and (f.endswith('.tar.gz') or f.endswith('.tgz'))]
            if not sip_tarballs and not cfg.dry_run:
                # --- patched: auto-download SIP 4.16.9 instead of warning user ---
                log.info('SIP tarball not found in downloads; fetching SIP %s ...', SIP_VERSION)
                sip_dest = join(cfg.downloads_dir, 'sip-{0}.tar.gz'.format(SIP_VERSION))
                _download_with_fallback([SIP_URL, SIP_URL_FALLBACK], sip_dest)
                sip_tarballs = [basename(sip_dest)]
            if sip_tarballs:
                archive = join(cfg.downloads_dir, sorted(sip_tarballs)[-1])
                if not cfg.dry_run:
                    _extract_tgz(archive, cfg.work_dir)
                sip_dirs2 = [d for d in listdir(cfg.work_dir) if d.lower().startswith('sip') and isdir(
                    join(cfg.work_dir, d))]
                if sip_dirs2:
                    cfg.sip_src = join(cfg.work_dir, sorted(sip_dirs2)[-1])
    log.info('SIP source: %s', cfg.sip_src or '(not found)')
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
            if not pyqt_tarballs and not cfg.dry_run:
                # --- patched: auto-download PyQt 5.3.2 ---
                log.info('PyQt5 tarball not found in downloads; fetching PyQt %s ...', PYQT5_VERSION)
                pyqt_dest = join(cfg.downloads_dir, 'PyQt-gpl-{0}.tar.gz'.format(PYQT5_VERSION))
                _download_with_fallback([PYQT5_URL, PYQT5_URL_FALLBACK], pyqt_dest)
                pyqt_tarballs = [basename(pyqt_dest)]
            if pyqt_tarballs:
                archive = join(cfg.downloads_dir, sorted(pyqt_tarballs)[-1])
                if not cfg.dry_run:
                    _extract_tgz(archive, cfg.work_dir)
                pyqt_dirs2 = [
                    d for d in listdir(cfg.work_dir) if d.lower().startswith('pyqt') and isdir(join(cfg.work_dir, d))]
                if pyqt_dirs2:
                    cfg.pyqt5_src = join(cfg.work_dir, sorted(pyqt_dirs2)[-1])
    log.info('PyQt5 source: %s', cfg.pyqt5_src or '(not found)')
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
    _run(_maybe_sudo([getMakeExecutable(), 'install']), cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
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
        with open(python_pro, 'a') as fh:
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

def _ensure_sip_header(cfg):
    """ Ensure the generated sip.h lives in the target sysroot include dir
    ($SYSROOT/include/pythonX.Y), which is where PyQt5's per-module Makefiles
    look for it via `#include <sip.h>`.  SIP's own qmake-driven 'make install'
    does not reliably place it there, so copy it explicitly.  Idempotent.
    :param cfg: BuildConfig
    """
    from shutil import copyfile
    majmin = '.'.join(PYTHON_VERSION.split('.')[:2])
    inc_dir = join(cfg.sysroot, 'include', 'python' + majmin)
    dst = join(inc_dir, 'sip.h')
    if isfile(dst):
        log.info('sip.h already present in sysroot: %s', dst)
        return
    # sip.h is generated by configure.py in the SIP source tree.
    candidates = [join(cfg.sip_src, 'siplib', 'sip.h'), join(cfg.sip_src, 'sip.h')]
    src = None
    for c in candidates:
        if isfile(c):
            src = c
            break
    if src is None:
        for root_dir, _dirs, files in walk(cfg.sip_src):
            if 'sip.h' in files:
                src = join(root_dir, 'sip.h')
                break
    if src is None:
        log.warning('Could not find a generated sip.h under %s; the PyQt5 build '
                    'may fail with "sip.h: No such file or directory".', cfg.sip_src)
        return
    _makedirs(inc_dir)
    copyfile(src, dst)
    log.info('Installed sip.h into sysroot: %s -> %s', src, dst)


def _ensure_sip_lib(cfg):
    """ Ensure the static SIP library (libsip.a) lives in the target sysroot
    site-packages dir, where the final pyqtdeploy-generated link looks for it
    via `-L$SYSROOT/lib/pythonX.Y/site-packages -lsip`.

    SIP's qmake-driven top-level 'make'/'make install' does not reliably build
    or install the static sip module library, so we (1) search the build tree
    and the sysroot for it, (2) build it directly inside siplib/ if it is
    missing, and (3) copy it into site-packages.  Idempotent.
    :param cfg: BuildConfig
    """
    from shutil import copyfile
    from glob import glob
    majmin = '.'.join(PYTHON_VERSION.split('.')[:2])
    sp_dir = join(cfg.sysroot, 'lib', 'python' + majmin, 'site-packages')
    dst = join(sp_dir, 'libsip.a')
    siplib_dir = join(cfg.sip_src, 'siplib') if cfg.sip_src else ''

    def _find_libsip():
        direct = [join(cfg.sip_src, 'siplib', 'libsip.a'),
                  join(cfg.sip_src, 'libsip.a')] if cfg.sip_src else []
        for c in direct:
            if isfile(c):
                return c
        for root in [r for r in (cfg.sip_src, cfg.sysroot) if r and isdir(r)]:
            for root_dir, _dirs, files in walk(root):
                if 'libsip.a' in files:
                    return join(root_dir, 'libsip.a')
        return None

    if isfile(dst):
        log.info('libsip.a already present in sysroot site-packages: %s', dst)
        return

    src = _find_libsip()

    # If the static sip module library was never compiled, build it now,
    # directly inside siplib/ (the top-level qmake build sometimes skips it).
    if src is None and siplib_dir and isdir(siplib_dir):
        env = cfg.build_env()
        try:
            if isfile(join(siplib_dir, 'Makefile')):
                log.info('libsip.a missing; running make in %s ...', siplib_dir)
                _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)],
                     cwd=siplib_dir, env=env, dry_run=cfg.dry_run)
            else:
                pros = glob(join(siplib_dir, '*.pro'))
                if pros:
                    log.info('libsip.a missing; running qmake+make in %s ...', siplib_dir)
                    _run([cfg.qmake, basename(pros[0])], cwd=siplib_dir, env=env,
                         dry_run=cfg.dry_run)
                    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)],
                         cwd=siplib_dir, env=env, dry_run=cfg.dry_run)
                else:
                    log.warning('No Makefile or .pro in %s to build libsip.a.', siplib_dir)
        except Exception as exc:
            log.warning('Attempt to build libsip.a in %s failed: %s', siplib_dir, exc)
        src = _find_libsip()

    if src is None:
        try:
            listing = ', '.join(sorted(listdir(siplib_dir))) if (siplib_dir and isdir(siplib_dir)) else '(no siplib dir)'
        except OSError:
            listing = '(unreadable)'
        log.warning('Could not find or build libsip.a.\n'
                    '  Searched (recursively): %s and %s\n'
                    '  siplib/ contents: %s\n'
                    '  The final link will fail with "cannot find -lsip".',
                    cfg.sip_src, cfg.sysroot, listing)
        return

    _makedirs(sp_dir)
    copyfile(src, dst)
    log.info('Installed libsip.a into sysroot site-packages: %s -> %s', src, dst)


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
    _run([getPythonExecutable(), 'configure.py', '--static', '--sysroot={0}'.format(cfg.sysroot), '--no-tools',
          '--use-qmake', '--configuration={0}'.format(cfg_file)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    # 3. qmake (Android qmake; ANDROID_NDK_ROOT must be set)
    log.info('Running qmake for SIP ...')
    _run([cfg.qmake], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    # 4. make
    log.info('Running make for SIP ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run, )
    # 5. make install
    log.info('Running make install for SIP ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    # 6. Make sure sip.h is where the PyQt5 build looks for it.  SIP's qmake
    #    'make install' does not reliably copy sip.h into the target sysroot
    #    include dir, and PyQt5's per-module Makefiles do `#include <sip.h>`
    #    with -I$SYSROOT/include/pythonX.Y.  Install it explicitly.
    if not cfg.dry_run:
        _ensure_sip_header(cfg)
        _ensure_sip_lib(cfg)
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
    keep_set = set(keep_modules)
    out_lines = []
    i = 0
    n = len(lines)
    edited_any = False
    while i < n:
        line = lines[i]
        # pyqtdeploy 0.5 emits the module list as a single space-separated
        # 'pyqt_modules = A B C' value (optionally continued on indented lines),
        # inside each [Qt x.y] section -- NOT as individual 'QtFoo =' lines.
        m = match(r'^(\s*)pyqt_modules\s*=(.*)$', line)
        if m:
            indent = m.group(1)
            tokens = m.group(2).split()
            j = i + 1
            # Gather indented continuation lines (more module names).
            while j < n:
                nxt = lines[j]
                if nxt.strip() == '':
                    break
                if match(r'^\s*\[', nxt) or match(r'^\s*\w+\s*=', nxt):
                    break
                if nxt[:1] in (' ', '\t'):
                    tokens += nxt.split()
                    j += 1
                else:
                    break
            kept = [t for t in tokens if t in keep_set]
            out_lines.append('{0}pyqt_modules = {1}\n'.format(indent, ' '.join(kept)))
            edited_any = True
            i = j
            continue
        out_lines.append(line)
        i += 1
    with open(cfg_file, 'w') as fh:
        fh.writelines(out_lines)
    if edited_any:
        log.info('Edited pyqt5-android.cfg: kept modules %s', keep_modules)
    else:
        log.warning('pyqt5-android.cfg: no pyqt_modules line found; left unmodified.')


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
        '--confirm-license',
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
        with open(cfg.pdy_file, 'r') as fh:
            content = fh.read()
        # Only reuse a project file that is already in pyqtdeploy's real format
        # (root element <Project ...>).  Older runs of this script wrote a
        # bogus <pyqtdeploy> template that Project.load() rejects outright, so
        # regenerate in that case.
        if '<Project' in content[:600] and 'version=' in content[:600] and '<Application' in content:
            log.info('Using existing .pdy: %s', cfg.pdy_file)
            return
        log.warning('Existing .pdy is not in pyqtdeploy 0.5 format; regenerating: %s',
                    cfg.pdy_file)
    _write_pdy_with_project_api(cfg)


def _scan_qrc_contents(root_dir, cfg, QrcFile, QrcDirectory):
    """ Recursively scan root_dir and return a list of QrcFile/QrcDirectory
    describing the application's source tree, skipping build/output/work dirs.
    :param root_dir: str
    :param cfg: BuildConfig
    :param QrcFile: type
    :param QrcDirectory: type
    :return: list
    """
    skip_dirs = {'__pycache__', '.git', '.hg', '.svn'}
    for p in (cfg.build_dir, getattr(cfg, 'output_dir', ''), cfg.work_dir):
        if p:
            skip_dirs.add(basename(p.rstrip('/\\')))
    pdy_base = basename(cfg.pdy_file)
    out = []
    try:
        names = sorted(listdir(root_dir))
    except OSError:
        return out
    for name in names:
        if name.startswith('.'):
            continue
        full = join(root_dir, name)
        if isdir(full):
            if name in skip_dirs:
                continue
            directory = QrcDirectory(name, True)
            directory.contents = _scan_qrc_contents(full, cfg, QrcFile, QrcDirectory)
            out.append(directory)
        else:
            if name == pdy_base or name.endswith(('.pyc', '.pyo')):
                continue
            out.append(QrcFile(name, True))
    return out


def _write_pdy_with_project_api(cfg):
    """ Write a schema-valid .pdy using pyqtdeploy's own Project class, so the
    file is guaranteed to be exactly what `pyqtdeploy build` can load.
    :param cfg: BuildConfig
    :return: None
    """
    # Make the head-less PyQt5 shim importable in *this* process too (Step 3b
    # already wrote it and patched the pyqtdeploy package).
    if getattr(cfg, 'pyqt5_shim_dir', '') and isdir(cfg.pyqt5_shim_dir):
        if cfg.pyqt5_shim_dir not in path:
            path.insert(0, cfg.pyqt5_shim_dir)
    try:
        from pyqtdeploy.project import Project, QrcPackage, QrcFile, QrcDirectory
    except Exception as exc:
        raise EnvironmentError(
            'Unable to import pyqtdeploy.project to generate the .pdy ({0}). '
            'Make sure Step 3b (head-less pyqtdeploy) ran.'.format(exc))

    majmin = '.'.join(PYTHON_VERSION.split('.')[:2])
    proj = Project()
    proj.application_is_pyqt5 = True
    proj.application_script = 'main.py'
    proj.sys_path = ''

    # Application package: the project directory, scanned for source files.
    app_pkg = QrcPackage()
    app_pkg.name = ''  # empty -> resolves to the .pdy's own directory.
    app_pkg.contents = _scan_qrc_contents(cfg.project_dir, cfg, QrcFile, QrcDirectory)
    proj.application_package = app_pkg

    proj.pyqt_modules = list(cfg.qt_modules)

    # The schema requires a SitePackages package and a Stdlib package to exist.
    site_pkg = QrcPackage()
    site_pkg.name = ''
    proj.packages = [site_pkg]
    stdlib_pkg = QrcPackage()
    stdlib_pkg.name = ''
    proj.stdlib_package = stdlib_pkg

    # Target Python locations (produced in the sysroot by the earlier steps).
    proj.python_host_interpreter = getPythonExecutable()
    proj.python_target_include_dir = join(cfg.sysroot, 'include', 'python' + majmin)
    # pyqtdeploy's python.pro builds TARGET = python<maj>.<min> -> libpython3.4.a
    # (no 'm' ABI suffix).  Probe the sysroot for whichever was actually built
    # so the generated main.pro links the correct -lpython... name.
    sysroot_lib = join(cfg.sysroot, 'lib')
    py_lib = None
    for cand in ('libpython{0}.a'.format(majmin), 'libpython{0}m.a'.format(majmin)):
        if isfile(join(sysroot_lib, cand)):
            py_lib = join(sysroot_lib, cand)
            break
    if py_lib is None:
        # Fall back to the no-'m' name (matches python.pro's TARGET).
        py_lib = join(sysroot_lib, 'libpython{0}.a'.format(majmin))
        log.warning('No static Python lib found in %s; defaulting target library '
                    'to %s', sysroot_lib, py_lib)
    proj.python_target_library = py_lib
    proj.python_target_stdlib_dir = join(cfg.sysroot, 'lib', 'python' + majmin)
    proj.build_dir = cfg.build_dir
    proj.qmake = cfg.qmake

    proj.save_as(cfg.pdy_file)
    log.info('Created schema-valid .pdy via pyqtdeploy: %s', cfg.pdy_file)
    log.info('  PyQt modules: %s', ', '.join(cfg.qt_modules))
    log.info('  target include dir: %s', proj.python_target_include_dir)
    log.info('  target library:     %s', proj.python_target_library)


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
    # pyqtdeploy 0.5's CLI takes the project via --project (NOT positionally)
    # and the output directory via --output; the action word ('build') is a
    # positional.  (v0.6 renamed the binary to pyqtdeploycli but keeps the same
    # option names.)  Correct form:  pyqtdeploy --project <app>.pdy --output <dir> build
    build_cmd = getattr(cfg, 'pyqtdeploycli', 'pyqtdeploycli')
    _makedirs(cfg.build_dir)
    _run([build_cmd, '--project', cfg.pdy_file, '--output', cfg.build_dir, '--verbose', 'build'],
         cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
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
    lines = (result.stdout or b'').decode('utf-8', errors='replace').splitlines()
    devices = [l for l in lines if l.strip() and 'List of devices' not in l]
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
    parser.add_argument('--app-name', default=None, metavar='NAME',
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
    parser.add_argument('--output-dir', default='', metavar='DIR',
                        help='Where to copy the final APK after a successful build (default: <project-dir>/output).')
    return parser


# =============================================================================
# APK locate + copy to output dir.
# =============================================================================

def _locate_and_copy_apk(cfg):
    """
    After a successful build, find any .apk under build_dir and copy it into
    cfg.output_dir under the friendly name <AppName>-debug.apk.
    Non-fatal on failure (build_with_qtcreator may have printed guidance for
    manual finishing in Qt Creator GUI).
    :param cfg: BuildConfig
    :return:
    """
    step('Locate + copy APK')
    candidates = []
    try:
        for root_dir, dirs, files in walk(cfg.build_dir):
            for name in files:
                if name.lower().endswith('.apk'):
                    candidates.append(join(root_dir, name))
    except OSError:
        pass
    # Also check project_dir as a fallback (some pipelines drop the APK there).
    try:
        for root_dir, dirs, files in walk(cfg.project_dir):
            for name in files:
                if name.lower().endswith('.apk') and join(root_dir, name) not in candidates:
                    candidates.append(join(root_dir, name))
    except OSError:
        pass
    if not candidates:
        log.warning('No .apk file found under %s -- nothing to copy.\n'
                    '  Check %s for build artifacts you can manually transfer.',
                    cfg.build_dir, cfg.build_dir)
        return
    # Prefer debug APKs / sort by mtime
    candidates.sort(key=lambda p: (0 if 'debug' in p.lower() else 1, -getmtime(p)))
    apk = candidates[0]
    sz = getsize(apk)
    _makedirs(cfg.output_dir)
    dest_name = '{0}-debug.apk'.format(cfg.app_name)
    dest = join(cfg.output_dir, dest_name)
    log.info('Source APK:  %s (%d bytes)', apk, sz)
    log.info('Copying to:  %s', dest)
    try:
        from shutil import copy2
        copy2(apk, dest)
    except (IOError, OSError) as exc:
        log.error('APK copy failed: %s', exc)
        return
    log.info('APK copied OK: %s', dest)
    if len(candidates) > 1:
        log.info('(%d additional APK candidate(s) were ignored)', len(candidates) - 1)


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
        ensure_pyqtdeploy_headless(cfg)
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
        _locate_and_copy_apk(cfg)
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
