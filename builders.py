# -*- coding: utf-8 -*-
"""
Builder tool resolution helpers.

Patched from the original to:
  1. Fix str.rstrip('.exe') bug -- rstrip strips CHARACTERS not a suffix.
     'make'.rstrip('.exe')  => 'mak'   (eats trailing 'e')
     'cmake'.rstrip('.exe') => 'cmak'
     We use a proper endswith() check instead.
  2. Make the `cmake` PyPI package optional.  The plashless pipeline never
     calls getCmakeExecutable(); we don't want it failing to import.
"""
from os.path import isfile, join, exists, dirname
from sys import executable, platform, path
from json import loads

if dirname(__file__) not in path:
    path.append(dirname(__file__))

# NB: a failed *relative* import raises different exceptions depending on the
# Python version:
#   * Python 3.4 -> SystemError ("Parent module '' not loaded, ...")
#   * Python 3.5+ -> ImportError
#   * Older edge cases -> ValueError ("Attempted relative import in non-package")
# This file is imported as a top-level module (the build scripts are run as
# `python3.4 pyqt5_android_plashless.py`, with no parent package), so on
# Python 3.4 the relative import below WILL raise SystemError. We must catch
# all three so the absolute-import fallback can take over.
try:
    from .build_utils import run, which
except (ImportError, ValueError, SystemError):
    from build_utils import run, which

# cmake PyPI package is optional -- only needed by getCmakeExecutable(), which
# the plashless build never calls.  If it's missing we just provide a stub.
try:
    from cmake import CMAKE_BIN_DIR
except ImportError:
    CMAKE_BIN_DIR = ''


def _strip_exe_suffix(name):
    """
    Correctly strip a trailing '.exe' suffix (case-insensitive).

    The original code used name.rstrip('.exe') which removes any combination
    of '.', 'e', 'x' chars from the right -- 'make' becomes 'mak'.
    """
    if name.lower().endswith('.exe'):
        return name[:-4]
    return name


def getCmakeVersion(cmakePath):
    """
    :param cmakePath: str | unicode
    :return: str | unicode
    """
    try:
        result = run([str(cmakePath), '-E', 'capabilities'], capture_output=True, text=True)
        return loads(result.stdout)['version']['string']
    except Exception:
        try:
            result = run([str(cmakePath), '--version'], capture_output=True, text=True)
            return result.stdout.splitlines()[0].split()[-1].split('-')[0]
        except Exception:
            return '0.0'


def getCurrentExecutable(name):
    """
    :param name: str | unicode
    :return: str | unicode
    """
    base = _strip_exe_suffix(name)
    full = '{0}{1}'.format(base, '.exe' if platform.lower() == 'win32' else '')
    if exists(full):
        return full
    prog = which(base)
    if prog:
        return prog
    return join(dirname(__file__), full)


def getCmakeExecutable():
    """
    :return: str | unicode
    """
    if CMAKE_BIN_DIR:
        pth = '{0}/cmake'.format(CMAKE_BIN_DIR)
        cmakeExec = '{0}.exe'.format(pth) if isfile('{0}.exe'.format(pth)) else pth
        if exists(cmakeExec):
            return cmakeExec
    for name in ('cmake', 'cmake3'):
        prog = which(name)
        if prog and getCmakeVersion(prog) != '0.0':
            return prog
    return 'cmake'


def getMakeExecutable():        return getCurrentExecutable('make')
def getGitExecutable():         return getCurrentExecutable('git')
def getAntExecutable():         return getCurrentExecutable('ant')
def getAdbExecutable():         return getCurrentExecutable('adb')
def getNdkBuildExecutable():    return getCurrentExecutable('ndk-build')
def getUVExecutable():          return getCurrentExecutable('uv')
def getXcodebuildExecutable():  return getCurrentExecutable('xcodebuild')
def getXcrunExecutable():       return getCurrentExecutable('xcrun')
def getXcodeSelectExecutable(): return getCurrentExecutable('xcode-select')
def getHgExecutable():          return getCurrentExecutable('hg')
def getJavaExecutable():        return getCurrentExecutable('java')


def getArmLinuxAndroideabiStripExecutable():
    return getCurrentExecutable('arm-linux-androideabi-strip')


def getPythonExecutable():
    if exists(executable):
        return executable
    for name in ('python3.4', 'python3', 'python', 'python2'):
        prog = which(name)
        if prog:
            return prog
    return getCurrentExecutable(join('bin', 'python'))


def getPyqtdeploySysrootExecutable():
    return getCurrentExecutable('pyqtdeploy-sysroot')


def getPyqtdeployBuildExecutable():
    return getCurrentExecutable('pyqtdeploy-build')


def getOpenExecutable():
    o = getCurrentExecutable('open')
    return o if exists(o) else ''
