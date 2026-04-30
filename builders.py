# -*- coding: utf-8 -*-
"""
Helping to get builders tools currently.
"""
from os.path import isfile, join, exists, dirname
from sys import executable, platform, path
from cmake import CMAKE_BIN_DIR
from json import loads

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .build_utils import run, which
except:
    from build_utils import run, which


def getCmakeVersion(cmakePath):
    """
    :param cmakePath: str | unicode
    :return: str | unicode
    """
    try:
        result = run([str(cmakePath), '-E', 'capabilities'], capture_output=True, text=True)
        return loads(result.stdout)['version']['string']
    except:
        # In some cases (like Pyodide<0.26's cmake wrapper), `-E` isn't handled
        # correctly, so let's try `--version`, which is more common so more likely to be wrapped correctly.
        try:
            result = run([str(cmakePath), '--version'], capture_output=True, text=True)
            return result.stdout.splitlines()[0].split()[-1].split('-')[0]
        except:
            return '0.0'


def getCurrentExecutable(name):
    """
    :param name: str | unicode
    :return: str | unicode
    """
    name = '{}{}'.format(name.rstrip('.exe'), '.exe' if platform.lower() == 'win32' else '')
    if exists(name):
        return name
    prog = which(name.rstrip('.exe'))  # type: str
    if prog:
        return prog
    # Just guess otherwise.
    return join(dirname(__file__), name)


def getCmakeExecutable():
    """
    :return: str | unicode
    """
    pth = '{}/cmake'.format(CMAKE_BIN_DIR)  # type: str
    cmakeExec = '{}.exe'.format(pth) if isfile('{}.exe'.format(pth)) else pth
    if exists(cmakeExec):
        return cmakeExec
    for name in ('cmake', 'cmake3'):
        prog = which(name)  # type: str
        if prog and getCmakeVersion(prog) != '0.0':
            return prog
    # Just guess otherwise.
    return 'cmake'


def getMakeExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('make')


def getGitExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('git')


def getAntExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('ant')


def getAdbExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('adb')


def getNdkBuildExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('ndk-build')


def getArmLinuxAndroideabiStripExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('arm-linux-androideabi-strip')


def getUVExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('uv')


def getXcodebuildExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('xcodebuild')


def getXcrunExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('xcrun')


def getXcodeSelectExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('xcode-select')


def getPythonExecutable():
    """
    :return: str | unicode
    """
    if exists(executable):
        return executable
    for name in ('python', 'python3', 'python3.4', 'python2'):
        if exists(getCurrentExecutable(name)):
            return getCurrentExecutable(name)
    # Just guess otherwise.
    return getCurrentExecutable(join('bin', 'python'))


def getPyqtdeploySysrootExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('pyqtdeploy-sysroot')


def getPyqtdeployBuildExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('pyqtdeploy-build')


def getHgExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('hg')


def getOpenExecutable():
    """
    :return: str | unicode
    """
    o = getCurrentExecutable('open')  # type: str
    return o if exists(o) else ''


def getJavaExecutable():
    """
    :return: str | unicode
    """
    return getCurrentExecutable('java')
