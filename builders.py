# -*- coding: utf-8 -*-
"""
Helping to get builders tools currently.
"""
from os.path import isfile, join, exists, split, dirname
from cmake import CMAKE_BIN_DIR
from json import loads

# -- shutil.which ------------------------------------------------------------
try:
    from shutil import which
except:
    from os import pathsep, environ, access, X_OK


    def which(name):
        """
        Locate an executable on PATH.
        :param name: str | unicode
        :return: str | unicode | None
        """

        def isExecutable(pth):
            """
            :param pth: str | unicode
            :return: bool
            """
            return isfile(pth) and access(pth, X_OK)

        path, _ = split(name)
        if path:
            if isExecutable(name):
                return name
        else:
            for directory in environ.get('PATH', '').split(pathsep):
                fullPath = join(directory, name)
                if isExecutable(fullPath):
                    return fullPath
        exts = environ.get('PATHEXT', '').split(pathsep)
        for directory in environ.get('PATH', '').split(pathsep):
            for ext in [''] + exts:
                fullPath = join(directory, name + ext)
                if isfile(fullPath) and access(fullPath, X_OK):
                    return fullPath
        return None

try:
    from subprocess import run
except:
    from subprocess import Popen, call, PIPE


    class _CompletedProcess(object):
        """
        Minimal subprocess.CompletedProcess shim.
        """

        def __init__(self, args, returncode, stdout=None, stderr=None):
            self.args = args
            self.returncode = returncode
            self.stdout = stdout or ''
            self.stderr = stderr or ''


    def run(cmd, cwd=None, env=None, capture_output=False, text=True):
        """
        Python shim for subprocess.run(capture_output=...).
        """
        if capture_output:
            proc = Popen(cmd, cwd=cwd, env=env, stdout=PIPE, stderr=PIPE)
            stdoutBytes, stderrBytes = proc.communicate()
            if text:
                stdout = stdoutBytes.decode("utf-8", errors="replace")
                stderr = stderrBytes.decode("utf-8", errors="replace")
            else:
                stdout, stderr = stdoutBytes, stderrBytes
            return _CompletedProcess(cmd, proc.returncode, stdout, stderr)
        return _CompletedProcess(cmd, call(cmd, cwd=cwd, env=env))


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
        # correctly, so let's try `--version`, which is more common so more
        # likely to be wrapped correctly
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
    if exists(name):
        return name
    prog = which(name)  # type: str
    if prog:
        return prog
    # Just guess otherwise.
    return join(dirname(__file__), name)


def getCmakeExecutable():
    """
    :return: str | unicode
    """
    path = '{}/cmake'.format(CMAKE_BIN_DIR)  # type: str
    cmakeExec = '{}.exe'.format(path) if isfile('{}.exe'.format(path)) else path
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
