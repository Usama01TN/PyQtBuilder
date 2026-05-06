#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PySide6 / PyQt -> Android Build Script
======================================
Automates the full pipeline for packaging a PySide6 application as an
Android APK/AAB using the official `pyside6-android-deploy` toolchain.

Sources / References:
--------------------
- https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-android-deploy.html
- https://github.com/EchterAlsFake/PySide6-to-Android
- https://github.com/achille-martin/pyqt-crom
- https://plashless.wordpress.com/2014/08/14/using-qtcreator-on-linux-to-cross-compile-for-android/

Requirements:
------------
- Linux (Ubuntu 22.04+ recommended) or macOS
- Python 3.10 or 3.11
- Internet access (first run only)
- ~15 GB free disk space

Usage:
-----
    python pyside_android_builder.py --project-dir /path/to/myapp [OPTIONS]
    # Full automated build (aarch64):
    python pyside_android_builder.py --project-dir ./myapp --arch aarch64
    # Download SDK/NDK only:
    python pyside_android_builder.py --project-dir ./myapp --only-setup-env
    # Install APK to connected device after build:
    python pyside_android_builder.py --project-dir ./myapp --arch aarch64 --install-apk
    # Use pre-downloaded wheels:
    python pyside_android_builder.py --project-dir ./myapp \\
        --wheel-pyside /path/to/PySide6-...-android_aarch64.whl \\
        --wheel-shiboken /path/to/shiboken6-...-android_aarch64.whl
    # Keep intermediate build files for debugging:
    python pyside_android_builder.py --project-dir ./myapp --keep-build-files --verbose
"""
from os.path import isdir, sep, join, dirname, exists, basename, getsize, getmtime, normpath, realpath, expanduser, \
    relpath
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from logging import basicConfig, getLogger, INFO, DEBUG
from sys import exit, version_info, path
from platform import release, system
from subprocess import Popen, PIPE
from textwrap import dedent

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .build_utils import which, _rglob, disk_usage, _makedirs, create, urlretrieve, URLError, FileNotFoundError
    from .builders import getAdbExecutable, getGitExecutable, getJavaExecutable
except:
    from build_utils import which, _rglob, disk_usage, _makedirs, create, urlretrieve, URLError, FileNotFoundError
    from builders import getAdbExecutable, getGitExecutable, getJavaExecutable


def _is_relative_to(pth, base):
    """
    Return True if *path* is located inside *base*.
    Replicates Path.is_relative_to() without pathlib.
    :param pth: str
    :param base: str
    :return: bool
    """
    pth = realpath(pth)  # type: str
    base = realpath(base)  # type: str
    rel = relpath(pth, base)  # type: str
    # relpath returns '..' or '../…' when path escapes base.
    return not (rel == '..' or rel.startswith('..' + sep))


# ---------------------------------------------------------------------------
# subprocess helper (replacing subprocess.run + CompletedProcess).
# ---------------------------------------------------------------------------

class SimpleProcess(object):
    """
    Minimal stand-in for subprocess.CompletedProcess
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
# Constants
# ------------------------------------------------------------------------------
PYSIDE_VERSION = '6.10.2'
PYTHON_VERSION = '3.11'  # 3.10 also supported.
PYSIDE_SETUP_URL = 'https://code.qt.io/pyside/pyside-setup'
# Official wheelbase URL (Qt downloads page).
WHEEL_BASE_URL = 'https://download.qt.io/official_releases/QtForPython'
# Architecture mapping: script name -> Android ABI
ARCH_MAP = {'aarch64': 'aarch64', 'x86_64': 'x86_64', 'armv7a': 'armv7a', 'i686': 'i686'}
# NDK / SDK paths placed by the Qt helper script.
HOME_DIR = expanduser('~')  # type: str
DEFAULT_CACHE_DIR = join(HOME_DIR, '.pyside6_android_deploy')  # type: str
DEFAULT_NDK_DIR = join(DEFAULT_CACHE_DIR, 'android-ndk', 'android-ndk-r27c')  # type: str
DEFAULT_SDK_DIR = join(DEFAULT_CACHE_DIR, 'android-sdk')  # type: str
# Minimum disk space required (bytes).
MIN_DISK_GB = 15  # type: int
# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
basicConfig(format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S', level=INFO)
log = getLogger('pyside-android-builder')


def _step(msg):
    """
    Print a visually distinct step header.
    :param msg: str
    :return:
    """
    bar = '-' * 60  # type: str
    log.info('\n%s\n  %s\n%s', bar, msg, bar)


# ------------------------------------------------------------------------------
# Configuration class.
# ------------------------------------------------------------------------------

class BuildConfig(object):
    """
    Validated, resolved configuration for a single build run.
    """

    def __init__(self, project_dir, app_name, arch, pyside_version=PYSIDE_VERSION, python_version=PYTHON_VERSION,
                 ndk_path=None, sdk_path=None, wheel_pyside=None, wheel_shiboken=None, mode='debug', verbose=False,
                 dry_run=False, keep_build_files=False, install_apk=False, venv_dir=None):
        """
        :param project_dir: str
        :param app_name: str
        :param arch: str
        :param pyside_version: str
        :param python_version: str
        :param ndk_path: str | None
        :param sdk_path: str | None
        :param wheel_pyside: str | None
        :param wheel_shiboken: str | None
        :param mode: (str) 'debug' -> .apk | 'release' -> .aab
        :param verbose: bool
        :param dry_run: bool
        :param keep_build_files: bool
        :param install_apk: bool
        :param venv_dir: str | None
        """
        self.project_dir = project_dir  # type: str
        self.app_name = app_name  # type: str
        self.arch = arch  # type: str
        self.pyside_version = pyside_version  # type: str
        self.python_version = python_version  # type: str
        self.ndk_path = ndk_path  # type: str
        self.sdk_path = sdk_path  # type: str
        self.wheel_pyside = wheel_pyside  # type: str
        self.wheel_shiboken = wheel_shiboken  # type: str
        self.mode = mode  # type: str
        self.verbose = verbose  # type: bool
        self.dry_run = dry_run  # type: bool
        self.keep_build_files = keep_build_files  # type: bool
        self.install_apk = install_apk  # type: bool
        # Derived / resolved at runtime.
        # The venv MUST live outside the project directory, otherwise pyside6-android-deploy
        # tries to bundle the entire installed PySide6 (thousands of QML files) into the APK.
        # Default: a sibling of the project named .venv_<project_name>_android_build.
        # Override via --venv-dir.
        if venv_dir:
            self.venv_dir = realpath(venv_dir)  # type: str
        else:
            self.venv_dir = join(dirname(self.project_dir),
                                 '.venv_' + basename(self.project_dir) + '_android_build')
        self.pyside_setup_dir = join(DEFAULT_CACHE_DIR, 'pyside-setup')  # type: str
        self.wheels_dir = join(DEFAULT_CACHE_DIR, 'wheels')  # type: str

    @property
    def python_exe(self):
        """
        :return: str
        """
        if system() == 'Windows':
            return join(self.venv_dir, 'Scripts', 'python.exe')
        return join(self.venv_dir, 'bin', 'python')

    @property
    def pip_exe(self):
        """
        :return: str
        """
        if system() == 'Windows':
            return join(self.venv_dir, 'Scripts', 'pip.exe')
        return join(self.venv_dir, 'bin', 'pip')

    @property
    def pyside6_android_deploy(self):
        """
        :return: str
        """
        if system() == 'Windows':
            return join(self.venv_dir, 'Scripts', 'pyside6-android-deploy.exe')
        return join(self.venv_dir, 'bin', 'pyside6-android-deploy')

    @property
    def adb_exe(self):
        """
        :return: str
        """
        return join(DEFAULT_SDK_DIR, 'platform-tools', 'adb')


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _run(cmd, cwd=None, check=True, dry_run=False, capture=False):
    """
    Run a subprocess with unified error handling.
    Replaces subprocess.run() + CompletedProcess for Python 2/3 compatibility.
    :param cmd:      list[str]
    :param cwd:      str | None
    :param check:    bool
    :param dry_run:  bool
    :param capture:  bool
    :return:         SimpleProcess
    """
    cmd_strs = [c for c in cmd]
    display = ' '.join(cmd_strs)
    log.debug('$ %s', display)
    if dry_run:
        log.info('[DRY-RUN] %s', display)
        return SimpleProcess(cmd, 0, '', '')
    try:
        if capture:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, stdout=PIPE, stderr=PIPE, universal_newlines=True)
        else:
            proc = Popen(cmd_strs, cwd=cwd if cwd else None, universal_newlines=True)
        stdout, stderr = proc.communicate()
    except OSError as exc:
        raise RuntimeError('Failed to start subprocess: {}\n{}'.format(display, exc))
    result = SimpleProcess(cmd, proc.returncode, stdout or '', stderr or '')
    if check and result.returncode != 0:
        log.error('Command failed (exit %d):\n  %s', result.returncode, display)
        if result.stdout:
            log.error('stdout:\n%s', result.stdout[-2000:])
        if result.stderr:
            log.error('stderr:\n%s', result.stderr[-2000:])
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
    :return:
    """
    _makedirs(dirname(dest))
    log.info('Downloading %s -> %s', url, dest)
    try:
        urlretrieve(url, dest)
    except URLError as exc:
        raise RuntimeError('Download failed: {}\n{}'.format(url, exc))


def _check_disk_space(pth, required_gb=MIN_DISK_GB):
    """
    Warn if free disk space at *path* is below *required_gb* gigabytes.
    :param pth: str
    :param required_gb: int
    :return:
    """
    usage = disk_usage(pth)
    free_gb = usage.free / 1024.0 ** 3  # Float division.
    if free_gb < required_gb:
        log.warning('Low disk space: %.1f GB free at %s (recommended: %d GB)', free_gb, pth, required_gb)


# ------------------------------------------------------------------------------
# Step 1 – Preflight checks
# ------------------------------------------------------------------------------

def preflight_checks(cfg):
    """
    Validate the host environment before doing any work.
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 1/7 - Preflight checks')
    # OS check.
    if system() not in ('Linux', 'Darwin'):
        raise EnvironmentError('pyside6-android-deploy requires Linux or macOS (detected: {}).'.format(system()))
    log.info('Host OS: %s %s', system(), release())
    # Python version.
    major, minor = version_info[:2]
    supported = [(3, 10), (3, 11)]
    if (major, minor) not in supported:
        raise EnvironmentError('Python {}.{} is not supported. Use 3.10 or 3.11.'.format(major, minor))
    log.info('Python: %d.%d', major, minor)
    # Disk space.
    cache_or_home = DEFAULT_CACHE_DIR if exists(DEFAULT_CACHE_DIR) else HOME_DIR
    _check_disk_space(cache_or_home)
    # Required system tools.
    for tool in ('git', 'java', 'zip', 'unzip'):
        log.info('Found: %s -> %s', tool, _require_tool(tool))
    # Java version (JDK 17 required by Qt toolchain).
    java_out = _run([getJavaExecutable(), '-version'], capture=True, check=False)
    raw_output = java_out.stderr or java_out.stdout or ''
    output_lines = raw_output.splitlines()
    version_line = output_lines[0] if output_lines else ''
    log.info('Java: %s', version_line)
    if '17' not in version_line and '21' not in version_line:
        log.warning("Qt's Android toolchain aligns with JDK 17. Other versions may cause Gradle errors.")
    # Project structure.
    main_py = join(cfg.project_dir, 'main.py')
    if not exists(main_py):
        raise FileNotFoundError(
            'Entry point not found: {}\npyside6-android-deploy requires the main script to be named "main.py".'.format(
                main_py))
    log.info('Entry point: %s', main_py)
    # Architecture
    if cfg.arch not in ARCH_MAP:
        raise ValueError('Unknown architecture "{}". Choose from: {}'.format(cfg.arch, ', '.join(ARCH_MAP)))
    log.info('Target architecture: %s', cfg.arch)
    log.info('Build mode: %s (%s)', cfg.mode, '.apk (debug)' if cfg.mode == 'debug' else '.aab (release)')
    log.info('Preflight checks passed :)')


# ------------------------------------------------------------------------------
# Step 2 – Virtual environment
# ------------------------------------------------------------------------------

def setup_virtualenv(cfg):
    """
    Create a fresh virtual environment for the build tooling.
    :param cfg: BuildConfig
    :return:
    """
    _step('Step 2/7 - Virtual environment')
    # The venv must NOT be inside the project directory (Qt quirk: it will try
    # to bundle it, causing a RuntimeError about 'too many QML files').
    if _is_relative_to(cfg.venv_dir, cfg.project_dir):
        raise ValueError(
            'Virtual environment must be outside the project directory. Current venv path: {}'.format(cfg.venv_dir))
    if exists(cfg.venv_dir):
        log.info('Reusing existing venv: %s', cfg.venv_dir)
    else:
        log.info('Creating venv at: %s', cfg.venv_dir)
        create(cfg.venv_dir, with_pip=True, clear=True)
    # Upgrade pip inside venv.
    _run([cfg.python_exe, '-m', 'pip', 'install', '--upgrade', 'pip', '--quiet'], dry_run=cfg.dry_run)
    # Install host-side PySide6 (used by pyside6-android-deploy itself).
    log.info('Installing PySide6 %s (host)...', cfg.pyside_version)
    _run([cfg.pip_exe, 'install', 'pyside6=={}'.format(cfg.pyside_version), '--quiet', '--no-warn-script-location'],
         dry_run=cfg.dry_run)
    # Cython is required by buildozer / python-for-android to compile native modules.
    # It is NOT shipped in PySide6's requirements-android.txt — buildozer just assumes
    # you have it on the dev machine. In CI we need to install it explicitly.
    log.info('Installing Cython (required by buildozer)...')
    _run([cfg.pip_exe, 'install', '--quiet', '--no-warn-script-location', 'cython'],
         dry_run=cfg.dry_run)
    # pyside6-android-deploy needs extra runtime deps (pkginfo, packaging, ...) that ship
    # inside PySide6 itself as scripts/requirements-android.txt. The original script
    # relied on setup_android_sdk_ndk() installing them as a side effect, which only
    # happens when the Qt helper runs — i.e. NOT when the NDK/SDK cache is already warm.
    # Install them explicitly so the deploy tool can always start.
    log.info('Installing pyside6-android-deploy runtime requirements...')
    locator = (
        'import os, sys, PySide6; '
        'p = os.path.join(os.path.dirname(PySide6.__file__), "scripts", "requirements-android.txt"); '
        'sys.stdout.write(p if os.path.exists(p) else "")'
    )
    res = _run([cfg.python_exe, '-c', locator], capture=True, check=False, dry_run=cfg.dry_run)
    req_path = (res.stdout or '').strip()
    if req_path:
        _run([cfg.pip_exe, 'install', '-r', req_path, '--quiet', '--no-warn-script-location'],
             dry_run=cfg.dry_run)
    else:
        # Fall back: install the known-required packages directly so the deploy tool starts.
        log.warning('requirements-android.txt not found inside installed PySide6; '
                    'falling back to a hardcoded minimum set.')
        _run([cfg.pip_exe, 'install', '--quiet', '--no-warn-script-location',
              'pkginfo', 'packaging'], dry_run=cfg.dry_run)
    log.info('Virtual environment ready :)')


# ------------------------------------------------------------------------------
# Step 3 – Android SDK / NDK
# ------------------------------------------------------------------------------

def setup_android_sdk_ndk(cfg):
    """
    Download Android NDK + SDK using Qt's cross-compilation helper script.
    Equivalent shell commands
    -------------------------
    git clone https://code.qt.io/pyside/pyside-setup  ~/.pyside6_android_deploy/pyside-setup
    cd ~/.pyside6_android_deploy/pyside-setup
    git checkout <version>
    pip install -r requirements.txt -r tools/cross_compile_android/requirements.txt
    python tools/cross_compile_android/main.py --download-only --skip-update --auto-accept-license
    :param cfg: BuildConfig
    :return:    None
    """
    _step('Step 3/7 - Android SDK / NDK')
    # If both paths are already provided and exist, skip download.
    if cfg.ndk_path and cfg.sdk_path:
        if exists(cfg.ndk_path) and exists(cfg.sdk_path):
            log.info('Using pre-existing NDK: %s', cfg.ndk_path)
            log.info('Using pre-existing SDK: %s', cfg.sdk_path)
            return
    # Check if the Qt helper already downloaded them.
    if exists(DEFAULT_NDK_DIR) and exists(DEFAULT_SDK_DIR):
        log.info('Found cached NDK/SDK at %s', DEFAULT_CACHE_DIR)
        cfg.ndk_path = DEFAULT_NDK_DIR
        cfg.sdk_path = DEFAULT_SDK_DIR
        return
    # Clone pyside-setup if needed.
    setup_dir = cfg.pyside_setup_dir
    _makedirs(DEFAULT_CACHE_DIR)
    if not exists(setup_dir):
        log.info('Cloning pyside-setup (depth=1)...')
        _run([getGitExecutable(), 'clone', '--depth', '1', '--branch', cfg.pyside_version, PYSIDE_SETUP_URL,
              setup_dir], dry_run=cfg.dry_run)
    else:
        log.info('pyside-setup already cloned at %s', setup_dir)
    # Install helper requirements into our venv.
    reqs = [join(setup_dir, 'requirements.txt'), join(setup_dir, 'tools', 'cross_compile_android', 'requirements.txt')]
    for req in reqs:
        if exists(req):
            _run([cfg.pip_exe, 'install', '-r', req, '--quiet'], dry_run=cfg.dry_run)
    # Run Qt's download helper.
    log.info('Downloading Android NDK + SDK (this takes a few minutes)...')
    helper = join(setup_dir, 'tools', 'cross_compile_android', 'main.py')
    _run([cfg.python_exe, helper, '--download-only', '--skip-update', '--auto-accept-license'], cwd=setup_dir,
         dry_run=cfg.dry_run)
    # Resolve paths.
    cfg.ndk_path = DEFAULT_NDK_DIR
    cfg.sdk_path = DEFAULT_SDK_DIR
    if not cfg.dry_run:
        if not exists(cfg.ndk_path):
            raise FileNotFoundError(
                'NDK not found at expected path: {}\nRun the Qt helper manually and pass --ndk-path.'.format(
                    cfg.ndk_path))
        if not exists(cfg.sdk_path):
            raise FileNotFoundError('SDK not found at expected path: {}'.format(cfg.sdk_path))
    log.info('NDK: %s', cfg.ndk_path)
    log.info('SDK: %s', cfg.sdk_path)
    log.info('Android SDK/NDK ready :)')


# ------------------------------------------------------------------------------
# Step 4 – PySide6 Android wheels
# ------------------------------------------------------------------------------

def _wheel_urls(version, arch, py_minor='11'):
    """
    Return (pyside6_url, shiboken6_url) for the given version and arch.
    Example URL:
      https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.10.2-6.10.2-cp311-cp311-android_aarch64.whl
    :param version:   str
    :param arch:      str
    :param py_minor:  str
    :return:          tuple[str, str]
    """
    tag = 'cp3{}-cp3{}'.format(py_minor, py_minor)
    pyside_fn = 'PySide6-{}-{}-{}-android_{}.whl'.format(version, version, tag, arch)
    shib_fn = 'shiboken6-{}-{}-{}-android_{}.whl'.format(version, version, tag, arch)
    base_p = "{}/pyside6/{}".format(WHEEL_BASE_URL, pyside_fn)
    base_s = "{}/shiboken6/{}".format(WHEEL_BASE_URL, shib_fn)
    return base_p, base_s


def download_wheels(cfg):
    """
    Download pre-built PySide6/Shiboken6 Android wheels from Qt servers.
    :param cfg: BuildConfig
    :return:    None
    """
    _step('Step 4/7 - PySide6 Android wheels')
    if cfg.wheel_pyside and cfg.wheel_shiboken:
        if exists(cfg.wheel_pyside) and exists(cfg.wheel_shiboken):
            log.info('Using pre-downloaded wheels:')
            log.info('  PySide6:   %s', cfg.wheel_pyside)
            log.info('  Shiboken6: %s', cfg.wheel_shiboken)
            return
        else:
            log.warning('Specified wheel paths not found; will download.')
    py_minor = cfg.python_version.split('.')[-1]
    pyside_url, shib_url = _wheel_urls(cfg.pyside_version, cfg.arch, py_minor)
    _makedirs(cfg.wheels_dir)
    # Basename works correctly on URL strings (forward-slash paths).
    pyside_dest = join(cfg.wheels_dir, basename(pyside_url))
    shib_dest = join(cfg.wheels_dir, basename(shib_url))
    for url, dest in [(pyside_url, pyside_dest), (shib_url, shib_dest)]:
        if exists(dest):
            log.info('Wheel already cached: %s', basename(dest))
        else:
            if not cfg.dry_run:
                _download(url, dest)
            else:
                log.info('[DRY-RUN] Would download %s', url)
    cfg.wheel_pyside = pyside_dest
    cfg.wheel_shiboken = shib_dest
    log.info('PySide6 wheel:   %s', cfg.wheel_pyside)
    log.info('Shiboken6 wheel: %s', cfg.wheel_shiboken)
    log.info('Wheels ready :)')


# ------------------------------------------------------------------------------
# Step 5 – Build the APK
# ------------------------------------------------------------------------------

def build_apk(cfg):
    """
    Invoke pyside6-android-deploy to package the project.
    Equivalent shell command
    ------------------------
    pyside6-android-deploy \\
        --name "MyApp" \\
        --wheel-pyside   /path/to/PySide6-...-android_aarch64.whl \\
        --wheel-shiboken /path/to/shiboken6-...-android_aarch64.whl \\
        --ndk-path ~/.pyside6_android_deploy/android-ndk/android-ndk-r27c \\
        --sdk-path ~/.pyside6_android_deploy/android-sdk/
    :param cfg: BuildConfig
    :return:    str
    """
    _step('Step 5/7 - Building APK/AAB')
    cmd = [
        cfg.pyside6_android_deploy, '--name', cfg.app_name, '--wheel-pyside', cfg.wheel_pyside, '--wheel-shiboken',
        cfg.wheel_shiboken, '--ndk-path', cfg.ndk_path, '--sdk-path', cfg.sdk_path, '--force',  # non-interactive.
    ]
    if cfg.keep_build_files:
        cmd.append('--keep-deployment-files')
    if cfg.verbose:
        cmd.append('--verbose')
    if cfg.dry_run:
        cmd.append('--dry-run')
    log.info('Running: %s', ' '.join(c for c in cmd))
    _run(cmd, cwd=cfg.project_dir)  # Must run even in dry_run mode.
    # Locate the produced artifact.
    ext = '.apk' if cfg.mode == 'debug' else '.aab'
    matches = _rglob(cfg.project_dir, '*{}'.format(ext))
    if not matches:
        if cfg.dry_run:
            log.info('[DRY-RUN] Build skipped; no artifact produced.')
            return join(cfg.project_dir, '{}{}'.format(cfg.app_name, ext))
        raise FileNotFoundError('Build completed but no {} file was found under {}.'.format(ext, cfg.project_dir))
    artifact = sorted(matches, key=lambda p: getmtime(p))[-1]
    size_mb = getsize(artifact) / 1024.0 ** 2  # Float division.
    log.info('Build artifact: %s (%.1f MB)', artifact, size_mb)
    log.info('Build complete :)')
    return artifact


# ------------------------------------------------------------------------------
# Step 6 – ADB install
# ------------------------------------------------------------------------------

def install_via_adb(cfg, apk_path):
    """
    Install the built APK on the first ADB-connected Android device.
    Prerequisites
    -------------
    - USB debugging enabled on device
    - Device authorized (accept the RSA prompt)
    :param cfg:      BuildConfig
    :param apk_path: str
    :return:         None
    """
    _step('Step 6/7 - Installing APK via ADB')
    # Prefer SDK adb, fall back to system adb.
    sdk_adb = cfg.adb_exe
    adb = sdk_adb if exists(sdk_adb) else getAdbExecutable()
    if not adb or not exists(adb):
        log.warning('adb not found; skipping device install.')
        log.warning('Install android-tools-adb (Ubuntu) or Android SDK platform-tools.')
        return
    # List devices
    res = _run([adb, 'devices'], capture=True, check=False)
    device_lines = [l for l in res.stdout.splitlines() if l.strip() and 'List of devices' not in l]
    if not device_lines:
        log.warning('No ADB devices found. Connect a device and re-run.')
        return
    log.info('Connected devices:\n%s', '\n'.join('  {}'.format(l) for l in device_lines))
    log.info('Installing %s...', basename(apk_path))
    _run([adb, 'install', '-r', apk_path], dry_run=cfg.dry_run)
    log.info('Installation complete :)')
    log.info('Tip: stream logs with:\n  %s logcat --regex "%s"', adb, cfg.app_name.lower())


# ------------------------------------------------------------------------------
# Step 7 – Summary
# ------------------------------------------------------------------------------

def print_summary(cfg, artifact):
    """
    :param cfg:      BuildConfig
    :param artifact: str | None
    :return:
    """
    _step('Step 7/7 - Summary')
    lines = [
        '  App name      : {}'.format(cfg.app_name),
        '  Architecture  : {}'.format(cfg.arch),
        '  PySide6       : {}'.format(cfg.pyside_version),
        '  Mode          : {}'.format(cfg.mode),
        '  Project dir   : {}'.format(cfg.project_dir),
        '  NDK           : {}'.format(cfg.ndk_path),
        '  SDK           : {}'.format(cfg.sdk_path)]
    if artifact:
        lines.append('  Output        : {}'.format(artifact))
    log.info('\n'.join(lines))
    if artifact and exists(artifact):
        log.info('\n  Build succeeded!')
        log.info('   To install manually:')
        log.info('     adb install %s', artifact)
        log.info('   To stream device logs:')
        log.info('     adb logcat | grep -i %s', cfg.app_name.lower())
    elif cfg.dry_run:
        log.info('\n  Dry-run finished - no files were produced.')
    else:
        log.warning('\n  Build may have failed; artifact not found.')


# ------------------------------------------------------------------------------
# Argument parser.
# ------------------------------------------------------------------------------

def build_arg_parser():
    """
    :return: ArgumentParser
    """
    parser = ArgumentParser(
        prog='pyside_android_builder',
        formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PySide6 -> Android APK Builder
            ==============================
            Full automation of the pyside6-android-deploy pipeline.
        """),
        epilog=dedent("""\
            Examples
            --------
            # Basic build (aarch64 debug APK):
              python pyside_android_builder.py --project-dir ./myapp --arch aarch64

            # Release AAB (signed later with Gradle):
              python pyside_android_builder.py --project-dir ./myapp --mode release

            # Download environment only (no build):
              python pyside_android_builder.py --project-dir ./myapp --only-setup-env

            # Use existing wheels + custom NDK/SDK:
              python pyside_android_builder.py --project-dir ./myapp \\
                  --wheel-pyside /opt/wheels/PySide6-6.10.2-...-android_aarch64.whl \\
                  --wheel-shiboken /opt/wheels/shiboken6-6.10.2-...-android_aarch64.whl \\
                  --ndk-path /opt/ndk/android-ndk-r27c \\
                  --sdk-path /opt/android-sdk

            Common Errors & Fixes
            ---------------------
            * "RuntimeError: You are including a lot of QML files from a local venv"
              -> Move your venv OUTSIDE the project directory.
            * "C compiler cannot create executables"
              -> Lower the Android API level or check your NDK version.
            * "ModuleNotFoundError: No module named <x>"
              -> Add the missing package to buildozer.spec under 'requirements'.
            * Architecture mismatch errors
              -> Ensure each third-party wheel has an Android build for your target arch.
        """),
    )
    # Paths: use str instead of pathlib.Path – resolved manually in main()
    parser.add_argument(
        '--project-dir', required=True, type=str, help='Path to your PySide6 project directory (must contain main.py).')
    parser.add_argument(
        '--app-name', type=str, default=None, help='Application name. Defaults to the project directory name.')
    parser.add_argument(
        '--arch', choices=list(ARCH_MAP), default='aarch64', help='Target Android CPU architecture (default: aarch64).')
    parser.add_argument(
        '--pyside-version', default=PYSIDE_VERSION, help='PySide6 version to use (default: {}).'.format(PYSIDE_VERSION))
    parser.add_argument('--python-version', default=PYTHON_VERSION, choices=['3.10', '3.11'],
                        help='Python version for Android wheels (default: {}).'.format(PYTHON_VERSION))
    parser.add_argument('--mode', choices=['debug', 'release'], default='debug',
                        help='Build mode: "debug" produces .apk, "release" produces .aab (default: debug).')
    # Pre-downloaded asset paths.
    parser.add_argument('--ndk-path', type=str, default=None, help='Path to Android NDK root. Auto-detected if absent.')
    parser.add_argument('--sdk-path', type=str, default=None, help='Path to Android SDK root. Auto-detected if absent.')
    parser.add_argument('--wheel-pyside', type=str, default=None,
                        help='Path to the PySide6 Android wheel (*.whl). Downloaded if absent.')
    parser.add_argument('--wheel-shiboken', type=str, default=None,
                        help='Path to the Shiboken6 Android wheel (*.whl). Downloaded if absent.')
    # Control flags.
    parser.add_argument('--only-setup-env', action='store_true',
                        help='Only set up the virtual environment + SDK/NDK; skip building.')
    parser.add_argument('--install-apk', action='store_true',
                        help='Install the resulting APK on the first ADB-connected device.')
    parser.add_argument('--venv-dir', type=str, default=None,
                        help='Path for the build-tooling virtual environment. MUST be outside the '
                             'project directory. Defaults to a sibling of the project. Pinning '
                             'this in CI lets you cache the venv across runs.')
    parser.add_argument('--keep-build-files', action='store_true',
                        help='Retain intermediate buildozer / Gradle files after the build.')
    parser.add_argument('--dry-run', action='store_true', help='Print commands without executing them.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose output.')
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
    # realpath is the equivalent of Path.resolve()
    project_dir = realpath(args.project_dir)
    if not isdir(project_dir):
        log.error('Project directory not found: %s', project_dir)
        return 1
    # basename(normpath(...)) correctly handles trailing slashes.
    app_name = args.app_name or basename(normpath(project_dir))
    # -- Build config --------------------------------------------------------
    cfg = BuildConfig(
        project_dir=project_dir,
        app_name=app_name,
        arch=args.arch,
        pyside_version=args.pyside_version,
        python_version=args.python_version,
        ndk_path=args.ndk_path,
        sdk_path=args.sdk_path,
        wheel_pyside=args.wheel_pyside,
        wheel_shiboken=args.wheel_shiboken,
        mode=args.mode,
        verbose=args.verbose,
        dry_run=args.dry_run,
        keep_build_files=args.keep_build_files,
        install_apk=args.install_apk,
        venv_dir=args.venv_dir)
    artifact = None  # type: str | None
    try:
        # -- Pipeline --------------------------------------------------------
        preflight_checks(cfg)
        setup_virtualenv(cfg)
        setup_android_sdk_ndk(cfg)
        download_wheels(cfg)
        if not args.only_setup_env:
            artifact = build_apk(cfg)
            if args.install_apk and artifact:
                install_via_adb(cfg, artifact)
        print_summary(cfg, artifact)
        return 0
    except (EnvironmentError, FileNotFoundError, ValueError) as exc:
        log.error('Configuration error: %s', exc)
        return 2
    except RuntimeError as exc:
        log.error('Build error: %s', exc)
        return 3
    except KeyboardInterrupt:
        log.warning('Interrupted by user.')
        return 130


if __name__ == '__main__':
    exit(main())
