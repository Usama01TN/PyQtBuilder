# -*- coding: utf-8 -*-
"""
Builders utils.
"""
from os import walk, makedirs, statvfs, listdir
from subprocess import check_call
from os.path import join, isdir
import io

try:
    from urllib import urlretrieve, urlopen  # noqa: F401
    from urllib2 import URLError  # noqa: F401
except:
    from urllib.request import urlretrieve, urlopen  # noqa: F401
    from urllib.error import URLError  # noqa: F401
try:
    FileNotFoundError = FileNotFoundError
except:
    FileNotFoundError = IOError
try:
    from shutil import which
except:
    from os import pathsep, environ, access, X_OK
    from os.path import isfile, split


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

try:
    from shutil import disk_usage
except:
    from collections import namedtuple

    _DiskUsage = namedtuple('DiskUsage', ['total', 'used', 'free'])


    def disk_usage(pth):
        """
        :param pth: str
        :return: _DiskUsage
        """
        st = statvfs(pth)
        return _DiskUsage(
            st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)

try:
    from os import cpu_count
except:
    def cpu_count():
        """
        Fallback cpu_count using /proc/cpuinfo.
        :return: int | None
        """
        try:
            with open('/proc/cpuinfo') as fh:
                return sum(1 for line in fh if line.strip().startswith('processor'))
        except:
            return None

try:
    from venv import create
except:
    def create(venv_dir, with_pip=True, clear=True):
        """
        :param venv_dir: str
        :param with_pip: bool
        :param clear: bool
        :return:
        """
        check_call(['virtualenv', venv_dir])


def _makedirs(pth):
    """
    Create *path* and all missing parents; silently ignore if it exists.
    :param pth: str
    :return:
    """
    if not isdir(pth):
        try:
            makedirs(pth)
        except OSError:
            if not isdir(pth):
                raise


def _rglob(directory, pattern):
    """
    Recursively yield file paths under *directory* whose names match *pattern*.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    matches = []  # type: list[str]
    for root, _dirs, files in walk(directory):
        for filename in filter(files, pattern):
            matches.append(join(root, filename))
    return matches


def _glob_dir(directory, pattern):
    """
    List direct children of *directory* whose names match *pattern*.
    :param directory: str
    :param pattern: str
    :return: list[str]
    """
    results = []
    try:
        entries = listdir(directory)
    except OSError:
        return results
    for entry in filter(entries, pattern):
        results.append(join(directory, entry))
    return results


def _read_text(pth, encoding='utf-8'):
    """
    Read and return the entire contents of *path* as a Unicode string.
    :param pth: str
    :param encoding: str
    :return:
    """
    with io.open(pth, 'r', encoding=encoding) as fh:
        return fh.read()


def _write_text(pth, text, encoding='utf-8'):
    """
    Write *text* to *path*, overwriting any existing content.
    :param pth: str
    :param text: str
    :param encoding: str
    :return:
    """
    with io.open(pth, 'w', encoding=encoding) as fh:
        fh.write(text)
