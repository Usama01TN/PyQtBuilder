#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pyqt6_ios_builder.py
====================
A professional, automated build pipeline for compiling PyQt6 applications
as native iOS apps using the modified pyqtdeploy toolchain.

Based on:
  - https://oforoshima.medium.com/compiling-ios-apps-with-pyqt6-c07082001f11
  - https://oforoshima.medium.com/tutorial-creating-a-todo-app-with-pyqt6-ios-7b671ebe4eab
  - https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment

Requirements (macOS only):
  - macOS with Xcode installed and configured
  - Qt 6.9.x installed from https://www.qt.io/download
      -> Include: iOS module + Qt5 Compatibility Module (Additional Libraries)
  - Python 3.x (via miniconda or system)
  - pip packages: PyQt6, PyQt6_sip, pyqt-builder
  - Modified pyqtdeploy from:
      https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment

Usage:
  python pyqt6_ios_builder.py --app myapp.py --qmake /path/to/Qt/6.9.x/ios/bin/qmake
  python pyqt6_ios_builder.py --app myapp.py --qmake /path/to/qmake --qt-version 6.9.2 --python-version 3.12.0
  python pyqt6_ios_builder.py --check-deps
  python pyqt6_ios_builder.py --install-pyqtdeploy

Python 2/3 compatibility notes
-------------------------------
  - No pathlib -- all paths use os.path, glob, fnmatch, and io.
  - subprocess.run shimmed for Python 2 via Popen.
  - urllib.request / urllib handled via try/except.
  - shutil.which shimmed for Python 2.
  - typing is optional (try/except); all annotations removed from signatures.
  - list[str] generic syntax (3.9+) replaced with comments.
  - Keyword-only arguments (*) removed; all args positional/keyword.
  - *iterable spread inside list literals replaced with list concatenation.
  - print(..., flush=True) replaced with explicit sys.stdout.flush().
  - exit() replaced with sys.exit().
"""
from os.path import exists, join, isdir, basename, dirname, abspath, splitext
from argparse import RawDescriptionHelpFormatter, ArgumentParser
from logging import basicConfig, INFO, getLogger, DEBUG
from os import listdir, makedirs
from tempfile import gettempdir
from platform import system
from fnmatch import fnmatch
from textwrap import dedent
from zipfile import ZipFile
from sys import path, exit
from glob import glob
import io

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import run, which, getXcodeSelectExecutable, getPyqtdeploySysrootExecutable, \
        getPyqtdeployBuildExecutable, getMakeExecutable, getPythonExecutable
except:
    from builders import run, which, getXcodeSelectExecutable, getPyqtdeploySysrootExecutable, \
        getPyqtdeployBuildExecutable, getMakeExecutable, getPythonExecutable

# -- urllib ------------------------------------------------------------------
try:
    from urllib import urlretrieve  # noqa: F401
except:
    from urllib.request import urlretrieve  # noqa: F401


def _makedirs(pth):
    """
    Create *path* and all missing parent directories.
    Equivalent to Path.mkdir(parents=True, exist_ok=True).
    """
    try:
        makedirs(pth)
    except OSError:
        if not isdir(pth):
            raise


def _write_text(pth, text, encoding='utf-8'):
    """
    Write the unicode string *text* to *path*.
    Uses io.open so the encoding keyword.
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
    """
    return sorted(glob(join(directory, pattern)))


def _stem(pth):
    """
    Return the filename stem (no directory, no final extension).
    Equivalent to Path.stem.
    Example: _stem("/foo/bar/myapp.py") == "myapp"
    :param pth: str
    :return: str
    """
    return splitext(basename(pth))[0]


# ---------------------------------------------------------------------------
# Logging setup.
# ---------------------------------------------------------------------------
basicConfig(level=INFO, format='%(asctime)s  %(levelname)-8s  %(message)s', datefmt='%H:%M:%S')
log = getLogger('pyqt6-ios-builder')
# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------
MODIFIED_PYQTDEPLOY_ZIP_URL = 'https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment/archive/refs/heads/main.zip'
MODIFIED_PYQTDEPLOY_REPO = 'https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment'  # type: str
DEFAULT_QT_VERSION = '6.9.1'  # type: str
DEFAULT_PYTHON_VERSION = '3.12.0'  # type: str
DEFAULT_TARGET = 'ios-64'  # type: str
BUILD_DIR_NAME = 'build-ios-64'  # type: str
# Qt modules frozen into the sysroot -- trimmed to those known to compile cleanly.
SYSROOT_PYQT6_MODULES = ['QtCore', 'QtGui', 'QtWidgets', 'QtQuick', 'QtQml']  # type: list[str]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

class BuildError(RuntimeError):
    """
    Raised when any build step fails.
    """


def _run(cmd, cwd=None, capture=False):
    """
    Run a shell command, streaming output unless *capture* is True.
    NOTE: The original Python-3-only keyword-only argument separator (*) has
    been removed.  Always use keyword syntax when calling this function to preserve readability.
    :param cmd: (list[str]) list of str -- command tokens.
    :param cwd: (str | None) str or None -- working directory.
    :param capture: (bool) -- if True, capture and return stdout.
    :return: (str) Stripped stdout string when capture=True, else ''.
    """
    display = ' '.join(str(c) for c in cmd)
    log.debug('$ %s', display)
    result = run(cmd, cwd=cwd, capture_output=capture, text=True)
    if result.returncode != 0:
        if capture:
            log.error('STDOUT:\n%s', result.stdout)
            log.error('STDERR:\n%s', result.stderr)
        raise BuildError('Command failed (exit {}): {}'.format(result.returncode, display))
    return result.stdout.strip() if capture else ''


def check_macos():
    """
    Assert we are running on macOS.
    :return:
    """
    if system() != 'Darwin':
        raise BuildError('iOS builds require macOS with Xcode. Current platform: {}'.format(system()))


def which_required(binary):
    """
    Return the full path to *binary* as a str, raising BuildError if not found.
    :param binary: str
    :return: str
    """
    pth = which(binary)
    if not pth:
        raise BuildError("'{}' not found on PATH. Please install it and ensure it is on your PATH.".format(binary))
    return pth


def pip_install(*packages):
    """
    Install/upgrade one or more pip packages using the current interpreter.
    NOTE: The original code used  [executable, ..., *packages]  (PEP 448
    iterable unpacking inside a list literal), which is Python 3 only.
    Replaced with explicit list concatenation for Py2/3 compatibility.
    :param packages: (str) Package names passed as positional arguments.
    :return:
    """
    log.info('pip install %s', ' '.join(packages))
    _run([getPythonExecutable(), '-m', 'pip', 'install', '--upgrade'] + list(packages))


# ---------------------------------------------------------------------------
# Step 1 - Dependency check
# ---------------------------------------------------------------------------

def check_dependencies(qmake_path=None):
    """
    Verify that all prerequisite tools and packages are present.
    :param qmake_path: str | None
    :return:
    """
    log.info('=== Checking dependencies ===')
    check_macos()
    # Xcode command-line tools.
    try:
        log.info('  Xcode tools : %s', _run([getXcodeSelectExecutable(), '-p'], capture=True))
    except BuildError:
        raise BuildError('Xcode command-line tools not found. Run: xcode-select --install')
    # qmake (iOS).
    if qmake_path:
        if not exists(qmake_path):
            raise BuildError('qmake not found at: {}'.format(qmake_path))
        log.info('  qmake       : %s', qmake_path)
    else:
        log.warning('  qmake path not provided -- skipping qmake check.')
    # Python packages.
    missing = []
    for pkg in ['PyQt6', 'PyQt6.sip', 'pyqtbuild']:
        try:
            __import__(pkg.replace('-', '_').replace('.', '_').split('_')[0])
        except ImportError:
            # Some packages use different import names; just check via pip.
            pass
    result = _run([getPythonExecutable(), '-m', 'pip', 'list', '--format=columns'], capture=True)
    installed_lower = result.lower()
    for pkg in ('pyqt6', 'pyqt6-sip', 'pyqt-builder'):
        if pkg.replace('-', '').replace('_', '') not in \
                installed_lower.replace('-', '').replace('_', ''):
            missing.append(pkg)
    if missing:
        log.warning('  Missing pip packages: %s', ', '.join(missing))
        log.warning('  Run: pip install %s', ' '.join(missing))
    else:
        log.info('  PyQt6 packages: OK')
    # Modified pyqtdeploy.
    deploy_path = which('pyqtdeploy-sysroot')
    if deploy_path:
        log.info('  pyqtdeploy-sysroot : %s', deploy_path)
    else:
        log.warning('  pyqtdeploy-sysroot not found. Run with --install-pyqtdeploy to fetch the modified version.')
    log.info('Dependency check complete.')


# ---------------------------------------------------------------------------
# Step 2 - Install modified pyqtdeploy.
# ---------------------------------------------------------------------------

def install_modified_pyqtdeploy(work_dir):
    """
    Download OforOshima's modified pyqtdeploy and install it via pip.
    The modification patches out the iOS-incompatible SIP code and removes
    modules that block compilation on Qt 6.x.
    :param work_dir: (str) Working directory for the download.
    :return:
    """
    log.info('=== Installing modified pyqtdeploy ===')
    log.info('  Source: %s', MODIFIED_PYQTDEPLOY_REPO)
    zip_path = join(work_dir, 'pyqtdeploy-pyqt6ios.zip')
    extract_dir = join(work_dir, 'pyqtdeploy-pyqt6ios-experiment-main')
    # Download.
    log.info('  Downloading zip archive ...')
    try:
        urlretrieve(MODIFIED_PYQTDEPLOY_ZIP_URL, zip_path)
    except Exception as exc:
        raise BuildError('Failed to download modified pyqtdeploy: {}\n'
                         'Please download manually from {} and run: pip install .'.format(
            exc, MODIFIED_PYQTDEPLOY_REPO))
    # Extract.
    log.info('  Extracting ...')
    with ZipFile(zip_path, 'r') as zf:
        zf.extractall(work_dir)
    if not exists(extract_dir):
        # _glob only matches files; also check directories via os.listdir.
        candidates = [join(work_dir, name) for name in listdir(work_dir) if fnmatch(
            name, 'pyqtdeploy-pyqt6ios-experiment*') and isdir(join(work_dir, name))]
        if not candidates:
            raise BuildError('Could not locate extracted pyqtdeploy folder.')
        extract_dir = sorted(candidates)[0]
    # Install
    log.info('  Running pip install from: %s', extract_dir)
    pip_install(extract_dir)
    log.info('  Modified pyqtdeploy installed successfully.')


# ---------------------------------------------------------------------------
# Step 3 - Generate sysroot.toml
# ---------------------------------------------------------------------------

def generate_sysroot_toml(output_path, qt_version, python_version, pyqt_source_tarball=None):
    """
    Write a sysroot.toml file tuned for iOS cross-compilation with PyQt6.
    IMPORTANT: qt_version must exactly match your Qt installation so that
    pyqtdeploy-sysroot does not attempt a full Qt source rebuild.
    :param output_path:         (str) Destination path for sysroot.toml
    :param qt_version:          str
    :param python_version:      str
    :param pyqt_source_tarball: (str | None) Path to a patched PyQt6-*.tar.gz
    :return:
    """
    log.info('=== Generating sysroot.toml (%s) ===', basename(output_path))
    # If a patched tarball is provided use it; otherwise fall back to the
    # bundled one that ships with the modified pyqtdeploy experiment repo.
    if pyqt_source_tarball:
        pyqt6_source_line = 'source = "{}"'.format(pyqt_source_tarball)
    else:
        pyqt6_source_line = '# source = ""  # Uses bundled patched tarball from modified pyqtdeploy'
    # Build the frozen PyQt6 module list.
    modules_list = '\n        '.join('"{}",'.format(m) for m in SYSROOT_PYQT6_MODULES)
    toml_content = dedent("""\
        # sysroot.toml  --  Auto-generated by pyqt6_ios_builder.py
        # ---------------------------------------------------------
        # Tuned for iOS cross-compilation with PyQt6 (modified pyqtdeploy).
        # Edit versions to match your local Qt / Python installation.
        # ---------------------------------------------------------

        [Python]
        version = "{python_version}"
        # iOS-specific build flags
        build_args = [
            "--enable-framework",
            "--without-doc-strings",
        ]

        [Qt]
        version = "{qt_version}"
        # Do NOT change this unless you want a full Qt source rebuild.

        [SIP]
        version = "6.9.1"

        [PyQt6]
        # !! IMPORTANT: Keep this at 6.9.1 -- the bundled patched tarball is 6.9.1.
        # Changing it will trigger a fresh download of the unpatched upstream source.
        version = "6.9.1"
        {pyqt6_source_line}

        # Include only the Qt modules known to compile cleanly on iOS with
        # this modified toolchain. Add more with caution.
        [PyQt6.modules]
        include = [
            {modules_list}
        ]

        [OpenSSL]
        version = "3.0.13"
        # Needed by Python's ssl module on iOS.
    """).format(python_version=python_version, qt_version=qt_version, pyqt6_source_line=pyqt6_source_line,
                modules_list=modules_list)
    _write_text(output_path, toml_content)
    log.info('  Written: %s', output_path)


# ---------------------------------------------------------------------------
# Step 4 - Build sysroot
# ---------------------------------------------------------------------------

def build_sysroot(project_dir, qmake_path, target=DEFAULT_TARGET, verbose=True):
    """
    Run pyqtdeploy-sysroot to cross-compile Python + Qt + PyQt6 for iOS.
    This step is long (can take 30-90 min on first run) but is cached on reruns.
    :param project_dir: str
    :param qmake_path:  str
    :param target:      str
    :param verbose:     bool
    :return:
    """
    log.info('=== Building sysroot (target=%s) ===', target)
    log.info('  This can take 30-90 minutes on a first run.')
    sysroot_toml = join(project_dir, 'sysroot.toml')
    if not exists(sysroot_toml):
        raise BuildError('sysroot.toml not found at {}'.format(sysroot_toml))
    cmd = [getPyqtdeploySysrootExecutable(), '--target', target, '--qmake={}'.format(qmake_path)]
    if verbose:
        cmd.append('--verbose')
    cmd.append(sysroot_toml)
    _run(cmd, cwd=project_dir)
    log.info('  Sysroot build complete.')


# ---------------------------------------------------------------------------
# Step 5 - Generate / validate .pdt project file
# ---------------------------------------------------------------------------

def ensure_pdt_file(project_dir, app_script, pdt_path=None):
    """
    Return the path to a .pdt project file (str).
    If *pdt_path* is given and exists, use it directly.
    Otherwise, write a minimal template and instruct the user to open it in
    the `pyqtdeploy` GUI to complete the Packages configuration.
    A .pdt file is an XML document that records:
      - the main Python entry-point.
      - which stdlib/PyQt6 packages to freeze into the bundle.
      - the path to sysroot.toml
    :param project_dir: str
    :param app_script:  str
    :param pdt_path:    str | None
    :return: (str) Path to the .pdt file
    """
    if pdt_path and exists(pdt_path):
        log.info('  Using existing .pdt file: %s', pdt_path)
        return pdt_path
    stem = _stem(app_script)
    generated_pdt = join(project_dir, '{}.pdt'.format(stem))
    log.info('=== Generating .pdt project file (%s) ===', basename(generated_pdt))
    log.warning(
        '  A minimal .pdt template will be written.\n'
        '  For a complete build you should open it with:\n'
        '      pyqtdeploy %s\n'
        '  and manually check the PyQt6 packages on the Packages tab.', generated_pdt)
    # Minimal PDT XML -- pyqtdeploy 3.x schema.
    # This is enough to kick off pyqtdeploy-build; the GUI can refine it.
    pdt_content = dedent("""\
        <?xml version="1.0" encoding="utf-8"?>
        <!DOCTYPE Project>
        <Project version="3">
            <Application
                name="{stem}"
                entry_point="{app_name}"
                sys_path=""
                application_package="false"
                pyqtdeploy_version="3.0"/>
            <Python
                source_dir=""
                host_installation_bin_dir=""
                target_installation_dir=""/>
            <SysRootSpecification
                specification_file="sysroot.toml"/>
            <PyQt>
                <Module name="PyQt6.QtCore"/>
                <Module name="PyQt6.QtGui"/>
                <Module name="PyQt6.QtWidgets"/>
            </PyQt>
            <StdlibPackages>
                <Package name="codecs"/>
                <Package name="io"/>
                <Package name="os"/>
                <Package name="sys"/>
                <Package name="types"/>
                <Package name="warnings"/>
            </StdlibPackages>
        </Project>
    """).format(stem=stem, app_name=basename(app_script))
    _write_text(generated_pdt, pdt_content)
    log.info('  Written: %s', generated_pdt)
    return generated_pdt


# ---------------------------------------------------------------------------
# Step 6 - pyqtdeploy-build
# ---------------------------------------------------------------------------

def run_pyqtdeploy_build(project_dir, pdt_path, qmake_path, target=DEFAULT_TARGET, verbose=True):
    """
    Run pyqtdeploy-build to freeze Python source files, generate C++ wrappers,
    and produce a Qt .pro file inside the build directory.
    :param project_dir: str
    :param pdt_path:    str
    :param qmake_path:  str
    :param target:      str
    :param verbose:     bool
    :return: (str) Path to the build directory.
    """
    log.info('=== Running pyqtdeploy-build ===')
    cmd = [getPyqtdeployBuildExecutable(), '--target', target, '--qmake={}'.format(qmake_path)]
    if verbose:
        cmd.append('--verbose')
    cmd.append(pdt_path)
    _run(cmd, cwd=project_dir)
    build_dir = join(project_dir, BUILD_DIR_NAME)
    if not exists(build_dir):
        raise BuildError(
            'Expected build directory not found: {}\npyqtdeploy-build may have used a different output path.'.format(
                build_dir))
    log.info('  Build directory: %s', build_dir)
    return build_dir


# ---------------------------------------------------------------------------
# Step 7 - Generate Xcode project via qmake.
# ---------------------------------------------------------------------------

def generate_xcodeproj(build_dir, qt_version, qmake_ios_path):
    """
    Inside the build directory, run qmake (macOS build) against widget.pro to
    produce a Makefile, then run `make xcodeproj` to generate the .xcodeproj.
    Two separate qmake binaries are needed:
      - macOS qmake  -> generates the Makefile.
      - iOS qmake    -> provides the iOS target Qt configuration.
    :param build_dir:      str
    :param qt_version:     str
    :param qmake_ios_path: str
    :return: (str) Path to the generated .xcodeproj
    """
    log.info('=== Generating Xcode project ===')
    # Derive macOS qmake from the iOS qmake path.
    # Typical layout: Qt/6.9.x/ios/bin/qmake  ->  Qt/6.9.x/macos/bin/qmake6
    ios_bin = dirname(qmake_ios_path)
    qt_root = dirname(dirname(ios_bin))
    macos_qmake = join(qt_root, 'macos', 'bin', 'qmake6')
    if not exists(macos_qmake):
        # Fallback: try qmake (without the 6 suffix).
        macos_qmake = join(qt_root, 'macos', 'bin', 'qmake')
    if not exists(macos_qmake):
        raise BuildError(
            'Could not find macOS qmake at {}.\n'
            'Please ensure Qt macOS components are installed alongside iOS.'.format(macos_qmake))
    ios_qtconf = join(ios_bin, 'target_qt.conf')
    if not exists(ios_qtconf):
        raise BuildError('iOS Qt configuration file not found: {}\nEnsure Qt iOS components are installed.'.format(
            ios_qtconf))
    # Locate the .pro file.
    pro_files = _glob(build_dir, '*.pro')
    if not pro_files:
        raise BuildError('No .pro file found in {}'.format(build_dir))
    pro_file = pro_files[0]
    log.info('  Found .pro file: %s', basename(pro_file))
    # Generate Makefile.
    log.info('  Running qmake to generate Makefile ...')
    _run([macos_qmake, '-o', 'Makefile', pro_file, '-qtconf={}'.format(ios_qtconf)], cwd=build_dir)
    # Generate .xcodeproj
    log.info('  Running make xcodeproj ...')
    _run([getMakeExecutable(), 'xcodeproj'], cwd=build_dir)
    xcodeproj_files = _glob(build_dir, '*.xcodeproj')
    if not xcodeproj_files:
        raise BuildError('No .xcodeproj generated in {}'.format(build_dir))
    xcodeproj = xcodeproj_files[0]
    log.info('  Xcode project: %s', xcodeproj)
    return xcodeproj


# ---------------------------------------------------------------------------
# Step 8 - Open in Xcode (optional).
# ---------------------------------------------------------------------------

def open_in_xcode(xcodeproj):
    """
    :param xcodeproj: str
    :return:
    """
    log.info('=== Opening in Xcode ===')
    _run(['open', xcodeproj])
    log.info('  Xcode launched. Build and run on simulator or device from there.')


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def build_ios_app(app_script, qmake_path, qt_version=DEFAULT_QT_VERSION, python_version=DEFAULT_PYTHON_VERSION,
                  pdt_path=None, pyqt_tarball=None, open_xcode=False, verbose=True, skip_sysroot=False):
    """
    End-to-end pipeline: source -> Xcode project.
    Steps
    -----
    1. Pre-flight checks.
    2. Resolve project directory.
    3. Generate sysroot.toml (if not present).
    4. Build sysroot              (pyqtdeploy-sysroot)  [skippable].
    5. Ensure .pdt project file.
    6. Build frozen bundle        (pyqtdeploy-build).
    7. Generate .xcodeproj        (qmake + make xcodeproj).
    8. Optionally open in Xcode.
    :param app_script:     (str) Path to the main PyQt6 Python script.
    :param qmake_path:     (str) Path to the iOS qmake binary.
    :param qt_version:     str
    :param python_version: str
    :param pdt_path:       str | None
    :param pyqt_tarball:   str | None
    :param open_xcode:     bool
    :param verbose:        bool
    :param skip_sysroot:   bool
    :return:
    """
    check_macos()
    app_script = abspath(app_script)
    if not exists(app_script):
        raise BuildError('Application script not found: {}'.format(app_script))
    project_dir = dirname(app_script)
    log.info('Project directory : %s', project_dir)
    log.info('App entry point   : %s', basename(app_script))
    log.info('Qt version        : %s', qt_version)
    log.info('Python version    : %s', python_version)
    log.info('qmake (iOS)       : %s', qmake_path)
    # -- sysroot.toml --
    sysroot_toml = join(project_dir, 'sysroot.toml')
    if not exists(sysroot_toml):
        generate_sysroot_toml(
            sysroot_toml, qt_version=qt_version, python_version=python_version, pyqt_source_tarball=pyqt_tarball)
    else:
        log.info('Existing sysroot.toml found -- skipping generation.')
    # -- pyqtdeploy-sysroot --
    if skip_sysroot:
        log.warning('Skipping sysroot build (--skip-sysroot). Ensure it was already built.')
    else:
        build_sysroot(project_dir, qmake_path=qmake_path, verbose=verbose)
    # -- .pdt file --
    resolved_pdt = ensure_pdt_file(project_dir, app_script=app_script, pdt_path=pdt_path)
    # -- pyqtdeploy-build --
    build_dir = run_pyqtdeploy_build(project_dir, pdt_path=resolved_pdt, qmake_path=qmake_path, verbose=verbose)
    # -- .xcodeproj --
    xcodeproj = generate_xcodeproj(build_dir, qt_version=qt_version, qmake_ios_path=qmake_path)
    log.info('')
    log.info('╔══════════════════════════════════════════════════╗')
    log.info('║             BUILD SUCCEEDED :)                   ║')
    log.info('╠══════════════════════════════════════════════════╣')
    log.info('║  Xcode project:                                  ║')
    log.info('║  %s', xcodeproj)
    log.info('╠══════════════════════════════════════════════════╣')
    log.info('║  Next steps:                                     ║')
    log.info('║  1. open %s', xcodeproj)
    log.info('║  2. Select an iOS Simulator or connected device  ║')
    log.info('║  3. Product -> Build (Cmd+B)                     ║')
    log.info('╚══════════════════════════════════════════════════╝')
    if open_xcode:
        open_in_xcode(xcodeproj)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    """
    :return: Namespace
    """
    parser = ArgumentParser(
        prog='pyqt6_ios_builder',
        description=(
            'Build pipeline: PyQt6 Python app -> native iOS .xcodeproj\n\n'
            'Requires macOS, Xcode, Qt 6.9.x (with iOS + Qt5Compat modules),\n'
            'and the modified pyqtdeploy from:\n  {}'.format(MODIFIED_PYQTDEPLOY_REPO)),
        formatter_class=RawDescriptionHelpFormatter,
        epilog=dedent("""\
            Examples:
              # Full build
              python pyqt6_ios_builder.py --app myapp.py --qmake ~/Qt/6.9.1/ios/bin/qmake

              # With explicit versions
              python pyqt6_ios_builder.py --app myapp.py --qmake ~/Qt/6.9.2/ios/bin/qmake --qt-version 6.9.2 --python-version 3.12.0

              # Skip the slow sysroot rebuild (already built)
              python pyqt6_ios_builder.py --app myapp.py --qmake ~/Qt/6.9.1/ios/bin/qmake --skip-sysroot

              # Check prerequisites only
              python pyqt6_ios_builder.py --check-deps --qmake ~/Qt/6.9.1/ios/bin/qmake

              # Download & install the modified pyqtdeploy
              python pyqt6_ios_builder.py --install-pyqtdeploy
        """))
    # Main action (mutually exclusive).
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--app', metavar='SCRIPT',
                        # type=Path removed; path stays as a plain str.
                        help='Path to the main PyQt6 Python script to compile for iOS.')
    action.add_argument('--check-deps', action='store_true', help='Check all prerequisites and exit.')
    action.add_argument('--install-pyqtdeploy', action='store_true',
                        help='Download and install the modified pyqtdeploy, then exit.')
    # Build options.
    parser.add_argument('--qmake', metavar="PATH", default=None,
                        help='Full path to the iOS qmake binary, e.g. ~/Qt/6.9.1/ios/bin/qmake')
    parser.add_argument(
        '--qt-version', metavar='VER', default=DEFAULT_QT_VERSION,
        help='Qt version installed (default: {}). Must match your Qt installation exactly.'.format(DEFAULT_QT_VERSION))
    parser.add_argument(
        '--python-version', metavar='VER', default=DEFAULT_PYTHON_VERSION,
        help='Python version to embed in the iOS app (default: {}).'.format(DEFAULT_PYTHON_VERSION))
    parser.add_argument('--pdt', metavar='FILE', default=None,
                        # type=Path removed; path stays as a plain str.
                        help='Path to an existing .pdt project file (skips auto-generation).')
    parser.add_argument('--pyqt-tarball', metavar='FILE', default=None,
                        # type=Path removed; path stays as a plain str.
                        help='Path to a patched PyQt6-*.tar.gz (uses the bundled one if omitted).')
    parser.add_argument(
        '--skip-sysroot', action='store_true', help='Skip the sysroot build step (use when already built).')
    parser.add_argument(
        '--open-xcode', action='store_true', help='Automatically open the .xcodeproj in Xcode when done.')
    parser.add_argument('--verbose', action="store_true", default=True,
                        help='Pass --verbose to pyqtdeploy tools (default: on).')
    parser.add_argument('--quiet', action='store_true', help='Suppress verbose output from pyqtdeploy tools.')
    parser.add_argument('--work-dir', metavar='DIR',
                        # type=Path removed; default is now a plain str via join.
                        default=join(gettempdir(), 'pyqt6-ios-builder'),
                        help='Working directory for downloads (default: system temp).')
    parser.add_argument('--debug', action='store_true', help='Enable DEBUG-level logging.')
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """
    :return:
    """
    args = parse_args()
    if args.debug:
        getLogger().setLevel(DEBUG)
    verbose = args.verbose and not args.quiet
    try:
        # -- --check-deps --
        if args.check_deps:
            check_dependencies(qmake_path=args.qmake)
            exit(0)
        # -- --install-pyqtdeploy --
        if args.install_pyqtdeploy:
            work_dir = args.work_dir
            _makedirs(work_dir)
            install_modified_pyqtdeploy(work_dir)
            exit(0)
        # -- Full build --
        if not args.qmake:
            log.error('--qmake is required for a build.\nExample: --qmake ~/Qt/6.9.1/ios/bin/qmake')
            exit(1)
        # Install base pip dependencies if missing.
        log.info('Ensuring base pip packages are installed ...')
        pip_install('PyQt6', 'PyQt6-sip', 'pyqt-builder')
        build_ios_app(
            app_script=args.app,
            qmake_path=args.qmake,
            qt_version=args.qt_version,
            python_version=args.python_version,
            pdt_path=args.pdt,
            pyqt_tarball=args.pyqt_tarball,
            open_xcode=args.open_xcode,
            verbose=verbose,
            skip_sysroot=args.skip_sysroot)
    except BuildError as exc:
        log.error('Build failed: %s', exc)
        exit(1)
    except KeyboardInterrupt:
        log.warning('Interrupted by user.')
        exit(130)


if __name__ == "__main__":
    main()
