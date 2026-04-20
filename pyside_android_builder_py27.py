#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
"""
PySide for Android Builder  (Python 2.7)
=========================================
Automates the complete pipeline for building Shiboken & PySide for Android
using the Necessitas SDK, then packages them with your Python 2.7 application into a standalone APK.

References / Sources:
--------------------
- https://modrana.org/trac/wiki/PySideForAndroid
- https://github.com/M4rtinK/android-pyside-build-scripts
- https://github.com/M4rtinK/android-pyside-example-project
- https://github.com/M4rtinK/expyside/tree/android
- https://github.com/M4rtinK/shiboken-android
- https://github.com/M4rtinK/pyside-android

Pipeline overview:
-----------------
  1.  Preflight         -- Ubuntu host, Python 2.7, cmake, git, API-14 check.
  2.  Clone sources     -- android-pyside-build-scripts + shiboken + pyside forks.
  3.  Environment       -- write env.sh equivalents into the build process.
  4.  Android Python    -- download pre-built Python 2.7 for Android.
  5.  Build Shiboken    -- cmake cross-compile Shiboken for ARM.
  6.  Build PySide      -- cmake cross-compile PySide for ARM.
  7.  Strip binaries    -- reduce .so size with arm-linux-androideabi-strip.
  8.  App packaging     -- create my_python_project.zip + python27.zip.
  9.  C++ wrapper       -- generate main.h / main.cpp with correct paths.
  10. Project scaffold  -- clone example project, rename & inject your app.
  11. Rename            -- sed-based rename of all project files.
  12. Build APK         -- ant debug via Necessitas build system.
  13. Deploy            -- optional adb install to connected device.
  14. Summary           -- print artifact paths + full error FAQ.

Usage:
-----
    python2.7 pyside_android_builder_py27.py --project-dir /path/to/myapp [OPTIONS]

    # Full automated build:
    python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas --app-name MyApp
        --unique-name com.example.MyApp
    # Use pre-built PySide libs (skip Shiboken/PySide compile):
    python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas
        --pyside-stage /path/to/stage
    # Build + deploy to connected device:
    python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas --install-apk
    # Verbose dry-run (print commands without executing):
    python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas --dry-run --verbose

Requirements (host machine):
----------------------------
    Ubuntu 12.04 / 12.10 / 14.04 (64-bit)
    Python 2.7  (host interpreter, this very script)
    Necessitas SDK  (contains Qt 4.8.x + Android NDK 8b1)
    Android SDK API 14 installed inside the Necessitas SDK
    System-wide Shiboken  (apt-get install shiboken)
    cmake >= 2.8
    git
    ant (for APK build)
    ~10 GB free disk space
"""
from os.path import join, isfile, isdir, expanduser, realpath, basename, relpath, getsize, pathsep, dirname, exists
from os import rename, walk, listdir, chmod, environ, access, X_OK, makedirs, statvfs
from argparse import RawDescriptionHelpFormatter, ArgumentParser
from sys import exit, version_info, argv, executable, stdin, path
from logging import basicConfig, getLogger, INFO, DEBUG
from shutil import rmtree, copytree, copy2
from zipfile import ZipFile, ZIP_DEFLATED
from platform import release, system
from subprocess import PIPE, Popen
from textwrap import dedent
from errno import EEXIST
from re import compile
import tarfile

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import getGitExecutable, getCmakeExecutable, getAntExecutable, getMakeExecutable
except:
    from builders import getGitExecutable, getCmakeExecutable, getAntExecutable, getMakeExecutable

try:
    from urllib.request import urlopen
except:
    from urllib import urlopen

# ---------------------------------------------------------------------------
# Python 2.7 compatibility shims.
# ---------------------------------------------------------------------------
try:
    from distutils.spawn import find_executable
except ImportError:
    find_executable = None
# ===========================================================================
# Constants.
# ===========================================================================
# Necessitas / Shiboken / PySide repos (Android-patched forks).
SHIBOKEN_ANDROID_REPO = 'https://github.com/M4rtinK/shiboken-android.git'
PYSIDE_ANDROID_REPO = 'https://github.com/M4rtinK/pyside-android.git'
BUILD_SCRIPTS_REPO = 'https://github.com/M4rtinK/android-pyside-build-scripts.git'
EXAMPLE_PROJECT_REPO = 'https://github.com/M4rtinK/android-pyside-example-project.git'
# Pre-built Android Python 2.7 bundle (from modrana.org binary listing).
PYTHON_ANDROID_ZIP_URL = 'http://www.modrana.org/platforms/android/python2.7/python2.7_for_android_v1.zip'
# Qt Components for Android.
QT_COMPONENTS_URL = 'http://modrana.org/platforms/android/qt_components/qt_components_v1.zip'
QT_COMPONENTS_THEME_URL = 'http://modrana.org/platforms/android/qt_components/qt_components_theme_mini_v1.zip'
# Android API level required (as stated by modrana.org guide).
REQUIRED_API = '14'
NDK_TOOLCHAIN_PREFIX = 'arm-linux-androideabi'
DEFAULT_BUILD_THREADS = 4
# Placeholder unique name used in the example project.
EXAMPLE_UNIQUE_NAME = 'org.modrana.PySideExample'
EXAMPLE_APP_NAME = 'PySideExample'
# Required minimum disk space in MB.
MIN_DISK_MB = 8192
# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
basicConfig(format=LOG_FORMAT, datefmt="%H:%M:%S", level=INFO)
log = getLogger("pyside-android-builder")


def step(title):
    """
    Print a visually distinct step header.
    :param title: str
    :return:
    """
    bar = '=' * 66
    log.info("\n%s\n  %s\n%s", bar, title, bar)


# ===========================================================================
# Build configuration (plain class, Python 2.7 has no dataclasses)
# ===========================================================================

class BuildConfig(object):
    """
    All resolved paths and options for a single build run.
    """

    def __init__(self, args):
        """
        :param args: any
        """
        self.project_dir = realpath(args.project_dir)
        self.app_name = args.app_name or basename(self.project_dir)
        self.unique_name = args.unique_name or ("com.example.%s" % self.app_name)
        self.necessitas_sdk = expanduser(args.necessitas_sdk)
        self.pyside_stage = expanduser(args.pyside_stage) if args.pyside_stage else None
        self.build_threads = args.build_threads
        self.verbose = args.verbose
        self.dry_run = args.dry_run
        self.skip_build = args.skip_build
        self.install_apk = args.install_apk
        self.keep_build = args.keep_build
        # Derived: all work lives under ~/.pyside_android_build
        self.work_dir = expanduser("~/.pyside_android_build")
        self.scripts_dir = join(self.work_dir, "android-pyside-build-scripts")
        self.shiboken_src = join(self.work_dir, "shiboken-android")
        self.pyside_src = join(self.work_dir, "pyside-android")
        self.stage_dir = self.pyside_stage or join(self.work_dir, "stage")
        self.android_python = join(self.work_dir, "android_python")
        self.project_build = join(self.work_dir, "project")
        self.downloads_dir = join(self.work_dir, "downloads")
        # Install path on the Android device.
        self.install_path = "/data/data/%s" % self.unique_name
        # APK output.
        self.apk_path = join(self.project_build, "android", "bin", "%s-debug.apk" % self.app_name)

    # -----------------------------------------------------------------------
    # Derived paths inside the Necessitas SDK.
    # -----------------------------------------------------------------------

    @property
    def ndk_root(self):
        """
        Path to the Android NDK bundled with Necessitas.
        :return: str
        """
        # Necessitas embeds NDK under necessitas/android-ndk-*
        for name in listdir(self.necessitas_sdk):
            if name.startswith("android-ndk"):
                return join(self.necessitas_sdk, name)
        # Fallback: common location.
        return join(self.necessitas_sdk, "android-ndk-r8b")

    @property
    def qt_android_dir(self):
        """
        Qt 4.8 ARM directory inside Necessitas.
        :return: str
        """
        # Necessitas places Qt under necessitas/NecessitasQt/Qt4.8.x/...
        for root, dirs, _ in walk(self.necessitas_sdk):
            for d in dirs:
                if d in ["android_armeabi", "android_armeabi-v7a"]:
                    return join(root, d)
            break  # only check one level deep.
        # Fallback.
        return join(self.necessitas_sdk, "NecessitasQt", "qt", "android_armeabi-v7a")

    @property
    def qmake(self):
        """
        :return: str
        """
        return join(self.qt_android_dir, "bin", "qmake")

    @property
    def sdk_root(self):
        """
        Android SDK path embedded in Necessitas.
        :return: str
        """
        for name in listdir(self.necessitas_sdk):
            if name.startswith("android-sdk") or name == "Sdk":
                return join(self.necessitas_sdk, name)
        return join(self.necessitas_sdk, "android-sdk")

    @property
    def toolchain_bin(self):
        """
        arm-linux-androideabi-* compiler bin directory inside the NDK.
        :return: str
        """
        candidate = join(self.ndk_root, "toolchains", "arm-linux-androideabi-4.6", "prebuilt", "linux-x86_64", "bin")
        if isdir(candidate):
            return candidate
        # 32-bit host fallback.
        candidate32 = candidate.replace("linux-x86_64", "linux-x86")
        return candidate32 if isdir(candidate32) else candidate

    @property
    def ndk_sysroot(self):
        """
        :return: str
        """
        return join(self.ndk_root, "platforms", "android-%s" % REQUIRED_API, "arch-arm")

    @property
    def adb_exe(self):
        """
        :return: str
        """
        return join(self.sdk_root, "platform-tools", "adb")


# ===========================================================================
# Subprocess helpers.
# ===========================================================================

def _run(cmd, cwd=None, env=None, check=True, dry_run=False, capture=False):
    """
    Execute *cmd* (list of strings) with unified error handling.
    Returns a subprocess.Popen-compatible object with .returncode,
    .stdout, .stderr attributes.
    :param cmd: list[str]
    :param cwd: str | None
    :param env: dict[str, str] | None
    :param check: bool
    :param dry_run: bool
    :param capture: bool
    :return: subprocess.Popen-compatible object
    """
    display = " ".join(str(c) for c in cmd)
    log.debug("$ %s  [cwd=%s]", display, cwd or ".")
    if dry_run:
        log.info("[DRY-RUN] %s", display)

        class _FakeResult(object):
            """
            _FakeResult class.
            """
            returncode = 0
            stdout = ""
            stderr = ""

        return _FakeResult()
    merged_env = dict(environ)
    if env:
        merged_env.update(env)
    kwargs = dict(cwd=cwd, env=merged_env)
    if capture:
        kwargs["stdout"] = PIPE
        kwargs["stderr"] = PIPE
    proc = Popen([str(c) for c in cmd], **kwargs)
    stdout, stderr = proc.communicate()
    proc.stdout = stdout or ''
    proc.stderr = stderr or ''
    if check and proc.returncode != 0:
        log.error("Command failed (exit %d):\n  %s", proc.returncode, display)
        if proc.stdout:
            log.error("stdout:\n%s", proc.stdout[-2000:])
        if proc.stderr:
            log.error("stderr:\n%s", proc.stderr[-2000:])
        raise RuntimeError("Subprocess exited with code %d" % proc.returncode)
    return proc


def _require_tool(name):
    """
    Assert that *name* is on PATH and return its full path.
    :param name: str
    :return: str
    """
    if find_executable is not None:
        path = find_executable(name)
    else:
        path = None
        for d in environ.get("PATH", "").split(pathsep):
            candidate = join(d, name)
            if isfile(candidate) and access(candidate, X_OK):
                path = candidate
                break
    if not path:
        raise EnvironmentError("Required tool '%s' not found on PATH.\nInstall it and re-run." % name)
    return path


def _makedirs(path):
    """
    os.makedirs without exist_ok (Python 2.7 compat).
    :param path: str
    :return:
    """
    try:
        makedirs(path)
    except OSError as exc:
        if exc.errno != EEXIST:
            raise


def _download(url, dest):
    """
    Download *url* -> *dest* using urllib2.
    :param url: str
    :param dest: str
    :return: None
    """
    _makedirs(dirname(dest))
    if exists(dest):
        log.info("Cached: %s", basename(dest))
        return
    log.info("Downloading  %s", url)
    try:
        response = urlopen(url, timeout=120)
        chunk = 8192
        with open(dest, "wb") as fh:
            while True:
                data = response.read(chunk)
                if not data:
                    break
                fh.write(data)
        log.info("Saved:       %s", dest)
    except Exception as exc:
        raise RuntimeError("Download failed: %s\n%s" % (url, exc))


def _extract_zip(archive, dest_dir):
    """
    Extract a ZIP file into *dest_dir*.
    :param archive: str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info("Extracting  %s  ->  %s", basename(archive), dest_dir)
    with ZipFile(archive, "r") as zf:
        zf.extractall(dest_dir)


def _extract_tar(archive, dest_dir):
    """
    Extract a .tar.gz / .tgz file into *dest_dir*.
    :param archive: str
    :param dest_dir: str
    :return:
    """
    _makedirs(dest_dir)
    log.info("Extracting  %s  ->  %s", basename(archive), dest_dir)
    with tarfile.open(archive) as tf:
        tf.extractall(dest_dir)


def _check_disk_space(path, required_mb=MIN_DISK_MB):
    """
    Warn if free disk space at *path* is below *required_mb* MB.
    :param path: str
    :param required_mb: int
    :return:
    """
    stat = statvfs(path if exists(path) else dirname(path))
    free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
    if free_mb < required_mb:
        log.warning('Low disk space: %.0f MB free at %s (recommended >= %d MB).', free_mb, path, required_mb)
    else:
        log.info('Disk space: %.0f MB free OK', free_mb)


def _sed_inplace(filepath, old, new):
    """
    In-place string replacement in *filepath* (sed -i equivalent).
    :param filepath: str
    :param old: str
    :param new: str
    :return:
    """
    with open(filepath, "r") as fh:
        content = fh.read()
    content = content.replace(old, new)
    with open(filepath, "w") as fh:
        fh.write(content)
    log.debug("  sed  '%s' -> '%s'  in  %s", old, new, filepath)


def _find_files_containing(directory, pattern):
    """
    Return list of text files under *directory* that contain *pattern*.
    Equivalent to: find . -type f | xargs grep "pattern"
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []
    for root, dirs, files in walk(directory):
        # skip .git
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in files:
            fpath = join(root, fname)
            try:
                with open(fpath, "r") as fh:
                    if pattern in fh.read():
                        matches.append(fpath)
            except:
                pass
    return matches


# ===========================================================================
# Step 1 -- Preflight checks
# ===========================================================================

def preflight_checks(cfg):
    """
    Validate the host environment.
    From modrana.org guide:
      Prerequisites:
        - Necessitas SDK (with Android SDK API 14)
        - system-wide Shiboken
        - system-wide Python 2.7
        - Python 2.7 compiled for Android
        - cmake
        - git
      sudo apt-get install build-essential cmake git python2.7-minimal shiboken
    :param cfg: BuildConfig
    :return:
    """
    step("Step 1/13 -- Preflight checks")
    # OS -- must be Linux (Ubuntu recommended).
    if system() != "Linux":
        raise EnvironmentError(
            'The Necessitas / PySide-Android pipeline requires a Linux host. Detected: %s' % system())
    log.info("Host OS:  %s %s", system(), release())
    # This script itself must be run under Python 2.7.
    major, minor = version_info[:2]
    if (major, minor) != (2, 7):
        raise EnvironmentError(
            "This script must be run with Python 2.7 (found %d.%d). Try: python2.7 %s" % (major, minor, argv[0]))
    log.info("Python:   %d.%d (host) OK", major, minor)
    # Required system tools.
    tools = [
        ("cmake", "sudo apt-get install cmake"),
        ("git", "sudo apt-get install git"),
        ("ant", "sudo apt-get install ant"),
        ("shiboken", "sudo apt-get install shiboken"),
        ("make", "sudo apt-get install build-essential"),
        ("gcc", "sudo apt-get install build-essential"),
        ("g++", "sudo apt-get install build-essential"),
        ("zip", "sudo apt-get install zip"),
        ("unzip", "sudo apt-get install unzip")]
    for tool, hint in tools:
        try:
            path = _require_tool(tool)
            log.info("Found:    %-12s  %s", tool, path)
        except EnvironmentError:
            raise EnvironmentError("'%s' not found on PATH.\n  Install with: %s" % (tool, hint))
    # Necessitas SDK.
    if not isdir(cfg.necessitas_sdk):
        raise EnvironmentError(
            "Necessitas SDK directory not found: %s\n"
            "Download from: http://necessitas.kde.org/necessitas/"
            "necessitas_sdk_installer.php\n"
            "Then re-run with --necessitas-sdk /path/to/necessitas"
            % cfg.necessitas_sdk)
    log.info("Necessitas SDK:  %s  OK", cfg.necessitas_sdk)
    # Android NDK inside Necessitas.
    if not isdir(cfg.ndk_root):
        raise EnvironmentError(
            "Android NDK not found under Necessitas SDK at: %s\n"
            "Expected a directory named android-ndk-* inside: %s" % (cfg.ndk_root, cfg.necessitas_sdk))
    log.info("NDK:      %s  OK", cfg.ndk_root)
    # Android SDK + API 14
    api14_dir = join(cfg.sdk_root, "platforms", "android-%s" % REQUIRED_API)
    if not isdir(api14_dir):
        raise EnvironmentError(
            "Android SDK API %s not found at: %s\nRun the SDKMaintenanceTool inside Necessitas, select "
            "'Package manager' and install API %s under Miscellaneous -> Android SDK."
            % (REQUIRED_API, api14_dir, REQUIRED_API))
    log.info("Android SDK API %s:  OK", REQUIRED_API)
    # Qt Android qmake.
    if not isfile(cfg.qmake):
        log.warning(
            "Qt Android qmake not found at: %s\n"
            "  This is expected if you are only building PySide libraries and not the final APK.", cfg.qmake)
    else:
        log.info("qmake:    %s  OK", cfg.qmake)
    # NDK toolchain (arm-linux-androideabi-g++).
    cxx = join(cfg.toolchain_bin, "arm-linux-androideabi-g++")
    if not isfile(cxx):
        log.warning('ARM C++ compiler not found at: %s\nThe build will fail at the cmake cross-compile stage.', cxx)
    else:
        log.info("ARM g++:  %s  OK", cxx)
    # Project directory.
    if not isdir(cfg.project_dir):
        raise IOError("Project directory not found: %s" % cfg.project_dir)
    log.info("Project:  %s  OK", cfg.project_dir)
    # Disk space.
    _check_disk_space(cfg.work_dir if exists(cfg.work_dir) else expanduser("~"))
    log.info("Preflight passed OK")


# ===========================================================================
# Step 2 -- Clone source repositories.
# ===========================================================================

def clone_sources(cfg):
    """
    Clone the Android-patched Shiboken, PySide and build-scripts repos.
    From modrana.org guide (prepare.sh equivalent):
      git clone https://github.com/M4rtinK/android-pyside-build-scripts.git
      cd android-pyside-build-scripts
      ./prepare.sh   # which clones shiboken-android + pyside-android
    :param cfg: BuildConfig
    :return:
    """
    step("Step 2/13 -- Cloning source repositories")
    _makedirs(cfg.work_dir)
    repos = [
        (BUILD_SCRIPTS_REPO, cfg.scripts_dir, 'android-pyside-build-scripts'),
        (SHIBOKEN_ANDROID_REPO, cfg.shiboken_src, 'shiboken-android'),
        (PYSIDE_ANDROID_REPO, cfg.pyside_src, 'pyside-android'),
        (EXAMPLE_PROJECT_REPO, cfg.project_build, 'android-pyside-example-project')]
    for url, dest, label in repos:
        if isdir(join(dest, ".git")):
            log.info('Already cloned: %s', label)
        else:
            log.info('Cloning %s ...', label)
            _run([getGitExecutable(), 'clone', '--branch', 'android', '--depth', '1', url, dest], dry_run=cfg.dry_run)
    # Create stage directory (M4rtinK build scripts expect ./stage).
    stage_lib = join(cfg.stage_dir, "lib")
    _makedirs(stage_lib)
    log.info('Stage dir: %s  OK', cfg.stage_dir)
    log.info('Sources ready OK')


# ===========================================================================
# Step 3 -- Environment variables (env.sh equivalent)
# ===========================================================================

def make_build_env(cfg):
    """
    Return a dict of environment variables needed for the cmake cross-compile.
    From M4rtinK/android-pyside-build-scripts/env.sh:
      NECESSITAS=/home/user/necessitas
      ANDROID_NDK=$NECESSITAS/android-ndk-r8b
      ANDROID_NDK_TOOLCHAIN_ROOT=$ANDROID_NDK/toolchains/arm-linux-androideabi-4.6/...
      SYSROOT=$ANDROID_NDK/platforms/android-14/arch-arm
      STAGING=$WORK_DIR/stage
      PATH=$ANDROID_NDK_TOOLCHAIN_ROOT/bin:$PATH
    :param cfg: BuildConfig
    :return:
    """
    step("Step 3/13 -- Build environment")
    toolchain_bin = cfg.toolchain_bin
    env = {
        # Necessitas
        "NECESSITAS": cfg.necessitas_sdk,
        # NDK
        "ANDROID_NDK": cfg.ndk_root,
        "ANDROID_NDK_TOOLCHAIN_ROOT": dirname(toolchain_bin),
        # Sysroot (target Android filesystem).
        "SYSROOT": cfg.ndk_sysroot,
        # Stage (install destination for cross-compiled libs).
        "STAGING": cfg.stage_dir,
        # Python 2.7 for Android.
        "ANDROID_PYTHON": cfg.android_python,
        # Thread count.
        "BUILD_THREAD_COUNT": str(cfg.build_threads),
        # Extend PATH with NDK toolchain.
        "PATH": toolchain_bin + pathsep + environ.get("PATH", "")}
    log.info("Build environment:")
    for k, v in sorted(env.items()):
        log.info("  %-35s = %s", k, v)
    # Write an env.sh for reference / manual use.
    env_sh = join(cfg.work_dir, "env.sh")
    with open(env_sh, "w") as fh:
        fh.write("#!/bin/sh\n# Generated by pyside_android_builder_py27.py\n\n")
        for k, v in sorted(env.items()):
            fh.write("export %s='%s'\n" % (k, v))
    chmod(env_sh, 0o755)
    log.info("env.sh written: %s", env_sh)
    return env


# ===========================================================================
# Step 4 -- Download pre-built Android Python 2.7
# ===========================================================================

def download_android_python(cfg):
    """
    Download the pre-built Python 2.7 for Android provided by modrana.org.
    From modrana.org binary listing:
      http://www.modrana.org/platforms/android/python2.7/python2.7_for_android_v1.zip
    This provides:
      python/bin/python  (ARM executable)
      python/lib/        (standard library + headers)
      python/include/    (C headers needed to build extensions)
    :param cfg: BuildConfig
    :return: str
    """
    step("Step 4/13 -- Android Python 2.7")
    _makedirs(cfg.downloads_dir)
    zip_dest = join(cfg.downloads_dir, "python2.7_for_android_v1.zip")
    _download(PYTHON_ANDROID_ZIP_URL, zip_dest)
    if not isdir(cfg.android_python):
        _extract_zip(zip_dest, cfg.android_python)
    else:
        log.info("Android Python already extracted: %s", cfg.android_python)
    # Verify we have the Python include directory (needed for Shiboken cmake).
    python_inc = None
    for root, dirs, files in walk(cfg.android_python):
        if "Python.h" in files:
            python_inc = root
            break
    if python_inc is None and not cfg.dry_run:
        log.warning("Python.h not found under %s — Shiboken cmake may fail.", cfg.android_python)
    else:
        log.info("Python.h:  %s  OK", python_inc)
    log.info("Android Python ready OK")
    return cfg.android_python


# ===========================================================================
# Step 5 -- Build Shiboken (cross-compile for ARM)
# ===========================================================================

def _write_shiboken_toolchain_file(cfg):
    """
    Write a CMake toolchain file for ARM cross-compilation of Shiboken.
    From M4rtinK/android-pyside-build-scripts/build_shiboken.sh:
      cmake -DCMAKE_TOOLCHAIN_FILE=...
            -DCMAKE_INSTALL_PREFIX=$STAGING
            -DPYTHON_EXECUTABLE=...
            -DPYTHON_INCLUDE_DIR=...
            -DPYTHON_LIBRARY=...
    :param cfg: BuildConfig
    :return:
    """
    toolchain_cmake = join(cfg.work_dir, "android_toolchain.cmake")
    sysroot = cfg.ndk_sysroot
    compiler_prefix = join(cfg.toolchain_bin, "arm-linux-androideabi")
    # Find Python include + lib paths inside android_python.
    python_inc = ""
    python_lib = ""
    for root, dirs, files in walk(cfg.android_python):
        if "Python.h" in files:
            python_inc = root
        for f in files:
            if f.startswith("libpython2.7") and f.endswith(".a"):
                python_lib = join(root, f)
    content = dedent("""\
        # CMake toolchain for ARM Android cross-compilation
        # Generated by pyside_android_builder_py27.py

        SET(CMAKE_SYSTEM_NAME Linux)
        SET(CMAKE_SYSTEM_PROCESSOR arm)

        SET(CMAKE_C_COMPILER   {prefix}-gcc)
        SET(CMAKE_CXX_COMPILER {prefix}-g++)
        SET(CMAKE_STRIP        {prefix}-strip)
        SET(CMAKE_AR           {prefix}-ar)
        SET(CMAKE_RANLIB       {prefix}-ranlib)

        SET(CMAKE_FIND_ROOT_PATH
            {sysroot}
            {staging}
            {android_python}
        )
        SET(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
        SET(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
        SET(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)

        SET(CMAKE_SYSROOT {sysroot})

        SET(PYTHON_EXECUTABLE  {python_exec})
        SET(PYTHON_INCLUDE_DIR {python_inc})
        SET(PYTHON_LIBRARY     {python_lib})
    """.format(
        prefix=compiler_prefix,
        sysroot=sysroot,
        staging=cfg.stage_dir,
        android_python=cfg.android_python,
        python_exec=executable,  # host python (cmake needs host)
        python_inc=python_inc,
        python_lib=python_lib,
    ))
    with open(toolchain_cmake, "w") as fh:
        fh.write(content)
    log.info("CMake toolchain written: %s", toolchain_cmake)
    return toolchain_cmake


def build_shiboken(cfg, env):
    """
    Cross-compile Shiboken for Android ARM.
    From M4rtinK/android-pyside-build-scripts/build_shiboken.sh:
      rm -rf build/shiboken
      mkdir -p build/shiboken
      cd build/shiboken
      cmake ../../shiboken-android \\
            -DCMAKE_TOOLCHAIN_FILE=... \\
            -DCMAKE_INSTALL_PREFIX=$STAGING \\
            -DCMAKE_BUILD_TYPE=Release \\
            -DENABLE_VERSION_SUFFIX=FALSE \\
            -DPYTHON_EXECUTABLE=...
            ...
      [Press any key to continue after reviewing cmake output]
      make -j$BUILD_THREAD_COUNT
      make install
    From modrana.org:
      "The script is fully automatic, but waits for the user to press any key:
       after Shiboken is configured for build"
    :param cfg: BuildConfig
    :param env: dict[str, str]
    :return: None
    """
    step("Step 5/13 -- Building Shiboken (ARM cross-compile)")
    if cfg.skip_build:
        log.info("--skip-build set; skipping Shiboken compilation.")
        return
    toolchain_cmake = _write_shiboken_toolchain_file(cfg)
    build_dir = join(cfg.work_dir, "build", "shiboken")
    # Clear previous build (mirrors build_shiboken.sh behavior)
    if isdir(build_dir):
        log.info("Clearing previous Shiboken build dir ...")
        rmtree(build_dir)
    _makedirs(build_dir)
    cmake_args = [
        getCmakeExecutable(), cfg.shiboken_src,
        "-DCMAKE_TOOLCHAIN_FILE=%s" % toolchain_cmake,
        "-DCMAKE_INSTALL_PREFIX=%s" % cfg.stage_dir,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DENABLE_VERSION_SUFFIX=FALSE",
        "-DUSE_PYTHON3=FALSE",
        # Tell Shiboken not to build its own test suite (saves time).
        "-DBUILD_TESTS=FALSE",
        "-DCMAKE_VERBOSE_MAKEFILE=%s" % ("ON" if cfg.verbose else "OFF")]
    log.info("Running cmake for Shiboken ...")
    _run(cmake_args, cwd=build_dir, env=env, dry_run=cfg.dry_run)
    # From modrana.org: wait after cmake so user can verify config.
    if not cfg.dry_run:
        print("\n  Shiboken cmake configured. Press Enter to start make ...", end="")
        stdin.readline()
    log.info("Building Shiboken (threads=%d) ...", cfg.build_threads)
    _run([getMakeExecutable(), "-j%d" % cfg.build_threads], cwd=build_dir, env=env, dry_run=cfg.dry_run)
    log.info("Installing Shiboken to %s ...", cfg.stage_dir)
    _run([getMakeExecutable(), "install"], cwd=build_dir, env=env, dry_run=cfg.dry_run)
    log.info("Shiboken built OK")


# ===========================================================================
# Step 6 -- Build PySide (cross-compile for ARM)
# ===========================================================================

def _fix_pyside_cmake_paths(cfg, build_dir):
    """
    Fix absolute host paths baked into cmake cache files after Shiboken install.
    From M4rtinK/android-pyside-build-scripts/fix_pyside_cmake_paths.sh:
      sed -i "s|SHIBOKEN_INCLUDE_DIR:PATH=.*|...|g" CMakeCache.txt
    Replaces host-absolute Shiboken paths with stage/ paths so PySide cmake
    finds the cross-compiled Shiboken rather than the system one.
    :param cfg: BuildConfig
    :param build_dir: str
    :return: None
    """
    cmake_cache = join(build_dir, "CMakeCache.txt")
    if not isfile(cmake_cache):
        return
    log.info("Fixing PySide cmake paths ...")
    replacements = [
        # Replace host shiboken include with stage include.
        (compile(r"SHIBOKEN_INCLUDE_DIR:PATH=.*"), "SHIBOKEN_INCLUDE_DIR:PATH=%s/include/shiboken" % cfg.stage_dir),
        (compile(r"SHIBOKEN_PYTHON_INCLUDE_DIR:PATH=.*"),
         "SHIBOKEN_PYTHON_INCLUDE_DIR:PATH=%s/include/python2.7" % cfg.android_python),
        (compile(r"SHIBOKEN_LIBRARY:FILEPATH=.*"), "SHIBOKEN_LIBRARY:FILEPATH=%s/lib/libshiboken.so" % cfg.stage_dir)]
    with open(cmake_cache, "r") as fh:
        content = fh.read()
    for pattern, replacement in replacements:
        content = pattern.sub(replacement, content)
    with open(cmake_cache, "w") as fh:
        fh.write(content)
    log.info("cmake paths fixed in %s", cmake_cache)


def build_pyside(cfg, env):
    """
    Cross-compile PySide for Android ARM.
    From M4rtinK/android-pyside-build-scripts/build_pyside.sh:
      rm -rf build/pyside
      mkdir -p build/pyside
      cd build/pyside
      cmake ../../pyside-android \\
            -DCMAKE_TOOLCHAIN_FILE=... \\
            -DCMAKE_INSTALL_PREFIX=$STAGING \\
            -DCMAKE_BUILD_TYPE=Release \\
            -DSHIBOKEN_INCLUDE_DIR=$STAGING/include/shiboken \\
            -DSHIBOKEN_LIBRARY=$STAGING/lib/libshiboken.so \\
            -DPYTHON_INCLUDE_DIR=... \\
            -DPYTHON_LIBRARY=... \\
            -DPYSIDE_ENABLE_STDDEBUG=FALSE
      [Press any key]
      make -j$BUILD_THREAD_COUNT
      make install
    From modrana.org build issues:
      "If you get arm-linux-androideabi-g++: Internal error: Killed (cc1plus),
       try setting BUILD_THREAD_COUNT to 1."
    :param cfg: BuildConfig
    :param env: dict[str, str]
    :return: None
    """
    step("Step 6/13 -- Building PySide (ARM cross-compile)")
    if cfg.skip_build:
        log.info("--skip-build set; skipping PySide compilation.")
        return
    # Find the toolchain cmake file written in Step 5
    toolchain_cmake = join(cfg.work_dir, "android_toolchain.cmake")
    if not isfile(toolchain_cmake) and not cfg.dry_run:
        raise IOError("Toolchain cmake not found: %s\nRun Step 5 (Shiboken build) first." % toolchain_cmake)
    # Find Python library
    python_lib = ""
    for root, dirs, files in walk(cfg.android_python):
        for f in files:
            if f.startswith("libpython2.7") and f.endswith(".a"):
                python_lib = join(root, f)
                break
    python_inc = ""
    for root, dirs, files in walk(cfg.android_python):
        if "Python.h" in files:
            python_inc = root
            break
    build_dir = join(cfg.work_dir, "build", "pyside")
    if isdir(build_dir):
        log.info("Clearing previous PySide build dir ...")
        rmtree(build_dir)
    _makedirs(build_dir)
    cmake_args = [
        getCmakeExecutable(),
        cfg.pyside_src,
        "-DCMAKE_TOOLCHAIN_FILE=%s" % toolchain_cmake,
        "-DCMAKE_INSTALL_PREFIX=%s" % cfg.stage_dir,
        "-DCMAKE_BUILD_TYPE=Release",
        "-DSHIBOKEN_INCLUDE_DIR=%s/include/shiboken" % cfg.stage_dir,
        "-DSHIBOKEN_LIBRARY=%s/lib/libshiboken.so" % cfg.stage_dir,
        "-DPYTHON_INCLUDE_DIR=%s" % python_inc,
        "-DPYTHON_LIBRARY=%s" % python_lib,
        "-DPYSIDE_ENABLE_STDDEBUG=FALSE",
        "-DBUILD_TESTS=FALSE",
        "-DCMAKE_VERBOSE_MAKEFILE=%s" % ("ON" if cfg.verbose else "OFF")]
    log.info("Running cmake for PySide ...")
    _run(cmake_args, cwd=build_dir, env=env, dry_run=cfg.dry_run)
    # Fix any absolute host paths baked into the cache.
    if not cfg.dry_run:
        _fix_pyside_cmake_paths(cfg, build_dir)
    # From modrana.org: wait after cmake.
    if not cfg.dry_run:
        print("\n  PySide cmake configured. Press Enter to start make ...", end="")
        stdin.readline()
    log.info("Building PySide (threads=%d) ...", cfg.build_threads)
    _run([getMakeExecutable(), "-j%d" % cfg.build_threads], cwd=build_dir, env=env, dry_run=cfg.dry_run)
    log.info("Installing PySide to %s ...", cfg.stage_dir)
    _run([getMakeExecutable(), "install"], cwd=build_dir, env=env, dry_run=cfg.dry_run)
    log.info("PySide built OK")


# ===========================================================================
# Step 7 -- Strip binaries
# ===========================================================================

def strip_binaries(cfg):
    """
    Strip debug symbols from PySide .so files to reduce APK size.
    From M4rtinK/android-pyside-build-scripts/strip_binaries.sh:
      arm-linux-androideabi-strip stage/lib/*.so
      arm-linux-androideabi-strip stage/lib/python2.7/site-packages/PySide/*.so
    :param cfg: BuildConfig
    :return: None
    """
    step("Step 7/13 -- Stripping binaries")
    if cfg.skip_build:
        log.info("--skip-build set; skipping strip.")
        return
    strip = join(cfg.toolchain_bin, "arm-linux-androideabi-strip")
    if not isfile(strip):
        log.warning("strip not found at %s — skipping.", strip)
        return
    so_dirs = [join(cfg.stage_dir, "lib"), join(cfg.stage_dir, "lib", "python2.7", "site-packages", "PySide")]
    for so_dir in so_dirs:
        if not isdir(so_dir):
            continue
        for fname in listdir(so_dir):
            if fname.endswith(".so"):
                fpath = join(so_dir, fname)
                log.info("Stripping: %s", fname)
                _run([strip, fpath], dry_run=cfg.dry_run)
    log.info("Strip done OK")


# ===========================================================================
# Step 8 -- Package application and Python libraries into ZIPs
# ===========================================================================

def _zip_directory(source_dir, zip_path, arc_root=""):
    """
    Create *zip_path* from all files under *source_dir*.
    *arc_root* is a prefix inside the ZIP (e.g. "python/" for python27.zip).
    :param source_dir: str
    :param zip_path: str
    :param arc_root: str
    :return:
    """
    with ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for root, dirs, files in walk(source_dir):
            # skip .pyc files and __pycache__
            files = [f for f in files if not f.endswith(".pyc")]
            for fname in files:
                fpath = join(root, fname)
                zf.write(fpath, join(arc_root, relpath(fpath, source_dir)))
    log.info("Created %s  (%d KB)", basename(zip_path), getsize(zip_path) // 1024)


def package_bundles(cfg):
    """
    Create the two ZIP bundles that QtActivity.java unpacks on first start.
    From modrana.org guide:
      android/res/raw/my_python_project.zip
        -> unpacked to /data/data/<unique>/files/
        -> contains main.py and application sources
      android/res/raw/python_27.zip
        -> unpacked to /data/data/<unique>/files/python
        -> contains:
            lib/libshiboken.so
            lib/libpyside.so
            lib/python2.7/site-packages/PySide/*.so
            lib/python2.7/*.py  (standard library)
            bin/python           (ARM executable)
            imports/             (Qt Components)
            themes/              (Qt Components theme)
    From modrana.org:
      "Due to some not yet identified bug, unless libshiboken.so &
       libpyside.so are manually loaded to memory, importing any PySide
       module fails."
    :param cfg: BuildConfig
    :return:
    """
    step("Step 8/13 -- Packaging application bundles")
    res_raw = join(cfg.project_build, "android", "res", "raw")
    _makedirs(res_raw)
    # ------------------------------------------------------------------ #
    # 8a. python27.zip  (Python runtime + PySide libs)                   #
    # ------------------------------------------------------------------ #
    python27_zip = join(res_raw, "python_27.zip")
    # Assemble the content in a temp staging area.
    py27_stage = join(cfg.work_dir, "py27_bundle")
    if isdir(py27_stage):
        rmtree(py27_stage)
    _makedirs(py27_stage)
    # Copy Android Python tree.
    android_py_tree = join(cfg.android_python, "python")
    if isdir(android_py_tree):
        copytree(android_py_tree, join(py27_stage, "python"))
    # Overlay PySide libraries from stage/
    pyside_lib_src = join(cfg.stage_dir, "lib")
    pyside_lib_dst = join(py27_stage, "python", "lib")
    _makedirs(pyside_lib_dst)
    for fname in ("libshiboken.so", "libpyside.so"):
        src = join(pyside_lib_src, fname)
        if isfile(src):
            copy2(src, join(pyside_lib_dst, fname))
            log.info("Bundled: %s", fname)
    # PySide Python extension modules.
    pyside_pydir_src = join(cfg.stage_dir, "lib", "python2.7", "site-packages", "PySide")
    pyside_pydir_dst = join(py27_stage, "python", "lib", "python2.7", "site-packages", "PySide")
    if isdir(pyside_pydir_src) and not isdir(pyside_pydir_dst):
        copytree(pyside_pydir_src, pyside_pydir_dst)
        log.info("Bundled: PySide site-packages")
    # Qt Components (imports + themes from modrana.org).
    qt_comps_zip = join(cfg.downloads_dir, "qt_components_v1.zip")
    qt_theme_zip = join(cfg.downloads_dir, "qt_components_theme_mini_v1.zip")
    _download(QT_COMPONENTS_URL, qt_comps_zip)
    _download(QT_COMPONENTS_THEME_URL, qt_theme_zip)
    imports_dst = join(py27_stage, "python", "imports")
    themes_dst = join(py27_stage, "python", "themes")
    if not isdir(imports_dst):
        _extract_zip(qt_comps_zip, imports_dst)
    if not isdir(themes_dst):
        _extract_zip(qt_theme_zip, themes_dst)
    if not cfg.dry_run:
        _zip_directory(py27_stage, python27_zip)
    else:
        log.info("[DRY-RUN] Would create: %s", python27_zip)
    # ------------------------------------------------------------------ #
    # 8b. my_python_project.zip  (application code)                      #
    # ------------------------------------------------------------------ #
    project_zip = join(res_raw, "my_python_project.zip")
    # Collect .py files from the project_dir
    app_stage = join(cfg.work_dir, "app_bundle")
    if isdir(app_stage):
        rmtree(app_stage)
    _makedirs(app_stage)
    for fname in listdir(cfg.project_dir):
        src = join(cfg.project_dir, fname)
        if isfile(src):
            copy2(src, join(app_stage, fname))
    # Inject the mandatory ctypes loader preamble into main.py
    # From modrana.org:
    #   "unless libshiboken.so & libpyside.so are manually loaded,
    #    importing any PySide module fails."
    main_py = join(app_stage, "main.py")
    if isfile(main_py):
        _inject_ctypes_preamble(main_py)
    else:
        # Create a minimal main.py template if none exists.
        _create_main_py_template(main_py)
    if not cfg.dry_run:
        _zip_directory(app_stage, project_zip)
    else:
        log.info("[DRY-RUN] Would create: %s", project_zip)
    log.info("Bundles created OK")


def _inject_ctypes_preamble(main_py):
    """
    Ensure the mandatory ctypes preamble is at the top of main.py.
    From modrana.org:
      from ctypes import *
      PROJECT_FOLDER = os.environ['PYSIDE_APPLICATION_FOLDER']
      LIB_DIR = join(PROJECT_FOLDER, 'files/python/lib')
      CDLL(join(LIB_DIR, 'libshiboken.so'))
      CDLL(join(LIB_DIR, 'libpyside.so'))
    :param main_py: str
    :return:
    """
    preamble = dedent("""\
        # ---- PySide Android ctypes preamble (injected by pyside_android_builder_py27.py)
        # Must be executed BEFORE any PySide import.
        # See: https://modrana.org/trac/wiki/PySideForAndroid
        import os
        from ctypes import CDLL

        _PROJECT_FOLDER = os.environ.get('PYSIDE_APPLICATION_FOLDER', '')
        _LIB_DIR = join(_PROJECT_FOLDER, 'files/python/lib')
        CDLL(join(_LIB_DIR, 'libshiboken.so'))
        CDLL(join(_LIB_DIR, 'libpyside.so'))
        # ---- end preamble
    """)
    with open(main_py, "r") as fh:
        content = fh.read()
    # Only inject if not already present.
    if "libshiboken.so" not in content:
        with open(main_py, "w") as fh:
            fh.write(preamble + "\n" + content)
        log.info("ctypes preamble injected into main.py")
    else:
        log.info("ctypes preamble already present in main.py")


def _create_main_py_template(main_py):
    """
    Create a minimal PySide main.py template if none exists in the project.
    :param main_py: str
    :return:
    """
    template = dedent("""\
        #!/usr/bin/env python2.7
        # -*- coding: utf-8 -*-
        # Minimal PySide Android application template
        # Generated by pyside_android_builder_py27.py
        import os
        import sys
        from ctypes import CDLL

        # --- Mandatory ctypes preamble (must precede any PySide import) ---
        # See: https://modrana.org/trac/wiki/PySideForAndroid
        _PROJECT_FOLDER = os.environ.get('PYSIDE_APPLICATION_FOLDER', '')
        _LIB_DIR = join(_PROJECT_FOLDER, 'files/python/lib')
        CDLL(join(_LIB_DIR, 'libshiboken.so'))
        CDLL(join(_LIB_DIR, 'libpyside.so'))
        # --- end preamble ---

        from PySide import QtCore, QtGui

        app = QtGui.QApplication(sys.argv)

        label = QtGui.QLabel("Hello from PySide on Android!")
        label.setAlignment(QtCore.Qt.AlignCenter)
        label.setWindowTitle("PySide Android")
        label.resize(320, 240)
        label.show()

        sys.exit(app.exec_())
    """)
    with open(main_py, "w") as fh:
        fh.write(template)
    log.info("Created main.py template: %s", main_py)


# ===========================================================================
# Step 9 -- Generate main.h and main.cpp (C++ Python wrapper)
# ===========================================================================

def generate_cpp_wrapper(cfg):
    """
    Write main.h and main.cpp into the project build directory.
    From modrana.org guide (main.h section):
      #define MAIN_PYTHON_FILE "/data/data/<unique>/files/main.py"
      #define PYTHON_HOME      "/data/data/<unique>/files/python/"
      #define PYTHON_PATH      "...lib/python2.7/...site-packages..."
      #define LD_LIBRARY_PATH  "...python/lib:...ministro/files/qt/lib/"
      #define PATH             "...python/bin:$PATH"
      #define THEME_PATH       "...python/themes/"
      #define QML_IMPORT_PATH  "...python/imports/"
      #define PYSIDE_APPLICATION_FOLDER "/data/data/<unique>/"
    :param cfg: BuildConfig
    :return:
    """
    step("Step 9/13 -- C++ wrapper (main.h / main.cpp)")
    install = cfg.install_path
    files = install + "/files"
    python = files + "/python"
    main_h_content = dedent("""\
        #ifndef MAIN_H
        #define MAIN_H
        /* Generated by pyside_android_builder_py27.py
         * Source: https://modrana.org/trac/wiki/PySideForAndroid */

        #define MAIN_PYTHON_FILE        "{files}/main.py"
        #define PYTHON_HOME             "{python}/"
        #define PYTHON_PATH             "{python}/lib/python2.7/lib-dynload:{python}/lib/python2.7/:{python}/lib/python2.7/site-packages:{python}/lib"
        #define LD_LIBRARY_PATH         "{python}/lib:{python}/lib/python2.7/lib-dynload:/data/data/org.kde.necessitas.ministro/files/qt/lib/"
        #define PATH                    "{python}/bin:$PATH"
        #define THEME_PATH              "{python}/themes/"
        #define QML_IMPORT_PATH         "{python}/imports/"
        #define PYSIDE_APPLICATION_FOLDER "{install}/"

        #endif // MAIN_H
    """).format(install=install, files=files, python=python)
    main_cpp_content = dedent("""\
        /* Generated by pyside_android_builder_py27.py
         * Source: https://modrana.org/trac/wiki/PySideForAndroid
         *
         * C++ wrapper that initialises the embedded Python interpreter
         * and runs main.py on application startup. */

        #include <Python.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>
        #include "main.h"

        int main(int argc, char *argv[])
        {{
            /* Set required environment variables */
            setenv("PYTHON_HOME",                PYTHON_HOME,                1);
            setenv("PYTHONPATH",                 PYTHON_PATH,                1);
            setenv("LD_LIBRARY_PATH",            LD_LIBRARY_PATH,            1);
            setenv("PATH",                       PATH,                       1);
            setenv("THEME_PATH",                 THEME_PATH,                 1);
            setenv("QML_IMPORT_PATH",            QML_IMPORT_PATH,            1);
            setenv("PYSIDE_APPLICATION_FOLDER",  PYSIDE_APPLICATION_FOLDER,  1);

            /* Initialise the Python interpreter */
            Py_SetProgramName(argv[0]);
            Py_Initialize();
            PySys_SetArgv(argc, argv);
            /* Run the main Python file */
            FILE *fp = fopen(MAIN_PYTHON_FILE, "r");
            if (fp == NULL) {{
                fprintf(stderr, "Cannot open main Python file: %s\\n",
                        MAIN_PYTHON_FILE);
                Py_Finalize();
                return 1;
            }}
            int ret = PyRun_SimpleFile(fp, MAIN_PYTHON_FILE);
            fclose(fp);
            Py_Finalize();
            return ret;
        }}
    """)
    out_dir = cfg.project_build
    _makedirs(out_dir)
    main_h_path = join(out_dir, "main.h")
    main_cpp_path = join(out_dir, "main.cpp")
    if not cfg.dry_run:
        with open(main_h_path, "w") as fh:
            fh.write(main_h_content)
        with open(main_cpp_path, "w") as fh:
            fh.write(main_cpp_content)
    else:
        log.info("[DRY-RUN] Would write: %s", main_h_path)
        log.info("[DRY-RUN] Would write: %s", main_cpp_path)
    log.info("main.h written:   %s", main_h_path)
    log.info("main.cpp written: %s", main_cpp_path)
    log.info("C++ wrapper generated OK")


# ===========================================================================
# Step 10 -- GlobalConstants.java
# ===========================================================================

def generate_global_constants(cfg):
    """
    Write GlobalConstants.java with correct ZIP archive names and settings.
    From modrana.org guide (GlobalConstants.java section):
      PYTHON_MAIN_SCRIPT_NAME = "main.py"
      PYTHON_PROJECT_ZIP_NAME = "my_python_project.zip"
      PYTHON_ZIP_NAME         = "python_27.zip"
    :param cfg: BuildConfig
    :return:
    """
    step("Step 10/13 -- GlobalConstants.java")
    java_dir = join(cfg.project_build, "android", "src", "org", "kde", "necessitas", "origo")
    _makedirs(java_dir)
    content = dedent("""\
        // Generated by pyside_android_builder_py27.py
        // Source: https://modrana.org/trac/wiki/PySideForAndroid
        package org.kde.necessitas.origo;

        public class GlobalConstants {{

            public static final String PYTHON_MAIN_SCRIPT_NAME  = "main.py";
            public static final String PYTHON_PROJECT_ZIP_NAME  = "my_python_project.zip";
            public static final String PYTHON_ZIP_NAME          = "python_27.zip";
            public static final String PYTHON_EXTRAS_ZIP_NAME   = "python_extras_27.zip";

            public static final boolean IS_FOREGROUND_SERVICE   = true;

            public static final String PYTHON_BIN_RELATIVE_PATH = "/python/bin/python";
            public static final String PYTHON_NAME              = "python";
            public static final String PYTHON_NICE_NAME         = "Python 2.7";

            public static String[] SCRIPT_ARGS = {{ "--foreground" }};

            public static final String LOG_TAG = "{app_name}APK";
        }}
    """).format(app_name=cfg.app_name)
    out_path = join(java_dir, "GlobalConstants.java")
    if not cfg.dry_run:
        with open(out_path, "w") as fh:
            fh.write(content)
    else:
        log.info("[DRY-RUN] Would write: %s", out_path)
    log.info("GlobalConstants.java written: %s", out_path)


# ===========================================================================
# Step 11 -- Rename project files
# ===========================================================================

def rename_project(cfg):
    """
    Rename all occurrences of the example project name to the user's app name.
    From modrana.org guide (Project rename script):
      mv PySideExample.pro "${NEW_NAME}.pro"
      sed -i "s/PySideExample/${NEW_NAME}/g" "${NEW_NAME}.pro"
      sed -i "s/org.modrana.PySideExample/${NEW_UNIQUE_NAME}/g" main.h
      sed -i "s/org.modrana.PySideExample/${NEW_UNIQUE_NAME}/g" ...QtActivity.java
      sed -i "s/org.modrana.PySideExample/${NEW_UNIQUE_NAME}/g" ...AndroidManifest.xml
      sed -i "s/PySideExample/${NEW_NAME}/g" ...AndroidManifest.xml
      sed -i "s/PySideExample/${NEW_NAME}/g" android/res/values/strings.xml
      sed -i "s/PySideExample/${NEW_NAME}/g" android/build.xml
    :param cfg: BuildConfig
    :return:
    """
    step("Step 11/13 -- Renaming project files")
    proj = cfg.project_build
    new_name = cfg.app_name
    new_unique = cfg.unique_name
    # 1. Rename .pro file.
    old_pro = join(proj, "%s.pro" % EXAMPLE_APP_NAME)
    new_pro = join(proj, "%s.pro" % new_name)
    if isfile(old_pro) and old_pro != new_pro:
        rename(old_pro, new_pro)
        log.info("Renamed: %s.pro  ->  %s.pro", EXAMPLE_APP_NAME, new_name)
    # Build list of (filepath, old_string, new_string) replacements.
    replacements = []
    # .pro file content.
    if isfile(new_pro):
        replacements.append((new_pro, EXAMPLE_APP_NAME, new_name))
    # main.h
    main_h = join(proj, "main.h")
    if isfile(main_h):
        replacements.append((main_h, EXAMPLE_UNIQUE_NAME, new_unique))
    # Android files.
    android_dir = join(proj, "android")
    file_patterns = [
        ('src/org/kde/necessitas/origo/QtActivity.java', [(EXAMPLE_UNIQUE_NAME, new_unique)]),
        ('AndroidManifest.xml', [(EXAMPLE_UNIQUE_NAME, new_unique), (EXAMPLE_APP_NAME, new_name)]),
        ('res/values/strings.xml', [(EXAMPLE_APP_NAME, new_name)]), ("build.xml", [(EXAMPLE_APP_NAME, new_name)])]
    for rel_path, pairs in file_patterns:
        fpath = join(android_dir, rel_path)
        if isfile(fpath):
            for old, new in pairs:
                replacements.append((fpath, old, new))
    for fpath, old, new in replacements:
        if not cfg.dry_run:
            _sed_inplace(fpath, old, new)
        log.info("  sed '%s' -> '%s'  in  %s", old, new, basename(fpath))
    # Verify no leftover example names.
    leftovers = _find_files_containing(proj, EXAMPLE_APP_NAME)
    leftovers += _find_files_containing(proj, EXAMPLE_UNIQUE_NAME)
    if leftovers:
        log.warning(
            'Leftover references to example project names found in:\n%s', "\n".join("  " + f for f in leftovers))
    else:
        log.info('All example names replaced OK')
    log.info('Project rename done OK')


# ===========================================================================
# Step 12 -- Build APK via ant.
# ===========================================================================

def build_apk(cfg, env):
    """
    Build the debug APK using Ant + Necessitas build system.
    From modrana.org guide:
      "Just open the PySideExample.pro with the Necessitas Qt Creator."
      "To generate a new APK, just click the green deploy button."
    This function replicates what Qt Creator does via command line:
      ant -f android/build.xml debug
    (equivalent to ndk-build + ant debug)
    From M4rtinK build scripts:
      The Necessitas example project uses an ant-based build.
    :param cfg: BuildConfig
    :param env: dict[str, str]
    :return: None
    """
    step('Step 12/13 -- Building APK (ant debug)')
    android_dir = join(cfg.project_build, "android")
    build_xml = join(android_dir, "build.xml")
    if not isfile(build_xml) and not cfg.dry_run:
        log.warning(
            "build.xml not found: %s\n  The Necessitas example project may not have been cloned "
            "correctly. Try opening the .pro in Necessitas Qt Creator to build the APK interactively.", build_xml)
        return
    # ndk-build first (native .so compilation).
    ndk_build = join(cfg.ndk_root, "ndk-build")
    if isfile(ndk_build):
        log.info('Running ndk-build ...')
        _run([ndk_build, "-j%d" % cfg.build_threads], cwd=android_dir, env=env, dry_run=cfg.dry_run)
    else:
        log.warning('ndk-build not found at %s — skipping.', ndk_build)
    # ant debug.
    log.info("Running ant debug ...")
    _run([getAntExecutable(), 'debug', '-f', build_xml], cwd=cfg.project_build, env=env, dry_run=cfg.dry_run)
    # Locate APK.
    apk_search_dirs = [join(android_dir, 'bin'), cfg.project_build]
    found_apk = None
    for search_dir in apk_search_dirs:
        if isdir(search_dir):
            for fname in listdir(search_dir):
                if fname.endswith("-debug.apk") or fname.endswith(".apk"):
                    found_apk = join(search_dir, fname)
                    break
    if found_apk and isfile(found_apk):
        size_mb = getsize(found_apk) / (1024 * 1024.0)
        log.info("APK built: %s  (%.1f MB)", found_apk, size_mb)
        cfg.apk_path = found_apk
    elif cfg.dry_run:
        log.info("[DRY-RUN] APK would be at: %s", cfg.apk_path)
    else:
        log.warning("APK not found after ant build. Check ant output above.")
    log.info("APK build done OK")


# ===========================================================================
# Step 13 -- Optional ADB install
# ===========================================================================

def install_via_adb(cfg):
    """
    Install the APK on the first connected ADB device.
    From modrana.org guide:
      "Just install it and press the PySideExample icon."
      "If you haven't yet installed any Ministro using Qt application on
       your Android device, you will be redirected to the Play store to
       install the Ministro application."
    Steps:
      adb devices
      adb install -r <apk>
    :param cfg: BuildConfig
    :return: None
    """
    step("Step 13/13 -- ADB install")
    adb = cfg.adb_exe
    if not isfile(adb):
        # Fall back to system adb.
        try:
            adb = _require_tool("adb")
        except EnvironmentError:
            log.warning("adb not found. Install android-tools-adb:\n"
                        "  sudo apt-get install android-tools-adb\n  Or use the adb from Necessitas: %s", cfg.adb_exe)
            return
    # List devices.
    result = _run([adb, "devices"], capture=True, check=False, dry_run=cfg.dry_run)
    device_lines = [l for l in (result.stdout or '').splitlines() if l.strip() and "List of devices" not in l]
    if not device_lines:
        log.warning(
            "No ADB devices found.\n"
            "  1. Enable Developer Options on your Android device.\n"
            "  2. Enable USB Debugging.\n"
            "  3. Accept the RSA fingerprint prompt, then retry.\n"
            "\n"
            "  NOTE: On first launch, Ministro will be required.\n"
            "  Install it from the Play Store when prompted.")
        return
    log.info("Connected devices:\n%s", "\n".join("  " + l for l in device_lines))
    apk = cfg.apk_path
    if not isfile(apk) and not cfg.dry_run:
        log.warning("APK not found at: %s", apk)
        return
    log.info("Installing: %s", basename(apk))
    _run([adb, "install", "-r", apk], dry_run=cfg.dry_run)
    log.info("Installation done OK")
    log.info("Stream device logs:\n  %s logcat -s PythonAPK", adb)


# ===========================================================================
# Summary & error FAQ
# ===========================================================================

def print_summary(cfg, apk_path=None):
    """
    :param cfg: BuildConfig
    :param apk_path: str | None
    :return:
    """
    step("Build summary")
    log.info(
        "\n"
        "  App name      : %s\n"
        "  Unique name   : %s\n"
        "  Install path  : %s\n"
        "  Work dir      : %s\n"
        "  Stage dir     : %s\n"
        "  NDK           : %s\n"
        "  APK           : %s",
        cfg.app_name, cfg.unique_name, cfg.install_path, cfg.work_dir, cfg.stage_dir, cfg.ndk_root,
        apk_path or cfg.apk_path)
    apk = apk_path or cfg.apk_path
    if cfg.dry_run:
        log.info("\nDry-run complete -- no files produced.")
    elif isfile(apk):
        log.info("\nBuild succeeded!")
        log.info("  Install:  adb install %s", apk)
        log.info("  Logs:     adb logcat -s PythonAPK")
    else:
        log.warning("\nAPK not found -- build may have failed.")
    log.info(dedent("""
        -------------------------------------------------------------------
        Error FAQ  (from modrana.org/trac/wiki/PySideForAndroid)
        -------------------------------------------------------------------
        * arm-linux-androideabi-g++: Internal error: Killed (program cc1plus)
          Cause: Low RAM / too many parallel compile threads.
          Fix:   Set --build-threads 1  (equivalent to BUILD_THREAD_COUNT=1
                 in env.sh)

        * ImportError when importing PySide on device
          Cause: libshiboken.so / libpyside.so not pre-loaded via ctypes.
          Fix:   Ensure the ctypes preamble runs BEFORE any PySide import.
                 The builder injects it automatically (see main.py output).

        * Cannot find ELF information (in Necessitas Qt Creator)
          Safe to ignore -- it does not affect APK generation.

        * QtActivity.java bundling code removed by Qt Creator update
          Fix:   Re-check QtActivity.java after any Qt Creator update;
                 restore the zip-extraction code from the example project.

        * First start is slow (30+ seconds)
          Cause: Python + Qt Components being unpacked to device storage.
          This happens only once; subsequent starts are fast.

        * Ministro dialog appears on first start
          Expected: Ministro manages Qt libs on Android. Install it from the
          Play Store when prompted, then tap the app icon again.

        * "Cannot open main Python file" in logcat
          Cause: main.py path in main.h is wrong.
          Fix:   Verify MAIN_PYTHON_FILE in main.h matches your unique name:
                   /data/data/<unique_name>/files/main.py

        * PySide libraries not found (libpyside.so missing)
          Fix:   Check python_27.zip/python/lib/ contains libpyside.so.
                 Re-run the builder with correct --pyside-stage path.
        -------------------------------------------------------------------
    """))


# ===========================================================================
# Argument parser
# ===========================================================================

def build_arg_parser():
    """
    :return:
    """
    parser = ArgumentParser(
        prog="pyside_android_builder_py27.py",
        formatter_class=RawDescriptionHelpFormatter,
        description=dedent("""\
            PySide for Android Builder  (Python 2.7)
            =========================================
            Builds Shiboken + PySide for Android ARM using the Necessitas SDK,
            packages them with your Python 2.7 application into a standalone APK.
        """),
        epilog=dedent("""\
            Examples
            --------
            # Full build:
              python2.7 pyside_android_builder_py27.py \\
                  --project-dir ./myapp \\
                  --necessitas-sdk ~/necessitas \\
                  --app-name MyApp \\
                  --unique-name com.example.MyApp

            # Use pre-built PySide libs (skip cross-compile):
              python2.7 pyside_android_builder_py27.py \\
                  --project-dir ./myapp \\
                  --necessitas-sdk ~/necessitas \\
                  --pyside-stage /path/to/stage \\
                  --skip-build

            # Limit to 1 compile thread (low-RAM machine):
              python2.7 pyside_android_builder_py27.py \\
                  --project-dir ./myapp \\
                  --necessitas-sdk ~/necessitas \\
                  --build-threads 1

            # Build + install to connected device:
              python2.7 pyside_android_builder_py27.py \\
                  --project-dir ./myapp \\
                  --necessitas-sdk ~/necessitas \\
                  --install-apk

            # Verbose dry-run:
              python2.7 pyside_android_builder_py27.py \\
                  --project-dir ./myapp \\
                  --necessitas-sdk ~/necessitas \\
                  --dry-run --verbose
        """))
    parser.add_argument("--project-dir", required=True, metavar="DIR",
                        help="Directory containing your PySide Python application (with main.py).")
    parser.add_argument(
        "--necessitas-sdk", required=True, metavar="DIR", help="Path to your Necessitas SDK installation.")
    parser.add_argument("--app-name", metavar="NAME", default=None,
                        help="Application name (default: project directory basename).")
    parser.add_argument("--unique-name", metavar="NAME", default=None,
                        help=("Unique Android application name (e.g. com.example.MyApp). "
                              "This determines the install path on the device. Default: com.example.<app-name>"))
    parser.add_argument("--pyside-stage", metavar="DIR", default=None,
                        help=("Use pre-built PySide/Shiboken libraries from this directory "
                              "(the 'stage' folder from a previous build). "
                              "Skips cross-compilation when combined with --skip-build."))
    parser.add_argument(
        "--build-threads", type=int, default=DEFAULT_BUILD_THREADS, metavar="N",
        help="Number of parallel make jobs (default: %d)."
             "Set to 1 if you get 'cc1plus: Internal error: Killed'." % DEFAULT_BUILD_THREADS)
    parser.add_argument("--skip-build", action="store_true",
                        help="Skip Shiboken and PySide cross-compilation (use --pyside-stage).")
    parser.add_argument("--install-apk", action="store_true",
                        help="Install the produced APK on the first ADB-connected device.")
    parser.add_argument("--keep-build", action="store_true", help="Retain intermediate cmake build directories.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug-level output.")
    return parser


# ===========================================================================
# Main entry point.
# ===========================================================================

def main(argv=None):
    """
    :param argv: list[str]
    :return: int
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        getLogger().setLevel(DEBUG)
    cfg = BuildConfig(args)
    try:
        preflight_checks(cfg)
        clone_sources(cfg)
        env = make_build_env(cfg)
        download_android_python(cfg)
        if not cfg.skip_build:
            build_shiboken(cfg, env)
            build_pyside(cfg, env)
            strip_binaries(cfg)
        package_bundles(cfg)
        generate_cpp_wrapper(cfg)
        generate_global_constants(cfg)
        rename_project(cfg)
        build_apk(cfg, env)
        if cfg.install_apk:
            install_via_adb(cfg)
        apk_path = cfg.apk_path
        print_summary(cfg, apk_path)
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
        log.warning('Interrupted by user.')
        return 130


if __name__ == "__main__":
    exit(main())
