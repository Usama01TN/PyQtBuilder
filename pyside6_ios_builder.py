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
from os.path import exists, dirname, basename, abspath, join, isdir
from argparse import ArgumentParser, RawDescriptionHelpFormatter
from os import environ, rename, utime, walk, makedirs, getcwd
from logging import basicConfig, INFO, getLogger, DEBUG
from struct import unpack_from, error
from platform import system, machine
from sys import stdout, exit, path
from textwrap import dedent
from fnmatch import fnmatch
from glob import glob
from re import search
import tarfile
import io

# -- urllib ------------------------------------------------------------------
try:
    from urllib import urlretrieve  # noqa: F401
except:
    from urllib.request import urlretrieve  # noqa: F401

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import run, getCurrentExecutable, getUVExecutable, getGitExecutable, getCmakeExecutable, \
        getXcrunExecutable, getXcodeSelectExecutable, getXcodebuildExecutable
except:
    from builders import run, getCurrentExecutable, getUVExecutable, getGitExecutable, getCmakeExecutable, \
        getXcrunExecutable, getXcodeSelectExecutable, getXcodebuildExecutable


# ---------------------------------------------------------------------------
# Path utility helpers  (replaces all pathlib usage)
# ---------------------------------------------------------------------------


def _makedirs(pth):
    """
    Create *path* and all missing intermediate directories.
    Equivalent to Path.mkdir(parents=True, exist_ok=True).
    :param pth: str
    :return:
    """
    try:
        makedirs(pth)
    except OSError:
        if not isdir(pth):
            raise


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
    with open(pth, "wb") as fh:
        fh.write(data)


def _write_text(pth, text, encoding='utf-8'):
    """
    Write the unicode string *text* to *path*.
    Uses io.open so the encoding keyword works on both Python 2 and 3.
    Equivalent to Path.write_text(text, encoding=encoding).
    :param pth: str
    :param text: str
    :param encoding: str
    :return:
    """
    with io.open(pth, 'w', encoding=encoding) as fh:
        fh.write(text)


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


def _rglob(directory, pattern):
    """
    Recursively search *directory* for entries whose basename matches *pattern*
    (shell-style wildcards via fnmatch).  Returns a sorted list of full paths.
    Equivalent to Path.rglob(pattern).
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []
    for root, dirs, files in walk(directory):
        for name in files + dirs:
            if fnmatch(name, pattern):
                matches.append(join(root, name))
    return sorted(matches)


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
PYTHON_SUPPORT_URL_TPL = (
    'https://github.com/beeware/Python-Apple-support/releases/download/{tag}/Python-{pyver}-iOS-support.{tag}.tar.gz')
# Qt modules that can be cross-compiled for iOS with this toolchain.
SUPPORTED_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtNetwork', 'QtQml', 'QtQuick']  # type: list[str]
# N_PEXT mask -- Mach-O private-extern flag that must be cleared for re-export.
N_PEXT = 0x10  # type: int


# ---------------------------------------------------------------------------
# Helpers
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
            log.error("STDOUT:\n%s", result.stdout)
            log.error("STDERR:\n%s", result.stderr)
        raise BuildError("Command failed (exit {}): {}".format(result.returncode, display))
    return result.stdout.strip() if capture else ""


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
            print("\r     {:3d}%".format(pct), end="")
            stdout.flush()

    try:
        urlretrieve(url, dest, _progress)
        print()  # Newline after progress.
    except Exception as exc:
        raise BuildError("Download failed: {}\nURL: {}".format(exc, url))


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
        # Qt paths.
        self.qt_lib_dir = join(self.build_dir, 'Qt-{}'.format(qt_version))
        if qt_ios_override:
            self.qt_ios_dir = qt_ios_override
        elif environ.get('QT_IOS'):
            self.qt_ios_dir = environ.get('QT_IOS')
        else:
            self.qt_ios_dir = join(self.qt_lib_dir, qt_version, 'ios')
        self.qt_macos_dir = join(self.qt_lib_dir, qt_version, 'macos')
        # Python iOS framework.
        self.python_dir = join(self.build_dir, 'python')
        self.python_framework = join(self.python_dir, 'Python.xcframework')
        # PySide6 sources.
        self.pyside_src = join(self.build_dir, 'pyside-setup')
        # pyside6-ios tool clone.
        self.tool_dir = join(self.build_dir, 'pyside6-ios-tool')
        # Build outputs.
        self.qtruntime_framework = join(self.root, 'QtRuntime.framework')
        self.support_libs_dir = join(self.root, 'build', 'support_libs')
        self.pyside6_modules_dir = join(self.root, 'build', 'pyside6_modules')

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
        Return an environment dict with QT_IOS and related vars set.
        :return: dict[str, str]
        """
        qt_env = {'QT_IOS': self.qt_ios_dir, 'QT_MACOS': self.qt_macos_dir, 'PYSIDE_VERSION': self.pyside_version,
                  'QT_VERSION': self.qt_version}
        qt_env.update(dict(environ))
        return qt_env

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
    if exists(ctx.python_framework):
        log.info('  Python.xcframework already present -- skipping.')
        return
    _makedirs(ctx.python_dir)
    tag = ctx.python_support_tag
    pyver = ctx.python_version
    url = PYTHON_SUPPORT_URL_TPL.format(tag=tag, pyver=pyver)
    tarball = join(ctx.python_dir, 'Python-{}-iOS-support.{}.tar.gz'.format(pyver, tag))
    if not exists(tarball):
        download(url, tarball)
    log.info('  Extracting Python iOS support ...')
    with tarfile.open(tarball, "r:gz") as tf:
        tf.extractall(ctx.python_dir)
    if not exists(ctx.python_framework):
        # The tarball may unpack to a different name; find it.
        candidates = _rglob(ctx.python_dir, 'Python.xcframework')
        if not candidates:
            raise BuildError(
                'Python.xcframework not found after extraction. Check tarball contents in {}'.format(ctx.python_dir))
        # Use the first found, move it to the canonical location.
        rename(candidates[0], ctx.python_framework)
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
    _run([getGitExecutable(), 'clone', '--branch', 'v{}'.format(ctx.pyside_version), "--depth", "1", PYSIDE_SETUP_REPO,
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
    _run(['bash', script], cwd=ctx.root, env=ctx.env_with_qt())
    if not exists(ctx.qtruntime_framework):
        raise BuildError('QtRuntime.framework was not created. Check output above for linker errors.')
    log.info('  QtRuntime.framework built: %s  :)', ctx.qtruntime_framework)


# ---------------------------------------------------------------------------
# Step 8 - Cross-compile support libraries.
# ---------------------------------------------------------------------------


def build_support_libs(ctx):
    """
    Cross-compile libshiboken6, libpyside6, and libpysideqml for arm64-iOS.
    These are static libraries linked directly into the host executable.
    :param ctx: BuildContext
    :return: None
    """
    log.info('========== Building support libraries ==========')
    done_marker = join(ctx.support_libs_dir, '.done')
    if exists(done_marker):
        log.info('  Support libs already built -- skipping.')
        return
    script = join(ctx.tool_dir, 'scripts', 'build_support_libs.sh')
    if not exists(script):
        raise BuildError('build_support_libs.sh not found at: {}'.format(script))
    log.info('  Cross-compiling libshiboken6, libpyside6, libpysideqml ...  (~2 min)')
    _makedirs(ctx.support_libs_dir)
    env_dict = {}
    env_dict.update(ctx.env_with_qt())
    env_dict.update({'SUPPORT_LIBS_OUTDIR': ctx.support_libs_dir, 'PYSIDE_SRC': ctx.pyside_src,
                     'PYTHON_XCFRAMEWORK': ctx.python_framework})
    _run(['bash', script], cwd=ctx.root, env=env_dict)
    _touch(done_marker)
    log.info('  Support libs built: %s  :)', ctx.support_libs_dir)


# ---------------------------------------------------------------------------
# Step 9 - Cross-compile PySide6 modules
# ---------------------------------------------------------------------------


def build_pyside6_modules(ctx, modules=None):
    """
    Cross-compile each PySide6 Python extension module (QtCore, QtGui, etc.)
    as a *static* library for arm64-iOS via shiboken6 code generation +
    clang cross-compilation.
    Key details from the pyside6-ios report:
    - PyModuleDef.m_name must be "PySide6.QtCore" (not "QtCore") for type
      resolution to work across modules.
    - shouldLazyLoad() in the import machinery needs a "Qt" prefix check.
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
    _makedirs(ctx.pyside6_modules_dir)
    env = {
        'PYSIDE6_MODULES_OUTDIR': ctx.pyside6_modules_dir,
        'PYSIDE_SRC': ctx.pyside_src,
        'SUPPORT_LIBS_DIR': ctx.support_libs_dir,
        'PYTHON_XCFRAMEWORK': ctx.python_framework,
        'QTRUNTIME_FRAMEWORK': ctx.qtruntime_framework}
    env.update(ctx.env_with_qt())
    for mod in modules:
        done_marker = join(ctx.pyside6_modules_dir, ".{}.done".format(mod))
        if exists(done_marker):
            log.info('  %s already built -- skipping.', mod)
            continue
        log.info('  Building %s ...  (~2 min each)', mod)
        _run(['bash', script, mod], cwd=ctx.root, env=env)
        _touch(done_marker)
        log.info('  %s  :)', mod)
    log.info('  All PySide6 modules built.')


# ---------------------------------------------------------------------------
# Step 10 - Generate pyside6-ios.toml config
# ---------------------------------------------------------------------------


def generate_toml(ctx, app_dir, app_name, bundle_id, modules=None, ui_mode="widgets", team_id=""):
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
    :param modules:   list of str or None
    :param ui_mode:   str  ("widgets" or "qml")
    :param team_id:   str
    :return: str -- path to the written .toml file
    """
    if modules is None:
        modules = (['QtCore', "QtGui", 'QtWidgets'] if ui_mode == 'widgets' else [
            'QtCore', 'QtGui', 'QtNetwork', 'QtQml', 'QtQuick'])
    toml_path = join(app_dir, 'pyside6-ios.toml')
    modules_list = "\n".join('    "{}",'.format(m) for m in modules)
    team_line = 'team_id = "{}"'.format(
        team_id) if team_id else '# team_id = "XXXXXXXXXX"  # Set your Apple Developer Team ID here'
    toml_content = dedent("""\
        # pyside6-ios.toml -- Auto-generated by pyside6_ios_builder.py
        # -------------------------------------------------------------
        # Config for pyside6-ios CLI: https://github.com/patrickkidd/pyside6-ios
        # Edit paths below if you have a custom Qt/Python install location.
        # -------------------------------------------------------------

        [app]
        name       = "{app_name}"
        bundle_id  = "{bundle_id}"
        version    = "1.0"
        {team_line}

        # Entrypoint: ObjC++ host that owns UIApplicationMain,
        # inits CPython, imports your app's Python module, and
        # reparents Qt's UIView into the iOS UIWindow.
        main_mm = "main.mm"

        # Your Python application packages
        [app.packages]
        # paths = ["myapp/"]   # Python source packages to bundle

        # Vendor (pure-Python) packages
        # [app.vendor]
        # packages = ["dateutil"]

        # QML files (QML mode only)
        # [app.qml]
        # source_dirs = ["qml/"]

        [pyside6]
        modules = [
        {modules_list}
        ]
        # Paths to cross-compiled static module archives
        modules_dir = "{modules_dir}"

        [qt]
        ios_sdk  = "{qt_ios_dir}"
        runtime_framework = "{qtruntime_framework}"

        [python]
        xcframework = "{python_framework}"

        [shiboken]
        support_libs_dir = "{support_libs_dir}"

        [xcode]
        # Deployment target for the generated Xcode project
        ios_deployment_target = "16.0"
        # output_dir = "build-ios"

        [resources]
        # assets_xcassets = "Assets.xcassets"
        # settings_bundle = "Settings.bundle"
    """).format(
        app_name=app_name,
        bundle_id=bundle_id,
        team_line=team_line,
        modules_list=modules_list,
        modules_dir=ctx.pyside6_modules_dir,
        qt_ios_dir=ctx.qt_ios_dir,
        qtruntime_framework=ctx.qtruntime_framework,
        python_framework=ctx.python_framework,
        support_libs_dir=ctx.support_libs_dir)
    _write_text(toml_path, toml_content)
    log.info("  Written: %s", toml_path)
    return toml_path


# ---------------------------------------------------------------------------
# Step 11 - Generate main.mm host app stub
# ---------------------------------------------------------------------------


def generate_main_mm(app_dir, app_module, ui_mode='widgets'):
    """
    Write a main.mm ObjC++ host application stub.
    Critical iOS integration points (from the pyside6-ios technical report):
    - Host app owns UIApplicationMain (Qt does NOT call it).
    - Qt uses QIOSEventDispatcher (non-jumping) to integrate with CFRunLoop.
    - QtWidgets: use showFullScreen() -- show() produces a blank window.
    - QtWidgets: resize top-level QWidgets from main.mm after reparenting the
      Qt UIView into the iOS UIWindow.
    - QLayout(parent) is broken in static PySide6 modules; use setLayout().
    :param app_dir:    str -- path to application directory
    :param app_module: str
    :param ui_mode:    str  ("widgets" or "qml")
    :return: str -- path to the written main.mm
    """
    mm_path = join(app_dir, 'main.mm')
    if ui_mode == 'widgets':
        qt_init_code = dedent("""\
            // --- QtWidgets host ---
            // IMPORTANT: use QApplication, not QGuiApplication
            int argc = 0;
            char *argv[] = {{nullptr}};
            QApplication app(argc, argv);

            // Import your Python app module
            PyObject *mod = PyImport_ImportModule("{module}");
            if (!mod) {{
                PyErr_Print();
                return;
            }}
            // Call your app's _run() function
            PyObject *result = PyObject_CallMethod(mod, "run", nullptr);
            if (!result) {{ PyErr_Print(); }}
            Py_XDECREF(result);
            Py_DECREF(mod);
        """).format(module=app_module)
        qt_includes = "#include <QtWidgets/QApplication>"
    else:
        qt_init_code = dedent("""\
            // --- QML host ---
            int argc = 0;
            char *argv[] = {{nullptr}};
            QGuiApplication app(argc, argv);
            QQmlApplicationEngine engine;

            PyObject *mod = PyImport_ImportModule("{module}");
            if (!mod) {{
                PyErr_Print();
                return;
            }}
            PyObject *result = PyObject_CallMethod(mod, "run", "O",
                PySide6_PyObject(&engine));
            if (!result) {{ PyErr_Print(); }}
            Py_XDECREF(result);
            Py_DECREF(mod);
        """).format(module=app_module)
        qt_includes = '#include <QtGui/QGuiApplication>\n#include <QtQml/QQmlApplicationEngine>'
    content = dedent("""\
        // main.mm -- Host application entry point
        // Auto-generated by pyside6_ios_builder.py
        //
        // Architecture notes (from pyside6-ios technical report):
        //   - Host app owns UIApplicationMain; Qt does NOT call it.
        //   - Qt integrates via QIOSEventDispatcher (non-jumping CFRunLoop).
        //   - For QtWidgets use showFullScreen(), NOT show().
        //   - Use widget->setLayout(layout) NOT QLayout(parent) constructor.
        //   - QProcess is unavailable on iOS (QT_CONFIG(process) == false).

        #import <UIKit/UIKit.h>
        #include <Python.h>
        {qt_includes}

        // -- Forward declarations for PySide6 static built-in modules --
        // Each module must be registered before Py_Initialize so CPython
        // treats it as a built-in and skips dynamic loading.
        extern "C" {{
            PyObject *PyInit_PySide6_QtCore(void);
            PyObject *PyInit_PySide6_QtGui(void);
            PyObject *PyInit_PySide6_QtWidgets(void);
            // Add more as needed: PyInit_PySide6_QtNetwork, etc.
        }}

        @interface AppDelegate : UIResponder <UIApplicationDelegate>
        @property (strong, nonatomic) UIWindow *window;
        @end

        @implementation AppDelegate

        - (BOOL)application:(UIApplication *)application
            didFinishLaunchingWithOptions:(NSDictionary *)launchOptions
        {{
            // 1. Register PySide6 static modules BEFORE Py_Initialize
            //    PyModuleDef.m_name must be "PySide6.QtCore" (not "QtCore")
            //    for cross-module type resolution to work.
            PyImport_AppendInittab("PySide6.QtCore",    &PyInit_PySide6_QtCore);
            PyImport_AppendInittab("PySide6.QtGui",     &PyInit_PySide6_QtGui);
            PyImport_AppendInittab("PySide6.QtWidgets", &PyInit_PySide6_QtWidgets);

            // 2. Set Python home to the bundled stdlib
            NSString *resourcePath = [[NSBundle mainBundle] resourcePath];
            NSString *stdlibPath   = [resourcePath stringByAppendingPathComponent:@"python"];
            Py_SetPythonHome((wchar_t *)[stdlibPath UTF8String]);

            // 3. Initialize CPython
            Py_Initialize();
            if (!Py_IsInitialized()) {{
                NSLog(@"[pyside6-ios] ERROR: Py_Initialize() failed");
                return NO;
            }}

            // 4. Append app bundle resource path to sys.path
            PyObject *sys  = PyImport_ImportModule("sys");
            PyObject *path = PyObject_GetAttrString(sys, "path");
            PyList_Append(path, PyUnicode_FromString([resourcePath UTF8String]));
            Py_DECREF(path);
            Py_DECREF(sys);

            // 5. Run Qt + Python application
            {qt_init_code}

            return YES;
        }}

        @end

        int main(int argc, char *argv[])
        {{
            // Host app owns UIApplicationMain -- Qt integrates via QIOSEventDispatcher
            @autoreleasepool {{
                return UIApplicationMain(argc, argv, nil,
                    NSStringFromClass([AppDelegate class]));
            }}
        }}
    """).format(qt_includes=qt_includes, qt_init_code=qt_init_code)
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
    xcodeproj_candidates = _rglob(app_dir, '*.xcodeproj')
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
    setup_venv(ctx)
    install_qt(ctx)
    install_python_framework(ctx)
    clone_pyside_sources(ctx)
    install_pyside6_ios_tool(ctx)
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
    generate_main_mm(app_dir, app_module, ui_mode)
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
            Examples:
              # Full pipeline for a new app
              python pyside6_ios_builder.py \\
                  --app myapp/ --app-name "My App" --bundle-id com.example.myapp \\
                  --app-module myapp.main

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
    app_grp.add_argument('--app', metavar='DIR', help='App source directory.')
    app_grp.add_argument('--app-name', metavar='NAME', default='MyPySide6App', help='Human-readable app name.')
    app_grp.add_argument('--bundle-id', metavar='ID', default='com.example.myapp', help='iOS bundle identifier.')
    app_grp.add_argument('--app-module', metavar='MOD', default='main', help='Python module containing _run().')
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
                generate_main_mm(app_dir, args.app_module, args.ui_mode)
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
