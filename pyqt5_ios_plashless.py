#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PyQt5 5.3.1  iOS Builder  (pyqtdeploy 0.5 / 0.6 -- plashless trilogy)
=======================================================================
Automates the complete pipeline documented in the plashless three-part
macOS / iOS cross-compilation series:

  Part 1: https://plashless.wordpress.com/2014/09/10/
          using-pyqtdeploy-on-macos-to-cross-compile-a-pyqt-app-for-ios-part-1/
  Part 2: https://plashless.wordpress.com/2014/09/14/
          using-pyqtdeploy-on-macos-to-cross-compile-a-pyqt-app-for-ios-part-2/
  Part 3: https://plashless.wordpress.com/2014/09/19/
          using-pyqtdeploy-on-macos-to-cross-compile-a-pyqt-app-for-ios-part-3/

Pinned versions (from plashless blog):
--------------------------------------
  Host OS      macOS 10.9.4 Mavericks (or later)
  Xcode        5.1.1 (or later)
  Qt           5.3.0 / 5.3.1  (with iOS static libraries)
  Qt Creator   3.1.1 (or later)
  Python       3.4.0  (cross-compiled into iOS target)
  PyQt5        5.3.1  (GPL snapshot -- latest recommended)
  SIP          4.16.1 (latest snapshot -- must match PyQt5)
  pyqtdeploy   0.5 / 0.6  (installed from Mercurial)
  Target ABI   ios-64  (ARM64 real device)

Pipeline (14 steps):
-------------------
  1.  Preflight         -- macOS host, Xcode, tools, disk space
  2.  Directory layout  -- ~/ios/iRoot, ~/ios/Downloads, ~/ios/pensoolBuild
  3.  Environment       -- PYTHONPATH, SYSROOT, PATH setup + env.sh
  4.  pyqtdeploy        -- hg clone + make VERSION + setup.py install
  5.  Host SIP          -- build SIP for the macOS host (same version as target)
  6.  Host PyQt5        -- rebuild PyQt5 host dynamic libs after Qt update
  7.  Sources           -- locate/download Python 3.4.0, SIP, PyQt5 tarballs
  8.  Python static     -- pyqtdeploycli + qmake + make + make install (ios-64)
  9.  SIP static        -- pyqtdeploycli + configure.py + make + make install
  10. PyQt5 cfg patch   -- remove unused Qt modules from pyqt5-ios.cfg
  11. QML sip patch     -- patch qgraphicsvideoitem.sip if QML is used
  12. PyQt5 static      -- configure.py + qmake PyQt5.pro + make + make install
  13. pyqtdeploy build  -- create/validate .pdy + pyqtdeploycli build
  14. Xcode             -- qmake (creates .xcodeproj) + open in Xcode guidance

Usage:
-----
    python pyqt5_ios_plashless.py --project-dir /path/to/myapp [OPTIONS]
    # Full automated build (real device):
    python pyqt5_ios_plashless.py --project-dir ~/ios/pensoolBuild --qt-dir      ~/Qt/5.3 --python-home ~/python
    # Skip static builds (iRoot already populated):
    python pyqt5_ios_plashless.py --project-dir ~/ios/pensoolBuild --sysroot     ~/ios/iRoot --skip-static
    # Dry-run to see all commands:
    python pyqt5_ios_plashless.py --project-dir ~/ios/pensoolBuild --dry-run --verbose
"""
from os.path import join, exists, dirname, basename, isfile, isdir, pathsep, expanduser, realpath
from argparse import RawDescriptionHelpFormatter, ArgumentParser
from os import statvfs, listdir, walk, environ, makedirs, chmod
from logging import getLogger, DEBUG, basicConfig, INFO
from re import match, compile, DOTALL
from platform import mac_ver, system
from subprocess import Popen, PIPE
from sys import exit, version_info, path
from textwrap import dedent
from errno import EEXIST
import tarfile
import io

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import getMakeExecutable, getPythonExecutable, getOpenExecutable, getHgExecutable, which
except:
    from builders import getMakeExecutable, getPythonExecutable, getOpenExecutable, getHgExecutable, which

try:
    from urllib.request import urlretrieve
except:
    from urllib import urlretrieve

# ---------------------------------------------------------------------------
# Python version detection.
# ---------------------------------------------------------------------------
PY3 = version_info[0] == 3
# =============================================================================
# Constants  (pinned to plashless guide versions).
# =============================================================================
PYTHON_VERSION = '3.4.0'
QT_VERSION = '5.3'  # Qt 5.3.x
PYQT5_VERSION = '5.3.1'  # PyQt5 GPL snapshot.
SIP_VERSION = '4.16.1'  # SIP snapshot.
TARGET = 'ios-64'  # pyqtdeploy 0.5/0.6 target string.
QT_IOS_SUBDIR = 'ios'  # ~/Qt/5.3/ios/
# Mercurial repository for pyqtdeploy (plashless: use hg, NOT pip3).
PYQTDEPLOY_HG = 'http://www.riverbankcomputing.com/hg/pyqtdeploy'
PYTHON_URL = 'https://www.python.org/ftp/python/{ver}/Python-{ver}.tgz'.format(ver=PYTHON_VERSION)
# Default directories (from plashless Part 2 directory layout).
HOME = expanduser('~')
DEFAULT_IOS = join(HOME, "ios")
DEFAULT_IROOT = join(DEFAULT_IOS, 'iRoot')  # SYSROOT
DEFAULT_DLDIR = join(DEFAULT_IOS, 'Downloads')
DEFAULT_QTDIR = join(HOME, 'Qt', QT_VERSION)  # ~/Qt/5.3
DEFAULT_PYDIR = join(HOME, 'python')  # ~/python (host)
DEFAULT_WORK = join(HOME, ".pyqt5_ios_plashless")
# Qt modules to keep (plashless: remove extras to avoid link errors)
# Part 3: "No QPrintSupport on iOS" -- remove it!
DEFAULT_QT_MODULES = [
    'QtCore', 'QtGui', 'QtWidgets', 'QtSvg', 'QtNetwork',
    # NOT QtPrintSupport: Part 3: "If you import PyQt5.QtPrintSupport, you get an error at link time" on iOS.
]
MIN_DISK_MB = 8000
# =============================================================================
# Logging
# =============================================================================
LOG_FORMAT = '%(asctime)s  %(levelname)-8s  %(message)s'
basicConfig(format=LOG_FORMAT, datefmt='%H:%M:%S', level=INFO)
log = getLogger('pyqt5-ios-plashless')


def step(title):
    """
    Print a visually distinct step header.
    :param title: str
    :return:
    """
    bar = '=' * 66  # type: str
    log.info('\n%s\n  %s\n%s', bar, title, bar)


# =============================================================================
# Build configuration.
# =============================================================================

class BuildConfig(object):
    """
    All resolved paths and settings for one build run.
    """

    def __init__(self, args):
        """
        :param args: any
        """
        self.project_dir = realpath(args.project_dir)
        self.app_name = args.app_name or basename(self.project_dir)
        self.qt_dir = expanduser(args.qt_dir)
        self.python_home = expanduser(args.python_home)  # ~/python host
        self.sysroot = expanduser(args.sysroot)  # ~/ios/iRoot
        self.downloads_dir = expanduser(args.downloads_dir)
        self.work_dir = expanduser(args.work_dir)
        # Optional pre-extracted source paths.
        self.python_src = expanduser(args.python_src) if args.python_src else ''
        self.sip_src = expanduser(args.sip_src) if args.sip_src else ''
        self.pyqt5_src = expanduser(args.pyqt5_src) if args.pyqt5_src else ''
        self.pyqtdeploy_src = expanduser(args.pyqtdeploy_src) if args.pyqtdeploy_src else ''
        # Build control.
        self.skip_static = args.skip_static
        self.use_qml = args.use_qml
        self.simulator = args.simulator
        self.jobs = args.jobs
        self.verbose = args.verbose
        self.dry_run = args.dry_run
        self.keep_build = args.keep_build
        # Qt module list (strip QtPrintSupport per Part 3 FAQ).
        self.qt_modules = list(DEFAULT_QT_MODULES)
        if args.extra_qt_modules:
            extras = [m.strip() for m in args.extra_qt_modules.split(",") if m.strip()]
            self.qt_modules.extend(extras)
        # pyqtdeploycli command name (detected in step 4).
        self.pyqtdeploycli = 'pyqtdeploycli'
        # Build subdirectory (mirroring plashless layout).
        self.build_dir = join(self.project_dir, 'build')

    # ------------------------------------------------------------------ #
    # Derived paths                                                        #
    # ------------------------------------------------------------------ #

    @property
    def qt_ios_dir(self):
        """
        ~/Qt/5.3/ios
        :return: str
        """
        return join(self.qt_dir, QT_IOS_SUBDIR)

    @property
    def ios_qmake(self):
        """
        ~/Qt/5.3/ios/bin/qmake
        :return: str
        """
        return join(self.qt_ios_dir, 'bin', 'qmake')

    @property
    def qt_clang_dir(self):
        """
        ~/Qt/5.3/clang_64  (macOS host build).
        :return: str
        """
        for sub in ('clang_64', 'clang-64', 'macos', 'osx'):
            d = join(self.qt_dir, sub)
            if isdir(d):
                return d
        return join(self.qt_dir, 'clang_64')

    @property
    def host_qmake(self):
        """
        ~/Qt/5.3/clang_64/bin/qmake  (for host PyQt5 build).
        :return: str
        """
        return join(self.qt_clang_dir, 'bin', 'qmake')

    @property
    def host_sip(self):
        """
        ~/python/bin/sip
        :return: str
        """
        return join(self.python_home, 'bin', 'sip')

    @property
    def pdy_file(self):
        """
        :return: str
        """
        return join(self.project_dir, '{}.pdy'.format(self.app_name))

    @property
    def xcodeproj(self):
        """
        Expected path of the generated Xcode project.
        :return: str
        """
        for root, dirs, files in walk(self.build_dir):
            for d in dirs:
                if d.endswith('.xcodeproj'):
                    return join(root, d)
        return join(self.build_dir, '{}.xcodeproj'.format(self.app_name))

    def build_env(self):
        """
        Return the environment dict needed for cross-compilation.
        From plashless Part 2:
          export PYTHONPATH=/Users/bootch/python/lib/python3.4/site-packages
          SYSROOT passed explicitly to most commands (not as env var here)
        :return: str
        """
        env = dict(environ)
        # PYTHONPATH: host PyQt5 site-packages so pyqtdeploy can import PyQt5.
        pypath = join(self.python_home, 'lib', 'python3.4', 'site-packages')
        existing = env.get('PYTHONPATH', '')
        if pypath not in existing:
            env['PYTHONPATH'] = pypath + (pathsep + existing if existing else '')
        # Ensure host sip is on PATH.
        env['PATH'] = join(self.python_home, 'bin') + pathsep + env.get('PATH', '')
        return env


# =============================================================================
# subprocess helpers.
# =============================================================================

def _run(cmd, cwd=None, env=None, check=True, dry_run=False):
    """
    Execute cmd (list of str/unicode).
    :param cmd: str
    :param cwd: str
    :param env: dict[str, str]
    :param check: bool
    :param dry_run: bool
    :return: int
    """
    display = ' '.join(str(c) for c in cmd)
    log.debug('$ %s  [cwd=%s]', display, cwd or '.')
    if dry_run:
        log.info('[DRY-RUN] %s', display)
        return 0
    merged = dict(environ)
    if env:
        merged.update(env)
    proc = Popen([str(c) for c in cmd], cwd=cwd, env=merged)
    proc.communicate()
    if check and proc.returncode != 0:
        log.error('Command failed (exit %d):\n  %s', proc.returncode, display)
        raise RuntimeError('Subprocess exited with code {0}'.format(proc.returncode))
    return proc.returncode


def _run_capture(cmd, cwd=None, env=None):
    """
    Run cmd and return combined stdout+stderr as a str.
    :param cmd: str
    :param cwd: str | None
    :param env: dict[str, str] | None
    :return: str
    """
    merged = dict(environ)
    if env:
        merged.update(env)
    proc = Popen([str(c) for c in cmd], stdout=PIPE, stderr=PIPE, cwd=cwd, env=merged)
    out, err = proc.communicate()
    combined = (out or b'') + (err or b'')
    return combined.decode('utf-8', errors='replace') if PY3 else combined.decode('utf-8', 'replace')


def _require_tool(name):
    """
    Assert *name* is on PATH and return its full path.
    :param name: str
    :return: str
    """
    pth = which(name)
    if not pth:
        raise EnvironmentError('Required tool "{}" not found on PATH.'.format(name))
    return pth


def _makedirs(pth):
    """
    Create directory tree.  Equivalent to os.makedirs(path, exist_ok=True)
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
    Download *url* → *dest*.
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
    except Exception as exc:
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


def _read_file(pth):
    """
    Read a text file.
    :param pth: str
    :return: str
    """
    with io.open(pth, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _write_file(pth, content):
    """
    Write unicode content to a text file.
    :param pth: str
    :param content: str
    :return:
    """
    with io.open(pth, 'w', encoding='utf-8') as fh:
        fh.write(content)


def _patch_file(pth, old_str, new_str, description):
    """
    In-place string substitution.  Returns True if the patch was applied.
    :param pth: str
    :param old_str: str
    :param new_str: str
    :param description: str
    :return: bool
    """
    if not isfile(pth):
        return False
    content = _read_file(pth)
    if old_str not in content:
        log.debug('Patch "%s": pattern not found (already applied?).', description)
        return False
    _write_file(pth, content.replace(old_str, new_str))
    log.info('Patched %-34s  [%s]', basename(pth), description)
    return True


def _check_disk(pth, required_mb=MIN_DISK_MB):
    """
    Warn if less than required_mb MB are free at path.
    :param pth: str
    :param required_mb: int
    :return: None
    """
    stat = statvfs(pth if exists(pth) else dirname(pth))
    free_mb = (stat.f_bavail * stat.f_frsize) // (1024 * 1024)
    if free_mb < required_mb:
        log.warning('Low disk space: %d MB free at %s (recommended >= %d MB).', free_mb, pth, required_mb)
    else:
        log.info('Disk: %d MB free OK', free_mb)


# =============================================================================
# Step 1 -- Preflight checks.
# =============================================================================

def preflight(cfg):
    """
    From plashless Part 1:
      Requirements: macOS 10.9.4+, Xcode 5.1.1+, Qt Creator 3.1.1+
    From plashless Part 2:
      "My advice is to download the entire Xcode (rather than just the
       command line tools)."
      "Use the online installer to install Qt … it will know your platform
       and offer you choices to download the proper kits for iOS."
    :param cfg: BuildConfig
    :return:
    """
    step('Step 1/14 -- Preflight checks')
    if system() != 'Darwin':
        raise EnvironmentError('iOS cross-compilation requires macOS (Darwin). Detected: {0}'.format(system()))
    macVer = mac_ver()[0]
    log.info('macOS version: %s', macVer)
    try:
        major, minor = int(macVer.split(".")[0]), int(macVer.split(".")[1])
        if (major, minor) < (10, 9):
            log.warning('macOS 10.9 (Mavericks) or later is recommended. Detected: %s', macVer)
    except (ValueError, IndexError):
        pass
    # Python version of this script.
    log.info('Script Python: %d.%d', version_info[0], version_info[1])
    # Required system tools.
    required = {
        'xcodebuild': 'Install Xcode from the Mac App Store',
        'xcrun': 'Install Xcode Command Line Tools: xcode-select --install',
        'hg': 'brew install mercurial',
        'make': 'Install Xcode Command Line Tools',
        'python3': 'Download Python 3.4 from python.org',
        'git': 'brew install git',
        'tar': 'Should be pre-installed on macOS'}
    for tool, hint in sorted(required.items()):
        pth = which(tool)
        if pth:
            log.info('Found: %-14s  %s', tool, pth)
        else:
            raise EnvironmentError("'{0}' not found on PATH.\n  Fix: {1}".format(tool, hint))
    # Xcode version.
    try:
        log.info('Xcode: %s', _run_capture(['xcodebuild', '-version']).splitlines()[0])
    except:
        pass
    # Qt 5.3 iOS qmake.
    if not isfile(cfg.ios_qmake):
        raise EnvironmentError(
            'Qt iOS qmake not found: {0}\n\n'
            'From plashless Part 1:\n'
            '  Use the Qt online installer (https://www.qt.io/download)\n'
            '  When installing Qt {1}, check the "iOS" component.\n'
            '  This installs static iOS frameworks to ~/Qt/{1}/ios/\n'
            '  and the iOS Simulator kit for Qt Creator.\n\n'
            '  If you have Qt but no iOS component:\n'
            '    Open Qt Maintenance Tool --> Package Manager\n'
            '    Add: Qt {1} --> Qt 5.3.x for iOS\n\n'
            'Re-run with --qt-dir ~/Qt/{1}'.format(cfg.ios_qmake, QT_VERSION))
    ver_out = _run_capture([cfg.ios_qmake, '--version'])
    log.info('iOS qmake: %s', ver_out.splitlines()[0] if ver_out else "OK")
    # Project directory.
    if not isdir(cfg.project_dir):
        _makedirs(cfg.project_dir)
        log.info('Created project dir: %s', cfg.project_dir)
    _check_disk(cfg.work_dir if exists(cfg.work_dir) else HOME)
    log.info('Preflight passed OK')


# =============================================================================
# Step 2 -- Directory layout  (from plashless Part 2).
# =============================================================================

def setup_directories(cfg):
    """
    From plashless Part 2:
      ios/
        iRoot/           (SYSROOT -- stand-in for iOS device root)
          python/        (installed by Python static build)
        Downloads/
          Python-3.4.0/
          PyQt-gpl-5.3.1/
          sip-4.16.1/
        pensoolBuild/
          pensool.pdy
          build/
            pensool.pro
    From plashless: "iRoot is a stand-in for the root of the target device.
    Many build objects are placed (installed) here during the cross build."
    :param cfg: BuildConfig
    :return:
    """
    step('Step 2/14 -- Directory layout')
    for d in [
        cfg.sysroot, join(cfg.sysroot, 'python'), cfg.downloads_dir, cfg.project_dir, cfg.build_dir, cfg.work_dir]:
        _makedirs(d)
        log.info('Dir: %s', d)
    log.info(
        '\nDirectory layout (from plashless Part 2):\n'
        '  SYSROOT      : %s\n'
        '  Downloads    : %s\n'
        '  Project      : %s\n'
        '  Build        : %s\n'
        '  Qt iOS       : %s\n'
        '  Python home  : %s',
        cfg.sysroot, cfg.downloads_dir, cfg.project_dir, cfg.build_dir, cfg.qt_ios_dir, cfg.python_home)
    log.info('Directory layout ready OK')


# =============================================================================
# Step 3 -- Environment variables.
# =============================================================================

def setup_environment(cfg):
    """
    From plashless Part 2:
      export PYTHONPATH=/Users/bootch/python/lib/python3.4/site-packages
      "SYSROOT tells many commands where to install the built products, in this case to ~/ios/iRoot"
      "sip is a command that needs to be on the PATH."
    :param cfg: BuildConfig
    :return:
    """
    step('Step 3/14 -- Environment variables')
    env = cfg.build_env()
    log.info(
        'Key environment:\n'
        '  PYTHONPATH : %s\n'
        '  SYSROOT    : %s  (passed to commands explicitly)\n'
        '  host sip   : %s\n'
        '  iOS qmake  : %s',
        env.get('PYTHONPATH', '(not set)'), cfg.sysroot, cfg.host_sip, cfg.ios_qmake)
    # Write env.sh for manual reference
    env_sh = join(cfg.work_dir, 'env.sh')
    lines = [
        "#!/bin/sh",
        "# Generated by pyqt5_ios_plashless.py",
        "# Source with: source {0}".format(env_sh),
        "",
        "export PYTHONPATH='{0}'".format(env.get('PYTHONPATH', '')),
        "export SYSROOT='{0}'".format(cfg.sysroot),
        "export PATH='{0}'".format(env.get('PATH', '')),
        "",
        "# iOS qmake",
        "export QT_IOS_QMAKE='{0}'".format(cfg.ios_qmake)]
    _write_file(env_sh, '\n'.join(lines) + '\n')
    chmod(env_sh, 0o755)
    log.info('env.sh written: %s', env_sh)


# =============================================================================
# Step 4 -- Install pyqtdeploy from Mercurial.
# =============================================================================

def install_pyqtdeploy(cfg):
    """
    From plashless Part 2 / Notes on pyqtdeploy:
      "You always want to use the latest version, possibly even the unstable version."
      "See about installing and upgrading pyqtdeploy."
      hg clone http://www.riverbankcomputing.com/hg/pyqtdeploy
      cd pyqtdeploy
      make VERSION
      make pyqtdeploy/version.py
      sudo python3 setup.py install
    Note: In v0.6, 'pyqtdeploy' GUI command was joined by 'pyqtdeploycli'
    for command-line use.  We detect which is available.
    From plashless: "pyqtdeploy now uses a separate command pyqtdeploycli for use on a command line"
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 4/14 -- Installing / updating pyqtdeploy from Mercurial')
    for cmd_name in ('pyqtdeploycli', 'pyqtdeploy'):
        if which(cmd_name):
            cfg.pyqtdeploycli = cmd_name
            log.info('pyqtdeploy already installed as "%s" OK', cmd_name)
            return
    # Clone or update.
    if cfg.pyqtdeploy_src and isdir(cfg.pyqtdeploy_src):
        src_dir = cfg.pyqtdeploy_src
    else:
        src_dir = join(cfg.work_dir, 'pyqtdeploy')
        if not isdir(src_dir):
            log.info('Cloning pyqtdeploy from Mercurial ...')
            _run([getHgExecutable(), 'clone', PYQTDEPLOY_HG, src_dir], dry_run=cfg.dry_run)
        else:
            log.info('Updating existing pyqtdeploy clone ...')
            _run([getHgExecutable(), 'pull'], cwd=src_dir, dry_run=cfg.dry_run)
            _run([getHgExecutable(), 'update'], cwd=src_dir, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'VERSION'], cwd=src_dir, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'pyqtdeploy/version.py'], cwd=src_dir, dry_run=cfg.dry_run)
    _run(['sudo', getPythonExecutable(), 'setup.py', 'install'], cwd=src_dir, dry_run=cfg.dry_run)
    for cmd_name in ('pyqtdeploycli', 'pyqtdeploy'):
        if which(cmd_name):
            cfg.pyqtdeploycli = cmd_name
            log.info('Installed as "%s" OK', cmd_name)
            return
    cfg.pyqtdeploycli = 'pyqtdeploycli'
    log.warning('pyqtdeploycli/pyqtdeploy not found on PATH after install.')


# =============================================================================
# Step 5 -- Build host SIP (macOS, same version as iOS target).
# =============================================================================

def build_host_sip(cfg):
    """
    From plashless Part 2:
      "You should also have built SIP for the host."
      cd ~/ios/Downloads/sip*
      python3 configure.py
      make
      make install
    "Again, you need the same version of sip installed on your host as
     you intend to run on the target (iOS)."
    "sip is a command that needs to be on PATH."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 5/14 -- Building host SIP (macOS)')
    if cfg.skip_static:
        log.info('--skip-static set; skipping host SIP build.')
        return
    sip_dir = _find_source_dir(cfg.downloads_dir, "sip", cfg.sip_src)
    if not sip_dir:
        log.warning(
            'SIP source not found. Download from:\n'
            '  https://www.riverbankcomputing.com/software/sip/download\n'
            'Save to: %s\nThen re-run.', cfg.downloads_dir)
        return
    env = cfg.build_env()
    log.info('Configuring host SIP in %s ...', sip_dir)
    _run([getPythonExecutable(), 'configure.py'], cwd=sip_dir, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=sip_dir, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'install'], cwd=sip_dir, env=env, dry_run=cfg.dry_run)
    cfg.sip_src = sip_dir
    log.info('Host SIP built OK')


# =============================================================================
# Step 6 -- Rebuild host PyQt5 dynamic libraries (after Qt update).
# =============================================================================

def build_host_pyqt5(cfg):
    """
    From plashless Part 2:
      "To run pyqtdeploy requires Python3 and PyQt dynamic libraries on
       the host (regardless of target)."
      "If there is a new version of Qt or PyQt, you should rebuild SIP
       and PyQt (the dynamic libraries)."
      cd ~/Downloads/sip-4.16.5
      python3 configure.py
      make && make install
      cd ~/Downloads/PyQt-gpl-5.4
      python3 configure.py --qmake ~/Qt/5.4/clang-64/bin/qmake --sip ~/python/bin/sip
      make -j4 && make install
    NOTE: This is the HOST build (macOS dynamic libs) so pyqtdeploy itself
    can run.  The iOS static build happens in Steps 8-12.
    From plashless Part 2 (QML patch note):
      "patch sip/QtMultimediaWidgets/qgraphicsvideoitem.sip if you use QML.
       Delete the code %ConvertToSubClassCode...%End."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 6/14 -- Rebuilding host PyQt5 dynamic libs (macOS)')
    if cfg.skip_static:
        log.info('--skip-static set; skipping host PyQt5 rebuild.')
        return
    pyqt5_dir = _find_source_dir(cfg.downloads_dir, "pyqt", cfg.pyqt5_src)
    if not pyqt5_dir:
        log.warning(
            'PyQt5 source not found for host build.\nDownload from:\n'
            '  https://www.riverbankcomputing.com/software/pyqt/download5\nSave to: %s', cfg.downloads_dir)
        return
    env = cfg.build_env()
    # QML patch (Part 2: patch qgraphicsvideoitem.sip before configure).
    if cfg.use_qml:
        _apply_qml_sip_patch(pyqt5_dir)
    if not isfile(cfg.host_qmake):
        log.warning(
            'Host qmake not found: %s\n'
            'Skipping host PyQt5 rebuild.  If pyqtdeploy cannot find Qt modules, rebuild host PyQt5 manually.',
            cfg.host_qmake)
        return
    host_sip_arg = '--sip={0}'.format(cfg.host_sip) if isfile(cfg.host_sip) else ''
    configure_args = [getPythonExecutable(), 'configure.py', '--qmake={0}'.format(cfg.host_qmake)]
    if host_sip_arg:
        configure_args.append(host_sip_arg)
    log.info('Configuring host PyQt5 in %s ...', pyqt5_dir)
    _run(configure_args, cwd=pyqt5_dir, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=pyqt5_dir, env=env, dry_run=cfg.dry_run)
    _run([getMakeExecutable(), 'install'], cwd=pyqt5_dir, env=env, dry_run=cfg.dry_run)
    cfg.pyqt5_src = pyqt5_dir
    log.info('Host PyQt5 rebuilt OK')


# =============================================================================
# Step 7 -- Locate / download source tarballs
# =============================================================================

def locate_sources(cfg):
    """
    From plashless Part 2:
      "Download Python, PyQt, and SIP. Download the 'Gzipped source tarball'
       for Python."
      "For safety you might want to download fresh copies."
    Python 3.4.0 is auto-downloaded.  SIP and PyQt5 must be the LATEST
    SNAPSHOT (not stable GPL) to avoid QTBUG-39300.
    :param cfg: BuildConfig
    :return:
    """
    step('Step 7/14 -- Locating source tarballs')
    _makedirs(cfg.downloads_dir)
    # Python 3.4.0 -- auto-download.
    py_archive = join(cfg.downloads_dir, 'Python-3.4.0.tgz')
    if not cfg.dry_run:
        _download(PYTHON_URL, py_archive)
        extracted = join(cfg.downloads_dir, 'Python-3.4.0')
        if not isdir(extracted):
            _extract_tgz(py_archive, cfg.downloads_dir)
        cfg.python_src = extracted
    log.info('Python 3.4.0 source: %s', cfg.python_src or py_archive)
    # SIP snapshot -- must be in downloads_dir manually.
    sip_dir = _find_source_dir(cfg.downloads_dir, "sip", cfg.sip_src)
    if sip_dir:
        cfg.sip_src = sip_dir
    else:
        log.warning(
            'SIP snapshot not found in %s.\n'
            '  Download the LATEST SIP snapshot from:\n'
            '    https://www.riverbankcomputing.com/software/sip/download\n'
            '  Extract it to: %s',
            cfg.downloads_dir, cfg.downloads_dir)
    # PyQt5 snapshot -- must be in downloads_dir manually.
    pyqt_dir = _find_source_dir(cfg.downloads_dir, "pyqt", cfg.pyqt5_src)
    if pyqt_dir:
        cfg.pyqt5_src = pyqt_dir
    else:
        log.warning(
            'PyQt5 snapshot not found in %s.\n'
            '  Download the LATEST PyQt5 GPL snapshot from:\n'
            '    https://www.riverbankcomputing.com/software/pyqt/download5\n'
            '  Extract it to: %s', cfg.downloads_dir, cfg.downloads_dir)
    log.info('Sources: Python=%s  SIP=%s  PyQt5=%s',
             basename(cfg.python_src) if cfg.python_src else 'MISSING',
             basename(cfg.sip_src) if cfg.sip_src else 'MISSING',
             basename(cfg.pyqt5_src) if cfg.pyqt5_src else 'MISSING')


def _find_source_dir(base_dir, prefix, provided=''):
    """
    Return the first directory under base_dir whose name starts with prefix
    (case-insensitive).  Returns provided if it is already a valid directory.
    :param base_dir: str
    :param prefix: str
    :param provided: str
    :return: str
    """
    if provided and isdir(provided):
        return provided
    if not isdir(base_dir):
        return ''
    matches = sorted(
        [d for d in listdir(base_dir) if d.lower().startswith(prefix.lower()) and isdir(join(base_dir, d))])
    return join(base_dir, matches[-1]) if matches else ''


# =============================================================================
# Step 8 -- Build Python 3.4.0 statically for ios-64.
# =============================================================================

def build_python_static(cfg):
    """
    From plashless Part 2:
      cd ~/ios/Downloads/Python-3.4.0
      pyqtdeploycli --package python --target ios-64 configure
      ~/Qt/5.3/ios/bin/qmake sysroot=/Users/bootch/ios/iRoot
      make
      make install
      "The result is a new directory ~/ios/iRoot/python populated with a
       library of the Python interpreter."
    NOTE: sysroot is passed as a qmake argument (lowercase), not SYSROOT env.
    NOTE: Do NOT re-run pyqtdeploycli configure after patching (it overwrites).
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 8/14 -- Building Python 3.4.0 statically for ios-64')
    if cfg.skip_static:
        log.info('--skip-static set; skipping static Python build.')
        return
    if not cfg.python_src or not isdir(cfg.python_src):
        log.warning('Python source not found; skipping Step 8.')
        return
    # Warn if already patched (second run would fail).
    originals = []
    for root, dirs, files in walk(cfg.python_src):
        for f in files:
            if f.endswith('.original'):
                originals.append(f)
    if originals:
        log.warning(
            'Python source appears already patched (%d .original files).\n'
            'If pyqtdeploycli configure fails, delete and re-extract:\n  rm -rf %s && tar xzf %s -C %s',
            len(originals), cfg.python_src, join(cfg.downloads_dir, 'Python-3.4.0.tgz'), cfg.downloads_dir)
    env = cfg.build_env()
    log.info('pyqtdeploycli configure for Python (ios-64) ...')
    _run([cfg.pyqtdeploycli, '--package', 'python', '--target', TARGET, 'configure'], cwd=cfg.python_src, env=env,
         dry_run=cfg.dry_run)
    log.info('qmake for Python (sysroot=%s) ...', cfg.sysroot)
    _run([cfg.ios_qmake, 'sysroot={0}'.format(cfg.sysroot)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('make for Python (-j%d) ...', cfg.jobs)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('make install for Python ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.python_src, env=env, dry_run=cfg.dry_run)
    log.info('Static Python built OK  (installed to %s/python)', cfg.sysroot)


# =============================================================================
# Step 9 -- Build SIP statically for ios-64
# =============================================================================

def build_sip_static(cfg):
    """
    From plashless Part 2:
      cd ~/ios/Downloads/sip*
      pyqtdeploycli --package sip --target ios-64 configure
      python3 configure.py --static --sysroot=/Users/bootch/ios/iRoot \\
          --no-tools --configuration=sip-ios.cfg
      make
      make install
    Note from plashless: "(Edited: this not needed:
      ~/Qt/5.4/ios/bin/qmake sysroot=/Users/bootch/ios/iRoot)"
    -- so we do NOT run qmake separately for SIP on iOS (unlike Android).
    Note from plashless FAQ:
      "If you get 'You need a working sip on your PATH', pass
       --sip=/Users/bootch/python/bin/sip to the configure step."
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 9/14 -- Building SIP statically for ios-64')
    if cfg.skip_static:
        log.info('--skip-static set; skipping static SIP build.')
        return
    if not cfg.sip_src or not isdir(cfg.sip_src):
        log.warning('SIP source not found; skipping Step 9.')
        return
    env = cfg.build_env()
    log.info('pyqtdeploycli configure for SIP (ios-64) ...')
    _run([cfg.pyqtdeploycli, '--package', 'sip', '--target', TARGET, 'configure'], cwd=cfg.sip_src, env=env,
         dry_run=cfg.dry_run)
    cfg_file = join(cfg.sip_src, 'sip-ios.cfg')
    configure_args = [getPythonExecutable(), 'configure.py', '--static', '--sysroot={0}'.format(cfg.sysroot),
                      '--no-tools', '--configuration={0}'.format(cfg_file)]
    # Part 2 FAQ: pass --sip if sip is not on PATH.
    if isfile(cfg.host_sip):
        configure_args.append('--sip={0}'.format(cfg.host_sip))
    log.info('SIP configure.py ...')
    _run(configure_args, cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('make for SIP ...')
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('make install for SIP ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.sip_src, env=env, dry_run=cfg.dry_run)
    log.info('Static SIP built OK')


# =============================================================================
# Step 10 -- Patch pyqt5-ios.cfg (remove unused Qt modules)
# =============================================================================

def patch_pyqt5_ios_cfg(cfg):
    """
    From plashless Part 2:
      "Note that you may need an intermediate step, to edit pyqt5-ios.cfg
       to remove extra Qt modules that don't compile and that you don't need."
    Also from Part 3 (important!):
      "No QPrintSupport module of Qt.  If you import PyQt5.QtPrintSupport,
       you get an error at link time."
    We remove all Qt module lines NOT in cfg.qt_modules.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 10/14 -- Patching pyqt5-ios.cfg (module pruning)')
    if cfg.skip_static or not cfg.pyqt5_src or not isdir(cfg.pyqt5_src):
        log.info('Skipping pyqt5-ios.cfg patch (no source or skip-static).')
        return
    cfg_file = join(cfg.pyqt5_src, 'pyqt5-ios.cfg')
    if not isfile(cfg_file):
        log.info('pyqt5-ios.cfg not yet generated (will be in Step 12).')
        return
    content = _read_file(cfg_file)
    lines = content.splitlines(True)
    out = []
    in_qt = False
    for line in lines:
        stripped = line.strip()
        if match(r'^\[Qt\b', stripped):
            in_qt = True
            out.append(line)
            continue
        if in_qt and stripped.startswith('[') and not match(r'^\[Qt\b', stripped):
            in_qt = False
        if in_qt:
            m = match(r'^(Qt\w+)\s*=', stripped)
            if m and m.group(1) not in cfg.qt_modules:
                out.append('# [removed] ' + line)
                log.debug('Removed from pyqt5-ios.cfg: %s', m.group(1))
                continue
        out.append(line)
    _write_file(cfg_file, ''.join(out))
    log.info('Pruned pyqt5-ios.cfg to: %s', ', '.join(cfg.qt_modules))


# =============================================================================
# Step 11 -- QML sip patch  (optional; from plashless Part 2).
# =============================================================================

def _apply_qml_sip_patch(pyqt5_dir):
    """
    From plashless Part 2:
      "TODO: patch sip/QtMultimediaWidgets/qgraphicsvideoitem.sip if you
       use QML.  Delete the code %ConvertToSubClassCode…%End."
    This patch must be applied BEFORE the configure step (which generates C++ from .sip files).
    :param pyqt5_dir: str
    :return: None
    """
    sip_file = join(pyqt5_dir, 'sip', 'QtMultimediaWidgets', 'qgraphicsvideoitem.sip')
    if not isfile(sip_file):
        return
    content = _read_file(sip_file)
    # Remove %ConvertToSubClassCode...%End block.
    pattern = compile(r'%ConvertToSubClassCode.*?%End', DOTALL)
    new_content = pattern.sub('', content)
    if new_content != content:
        _write_file(sip_file, new_content)
        log.info('QML sip patch applied: %s', sip_file)
    else:
        log.debug('QML sip patch: pattern not found (already patched?).')


def apply_qml_patch(cfg):
    """
    Step 11: apply QML .sip patch if --use-qml is set.
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 11/14 -- QML sip patch (qgraphicsvideoitem.sip)')
    if not cfg.use_qml:
        log.info('--use-qml not set; skipping QML sip patch.')
        return
    if not cfg.pyqt5_src or not isdir(cfg.pyqt5_src):
        log.warning('PyQt5 source not found; cannot apply QML patch.')
        return
    _apply_qml_sip_patch(cfg.pyqt5_src)


# =============================================================================
# Step 12 -- Build PyQt5 statically for ios-64.
# =============================================================================

def build_pyqt5_static(cfg):
    """
    From plashless Part 2:
      cd ~/ios/Downloads/PyQt*
      pyqtdeploycli --package pyqt5 --target ios-64 configure
      python3 configure.py \\
          --static --verbose \\
          --sysroot=/Users/bootch/ios/iRoot \\
          --no-tools --no-qsci-api --no-designer-plugin --no-qml-plugin \\
          --configuration=pyqt5-ios.cfg \\
          --qmake=/Users/bootch/Qt/5.4/ios/bin/qmake \\
          --sip ~/python/bin/sip
      ~/Qt/5.4/ios/bin/qmake PyQt5.pro sysroot=/Users/bootch/ios/iRoot
      make      # "expect to wait tens of minutes"
      make install
    Part 2 edits applied here:
      - --no-qml-plugin removed if cfg.use_qml is True
      - --sip path always passed
      - PyQt5.pro sysroot argument included
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 12/14 -- Building PyQt5 statically for ios-64')
    if cfg.skip_static:
        log.info('--skip-static set; skipping static PyQt5 build.')
        return
    if not cfg.pyqt5_src or not isdir(cfg.pyqt5_src):
        log.warning('PyQt5 source not found; skipping Step 12.')
        return
    env = cfg.build_env()
    # 1. pyqtdeploycli configure (generates pyqt5-ios.cfg).
    log.info('pyqtdeploycli configure for PyQt5 (ios-64) ...')
    _run([cfg.pyqtdeploycli, '--package', 'pyqt5', '--target', TARGET, 'configure'], cwd=cfg.pyqt5_src, env=env,
         dry_run=cfg.dry_run)
    # 2. Now edit pyqt5-ios.cfg (patch step 10 might run here too).
    cfg_file = join(cfg.pyqt5_src, 'pyqt5-ios.cfg')
    if isfile(cfg_file) and not cfg.dry_run:
        # Inline call so patching happens immediately after cfg generation.
        _patch_pyqt5_cfg_inline(cfg_file, cfg.qt_modules)
    # 3. configure.py
    configure_args = [
        getPythonExecutable(), 'configure.py',
        '--static',
        '--verbose',
        '--sysroot={0}'.format(cfg.sysroot),
        '--no-tools',
        '--no-qsci-api',
        '--no-designer-plugin',
        '--configuration={0}'.format(cfg_file),
        '--qmake={0}'.format(cfg.ios_qmake)]
    # From plashless Part 2 edit: "--no-qml-plugin" removed when using QML.
    if not cfg.use_qml:
        configure_args.append('--no-qml-plugin')
    # From plashless Part 2 FAQ: always pass --sip.
    if isfile(cfg.host_sip):
        configure_args.append('--sip={0}'.format(cfg.host_sip))
    log.info('PyQt5 configure.py ...')
    _run(configure_args, cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    # 4. qmake PyQt5.pro sysroot=...  (Part 2: added PyQt5.pro explicitly).
    log.info('qmake PyQt5.pro sysroot=%s ...', cfg.sysroot)
    _run(
        [cfg.ios_qmake, 'PyQt5.pro', 'sysroot={}'.format(cfg.sysroot)], cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    # 5. make  (Part 2: "expect to wait tens of minutes")
    log.info('make for PyQt5 (-j%d) -- this takes tens of minutes ...', cfg.jobs)
    _run([getMakeExecutable(), '-j{0}'.format(cfg.jobs)], cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    # 6. make install.
    log.info('make install for PyQt5 ...')
    _run([getMakeExecutable(), 'install'], cwd=cfg.pyqt5_src, env=env, dry_run=cfg.dry_run)
    log.info('Static PyQt5 built OK')


def _patch_pyqt5_cfg_inline(cfg_file, keep_modules):
    """
    Inline helper to prune pyqt5-ios.cfg right after it is generated.
    :param cfg_file: str
    :param keep_modules: list[str]
    :return:
    """
    content = _read_file(cfg_file)
    lines = content.splitlines(True)
    out = []
    in_qt = False
    for line in lines:
        stripped = line.strip()
        if match(r'^\[Qt\b', stripped):
            in_qt = True
            out.append(line)
            continue
        if in_qt and stripped.startswith('[') and not match(r'^\[Qt\b', stripped):
            in_qt = False
        if in_qt:
            m = match(r'^(Qt\w+)\s*=', stripped)
            if m and m.group(1) not in keep_modules:
                out.append('# [removed] ' + line)
                continue
        out.append(line)
    _write_file(cfg_file, ''.join(out))
    log.info('pyqt5-ios.cfg pruned inline to: %s', ", ".join(keep_modules))


# =============================================================================
# Step 13 -- pyqtdeploy project + build (freeze Python code → .xcodeproj).
# =============================================================================

def pyqtdeploy_build(cfg):
    """
    From plashless Part 3:
      "Here you use the pyqtdeploy GUI app to prepare projects, makefiles,
       and source code.  This is very similar as for any target."
    From plashless Part 2 (directory layout):
      pensoolBuild/
        pensool.pdy          <- pyqtdeploy project
        build/
          pensool.pro        <- generated by pyqtdeploy
    From plashless Part 2 (SYSROOT):
      "Under the 'Locations' tab, the 'Target Python Locations' section
       points to a directory which contains cross-compiled artifacts
       (e.g. a SYSROOT of ~/ios/iRoot)."
    We:
      1. Generate a minimal .pdy template if none exists
      2. Run pyqtdeploycli build
      3. Run qmake (generates .xcodeproj for iOS)
    :param cfg: BuildConfig
    :return: None
    """
    step('Step 13/14 -- pyqtdeploy build (freeze + generate .xcodeproj)')
    env = cfg.build_env()
    _makedirs(cfg.build_dir)
    # Ensure main.py exists.
    _create_main_py_if_missing(cfg)
    # Create .pdy if missing.
    if not isfile(cfg.pdy_file):
        _create_pdy_template(cfg)
    # Run pyqtdeploycli build.
    log.info('Running pyqtdeploycli build ...')
    _run([cfg.pyqtdeploycli, 'build', cfg.pdy_file], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # Find .pro file
    pro_files = _find_pro_files(cfg.build_dir)
    if not pro_files and not cfg.dry_run:
        log.warning(
            'No .pro file generated in %s.\n  Open pyqtdeploy GUI and click Build:\n    pyqtdeploy %s',
            cfg.build_dir, cfg.pdy_file)
        return
    pro_file = pro_files[0] if pro_files else join(cfg.build_dir, '{0}.pro'.format(cfg.app_name))
    log.info('.pro file: %s', pro_file)
    # qmake -- creates .xcodeproj for iOS.
    log.info('Running iOS qmake on .pro (creates .xcodeproj) ...')
    _run([cfg.ios_qmake, pro_file, "sysroot={0}".format(cfg.sysroot)], cwd=cfg.build_dir, env=env, dry_run=cfg.dry_run)
    # Locate .xcodeproj
    xcodeproj = cfg.xcodeproj
    if isdir(xcodeproj):
        log.info('.xcodeproj generated: %s', xcodeproj)
    elif cfg.dry_run:
        log.info('[DRY-RUN] .xcodeproj would be at: %s', xcodeproj)
    else:
        log.warning('.xcodeproj not found after qmake.\n'
                    '  Try running qmake manually:\n    %s %s sysroot=%s', cfg.ios_qmake, pro_file, cfg.sysroot)
    log.info('pyqtdeploy build done OK')


def _create_main_py_if_missing(cfg):
    """
    Create a minimal PyQt5 main.py template if the project has none.
    :param cfg: BuildConfig
    :return: None
    """
    main_py = join(cfg.project_dir, 'main.py')
    if isfile(main_py):
        return
    template = dedent(
        "#!/usr/bin/env python\n"
        "# -*- coding: utf-8 -*-\n"
        "# Minimal PyQt5 iOS app -- generated by pyqt5_ios_plashless.py\n"
        "# Based on: plashless.wordpress.com pyqtdeploy 0.5 iOS guide\n"
        "\n"
        "import sys\n"
        "from PyQt5.QtWidgets import QApplication, QLabel\n"
        "from PyQt5.QtCore import Qt\n"
        "\n"
        "\n"
        "def main():\n"
        "    # From plashless Part 3:\n"
        "    # 'If your app was designed for the desktop, it may work since\n"
        "    #  the QtApplication flag about translating touch events to mouse\n"
        "    #  events defaults to True.'\n"
        "    app = QApplication(sys.argv)\n"
        "    label = QLabel('<center><h2>Hello from PyQt5 on iOS!</h2></center>')\n"
        "    label.setAlignment(Qt.AlignCenter)\n"
        "    label.setWindowTitle('{app}')\n"
        "    label.resize(375, 667)  # iPhone-like size\n"
        "    label.show()\n"
        "    return app.exec_()\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n").replace('{app}', cfg.app_name)
    _write_file(main_py, template)
    log.info('Created main.py template: %s', main_py)


def _create_pdy_template(cfg):
    """
    Generate a minimal pyqtdeploy 0.5 XML .pdy file.
    From plashless Part 2:
      "Under the 'Locations' tab, the 'Target Python Locations' section
       points to a directory which contains cross-compiled artifacts."
    :param cfg: BuildConfig
    :return:
    """
    module_xml = '\n'.join("                <Module name='{0}'/>".format(m) for m in cfg.qt_modules)
    content = dedent(
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<!DOCTYPE pyqtdeploy>\n"
        "<!--\n"
        "    pyqtdeploy 0.5/0.6 project file for {app}\n"
        "    Generated by pyqt5_ios_plashless.py\n"
        "\n"
        "    IMPORTANT:\n"
        "      Open in pyqtdeploy GUI to finish configuring:\n"
        "        pyqtdeploy {pdy}\n"
        "\n"
        "    Under 'Locations' tab:\n"
        "      Target Python Locations -> {sysroot}\n"
        "      (pyqtdeploy calls this the SYSROOT where static libs live)\n"
        "\n"
        "    From plashless Part 3:\n"
        "      - No QPrintSupport on iOS (link error)\n"
        "      - Touch events are auto-translated to mouse events by default\n"
        "-->\n"
        "<pyqtdeploy version='0.5'>\n"
        "    <Application>\n"
        "        <n>{app}</n>\n"
        "        <MainScript>main.py</MainScript>\n"
        "        <SysPath/>\n"
        "        <Modules/>\n"
        "    </Application>\n"
        "    <Python>\n"
        "        <TargetPythonLocation>{sysroot}</TargetPythonLocation>\n"
        "        <TargetSipLocation>{sysroot}</TargetSipLocation>\n"
        "    </Python>\n"
        "    <PyQt5>\n"
        "        <Modules>\n"
        "{modules}\n"
        "        </Modules>\n"
        "    </PyQt5>\n"
        "</pyqtdeploy>\n"
    ).format(app=cfg.app_name, pdy=cfg.pdy_file, sysroot=cfg.sysroot, modules=module_xml)
    _write_file(cfg.pdy_file, content)
    log.info('Created .pdy template: %s', cfg.pdy_file)
    log.info(
        '\n'
        '  Next: complete configuration in pyqtdeploy GUI:\n'
        '    pyqtdeploy %s\n'
        '  Set "Target Python Locations" to: %s\n'
        '  Then click Build to generate .xcodeproj',
        cfg.pdy_file, cfg.sysroot)


def _find_pro_files(directory):
    """
    Return list of all .pro files under directory.
    :param directory: str
    :return: list[str]
    """
    matches = []
    for root, dirs, files in walk(directory):
        for f in files:
            if f.endswith('.pro'):
                matches.append(join(root, f))
    return matches


# =============================================================================
# Step 14 -- Xcode deployment.
# =============================================================================

def deploy_with_xcode(cfg):
    """
    From plashless Part 3:
      "Assuming you ran qmake step in pyqtdeploy, it created an xcodeproj."
      "Open that project in Xcode."
      "Configure Xcode to deploy to your real device."
      "Choose Product > Run."
    From plashless Part 3 (signing):
      "A few dialogs may appear:
       - allowing codesigning to occur
       - to tell you to unlock your sleeping device using its four character
         passcode."
    From plashless Part 3 (developer account):
      "sign up and pay for an iOS developer"
    We open the .xcodeproj in Xcode automatically (via 'open') and print full step-by-step instructions.
    :param cfg: BuildConfig
    :return:
    """
    step('Step 14/14 -- Xcode deployment')
    xcodeproj = cfg.xcodeproj
    xcode_exists = isdir(xcodeproj) or isfile(xcodeproj)
    if xcode_exists and not cfg.dry_run:
        log.info('Opening in Xcode: %s', xcodeproj)
        _run([getOpenExecutable(), xcodeproj], dry_run=cfg.dry_run)
    elif cfg.dry_run:
        log.info('[DRY-RUN] Would open: %s', xcodeproj)
    else:
        log.warning('.xcodeproj not found: %s', xcodeproj)
    log.info(dedent(
        "\n"
        "  ╔══════════════════════════════════════════════════════════════╗\n"
        "  ║  Xcode deployment  (plashless Parts 1 & 3)                   ║\n"
        "  ╚══════════════════════════════════════════════════════════════╝\n"
        "\n"
        "  1. Install Xcode from the Mac App Store (if not already done).\n"
        "     Xcode 5.1.1 or later required.\n"
        "\n"
        "  2. (iOS Simulator) Test without a developer account:\n"
        "     From plashless Part 1:\n"
        "       'Follow Qt5 Tutorial: Pushing Example App to iOS Simulator'\n"
        "     In Qt Maintenance Tool, install 'Qt {qt} -> iOS -> iOS Simulator'.\n"
        "     In Xcode: change scheme to iPhone Simulator → Product → Run.\n"
        "\n"
        "  3. (Real Device) Sign up for Apple Developer Program:\n"
        "     https://developer.apple.com/programs/\n"
        "     From plashless Part 3: 'sign up and pay for an iOS developer'\n"
        "\n"
        "  4. Connect your iOS device via USB cable.\n"
        "     From Part 3: 'Hook up your device with a USB cable.'\n"
        "     'Xcode should start and display the Organizer.'\n"
        "     Click 'Configure for Development' in the Organizer.\n"
        "\n"
        "  5. Open the .xcodeproj:\n"
        "       open {xcodeproj}\n"
        "\n"
        "  6. In Xcode title bar: click 'Scheme' → choose your device.\n"
        "     From Part 3: 'A list of devices (real and simulator) appears.'\n"
        "\n"
        "  7. Product → Run (Cmd+R).\n"
        "     Dialogs may appear for codesigning and device unlock.\n"
        "\n"
        "  8. Your app appears on the device.\n"
        "     From Part 3: 'Touch the device to generate input events.'\n"
        "\n"
        "  9. To run later: pan left/right on the home screen to find the app.\n"
        "\n"
        "  ── FAQ (from plashless Part 3) ────────────────────────────────\n"
        "  • 'No QPrintSupport on iOS' → do NOT import PyQt5.QtPrintSupport\n"
        "    (it causes a link error; already excluded from the module list)\n"
        "\n"
        "  • 'Touch events' → Qt translates touch to mouse by default.\n"
        "    If menus/buttons stop working, check this flag:\n"
        "      QApplication.setAttribute(Qt.AA_SynthesizeMouseForUnhandledTouchEvents, True)\n"
        "\n"
        "  • 'Debug on iOS first' (Part 3):\n"
        "    'Xcode generates better error messages at link time and displays\n"
        "     stdout/stderr in a window.'\n"
        "  ───────────────────────────────────────────────────────────────".format(qt=QT_VERSION, xcodeproj=xcodeproj)))


# =============================================================================
# Summary
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
        '  Python (target)  : %s\n'
        '  PyQt5            : %s\n'
        '  SIP              : %s\n'
        '  Qt               : %s  (%s)\n'
        '  Target           : %s\n'
        '  Host OS          : macOS %s\n'
        '  SYSROOT          : %s\n'
        '  Project dir      : %s\n'
        '  Build dir        : %s\n'
        '  .pdy file        : %s\n'
        '  .xcodeproj       : %s\n'
        '  Qt modules       : %s',
        cfg.app_name,
        PYTHON_VERSION,
        PYQT5_VERSION,
        SIP_VERSION,
        QT_VERSION, QT_IOS_SUBDIR,
        TARGET,
        mac_ver()[0],
        cfg.sysroot,
        cfg.project_dir,
        cfg.build_dir,
        cfg.pdy_file,
        cfg.xcodeproj,
        ", ".join(cfg.qt_modules))
    log.info(dedent(
        "\n"
        "Error FAQ  (from plashless Parts 1, 2, 3)\n"
        "==========================================\n"
        "\n"
        "• 'requires a valid repository' in Qt Maintenance Tool\n"
        "  Fix: Maintenance Tool → Settings → Repositories → User specified\n"
        "       Add: http://download.qt-project.org/online/qt5/mac/x86/online_repository\n"
        "\n"
        "• 'No module named PyQt5' when running pyqtdeploy\n"
        "  Fix: set PYTHONPATH to where host PyQt5 is installed:\n"
        "         export PYTHONPATH=~/python/lib/python3.4/site-packages\n"
        "\n"
        "• 'You need a working sip on your PATH'\n"
        "  Fix: pass --sip=~/python/bin/sip to configure.py\n"
        "       The script does this automatically via cfg.host_sip.\n"
        "\n"
        "• PyQt5.QtPrintSupport causes link error on iOS\n"
        "  From Part 3: 'Qt does not yet support printing on mobile devices.'\n"
        "  Fix: Do NOT import QtPrintSupport (already excluded from module list).\n"
        "\n"
        "• 'line does not match diff context' in pyqtdeploycli configure\n"
        "  Cause: Python source already patched (second attempt).\n"
        "  Fix: Delete Python source and re-extract:\n"
        "         rm -rf ~/ios/Downloads/Python-3.4.0\n"
        "         tar xzf ~/ios/Downloads/Python-3.4.0.tgz -C ~/ios/Downloads/\n"
        "\n"
        "• App crashes on device but not simulator\n"
        "  From Part 3: 'Debug on iOS first -- Xcode shows stdout/stderr.'\n"
        "  Check Xcode Debug Console for Python tracebacks.\n"
        "\n"
        "• Touch / tap not working after Qt.AA_* flag change\n"
        "  From Part 3: 'Menus and buttons depend on mouse events.'\n"
        "  Leave AA_SynthesizeMouseForUnhandledTouchEvents at its default (True).\n"
        "\n"
        "• Only static libraries allowed on iOS\n"
        "  From Part 2: 'iOS does not allow dynamic libraries.'\n"
        "  That is why we build Python, SIP, PyQt5 all as .a files.\n"))


# =============================================================================
# Argument parser.
# =============================================================================

def make_parser():
    """
    :return: ArgumentParser
    """
    parser = ArgumentParser(
        prog='pyqt5_ios_plashless.py', formatter_class=RawDescriptionHelpFormatter,
        description=dedent(
            'PyQt5 {pyqt} iOS Builder  (pyqtdeploy {pyd} on macOS)\n'
            '=======================================================\n'
            'Automates the three-part plashless tutorial for building\n'
            'a PyQt5 {pyqt} app for iOS using pyqtdeploy 0.5/0.6.\n'
            'Pinned versions:\n'
            '  Python {py}  |  Qt {qt}  |  PyQt5 {pyqt}  |  SIP {sip}\n'
            '  Target: {target}  |  Xcode 5.1.1+  |  macOS 10.9+\n'
        ).format(pyqt=PYQT5_VERSION, pyd="0.5/0.6", py=PYTHON_VERSION, qt=QT_VERSION, sip=SIP_VERSION, target=TARGET),
        epilog=dedent(
            "Examples\n"
            "--------\n"
            "# Full build:\n"
            "python pyqt5_ios_plashless.py \\\n"
            "    --project-dir ~/ios/pensoolBuild \\\n"
            "    --qt-dir      ~/Qt/5.3 \\\n"
            "    --python-home ~/python\n"
            "\n"
            "# Skip static lib build (iRoot already populated):\n"
            "python pyqt5_ios_plashless.py \\\n"
            "    --project-dir ~/ios/pensoolBuild \\\n"
            "    --sysroot     ~/ios/iRoot \\\n"
            "    --skip-static\n"
            "\n"
            "# With QML support:\n"
            "python pyqt5_ios_plashless.py \\\n"
            "    --project-dir ~/ios/pensoolBuild \\\n"
            "    --use-qml\n"
            "\n"
            "# Dry-run:\n"
            "python pyqt5_ios_plashless.py \\\n"
            "    --project-dir ~/ios/pensoolBuild \\\n"
            "    --dry-run --verbose\n"))
    parser.add_argument('--project-dir', required=True, metavar='DIR',
                        help='App directory (equivalent to ~/ios/pensoolBuild in plashless guide). '
                             'Will be created if it does not exist.')
    parser.add_argument('--app-name', default=None, metavar='NAME',
                        help='Application name (default: project dir basename).')
    parser.add_argument('--qt-dir', default=DEFAULT_QTDIR, metavar='DIR',
                        help='Qt {0} root (default: {1}).'.format(QT_VERSION, DEFAULT_QTDIR))
    parser.add_argument('--python-home', default=DEFAULT_PYDIR, metavar='DIR',
                        help='Host Python installation (~/python in plashless guide). '
                             'Must contain bin/sip and lib/python3.4/site-packages/PyQt5. Default: {0}'.format(
                            DEFAULT_PYDIR))
    parser.add_argument('--sysroot', default=DEFAULT_IROOT, metavar='DIR',
                        help='iOS sysroot / iRoot (default: {0}). '
                             "From plashless: 'Stand-in for the root of the target device.'".format(DEFAULT_IROOT))
    parser.add_argument('--downloads-dir', default=DEFAULT_DLDIR, metavar='DIR',
                        help='Directory for source tarballs (default: {0}).'.format(DEFAULT_DLDIR))
    parser.add_argument('--work-dir', default=DEFAULT_WORK, metavar='DIR',
                        help='Working directory (default: {0}).'.format(DEFAULT_WORK))
    # Pre-extracted source paths.
    parser.add_argument('--python-src', default='', metavar='DIR',
                        help='Pre-extracted Python-3.4.0 source directory.')
    parser.add_argument('--sip-src', default='', metavar='DIR',
                        help='Pre-extracted SIP snapshot source directory.')
    parser.add_argument('--pyqt5-src', default='', metavar='DIR',
                        help='Pre-extracted PyQt5 snapshot source directory.')
    parser.add_argument('--pyqtdeploy-src', default='', metavar='DIR',
                        help='Pre-cloned pyqtdeploy Mercurial source directory.')
    # Build control.
    parser.add_argument('--extra-qt-modules', default='', metavar='LIST',
                        help='Comma-separated extra Qt modules to include (e.g. QtSql,QtBluetooth). '
                             'NOTE: QtPrintSupport is intentionally excluded (iOS link error).')
    parser.add_argument('--use-qml', action='store_true',
                        help='Enable QML support. From plashless Part 2: removes --no-qml-plugin and applies the '
                             'qgraphicsvideoitem.sip patch.')
    parser.add_argument('--simulator', action='store_true', help='Target the iOS Simulator instead of a real device.')
    parser.add_argument('--skip-static', action='store_true',
                        help='Skip Python/SIP/PyQt5 static library builds (use existing sysroot).')
    parser.add_argument('--jobs', type=int, default=4, metavar='N',
                        help='Parallel make jobs (default: 4). From plashless: "make -j4" for PyQt5.')
    parser.add_argument('--keep-build', action='store_true', help='Retain intermediate build files.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug-level output.')
    return parser


# =============================================================================
# Main.
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
    try:
        preflight(cfg)
        setup_directories(cfg)
        setup_environment(cfg)
        install_pyqtdeploy(cfg)
        build_host_sip(cfg)
        build_host_pyqt5(cfg)
        locate_sources(cfg)
        build_python_static(cfg)
        build_sip_static(cfg)
        patch_pyqt5_ios_cfg(cfg)
        apply_qml_patch(cfg)
        build_pyqt5_static(cfg)
        pyqtdeploy_build(cfg)
        deploy_with_xcode(cfg)
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


if __name__ == "__main__":
    exit(main())
