#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pyside6_ios_builder.py
======================
A professional, fully-automated build pipeline for compiling PySide6 applications
as native iOS apps using the patrickkidd/pyside6-ios architecture.

Architecture overview:
---------------------
PySide6 on iOS is blocked upstream (PYSIDE-2352) because Qt 6 ships only
*static* libraries for iOS.  When each PySide6 Python extension absorbs those
static objects into its own dynamic .so, Qt symbols duplicate across images --
a hard linker error.

The solution (due to Patrick Kidd) is:
  1. Merge every Qt static lib into one dynamic framework: QtRuntime.framework
     (N_PEXT "hidden" bits must be cleared first so symbols are re-exported).
  2. Cross-compile PySide6 modules (QtCore, QtGui, QtWidgets, ...) as *static*
     libraries linked into the host executable.
  3. Register each module via PyImport_AppendInittab so CPython treats them as
     built-ins -- no dynamic loading needed.
  4. The host app is an ObjC++ main.mm that owns UIApplicationMain and hands
     control to Qt's QIOSEventDispatcher.

References:
----------
  - https://github.com/patrickkidd/pyside6-ios
  - https://forum.qt.io/topic/161694/is-building-for-ios-supported
  - PYSIDE-2352  https://bugreports.qt.io/browse/PYSIDE-2352
  - QTBUG-85974  https://bugreports.qt.io/browse/QTBUG-85974
  - PEP 730      https://peps.python.org/pep-0730/

Requirements:
------------
  - macOS with Apple Silicon (arm64)
  - Xcode 16+ installed at /Applications/Xcode.app
  - uv  (https://docs.astral.sh/uv/)
  - Internet access (downloads Qt, Python framework, PySide6 sources)

Usage:
-----
  # Full build -> deploy to connected iPhone
  python pyside6_ios_builder.py --app  myapp/ --toml myapp/pyside6-ios.toml
  # Bootstrap only (env + Qt + Python framework + PySide sources)
  python pyside6_ios_builder.py --bootstrap
  # Build QtRuntime.framework only
  python pyside6_ios_builder.py --build-qtruntime
  # Cross-compile specific PySide6 modules only
  python pyside6_ios_builder.py --build-modules QtCore QtGui QtWidgets
  # Check all prerequisites
  python pyside6_ios_builder.py --check-deps

Python 2/3 compatibility notes:
-------------------------------
  - No pathlib -- all paths use os.path, glob, fnmatch, and io.
  - subprocess.run shimmed for Python 2 via Popen.
  - urllib.request / urllib handled via try/except.
  - shutil.which shimmed for Python 2.
  - typing is optional (try/except); annotations removed from signatures.
  - nonlocal replaced with mutable-container pattern.
  - print(..., flush=True) replaced with explicit sys.stdout.flush().
  - Keyword-only arguments (*) removed; all args positional/keyword.
"""
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from os.path import exists, dirname, basename, abspath, join
from logging import basicConfig, INFO, getLogger, DEBUG
from os import environ, rename, utime, getcwd
from struct import unpack_from, error
from platform import system, machine
from sys import stdout, exit, path
from textwrap import dedent
from glob import glob
from re import search
import tarfile

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import getCurrentExecutable, getUVExecutable, getGitExecutable, getCmakeExecutable, \
        getXcrunExecutable, getXcodeSelectExecutable, getXcodebuildExecutable
    from .build_utils import run, _makedirs, _rglob, _write_text, urlretrieve
except:
    from builders import getCurrentExecutable, getUVExecutable, getGitExecutable, getCmakeExecutable, \
        getXcrunExecutable, getXcodeSelectExecutable, getXcodebuildExecutable
    from build_utils import run, _makedirs, _rglob, _write_text, urlretrieve


def _touch(pth):
    """
    Create an empty marker file at *path* (or update mtime if it exists).
    Equivalent to Path.touch().
    :param pth: str
    :return:
    """
    with open(pth, 'a'):
        utime(pth, None)


def _read_bytes(pth):
    """
    Read and return the entire binary content of *path*.
    Equivalent to Path.read_bytes().
    :param pth: str
    :return: bytes
    """
    with open(pth, 'rb') as fh:
        return fh.read()


def _write_bytes(pth, data):
    """
    Write *data* (bytes) to *path*, overwriting any existing content.
    Equivalent to Path.write_bytes().
    :param pth: str
    :param data: bytes
    :return:
    """
    with open(pth, 'wb') as fh:
        fh.write(data)


def _glob(directory, pattern):
    """
    Non-recursive glob of *directory* for shell-style *pattern*.
    Returns a sorted list of matching full paths.
    Equivalent to Path.glob(pattern).
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    return sorted(glob(join(directory, pattern)))


# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------
LOG_FMT = '%(asctime)s  %(levelname)-8s  %(message)s'  # type: str
basicConfig(level=INFO, format=LOG_FMT, datefmt='%H:%M:%S')
log = getLogger('pyside6-ios')
# ---------------------------------------------------------------------------
# Defaults / constants.
# ---------------------------------------------------------------------------
DEFAULT_QT_VERSION = '6.8.3'  # type: str
DEFAULT_PYSIDE_VERSION = '6.8.3'  # type: str
DEFAULT_PYTHON_VERSION = '3.13'  # type: str
DEFAULT_PYTHON_SUPPORT = '3.13-b13'  # type: str  # BeeWare Python-Apple-support release tag.
PYSIDE6_IOS_REPO = 'https://github.com/patrickkidd/pyside6-ios'  # type: str
PYSIDE_SETUP_REPO = 'https://code.qt.io/pyside/pyside-setup.git'  # type: str
# BeeWare Python-Apple-support asset URL.
# Release tag looks like '3.13-b13'; the *asset filename* uses only the build
# suffix ('b13'), NOT the full tag.  i.e. the real asset is
#   Python-3.13-iOS-support.b13.tar.gz   (under release tag 3.13-b13)
# Using the full tag in the filename 404s.  We therefore template on {build}.
PYTHON_SUPPORT_URL_TPL = (
    'https://github.com/beeware/Python-Apple-support/releases/download/{tag}/Python-{pyver}-iOS-support.{build}.tar.gz')


def _support_build_suffix(tag):
    """
    Derive the asset build suffix (e.g. 'b13') from a Python-Apple-support
    release tag (e.g. '3.13-b13').  The suffix is everything after the last '-'.
    :param tag: str
    :return: str
    """
    return tag.rsplit('-', 1)[-1] if '-' in tag else tag
# Qt modules that can be cross-compiled for iOS with this toolchain.
SUPPORTED_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtNetwork', 'QtQml', 'QtQuick']  # type: list[str]
# N_PEXT mask -- Mach-O private-extern flag that must be cleared for re-export.
N_PEXT = 0x10  # type: int


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


class BuildError(RuntimeError):
    """
    Raised when any pipeline step fails.
    """


def _run(cmd, cwd=None, env=None, capture=False, check=True):
    """
    Run a subprocess, streaming output unless *capture* is True.
    NOTE: The original Python-3-only keyword-only argument separator (*) has
    been removed so that cwd/env/capture/check can be passed positionally on Python 2 as well.
    Always use keyword syntax when calling this function to preserve readability.
    :param cmd:     (list[str]) List of command tokens (str or path-like)
    :param cwd:     (str | None) Working directory (str, optional)
    :param env:     (dict[str, str] | None) Extra environment variables (dict, optional)
    :param capture: (bool) if True, capture and return stdout; default False
    :param check:   (bool) if True, raise BuildError on non-zero exit; default True
    :return:        (str) Stripped stdout string when capture=True, else ''
    """
    display = " ".join(c for c in cmd)
    log.debug("$ %s", display)
    merged_env = {}
    merged_env.update(dict(environ))
    merged_env.update(env or {})
    result = run([c for c in cmd], cwd=cwd, env=merged_env, capture_output=capture, text=True)
    if check and result.returncode != 0:
        if capture:
            log.error('STDOUT:\n%s', result.stdout)
            log.error('STDERR:\n%s', result.stderr)
        raise BuildError('Command failed (exit {}): {}'.format(result.returncode, display))
    return result.stdout.strip() if capture else ''


def which_required(binary):
    """
    Return the full path to *binary* as a str, raising BuildError if not found.
    :param binary: str
    :return: str
    """
    p = getCurrentExecutable(binary)
    if not exists(p):
        raise BuildError("'{}' not found on PATH. Please install it and ensure it is on your PATH.".format(binary))
    return p


def check_macos_arm64():
    """
    Assert that we are running on macOS.  Warn if not Apple Silicon.
    :return: None
    """
    if system() != 'Darwin':
        raise BuildError('iOS builds require macOS. Current platform: {}'.format(system()))
    arch = machine()
    if arch != 'arm64':
        log.warning(
            'pyside6-ios is tested on Apple Silicon (arm64). '
            'Current architecture: %s -- proceeding but builds may fail.', arch)


def download(url, dest):
    """
    Download *url* to *dest* (str path) with a progress indicator.
    :param url:  str -- remote URL
    :param dest: str -- local destination path
    :return: None
    """
    log.info("  down  %s", url)
    log.info("  ->    %s", dest)
    _makedirs(dirname(dest))

    def _progress(block_count, block_size, total_size):
        """
        :param block_count: int
        :param block_size: int
        :param total_size: int
        :return:
        """
        if total_size > 0:
            pct = min(100, block_count * block_size * 100 // total_size)
            # flush=True not available in Python 2's print(); use explicit flush
            print('\r     {:3d}%'.format(pct), end='')
            stdout.flush()

    try:
        urlretrieve(url, dest, _progress)
        print()  # Newline after progress.
    except Exception as exc:
        raise BuildError('Download failed: {}\nURL: {}'.format(exc, url))


# ---------------------------------------------------------------------------
# Step 0 - Dependency check
# ---------------------------------------------------------------------------


def check_dependencies(ctx):
    """
    Verify all prerequisite tools are present before starting the pipeline.
    :param ctx: BuildContext
    :return:
    """
    log.info('========== Dependency check ==========')
    check_macos_arm64()
    # Xcode (not just CLT).
    try:
        xcode_dev = _run([getXcodeSelectExecutable(), '-p'], capture=True)
        if 'Xcode.app' not in xcode_dev:
            log.warning(
                '  xcode-select points to: %s\n'
                '  This should point to Xcode.app, not standalone CLT.\n'
                '  Fix with: sudo xcode-select -s /Applications/Xcode.app/Contents/Developer', xcode_dev)
        else:
            log.info('  Xcode developer dir : %s  :)', xcode_dev)
    except BuildError:
        raise BuildError('Xcode command-line tools not found. Run: xcode-select --install')
    # Xcode version >= 16
    try:
        xcode_ver_raw = _run([getXcodebuildExecutable(), '-version'], capture=True).splitlines()[0]
        log.info("  %s  :)", xcode_ver_raw)
        major = int(search(r'Xcode (\d+)', xcode_ver_raw).group(1))
        if major < 16:
            log.warning('  Xcode 16+ recommended. Found: %s', xcode_ver_raw)
    except Exception:
        log.warning('  Could not determine Xcode version.')
    # uv
    if exists(getUVExecutable()):
        log.info('  uv : %s  :)', getUVExecutable())
    else:
        raise BuildError(
            'uv not found. Install from: https://docs.astral.sh/uv/\n  curl -LsSf https://astral.sh/uv/install.sh | sh')
    # git
    if exists(getGitExecutable()):
        log.info('  git : %s  :)', getGitExecutable())
    else:
        log.warning('  git not found!')
    # cmake (needed for cross-compiling support libs).
    if exists(getCmakeExecutable()):
        log.info('  cmake : %s  :)', getCmakeExecutable())
    else:
        log.warning('  cmake not found -- install via Xcode CLT or brew install cmake')
    # xcrun devicectl (Xcode 15+).
    try:
        _run([getXcrunExecutable(), 'devicectl', '--version'], capture=True)
        log.info('  xcrun devicectl : :)')
    except BuildError:
        log.warning('  xcrun devicectl not available (Xcode 15+ required for device deploy).')
    # Qt iOS SDK.
    qt_ios = ctx.qt_ios_dir
    if qt_ios and exists(qt_ios):
        log.info('  Qt iOS SDK : %s  :)', qt_ios)
    else:
        log.warning('  Qt iOS SDK not found at: %s\n  Run with --bootstrap or set QT_IOS env var.', qt_ios)
    log.info('========== Dependency check complete ==========')


# ---------------------------------------------------------------------------
# Build context -- all paths in one place.
# ---------------------------------------------------------------------------


class BuildContext(object):
    """
    Central store for all resolved paths and versions.
    All path attributes are plain str values -- no pathlib objects.
    """

    def __init__(self, root, qt_version=DEFAULT_QT_VERSION, pyside_version=DEFAULT_PYSIDE_VERSION,
                 python_version=DEFAULT_PYTHON_VERSION, python_support_tag=DEFAULT_PYTHON_SUPPORT,
                 qt_ios_override=None):
        """
        :param root: str
        :param qt_version: str
        :param pyside_version: str
        :param python_version: str
        :param python_support_tag: str
        :param qt_ios_override: object | None
        :return:
        """
        self.root = abspath(root)
        self.qt_version = qt_version
        self.pyside_version = pyside_version
        self.python_version = python_version
        self.python_support_tag = python_support_tag
        self.build_dir = join(self.root, 'build')
        self.venv_dir = join(self.root, '.venv')
        # Qt paths.  Qt lives under the project root (referenced everywhere by
        # the absolute QT_IOS path, so its location is independent of the tool).
        self.qt_lib_dir = join(self.build_dir, 'Qt-{}'.format(qt_version))
        if qt_ios_override:
            self.qt_ios_dir = qt_ios_override
        elif environ.get('QT_IOS'):
            self.qt_ios_dir = environ.get('QT_IOS')
        else:
            self.qt_ios_dir = join(self.qt_lib_dir, qt_version, 'ios')
        self.qt_macos_dir = join(self.qt_lib_dir, qt_version, 'macos')
        # ------------------------------------------------------------------
        # pyside6-ios tool clone.  This directory IS "P6IOS_ROOT" as far as the
        # upstream build scripts (scripts/env.sh) and the pyside6-ios CLI
        # (src/pyside6_ios/config.py) are concerned.  Both hard-code their inputs
        # and outputs relative to it:
        #     <tool>/build/pyside-setup/sources/{pyside6,shiboken6}
        #     <tool>/build/python/Python.xcframework  (+ VERSIONS sibling)
        #     <tool>/build/QtRuntime.framework
        #     <tool>/build/pyside6-ios-static/libPySide6_<Module>.a
        #     <tool>/build/{libshiboken,libpyside,libpysideqml,shiboken}-ios
        # so every build artifact below must live under it -- NOT under the
        # project root -- or the scripts/CLI will not find them.
        # ------------------------------------------------------------------
        self.tool_dir = join(self.build_dir, 'pyside6-ios-tool')
        self.tool_build = join(self.tool_dir, 'build')
        # Python iOS framework (extracted in-place; keep VERSIONS as a sibling).
        self.python_dir = join(self.tool_build, 'python')
        self.python_framework = join(self.python_dir, 'Python.xcframework')
        # PySide6 sources (cloned so that sources/pyside6 + sources/shiboken6 exist).
        self.pyside_src = join(self.tool_build, 'pyside-setup')
        # Build outputs produced by the upstream scripts.
        self.qtruntime_framework = join(self.tool_build, 'QtRuntime.framework')
        self.pyside6_static_dir = join(self.tool_build, 'pyside6-ios-static')
        # Back-compat aliases (older code/messages referenced these names).
        self.support_libs_dir = self.tool_build
        self.pyside6_modules_dir = self.pyside6_static_dir

    @property
    def uv(self):
        """
        :return: str -- path to the uv executable.
        """
        return getUVExecutable()

    @property
    def python(self):
        """
        Path to the Python interpreter inside the venv.
        :return: str
        """
        return join(self.venv_dir, 'bin', 'python')

    def env_with_qt(self):
        """
        Return an environment dict for the upstream build scripts.

        Two things matter here:
          1. Qt location vars (QT_IOS / QT_MACOS) -- scripts/env.sh auto-detects
             versions from these.
          2. venv activation -- the upstream scripts call bare ``python3`` (for
             ``import shiboken6_generator``) and ``uv run python`` (globalize
             step).  They are written to be run from an *activated* venv (see the
             project README).  We replicate activation by exporting VIRTUAL_ENV
             and prepending ``<venv>/bin`` to PATH so ``python3`` resolves to the
             venv interpreter (which has shiboken6-generator installed) and
             ``uv`` reuses the active environment instead of creating a new one.
        :return: dict[str, str]
        """
        env = dict(environ)
        venv_bin = join(self.venv_dir, 'bin')
        env['VIRTUAL_ENV'] = self.venv_dir
        env['PATH'] = venv_bin + ':' + env.get('PATH', '')
        # Let ``uv run`` use the already-active venv rather than syncing/creating one.
        env['UV_PROJECT_ENVIRONMENT'] = self.venv_dir
        env.update({'QT_IOS': self.qt_ios_dir, 'QT_MACOS': self.qt_macos_dir,
                    'PYSIDE_VERSION': self.pyside_version, 'QT_VERSION': self.qt_version})
        return env

    def summary(self):
        """
        :return:
        """
        log.info('  root            : %s', self.root)
        log.info('  qt_version      : %s', self.qt_version)
        log.info('  pyside_version  : %s', self.pyside_version)
        log.info('  python_version  : %s', self.python_version)
        log.info('  qt_ios_dir      : %s', self.qt_ios_dir)
        log.info('  pyside_src      : %s', self.pyside_src)


# ---------------------------------------------------------------------------
# Step 1 - Python virtual environment
# ---------------------------------------------------------------------------


def setup_venv(ctx):
    """
    Create a uv-managed venv and install host-side Python packages.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Setting up Python venv ==========')
    # uv runs with cwd=ctx.root below; the directory must exist first.
    _makedirs(ctx.root)
    if not exists(ctx.venv_dir):
        _run([ctx.uv, 'venv', ctx.venv_dir, '--python', ctx.python_version], cwd=ctx.root)
        log.info('  venv created at: %s', ctx.venv_dir)
    else:
        log.info('  venv exists: %s', ctx.venv_dir)
    # Host-side packages needed at build time (NOT cross-compiled).
    host_packages = ['aqtinstall', 'shiboken6-generator=={}'.format(ctx.pyside_version), 'cmake', 'ninja']
    log.info('  Installing host packages: %s', ', '.join(host_packages))
    _run([ctx.uv, 'pip', 'install', '--python', ctx.python] + host_packages, cwd=ctx.root)
    log.info('  Host packages installed.')


# ---------------------------------------------------------------------------
# Step 2 - Install Qt (iOS + macOS SDKs via aqtinstall).
# ---------------------------------------------------------------------------


def install_qt(ctx):
    """
    Use aqtinstall to download Qt iOS and macOS SDKs into the build dir.
    Skipped if QT_IOS already points to a valid installation.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Installing Qt %s ==========', ctx.qt_version)
    qt_ios_qmake = join(ctx.qt_ios_dir, 'bin', 'qmake')
    if exists(qt_ios_qmake):
        log.info('  Qt iOS SDK already present: %s  -- skipping download.', ctx.qt_ios_dir)
        return
    _makedirs(ctx.qt_lib_dir)
    aqt = join(ctx.venv_dir, 'bin', 'aqt')
    log.info('  Downloading Qt iOS SDK (~1.5 GB) ...')
    _run([aqt, 'install-qt', 'mac', 'ios', ctx.qt_version, '--outputdir', ctx.qt_lib_dir])
    log.info('  Downloading Qt macOS SDK (~800 MB) ...')
    _run([aqt, 'install-qt', 'mac', 'desktop', ctx.qt_version, '--outputdir', ctx.qt_lib_dir])
    log.info('  Qt installation complete.')


# ---------------------------------------------------------------------------
# Step 3 - CPython iOS framework (BeeWare Python-Apple-support, PEP 730)
# ---------------------------------------------------------------------------


def install_python_framework(ctx):
    """
    Download the pre-built CPython iOS XCFramework from BeeWare's
    Python-Apple-support releases (implements PEP 730).
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Installing CPython %s iOS framework ==========', ctx.python_version)
    if exists(ctx.python_framework) and exists(join(ctx.python_dir, 'VERSIONS')):
        log.info('  Python.xcframework already present -- skipping.')
        return
    _makedirs(ctx.python_dir)
    tag = ctx.python_support_tag
    pyver = ctx.python_version
    build = _support_build_suffix(tag)
    url = PYTHON_SUPPORT_URL_TPL.format(tag=tag, pyver=pyver, build=build)
    tarball = join(ctx.python_dir, 'Python-{}-iOS-support.{}.tar.gz'.format(pyver, build))
    if not exists(tarball):
        download(url, tarball)
    log.info('  Extracting Python iOS support ...')
    # The tarball unpacks its contents (Python.xcframework, VERSIONS, ...)
    # directly into the target dir, so extract in-place: the scripts and the
    # CLI both read the VERSIONS file as a sibling of Python.xcframework.
    with tarfile.open(tarball, 'r:gz') as tf:
        tf.extractall(ctx.python_dir)
    if not exists(ctx.python_framework):
        # Some mirrors nest the framework one level deeper; relocate it.
        candidates = _rglob(ctx.python_dir, 'Python.xcframework')
        if not candidates:
            raise BuildError(
                'Python.xcframework not found after extraction. Check tarball contents in {}'.format(ctx.python_dir))
        rename(candidates[0], ctx.python_framework)
    if not exists(join(ctx.python_dir, 'VERSIONS')):
        log.warning('  VERSIONS file not found next to Python.xcframework -- '
                    'sbkversion.h generation may fail.')
    log.info('  Python.xcframework: %s  :)', ctx.python_framework)


# ---------------------------------------------------------------------------
# Step 4 - Clone PySide6 source tree
# ---------------------------------------------------------------------------


def clone_pyside_sources(ctx):
    """
    Clone the pyside-setup source tree at the version matching PYSIDE_VERSION.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Cloning PySide6 sources (v%s) ==========', ctx.pyside_version)
    if exists(ctx.pyside_src):
        log.info('  pyside-setup already cloned: %s  -- skipping.', ctx.pyside_src)
        return
    which_required('git')
    _makedirs(dirname(ctx.pyside_src))
    _run([getGitExecutable(), 'clone', '--branch', 'v{}'.format(ctx.pyside_version), '--depth', '1', PYSIDE_SETUP_REPO,
          ctx.pyside_src])
    log.info('  PySide6 sources cloned to: %s', ctx.pyside_src)


# ---------------------------------------------------------------------------
# Step 5 - Install pyside6-ios build tool
# ---------------------------------------------------------------------------


def install_pyside6_ios_tool(ctx):
    """
    Clone patrickkidd/pyside6-ios and install the pyside6-ios CLI into the venv.
    The tool generates Xcode projects from a pyside6-ios.toml config.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Installing pyside6-ios build tool ==========')
    if exists(ctx.tool_dir):
        log.info('  pyside6-ios repo already cloned -- reinstalling tool ...')
    else:
        which_required('git')
        _makedirs(dirname(ctx.tool_dir))
        _run([getGitExecutable(), 'clone', '--depth', '1', PYSIDE6_IOS_REPO, ctx.tool_dir])
    _run([ctx.uv, 'pip', 'install', '--python', ctx.python, '-e', ctx.tool_dir], cwd=ctx.tool_dir)
    log.info('  pyside6-ios tool installed.')


# ---------------------------------------------------------------------------
# Step 6 - Globalize Mach-O N_PEXT symbols  (Python re-implementation)
# ---------------------------------------------------------------------------


def _globalize_npext_in_file(pth):
    """
    Clear the N_PEXT (private-extern) bit on all defined symbols in a Mach-O
    static library (.a) or object file (.o).
    Qt builds iOS static libs with -fvisibility=hidden, which sets N_PEXT on
    every symbol.  Before merging those libs into a dynamic framework we must
    clear the bit -- otherwise the dynamic linker silently drops the symbols and
    PySide6 type resolution fails at runtime.
    Returns the number of symbols modified.
    :param pth: str -- path to the .a or .o file
    :return: int
    """
    MH_MAGIC = 0xFEEDFACE  # type: int
    MH_MAGIC_64 = 0xFEEDFACF  # type: int
    MH_CIGAM = 0xCEFAEDFE  # type: int
    MH_CIGAM_64 = 0xCFFAEDFE  # type: int
    FAT_MAGIC = 0xCAFEBABE  # type: int
    AR_MAGIC = b'!<arch>\n'
    # Use a single-element list as a mutable counter so the nested function
    # _patch_macho can increment it without 'nonlocal' (Python 2 has no nonlocal).
    _modified = [0]

    def _patch_macho(data, offset=0):
        """
        Patch N_PEXT bits inside a single Mach-O image starting at *offset*
        within the bytearray *data*.
        :param data:   bytearray
        :param offset: int
        :return: int  (current modified count)
        """
        magic = unpack_from('<I', data, offset)[0]
        if magic not in (MH_MAGIC, MH_MAGIC_64, MH_CIGAM, MH_CIGAM_64):
            return _modified[0]
        is_64 = magic in (MH_MAGIC_64, MH_CIGAM_64)
        is_swap = magic in (MH_CIGAM, MH_CIGAM_64)
        endian = '>' if is_swap else '<'
        # Read mach_header
        hdr_size = 32 if is_64 else 28
        _, cpu_type, cpu_sub, filetype, ncmds, sizeofcmds, flags = unpack_from("{}IIIIIII".format(endian), data, offset)
        if is_64:
            hdr_size = 32
        cmd_offset = offset + hdr_size
        for _ in range(ncmds):
            cmd, cmdsize = unpack_from('{}II'.format(endian), data, cmd_offset)
            LC_SYMTAB = 0x2
            if cmd == LC_SYMTAB:
                symoff, nsyms, stroff, strsize = unpack_from('{}IIII'.format(endian), data, cmd_offset + 8)
                nlist_size = 16 if is_64 else 12
                for i in range(nsyms):
                    sym_off = offset + symoff + i * nlist_size
                    # struct nlist_64: n_strx(4) n_type(1) n_sect(1) n_desc(2) n_value(8)
                    # struct nlist:    n_strx(4) n_type(1) n_sect(1) n_desc(2) n_value(4)
                    n_type = data[sym_off + 4]
                    # N_PEXT = 0x10; only meaningful for defined symbols (N_TYPE != N_UNDF=0)
                    if (n_type & 0x0E) != 0 and (n_type & N_PEXT):
                        data[sym_off + 4] = n_type & ~N_PEXT
                        _modified[0] += 1
            cmd_offset += cmdsize
        return _modified[0]

    raw = bytearray(_read_bytes(pth))
    if raw[:8] == AR_MAGIC:
        # Static library (.a): iterate ar members.
        pos = 8
        while pos < len(raw):
            if pos + 60 > len(raw):
                break
            ar_header = raw[pos:pos + 60]
            try:
                ar_size = int(ar_header[48:58].decode().strip())
            except ValueError:
                break
            member_data = raw[pos + 60: pos + 60 + ar_size]
            if len(member_data) >= 4:
                m = unpack_from('<I', member_data)[0]
                if m in (MH_MAGIC, MH_MAGIC_64, MH_CIGAM, MH_CIGAM_64):
                    member_ba = bytearray(member_data)
                    _patch_macho(member_ba)
                    raw[pos + 60: pos + 60 + ar_size] = member_ba
            pos += 60 + ar_size + (ar_size % 2)
    else:
        try:
            m = unpack_from('<I', raw)[0]
        except error:
            return 0
        if m in (MH_MAGIC, MH_MAGIC_64, MH_CIGAM, MH_CIGAM_64):
            _patch_macho(raw)
        elif m == FAT_MAGIC:
            nfat = unpack_from('>I', raw, 4)[0]
            for i in range(nfat):
                off, sz = unpack_from('>II', raw, 8 + i * 20)[0:2]
                sub = bytearray(raw[off:off + sz])
                _patch_macho(sub)
                raw[off:off + sz] = sub
    if _modified[0]:
        _write_bytes(pth, bytes(raw))
    return _modified[0]


def globalize_qt_symbols(ctx):
    """
    Walk every static library under QT_IOS and clear the N_PEXT hidden bit.
    This must run before build_qtruntime so re-exported symbols are visible.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Globalizing Qt symbol visibility ==========')
    qt_lib = join(ctx.qt_ios_dir, 'lib')
    if not exists(qt_lib):
        raise BuildError('Qt iOS lib dir not found: {}'.format(qt_lib))
    total_files = 0
    total_symbols = 0
    for lib in _glob(qt_lib, '*.a'):
        count = _globalize_npext_in_file(lib)
        if count:
            log.debug('  %s: %d symbols globalized', basename(lib), count)
            total_symbols += count
            total_files += 1
    log.info('  Globalized %d symbols across %d static libraries.', total_symbols, total_files)


# ---------------------------------------------------------------------------
# Step 7 - Build QtRuntime.framework
# ---------------------------------------------------------------------------


def build_qtruntime(ctx):
    """
    Merge all Qt static libs for iOS into a single dynamic framework
    (QtRuntime.framework).  This is the architectural key fix for PYSIDE-2352:
    Qt code lives in exactly one place, eliminating duplicate symbol errors.
    Delegates to the pyside6-ios repo's build_qtruntime.sh script after setting up the correct environment.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Building QtRuntime.framework ==========')
    if exists(ctx.qtruntime_framework):
        log.info('  QtRuntime.framework already built -- skipping.')
        return
    script = join(ctx.tool_dir, 'scripts', 'build_qtruntime.sh')
    if not exists(script):
        raise BuildError('build_qtruntime.sh not found at: {}'.format(script))
    log.info('  This merges all Qt static libs -- ~5 min first run.')
    # Upstream script writes to ``build/QtRuntime.framework`` relative to its CWD
    # and runs ``uv run python scripts/globalize_symbols.py`` (relative path), so
    # it MUST run from the tool root.  Pass --qt-ios explicitly (env QT_IOS is
    # also honoured but the flag is unambiguous).
    _run(['bash', script, '--qt-ios', ctx.qt_ios_dir], cwd=ctx.tool_dir, env=ctx.env_with_qt())
    if not exists(ctx.qtruntime_framework):
        raise BuildError('QtRuntime.framework was not created at {}. '
                         'Check output above for linker errors.'.format(ctx.qtruntime_framework))
    log.info('  QtRuntime.framework built: %s  :)', ctx.qtruntime_framework)


# ---------------------------------------------------------------------------
# Step 8 - Cross-compile support libraries.
# ---------------------------------------------------------------------------


def generate_shiboken_embedding(ctx):
    """
    Generate the shiboken6 ``embed`` headers that libshiboken's
    ``signature/signature_globals.cpp`` ``#include``s:
    ``embed/signature_inc.h`` and ``embed/signature_bootstrap_inc.h``.

    In a normal PySide6 build these are produced by a CMake custom command that
    runs shiboken6's own ``embedding_generator.py``.  The minimal pyside6-ios
    ``build_support_libs.sh`` compiles the sources directly and never runs that
    CMake step, so the headers are missing and the libshiboken6 compile dies with
    ``fatal error: 'embed/signature_inc.h' file not found``.

    We run the upstream generator straight out of the cloned ``pyside-setup`` (so
    it matches the exact version) to emit both headers into
    ``<pyside-setup>/sources/shiboken6/libshiboken/embed/`` -- which is on the
    compiler include path (``-I $LIBSHIBOKEN_SRC``).  ``--use-pyc no`` embeds the
    Python *source* (architecture-independent, host-Python-agnostic), matching the
    ``-DSHIBOKEN_NO_EMBEDDING_PYC`` the support-libs build compiles with.

    :param ctx: BuildContext
    :return: None
    """
    embed_dir = join(ctx.pyside_src, 'sources', 'shiboken6', 'libshiboken', 'embed')
    generator = join(embed_dir, 'embedding_generator.py')
    inc = join(embed_dir, 'signature_inc.h')
    boot_inc = join(embed_dir, 'signature_bootstrap_inc.h')
    if exists(inc) and exists(boot_inc):
        log.info('  shiboken embed headers already present -- skipping.')
        return
    if not exists(generator):
        raise BuildError(
            'shiboken embedding_generator.py not found at: {}\n'
            '  (expected inside the cloned pyside-setup sources).'.format(generator))
    log.info('  Generating shiboken embed headers (signature_inc.h, signature_bootstrap_inc.h)...')
    # The generator chdir's into --cmake-dir and writes both headers there.  It
    # also imports build_scripts/utils.py from the pyside-setup root and zips the
    # shibokensupport sources, so it must run from inside the cloned tree.
    _run([ctx.python, '-E', generator, '--cmake-dir', embed_dir, '--use-pyc', 'no', '--quiet'],
         cwd=embed_dir, env=ctx.env_with_qt())
    for h in (inc, boot_inc):
        if not exists(h):
            raise BuildError('shiboken embed header was not generated: {}'.format(h))
    log.info('  shiboken embed headers generated under: %s', embed_dir)


def _find_moc(ctx):
    """
    Locate the host Qt ``moc`` binary.  install_qt fetches the desktop (macOS) Qt
    alongside the iOS one specifically so host tools like moc are available.

    :param ctx: BuildContext
    :return: str -- path to moc
    """
    candidates = [join(ctx.qt_macos_dir, 'libexec', 'moc'),
                  join(ctx.qt_macos_dir, 'bin', 'moc')]
    for c in candidates:
        if exists(c):
            return c
    for base in (ctx.qt_macos_dir, ctx.qt_ios_dir):
        hits = sorted(glob(join(base, '**', 'moc'), recursive=True))
        if hits:
            return hits[0]
    raise BuildError(
        'Qt moc not found under {} or {}.\n'
        '  moc is required to generate the .moc sources that libpyside includes; '
        'install_qt should have fetched the desktop Qt that ships it.'
        .format(ctx.qt_macos_dir, ctx.qt_ios_dir))


def generate_moc_files(ctx):
    """
    Generate Qt meta-object (``moc``) output for any libpyside/libpysideqml
    source that ``#include``s a ``"<name>.moc"``.

    For example libpyside/dynamicslot.cpp defines an inline ``QObject`` subclass
    guarded by ``Q_OBJECT`` and ends with ``#include "dynamicslot.moc"``.  A normal
    PySide6 build runs moc through CMake/AUTOMOC; the minimal build_support_libs.sh
    does not, so the .moc is missing and the compile dies with
    ``'dynamicslot.moc' file not found``.

    We run the host Qt's moc on each such source and write ``<name>.moc`` next to
    it (the ``#include`` uses quotes, so it resolves against the source dir first).

    :param ctx: BuildContext
    :return: None
    """
    import re
    moc = _find_moc(ctx)
    py6 = join(ctx.pyside_src, 'sources', 'pyside6')
    libpyside = join(py6, 'libpyside')
    libpysideqml = join(py6, 'libpysideqml')
    libshiboken = join(ctx.pyside_src, 'sources', 'shiboken6', 'libshiboken')
    py_headers = join(ctx.python_framework, 'ios-arm64', 'Python.framework', 'Headers')

    def _qt_fw(mod):
        base = join(ctx.qt_ios_dir, 'lib', mod + '.framework', 'Headers')
        return ['-I', base, '-I', join(base, ctx.qt_version), '-I', join(base, ctx.qt_version, mod)]

    # Mirror the support-libs compile so moc preprocesses the sources the same way.
    moc_flags = ['-DQT_LEAN_HEADERS=1', '-DQT_NO_DEBUG', '-DSHIBOKEN_NO_EMBEDDING_PYC',
                 '-DNDEBUG', '-DBUILD_LIBPYSIDE',
                 '-F', join(ctx.qt_ios_dir, 'lib'),
                 '-I', join(ctx.qt_ios_dir, 'include'),
                 '-I', join(ctx.qt_ios_dir, 'mkspecs', 'macx-ios-clang'),
                 '-I', libshiboken, '-I', py_headers]
    moc_flags += _qt_fw('QtCore')

    pat = re.compile(r'#include\s+"([A-Za-z0-9_]+)\.moc"')
    total = 0
    log.info('  Generating moc sources (host moc: %s)...', moc)
    for srcdir in (libpyside, libpysideqml):
        if not exists(srcdir):
            continue
        for cpp in sorted(glob(join(srcdir, '*.cpp'))):
            try:
                with open(cpp, 'r', encoding='utf-8', errors='ignore') as fh:
                    text = fh.read()
            except OSError:
                continue
            for base in pat.findall(text):
                out = join(srcdir, base + '.moc')
                if exists(out):
                    continue
                _run([moc] + moc_flags + ['-I', srcdir, cpp, '-o', out],
                     cwd=srcdir, env=ctx.env_with_qt())
                if not exists(out):
                    raise BuildError('moc did not produce: {}'.format(out))
                log.info('    moc: %s -> %s', basename(cpp), basename(out))
                total += 1
    log.info('  Generated %d moc file(s).', total)


def build_support_libs(ctx):
    """
    Cross-compile libshiboken6, libpyside6, and libpysideqml for arm64-iOS.
    These are static libraries linked directly into the host executable.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Building support libraries ==========')
    shiboken_a = join(ctx.tool_build, 'libshiboken-ios', 'libshiboken6.a')
    pyside_a = join(ctx.tool_build, 'libpyside-ios', 'libpyside6.a')
    pysideqml_a = join(ctx.tool_build, 'libpysideqml-ios', 'libpysideqml.a')
    if exists(shiboken_a) and exists(pyside_a) and exists(pysideqml_a):
        log.info('  Support libs already built -- skipping.')
        return
    script = join(ctx.tool_dir, 'scripts', 'build_support_libs.sh')
    if not exists(script):
        raise BuildError('build_support_libs.sh not found at: {}'.format(script))
    log.info('  Cross-compiling libshiboken6, libpyside6, libpysideqml ...  (~2 min)')
    # libshiboken/signature/signature_globals.cpp #includes generated embed
    # headers; produce them first (the minimal upstream script never runs the
    # shiboken CMake step that normally generates these).
    generate_shiboken_embedding(ctx)
    # libpyside sources (e.g. dynamicslot.cpp) #include moc output; the minimal
    # script has no AUTOMOC, so generate the .moc files first.
    generate_moc_files(ctx)
    # The upstream script sources scripts/env.sh, which derives every input and
    # output path relative to the tool root (P6IOS_ROOT).  It does NOT read any
    # SUPPORT_LIBS_OUTDIR / PYSIDE_SRC overrides, so we just run it in-place with
    # QT vars + venv activation.
    _run(['bash', script], cwd=ctx.tool_dir, env=ctx.env_with_qt())
    for lib in (shiboken_a, pyside_a, pysideqml_a):
        if not exists(lib):
            raise BuildError('Expected support lib not produced: {}'.format(lib))
    log.info('  Support libs built under: %s  :)', ctx.tool_build)


# ---------------------------------------------------------------------------
# Step 9 - Cross-compile PySide6 modules.
# ---------------------------------------------------------------------------


def build_pyside6_modules(ctx, modules=None):
    """
    Cross-compile each PySide6 Python extension module (QtCore, QtGui, etc.)
    as a *static* library for arm64-iOS via shiboken6 code generation +
    clang cross-compilation.
    Key details from the pyside6-ios report:
    - PyModuleDef.m_name must be 'PySide6.QtCore' (not 'QtCore') for type
      resolution to work across modules.
    - shouldLazyLoad() in the import machinery needs a 'Qt' prefix check.
    - QProcess is unavailable on iOS (QT_CONFIG(process)==false); generated
      wrappers and headers are patched accordingly.
    :param ctx: BuildContext
    :param modules: (list[str] | None) list of str or None  (defaults to SUPPORTED_MODULES)
    :return:
    """
    if modules is None:
        modules = SUPPORTED_MODULES
    log.info('========== Building PySide6 modules: %s ==========', ", ".join(modules))
    script = join(ctx.tool_dir, 'scripts', 'build_pyside6_module.sh')
    if not exists(script):
        raise BuildError('build_pyside6_module.sh not found at: {}'.format(script))
    env = ctx.env_with_qt()
    for mod in modules:
        out_lib = join(ctx.pyside6_static_dir, 'libPySide6_{}.a'.format(mod))
        if exists(out_lib):
            log.info('  %s already built -- skipping.', mod)
            continue
        log.info('  Building %s ...  (~2 min each)', mod)
        # env.sh inside the script hard-codes output to
        # <tool>/build/pyside6-ios-static/libPySide6_<mod>.a, so run in-place.
        _run(['bash', script, mod], cwd=ctx.tool_dir, env=env)
        if not exists(out_lib):
            raise BuildError('Module {} did not produce {}'.format(mod, out_lib))
        log.info('  %s  :)', mod)
    log.info('  All PySide6 modules built.')


# ---------------------------------------------------------------------------
# Step 10a - Generate the iOS entry shim (scripts/app.py) that runs main.py.
# ---------------------------------------------------------------------------

# This is written verbatim into <app_dir>/scripts/app.py.  It lets this builder
# accept the SAME project layout as the PyQt5/PySide6 *Android* builders in this
# repo (pyqt5_android_builder.py, pyqt5_android_kviktor.py,
# pyqt5_android_plashless.py, pyside6_android_builder.py, ...): a plain main.py
# at the project root that builds its own QApplication and runs the event loop.
#
# On iOS the native host (main.mm) already owns the QApplication and the
# UIApplicationMain run loop, so a desktop main.py cannot run unchanged.  This
# shim bridges the two without touching the user's main.py:
#   * QApplication()/QGuiApplication()/QCoreApplication(...) return the existing
#     native instance instead of constructing (or erroring on) a second one.
#   * .exec()/.exec_() are no-ops (the iOS run loop already pumps events).
#   * sys.exit(...) raised from main() is swallowed.
#   * top-level widgets are retained past main() and shown full-screen.
#
# NOTE: kept as a RAW string so the embedded "\n" escapes are written literally
# into the generated file (and interpreted at runtime there), and so the body's
# literal { } braces need no escaping.
_ENTRY_SHIM_SRC = r'''"""
scripts/app.py -- iOS entry shim (AUTO-GENERATED by pyside6_ios_builder.py).
DO NOT EDIT.  Your real code lives in main.py and is left completely untouched.

Why this file exists
--------------------
Your project uses the standard desktop PySide6/PyQt layout: a main.py at the
project root that builds its own QApplication and runs the event loop, e.g.

    def main():
        app = QApplication(sys.argv)
        ...
        sys.exit(app.exec())

    if __name__ == "__main__":
        main()

That runs as-is on desktop.  On iOS the native host (main.mm) already creates
the QApplication and UIApplicationMain drives the Qt event loop, so this shim
bridges your desktop main.py to the iOS host:

  * QApplication()/QGuiApplication()/QCoreApplication(...) return the existing
    native instance instead of constructing (or erroring on) a second one.
  * .exec()/.exec_() become no-ops -- the iOS run loop already pumps events.
  * sys.exit(...) raised by main() is swallowed (it must not tear down the app).
  * Top-level widgets are kept alive past main() and shown full-screen
    (a plain show() yields a blank screen on iOS).

main.py is then executed unmodified, with __name__ == "__main__".
"""
import os
import sys
import runpy


def _log(msg):
    try:
        os.write(1, (str(msg) + "\n").encode())
    except Exception:
        pass


_log("=" * 60)
_log("pyside6-ios entry shim: starting")
_log("Python %s on %s" % (sys.version.split()[0], sys.platform))

# This file is <bundle>/scripts/app.py; main.py is copied alongside it, and the
# bundled packages live in <bundle>/packages.  Make all of them importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_MAIN_PY = os.path.join(_HERE, "main.py")
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "packages")):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from PySide6 import QtCore, QtGui, QtWidgets

# Capture the real classes before we shadow the constructors in their modules.
_RealQCoreApplication = QtCore.QCoreApplication
_RealQGuiApplication = QtGui.QGuiApplication
_RealQApplication = QtWidgets.QApplication

# Objects that must outlive main() (widgets, QML engines, windows).  Holding a
# Python reference here stops PySide6 from deleting the underlying C++ object
# when main()'s locals go out of scope.
_KEEP_ALIVE = []


def _present_top_level():
    """Retain and full-screen every top-level widget; retain top-level windows."""
    try:
        for w in list(_RealQApplication.topLevelWidgets()):
            if w not in _KEEP_ALIVE:
                _KEEP_ALIVE.append(w)
            try:
                if w.isWindow():
                    w.showFullScreen()
            except Exception as exc:
                _log("  present widget failed: %r" % (exc,))
    except Exception as exc:
        _log("  topLevelWidgets() unavailable: %r" % (exc,))
    try:
        for win in list(_RealQGuiApplication.topLevelWindows()):
            if win not in _KEEP_ALIVE:
                _KEEP_ALIVE.append(win)
    except Exception:
        pass


def _harvest_caller_objects(start_depth):
    """Walk the calling frames and retain every live QObject so that widgets and
    QML engines created as locals in main() survive once main() returns/exits."""
    try:
        frame = sys._getframe(start_depth)
    except Exception:
        frame = None
    while frame is not None:
        try:
            for val in list(frame.f_locals.values()):
                if isinstance(val, QtCore.QObject) and val not in _KEEP_ALIVE:
                    _KEEP_ALIVE.append(val)
        except Exception:
            pass
        frame = frame.f_back
    _present_top_level()


class _AppInstanceProxy(object):
    """Wraps the native application instance, forwarding everything except the
    event-loop entry points, which are no-ops on iOS."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def exec(self, *args, **kwargs):
        _log("app.exec() intercepted -- iOS run loop drives events (no-op)")
        _harvest_caller_objects(2)
        return 0

    exec_ = exec

    def exit(self, *args, **kwargs):
        _log("app.exit() intercepted (no-op on iOS)")
        return None

    def quit(self, *args, **kwargs):
        _log("app.quit() intercepted (no-op on iOS)")
        return None

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_real"), name, value)


class _AppClassProxy(object):
    """Stands in for QApplication/QGuiApplication/QCoreApplication in user code.
    Calling it returns the existing native instance (wrapped); attribute access
    (static methods, enums, .instance()) forwards to the real class."""

    def __init__(self, real_cls):
        object.__setattr__(self, "_real_cls", real_cls)

    def __call__(self, *args, **kwargs):
        inst = _RealQCoreApplication.instance()
        if inst is None:
            # No native instance (e.g. running on desktop) -- construct normally.
            inst = object.__getattribute__(self, "_real_cls")(*args, **kwargs)
        return _AppInstanceProxy(inst)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real_cls"), name)


# Shadow the constructors in their home modules so that
# `from PySide6.QtWidgets import QApplication` (run when main.py is imported)
# resolves to the proxy.
QtWidgets.QApplication = _AppClassProxy(_RealQApplication)
QtGui.QGuiApplication = _AppClassProxy(_RealQGuiApplication)
QtCore.QCoreApplication = _AppClassProxy(_RealQCoreApplication)

_log("native app instance: %r" % (_RealQCoreApplication.instance(),))

if not os.path.exists(_MAIN_PY):
    _log("ERROR: main.py not found next to app.py (%s)" % _MAIN_PY)
else:
    _log("running main.py ...")
    try:
        runpy.run_path(_MAIN_PY, run_name="__main__")
    except SystemExit as exc:
        _log("main.py raised SystemExit(%r) -- ignored on iOS" % (exc.code,))
    except Exception:
        _log("main.py raised an exception:")
        import traceback
        traceback.print_exc()
    finally:
        _present_top_level()
    _log("main.py finished; %d top-level object(s) retained" % len(_KEEP_ALIVE))
'''


def generate_entry_shim(ctx, app_dir):
    """
    Write the iOS entry shim to ``<app_dir>/scripts/app.py``.

    The shim runs the project's ``main.py`` under the iOS native host (see
    ``_ENTRY_SHIM_SRC``).  It is regenerated every time so it always matches the
    builder; the user's ``main.py`` is never read or modified.

    :param ctx:     BuildContext (unused; kept for signature symmetry).
    :param app_dir: str -- application/project directory (contains main.py).
    :return: str -- path to the written scripts/app.py
    """
    scripts_dir = join(app_dir, 'scripts')
    _makedirs(scripts_dir)
    shim_path = join(scripts_dir, 'app.py')
    _write_text(shim_path, _ENTRY_SHIM_SRC)
    log.info("  Generated iOS entry shim: %s (runs main.py)", shim_path)
    return shim_path


# ---------------------------------------------------------------------------
# Step 10b - Generate pyside6-ios.toml config
# ---------------------------------------------------------------------------


def generate_toml(ctx, app_dir, app_name, bundle_id, modules=None, ui_mode='widgets', team_id=''):
    """
    Generate a pyside6-ios.toml config file for the given application.
    This file drives `pyside6-ios generate` to produce the .xcodeproj.
    Key knobs (from the pyside6-ios build-tool-reference):
    - modules: which PySide6 modules to link as built-ins
    - python_framework: path to CPython XCFramework
    - qtruntime_framework: path to merged Qt framework
    - support_libs: shiboken6/pyside6 static libs
    - pyside6_modules: cross-compiled static module archives
    :param ctx:       BuildContext
    :param app_dir:   str -- path to application directory
    :param app_name:  str
    :param bundle_id: str
    :param modules:   list[str] | None
    :param ui_mode:   str  ('widgets' or 'qml')
    :param team_id:   str
    :return: str -- path to the written .toml file
    """
    if modules is None:
        modules = (['QtCore', 'QtGui', 'QtWidgets'] if ui_mode == 'widgets' else [
            'QtCore', 'QtGui', 'QtNetwork', 'QtQml', 'QtQuick'])
    toml_path = join(app_dir, 'pyside6-ios.toml')

    # -- Determine the application entry point -----------------------------
    # We accept the SAME project layout as this repo's Android builders: a plain
    # main.py at the project root (a normal desktop app that builds its own
    # QApplication and runs the event loop).  We then auto-generate
    # scripts/app.py as an iOS bridge that runs that main.py under the native
    # host (see generate_entry_shim / _ENTRY_SHIM_SRC).  main.py is never
    # modified.  A hand-written scripts/app.py (the pyside6-ios native style) is
    # still honoured for backwards compatibility.
    main_py = join(app_dir, 'main.py')
    native_app_py = join(app_dir, 'scripts', 'app.py')
    if exists(main_py):
        generate_entry_shim(ctx, app_dir)
        script_files = ['scripts/app.py', 'main.py']
        # Bundle any other loose top-level .py modules so main.py can import
        # them (they land in <bundle>/scripts, which is on sys.path).
        for py in sorted(glob(join(app_dir, '*.py'))):
            name = basename(py)
            if name != 'main.py' and not name.startswith('.') and name not in script_files:
                script_files.append(name)
        log.info("  Entry point: main.py (desktop layout) -> bridged via scripts/app.py")
    elif exists(native_app_py):
        script_files = ['scripts/app.py']
        log.info("  Entry point: scripts/app.py (pyside6-ios native layout)")
    else:
        raise BuildError(
            "No entry point found in {0}.\n"
            "  Provide a main.py at the project root (recommended -- same layout "
            "as the Android builders),\n"
            "  e.g. {0}/main.py, or a pyside6-ios-style {0}/scripts/app.py."
            .format(app_dir))
    scripts_inline = ", ".join('"{}"'.format(s) for s in script_files)

    # -- Auto-detect bundled Python packages -------------------------------
    # Any top-level directory in the app that has an __init__.py is bundled as
    # a package (mirrors the [python] packages entries in the pyside6-ios test
    # apps).  'scripts'/'qml'/etc. are reserved layout dirs, never packages.
    reserved = set(['scripts', 'qml', 'resources', 'vendor', 'native', 'generated'])
    pkg_names = []
    for init_path in sorted(glob(join(app_dir, '*', '__init__.py'))):
        name = basename(dirname(init_path))
        if name not in reserved and name not in pkg_names:
            pkg_names.append(name)
    if pkg_names:
        pkg_lines = "\n".join(
            '    {{ src = "{}", exclude = ["*.pyc", "__pycache__"] }},'.format(n)
            for n in pkg_names)
        packages_block = "packages = [\n{}\n]\n".format(pkg_lines)
    else:
        packages_block = "packages = []\n"

    modules_inline = ", ".join('"{}"'.format(m) for m in modules)
    team_line = 'team-id    = "{}"'.format(team_id) if team_id else 'team-id    = ""'

    # -- [app], [paths], [pyside6], [python] -------------------------------
    header = dedent("""\
        # pyside6-ios.toml -- Auto-generated by pyside6_ios_builder.py
        # -------------------------------------------------------------
        # Config schema for the pyside6-ios CLI:
        #   https://github.com/patrickkidd/pyside6-ios
        # Keys are kebab-case.  Paths under [paths] resolve relative to this
        # file unless absolute.  QT_IOS in the environment overrides
        # [paths] qt-ios.
        # -------------------------------------------------------------

        [app]
        name       = "{app_name}"
        bundle-id  = "{bundle_id}"
        version    = "1.0"
        entry-point = ""
        {team_line}
        deployment-target = "16.0"

        [paths]
        pyside6-ios = "{tool_dir}"
        qt-ios      = "{qt_ios_dir}"
        output-dir  = "generated"

        [pyside6]
        modules = [{modules_inline}]

        [python]
        {packages_block}scripts = [{scripts_inline}]
    """).format(
        app_name=app_name,
        bundle_id=bundle_id,
        team_line=team_line,
        tool_dir=ctx.tool_dir,
        qt_ios_dir=ctx.qt_ios_dir,
        modules_inline=modules_inline,
        scripts_inline=scripts_inline,
        packages_block=packages_block)

    # -- [qml] (QML mode only) --------------------------------------------
    if ui_mode == 'qml':
        qml_modules = [m for m in modules
                       if m in ('QtQuick', 'QtQml', 'QtCore', 'QtQuickControls2', 'QtQuickLayouts')]
        if 'QtQuick' not in qml_modules:
            qml_modules = ['QtQuick', 'QtQml', 'QtCore']
        qml_modules_inline = ", ".join('"{}"'.format(m) for m in qml_modules)
        qml_block = dedent("""\

            [qml]
            dirs = ["qml"]
            qt-modules = [{qml_modules_inline}]
        """).format(qml_modules_inline=qml_modules_inline)
    else:
        qml_block = ''

    # -- [sources] : custom main.mm for widgets; QML uses the CLI's
    #    auto-generated (QGuiApplication) main.mm, so omit main-mm there.
    if ui_mode == 'widgets':
        sources_block = dedent("""\

            [sources]
            main-mm = "main.mm"
        """)
    else:
        sources_block = ''

    # -- [signing] + ad-hoc CI [build-settings] ----------------------------
    if team_id:
        signing_block = dedent("""\

            [signing]
            style = "Automatic"
        """)
        build_settings_block = ''
    else:
        # No Apple Team -> unsigned / ad-hoc signing so the project builds on
        # CI without an Apple Developer account.  The workflow also passes
        # these on the xcodebuild command line (which wins), but keeping them
        # here makes a bare `pyside6-ios build` self-consistent too.
        signing_block = dedent("""\

            [signing]
            style = "Manual"
        """)
        build_settings_block = dedent("""\

            [build-settings]
            CODE_SIGN_IDENTITY = "-"
            CODE_SIGNING_REQUIRED = "NO"
            CODE_SIGNING_ALLOWED = "YES"
            AD_HOC_CODE_SIGNING_ALLOWED = "YES"
            DEVELOPMENT_TEAM = ""
        """)

    toml_content = header + qml_block + sources_block + signing_block + build_settings_block
    _write_text(toml_path, toml_content)
    log.info("  Written: %s", toml_path)
    return toml_path


# ---------------------------------------------------------------------------
# Step 11 - Generate main.mm host app stub.
# ---------------------------------------------------------------------------


def generate_main_mm(ctx, app_dir, app_name, modules=None):
    """
    Write a QtWidgets host ``main.mm`` (ObjC++).

    This mirrors the pyside6-ios reference ``test/test_widgets/main.mm`` and is
    only used in **widgets** mode -- in QML mode the pyside6-ios CLI emits its
    own (QGuiApplication-based) ``main.mm`` automatically, so we omit
    ``[sources] main-mm`` from the TOML and never call this function.

    Critical iOS integration points (verified against the reference host):
    - ``#pragma push_macro("slots")`` / ``#undef slots`` around ``<Python.h>``
      (Qt's ``slots`` keyword collides with CPython headers).
    - Use **QApplication** (not QGuiApplication) for QtWidgets.
    - Modern ``PyConfig`` / ``Py_InitializeFromConfig`` isolated config -- NOT
      the deprecated ``Py_SetPythonHome``/``Py_Initialize``.
    - PySide6 modules are registered as built-ins **before** Python init.  The
      C init symbol is ``PyInit_<Module>`` (basename, e.g. ``PyInit_QtCore``)
      but it is registered under the dotted name ``PySide6.<Module>`` so that
      cross-module shiboken type resolution works.
    - ``Q_IMPORT_PLUGIN(QIOSIntegrationPlugin)`` statically links the iOS
      platform plugin.
    - The app entry point is ``scripts/app.py`` (run via ``PyRun_SimpleFile``).
    - Reparent Qt's root ``UIView`` into the iOS ``UIWindow`` and resize the
      top-level ``QWidget``s to fill the screen.

    :param ctx:      BuildContext (used for the bundled Python version).
    :param app_dir:  str -- path to application directory.
    :param app_name: str -- product name (used as argv[0]).
    :param modules:  list[str] | None -- PySide6 modules to register as
                     built-ins.  Defaults to the widgets set.
    :return: str -- path to the written main.mm
    """
    if modules is None:
        modules = ['QtCore', 'QtGui', 'QtWidgets']
    mm_path = join(app_dir, 'main.mm')

    extern_lines = []
    inittab_lines = []
    for mod in modules:
        extern_lines.append('extern "C" PyObject *PyInit_{}();'.format(mod))
        inittab_lines.append(
            '    PyImport_AppendInittab("PySide6.{m}", PyInit_{m});'.format(m=mod))
    # shiboken6 runtime module
    extern_lines.append('extern "C" PyObject *PyInit_Shiboken();')
    inittab_lines.append(
        '    PyImport_AppendInittab("shiboken6.Shiboken", PyInit_Shiboken);')

    # NOTE: built with str token substitution (NOT str.format) because the
    # ObjC++ body is full of literal { } braces.
    template = """\
// main.mm -- QtWidgets host (auto-generated by pyside6_ios_builder.py)
// Mirrors the pyside6-ios reference test_widgets host.
//
// Runtime contract (handled for you by the auto-generated scripts/app.py):
//   * The host runs scripts/app.py (via PyRun_SimpleFile), which is a generated
//     bridge that runs your project's main.py.  Your main.py is NOT modified.
//   * The bridge already makes QApplication.instance() reuse this C++ instance,
//     turns app.exec() into a no-op, and shows top-level widgets full-screen,
//     so a standard desktop main.py works unchanged.
//   * Use widget.setLayout(layout) -- the QLayout(parent) ctor is broken in
//     static PySide6 modules.
//   * QProcess is unavailable on iOS (QT_CONFIG(process) == 0).

#pragma push_macro("slots")
#undef slots
#include <Python.h>
#pragma pop_macro("slots")

#import <UIKit/UIKit.h>

#include <QtWidgets/QApplication>
#include <QtWidgets/QWidget>
#include <QtGui/QWindow>
#include <QtCore/QDebug>
#include <QtCore/QtPlugin>

Q_IMPORT_PLUGIN(QIOSIntegrationPlugin)

// PySide6 built-in modules (static).
__EXTERN_DECLS__

static QApplication *qtApp = nullptr;

static void initPython() {
    NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
    NSString *stdlibPath = [bundlePath stringByAppendingPathComponent:@"lib/python__PYVER__"];
    NSString *dynloadPath = [stdlibPath stringByAppendingPathComponent:@"lib-dynload"];
    NSString *appScriptsPath = [bundlePath stringByAppendingPathComponent:@"scripts"];
    NSString *appPackagesPath = [bundlePath stringByAppendingPathComponent:@"packages"];

__INITTAB_CALLS__

    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.write_bytecode = 0;
    config.home = Py_DecodeLocale([bundlePath UTF8String], NULL);

    config.module_search_paths_set = 1;
    PyWideStringList_Append(&config.module_search_paths,
        Py_DecodeLocale([stdlibPath UTF8String], NULL));
    PyWideStringList_Append(&config.module_search_paths,
        Py_DecodeLocale([dynloadPath UTF8String], NULL));
    PyWideStringList_Append(&config.module_search_paths,
        Py_DecodeLocale([appScriptsPath UTF8String], NULL));
    PyWideStringList_Append(&config.module_search_paths,
        Py_DecodeLocale([appPackagesPath UTF8String], NULL));

    PyStatus status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        NSLog(@"Python init failed: %s", status.err_msg);
        return;
    }
    NSLog(@"Python %s initialized", Py_GetVersion());
}

static void runPythonApp() {
    NSString *bundlePath = [[NSBundle mainBundle] bundlePath];
    NSString *scriptPath = [bundlePath stringByAppendingPathComponent:@"scripts/app.py"];

    FILE *fp = fopen([scriptPath UTF8String], "r");
    if (!fp) {
        NSLog(@"Failed to open %@", scriptPath);
        return;
    }
    NSLog(@"Running Python script...");
    int result = PyRun_SimpleFile(fp, [scriptPath UTF8String]);
    fclose(fp);
    if (result != 0) {
        NSLog(@"Python script failed with code %d", result);
        if (PyErr_Occurred()) PyErr_Print();
    }
}

@interface SceneDelegate : UIResponder <UIWindowSceneDelegate>
@property (strong, nonatomic) UIWindow *window;
@end

@implementation SceneDelegate
- (void)scene:(UIScene *)scene
    willConnectToSession:(UISceneSession *)session
    options:(UISceneConnectionOptions *)connectionOptions {

    UIWindowScene *windowScene = (UIWindowScene *)scene;
    self.window = [[UIWindow alloc] initWithWindowScene:windowScene];
    self.window.backgroundColor = [UIColor blackColor];
    self.window.rootViewController = [[UIViewController alloc] init];
    [self.window makeKeyAndVisible];

    initPython();

    static int argc = 1;
    static const char *argv[] = {"__APP_NAME__", nullptr};
    qtApp = new QApplication(argc, const_cast<char **>(argv));
    qDebug() << "Qt" << qVersion() << "platform:" << qtApp->platformName();

    runPythonApp();

    dispatch_async(dispatch_get_main_queue(), ^{
        QWindowList windows = QGuiApplication::topLevelWindows();
        if (!windows.isEmpty()) {
            QWindow *qtWindow = windows.first();
            WId nativeId = qtWindow->winId();
            UIView *qtView = (__bridge UIView *)(void *)nativeId;
            if (qtView) {
                CGRect bounds = self.window.bounds;
                qtView.frame = bounds;
                qtView.autoresizingMask = UIViewAutoresizingFlexibleWidth |
                                          UIViewAutoresizingFlexibleHeight;
                [self.window.rootViewController.view addSubview:qtView];
                QWidgetList topWidgets = QApplication::topLevelWidgets();
                for (QWidget *w : topWidgets) {
                    w->resize((int)bounds.size.width, (int)bounds.size.height);
                }
                qDebug() << "Reparented Qt view into iOS window"
                         << bounds.size.width << "x" << bounds.size.height
                         << "widgets:" << topWidgets.size();
            }
        }
    });
}
@end

@interface AppDelegate : UIResponder <UIApplicationDelegate>
@end

@implementation AppDelegate
- (UISceneConfiguration *)application:(UIApplication *)application
    configurationForConnectingSceneSession:(UISceneSession *)connectingSceneSession
    options:(UISceneConnectionOptions *)options {
    UISceneConfiguration *config =
        [[UISceneConfiguration alloc] initWithName:@"Default"
                                       sessionRole:connectingSceneSession.role];
    config.delegateClass = [SceneDelegate class];
    return config;
}
@end

int main(int argc, char *argv[]) {
    @autoreleasepool {
        return UIApplicationMain(argc, argv, nil,
            NSStringFromClass([AppDelegate class]));
    }
}
"""
    content = (template
               .replace('__EXTERN_DECLS__', "\n".join(extern_lines))
               .replace('__INITTAB_CALLS__', "\n".join(inittab_lines))
               .replace('__PYVER__', ctx.python_version)
               .replace('__APP_NAME__', app_name.replace('"', '\\"').replace(' ', '')))
    _write_text(mm_path, content)
    log.info('  Written: %s', mm_path)
    return mm_path


# ---------------------------------------------------------------------------
# Step 12 - Generate Xcode project via pyside6-ios CLI
# ---------------------------------------------------------------------------


def generate_xcodeproj(ctx, app_dir, toml_path):
    """
    Run `pyside6-ios generate` to produce a .xcodeproj from the TOML config.
    The CLI handles:
      - Framework linking (QtRuntime.framework, Python.xcframework)
      - Python stdlib bundling
      - QML plugin static registration
      - Shiboken6 custom bindings
      - Code signing configuration
    :param ctx:       BuildContext
    :param app_dir:   str
    :param toml_path: str
    :return: str -- path to the generated .xcodeproj
    """
    log.info('========== Generating Xcode project ==========')
    pyside6_ios_bin = join(ctx.venv_dir, 'bin', 'pyside6-ios')
    if not exists(pyside6_ios_bin):
        raise BuildError('pyside6-ios CLI not found at {}. Run --install-tool first.'.format(pyside6_ios_bin))
    _run([pyside6_ios_bin, '-c', toml_path, 'generate'], cwd=app_dir, env=ctx.env_with_qt())
    # The CLI writes the project into the TOML's output-dir ("generated"), and a
    # .xcodeproj is a *directory* (bundle), not a file -- so _rglob (which only
    # walks files) never finds it.  Look in the output dir first, then fall back
    # to a recursive directory glob.
    xcodeproj_candidates = sorted(glob(join(app_dir, 'generated', '*.xcodeproj')))
    if not xcodeproj_candidates:
        xcodeproj_candidates = sorted(glob(join(app_dir, '**', '*.xcodeproj'), recursive=True))
    if not xcodeproj_candidates:
        raise BuildError('No .xcodeproj found in {} after generate.'.format(app_dir))
    xcodeproj = xcodeproj_candidates[0]
    log.info('  Xcode project: %s  :)', xcodeproj)
    return xcodeproj


# ---------------------------------------------------------------------------
# Step 13 - Build & deploy to device
# ---------------------------------------------------------------------------


def build_and_deploy(ctx, app_dir, toml_path, xcode_udid, coredevice_uuid, bundle_id, configuration='Debug'):
    """
    Build the Xcode project and deploy to a connected iPhone.
    Two device ID systems are required (Xcode quirk):
    - xcode_udid:       from `xcrun xctrace list devices`  (for xcodebuild --destination)
    - coredevice_uuid:  from `xcrun devicectl list devices` (for xcrun devicectl)
    :param ctx:              BuildContext
    :param app_dir:          str
    :param toml_path:        str
    :param xcode_udid:       str
    :param coredevice_uuid:  str
    :param bundle_id:        str
    :param configuration:    str
    :return:
    """
    log.info('========== Build & deploy (configuration=%s) ==========', configuration)
    pyside6_ios_bin = join(ctx.venv_dir, 'bin', 'pyside6-ios')
    _run([pyside6_ios_bin, '-c', toml_path, 'build', '--configuration', configuration, '--destination',
          'id={}'.format(xcode_udid), '--install'], cwd=app_dir, env=ctx.env_with_qt())
    log.info('  App installed to device.')
    log.info('  Launching %s ...', bundle_id)
    _run([getXcrunExecutable(), 'devicectl', 'device', 'process', 'launch', '--device', coredevice_uuid, '--console',
          bundle_id])
    log.info('  App launched on device.')


# ---------------------------------------------------------------------------
# Step 14 - List connected devices
# ---------------------------------------------------------------------------


def list_devices():
    """
    Print connected device information from both Xcode ID systems.
    :return:
    """
    log.info('========== Connected devices ==========')
    log.info('  xctrace (UDID -- for xcodebuild --destination):')
    try:
        out = _run([getXcrunExecutable(), 'xctrace', 'list', 'devices'], capture=True)
        for line in out.splitlines():
            if 'iPhone' in line or 'iPad' in line:
                log.info('    %s', line)
    except BuildError:
        log.warning('  xcrun xctrace not available.')
    log.info('  devicectl (CoreDevice UUID -- for xcrun devicectl):')
    try:
        out = _run([getXcrunExecutable(), 'devicectl', 'list', 'devices'], capture=True)
        for line in out.splitlines():
            if 'iPhone' in line or 'iPad' in line or 'identifier' in line.lower():
                log.info('    %s', line)
    except BuildError:
        log.warning('  xcrun devicectl not available.')


# ---------------------------------------------------------------------------
# High-level convenience pipelines
# ---------------------------------------------------------------------------


def bootstrap(ctx):
    """
    Stages 1-5: environment, Qt, Python framework, PySide sources, tool.
    :param ctx: BuildContext
    :return:
    """
    log.info('========================================')
    log.info('         Bootstrap pipeline')
    log.info('========================================')
    ctx.summary()
    # The build root (and everything under it) is created lazily; make sure it
    # exists before any step runs a subprocess with cwd=ctx.root (e.g. uv venv),
    # otherwise subprocess raises FileNotFoundError on the missing cwd.
    _makedirs(ctx.root)
    setup_venv(ctx)
    # Clone the tool FIRST: its directory IS the build root, and the steps below
    # populate <tool>/build/...  ``git clone`` refuses a non-empty target, so it
    # must happen before we create any <tool>/build subdirectories.
    install_pyside6_ios_tool(ctx)
    install_qt(ctx)
    install_python_framework(ctx)
    clone_pyside_sources(ctx)
    log.info('Bootstrap complete.  Next: --build-qtruntime')


def build_all(ctx, modules=None):
    """
    Stages 6-9: globalize symbols, QtRuntime, support libs, PySide6 modules.
    :param ctx: BuildContext
    :param modules: object | None
    :return:
    """
    log.info('========================================')
    log.info('         Library build pipeline         ')
    log.info('========================================')
    globalize_qt_symbols(ctx)
    build_qtruntime(ctx)
    build_support_libs(ctx)
    build_pyside6_modules(ctx, modules)
    log.info('Library build complete.  Next: --app <dir>')


def full_pipeline(ctx, app_dir, app_name, bundle_id, app_module, ui_mode='widgets', modules=None, team_id='',
                  xcode_udid='', coredevice_uuid='', configuration='Debug', deploy=False):
    """
    End-to-end: bootstrap -> libraries -> generate -> (deploy).
    :param ctx: BuildContext
    :param app_dir: str
    :param app_name: str
    :param bundle_id: str
    :param app_module: str
    :param ui_mode: str
    :param modules: object | None
    :param team_id: str
    :param xcode_udid: str
    :param coredevice_uuid: str
    :param configuration: str
    :param deploy: bool
    :return:
    """
    bootstrap(ctx)
    build_all(ctx, modules)
    _makedirs(app_dir)
    toml_path = generate_toml(ctx, app_dir, app_name, bundle_id, modules, ui_mode, team_id)
    # Widgets mode needs a QApplication host; QML mode lets the pyside6-ios CLI
    # auto-generate its (QGuiApplication) main.mm during `generate`.
    if ui_mode == 'widgets':
        generate_main_mm(ctx, app_dir, app_name, modules)
    xcodeproj = generate_xcodeproj(ctx, app_dir, toml_path)
    log.info('')
    log.info('╔══════════════════════════════════════════════════════════╗')
    log.info('║          Xcode project generated  :)                     ║')
    log.info('╠══════════════════════════════════════════════════════════╣')
    log.info('║  %s', xcodeproj)
    log.info('╠══════════════════════════════════════════════════════════╣')
    log.info('║  Important iOS runtime notes:                            ║')
    log.info('║  * Use showFullScreen() -- show() produces blank window  ║')
    log.info('║  * Use widget.setLayout(l) -- NOT QLayout(parent)        ║')
    log.info('║  * QProcess unavailable on iOS (QT_CONFIG(process)==0)   ║')
    log.info('║  * Trust your developer cert on device (first run):      ║')
    log.info('║    Settings > General > VPN & Device Management > Trust  ║')
    log.info('╚══════════════════════════════════════════════════════════╝')
    if deploy:
        if not xcode_udid or not coredevice_uuid:
            log.warning(
                'Deployment skipped -- provide --xcode-udid and --coredevice-uuid.\n'
                '  Get them with:\n'
                '    xcrun xctrace list devices        # UDID\n'
                '    xcrun devicectl list devices      # CoreDevice UUID')
        else:
            build_and_deploy(ctx, app_dir, toml_path, xcode_udid=xcode_udid, coredevice_uuid=coredevice_uuid,
                             bundle_id=bundle_id, configuration=configuration)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    """
    :return: Namespace
    """
    p = ArgumentParser(
        prog='pyside6_ios_builder', formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PySide6 -> iOS build pipeline (patrickkidd/pyside6-ios architecture).

            Solves PYSIDE-2352: merges Qt static libs into QtRuntime.framework,
            cross-compiles PySide6 modules as static built-ins, generates Xcode project.

            Tested: PySide6 6.8.3 / Qt 6.8.3 / CPython 3.13 / Xcode 16 / Apple Silicon
        """),
        epilog=dedent("""\
            Project layout (same as the Android builders in this repo):
              Your app is a directory with a plain main.py at its root, e.g.

                  myapp/
                    main.py            # standard desktop entry: builds its own
                                       #   QApplication and calls app.exec()
                    mypkg/             # (optional) packages -> bundled to packages/
                      __init__.py

              The builder generates myapp/scripts/app.py automatically -- a small
              bridge that runs your main.py under the iOS host.  Your main.py is
              never modified.  (A hand-written scripts/app.py is also accepted.)

            Examples:
              # Full pipeline for an app (myapp/main.py exists)
              python pyside6_ios_builder.py \\
                  --app myapp/ --app-name "My App" --bundle-id com.example.myapp

              # Bootstrap + library build (no app yet)
              python pyside6_ios_builder.py --bootstrap
              python pyside6_ios_builder.py --build-all

              # Generate Xcode project only (libs already built)
              python pyside6_ios_builder.py \\
                  --app myapp/ --app-name "My App" --bundle-id com.example.myapp \\
                  --generate-only

              # Build & deploy to connected iPhone
              python pyside6_ios_builder.py \\
                  --app myapp/ --app-name "My App" --bundle-id com.example.myapp \\
                  --deploy \\
                  --xcode-udid  00008120-000XXXXXXXXXX \\
                  --coredevice-uuid 12345678-1234-1234-1234-123456789ABC

              # List connected devices
              python pyside6_ios_builder.py --list-devices

              # Check prerequisites
              python pyside6_ios_builder.py --check-deps
        """),
    )
    # -- Pipeline actions --
    actions = p.add_argument_group('Pipeline actions (mutually exclusive)')
    ag = actions.add_mutually_exclusive_group()
    ag.add_argument('--check-deps', action='store_true', help='Check prerequisites and exit.')
    ag.add_argument('--bootstrap', action='store_true', help='Set up env, Qt, Python, PySide sources, tool.')
    ag.add_argument('--build-qtruntime', action='store_true', help='Globalize symbols + build QtRuntime.framework.')
    ag.add_argument('--build-support', action='store_true', help='Cross-compile shiboken/pyside support libs.')
    ag.add_argument('--build-all', action='store_true', help='Run all library-build stages.')
    ag.add_argument('--generate-only', action='store_true', help='Generate Xcode project only (libs must exist).')
    ag.add_argument('--list-devices', action='store_true', help='List connected iOS devices and exit.')
    # -- App configuration --
    app_grp = p.add_argument_group('App configuration')
    app_grp.add_argument('--app', metavar='DIR', help='App project directory (must contain main.py at its root).')
    app_grp.add_argument('--app-name', metavar='NAME', default='MyPySide6App', help='Human-readable app name.')
    app_grp.add_argument('--bundle-id', metavar='ID', default='com.example.myapp', help='iOS bundle identifier.')
    app_grp.add_argument('--app-module', metavar='MOD', default='main',
                         help='(Deprecated/unused) the entry point is main.py at the project root.')
    app_grp.add_argument(
        '--ui-mode', choices=['widgets', 'qml'], default='widgets',
        help="UI toolkit: 'widgets' (QtWidgets) or 'qml' (QML). Default: widgets.")
    app_grp.add_argument('--toml', metavar='FILE', help='Path to existing pyside6-ios.toml (skips auto-generation).')
    app_grp.add_argument('--team-id', metavar='ID', default='',
                         help='Apple Developer Team ID for code signing.')
    app_grp.add_argument('--modules', metavar='MOD', nargs='+',
                         choices=SUPPORTED_MODULES, help='PySide6 modules to include.')
    # -- Build versions --
    ver_grp = p.add_argument_group('Versions')
    ver_grp.add_argument('--qt-version', default=DEFAULT_QT_VERSION, metavar='VER')
    ver_grp.add_argument('--pyside-version', default=DEFAULT_PYSIDE_VERSION, metavar='VER')
    ver_grp.add_argument('--python-version', default=DEFAULT_PYTHON_VERSION, metavar='VER')
    ver_grp.add_argument('--python-support-tag', default=DEFAULT_PYTHON_SUPPORT, metavar='TAG',
                         help='BeeWare Python-Apple-support release tag.')
    # -- Paths --
    path_grp = p.add_argument_group('Paths')
    path_grp.add_argument('--root', metavar='DIR', default=getcwd(),
                          help='Project root directory (default: current working directory).')
    path_grp.add_argument('--qt-ios', metavar='DIR', default=None,
                          help='Path to Qt iOS SDK (overrides QT_IOS env var).')
    # -- Deployment --
    dep_grp = p.add_argument_group('Deployment')
    dep_grp.add_argument('--deploy', action='store_true',
                         help='Build and deploy to connected iPhone after generating.')
    dep_grp.add_argument('--configuration', default='Debug', choices=['Debug', 'Release'],
                         help='Xcode build configuration.')
    dep_grp.add_argument('--xcode-udid', metavar='UDID', default='',
                         help='Device UDID from `xcrun xctrace list devices`.')
    dep_grp.add_argument('--coredevice-uuid', metavar='UUID', default='',
                         help='CoreDevice UUID from `xcrun devicectl list devices`.')
    # -- Misc --
    p.add_argument('--debug', action='store_true', help='Enable DEBUG-level logging.')
    return p.parse_args()


def main():
    """
    :return:
    """
    args = parse_args()
    if args.debug:
        getLogger().setLevel(DEBUG)
    ctx = BuildContext(root=args.root, qt_version=args.qt_version, pyside_version=args.pyside_version,
                       python_version=args.python_version, python_support_tag=args.python_support_tag,
                       qt_ios_override=args.qt_ios)
    try:
        check_macos_arm64()
        if args.check_deps:
            check_dependencies(ctx)
            exit(0)
        if args.list_devices:
            list_devices()
            exit(0)
        if args.bootstrap:
            bootstrap(ctx)
            exit(0)
        if args.build_qtruntime:
            globalize_qt_symbols(ctx)
            build_qtruntime(ctx)
            exit(0)
        if args.build_support:
            build_support_libs(ctx)
            exit(0)
        if args.build_all:
            build_all(ctx, args.modules)
            exit(0)
        if args.generate_only:
            if not args.app:
                log.error('--app <dir> is required for --generate-only')
                exit(1)
            app_dir = abspath(args.app)
            toml_path = args.toml or join(app_dir, 'pyside6-ios.toml')
            if not exists(toml_path):
                toml_path = generate_toml(
                    ctx, app_dir, args.app_name, args.bundle_id, args.modules, args.ui_mode, args.team_id)
                if args.ui_mode == 'widgets':
                    generate_main_mm(ctx, app_dir, args.app_name, args.modules)
            xcodeproj = generate_xcodeproj(ctx, app_dir, toml_path)
            log.info('Xcode project: %s', xcodeproj)
            exit(0)
        # Default: full pipeline.
        if not args.app:
            log.error('No action specified. Pass --app <dir> for a full build, '
                      'or use --bootstrap / --build-all / --check-deps.')
            exit(1)
        full_pipeline(
            ctx,
            app_dir=abspath(args.app),
            app_name=args.app_name,
            bundle_id=args.bundle_id,
            app_module=args.app_module,
            ui_mode=args.ui_mode,
            modules=args.modules,
            team_id=args.team_id,
            xcode_udid=args.xcode_udid,
            coredevice_uuid=args.coredevice_uuid,
            configuration=args.configuration,
            deploy=args.deploy)
    except BuildError as exc:
        log.error('Build failed: %s', exc)
        exit(1)
    except KeyboardInterrupt:
        log.warning('Interrupted.')
        exit(130)


if __name__ == '__main__':
    main()
