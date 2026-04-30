# -*- coding: utf-8 -*-
"""
Init project.
"""
from os.path import dirname
from sys import path

if dirname(__file__) not in path:
    path.append(dirname(__file__))

try:
    from .builders import getMakeExecutable, getPythonExecutable, getOpenExecutable, getHgExecutable, \
        getPyqtdeployBuildExecutable, getJavaExecutable, getXcodeSelectExecutable, \
        getXcodebuildExecutable, getCmakeExecutable, getAntExecutable, getUVExecutable, \
        getGitExecutable, getXcrunExecutable, getNdkBuildExecutable, getCurrentExecutable, \
        getAdbExecutable, getPyqtdeploySysrootExecutable, getArmLinuxAndroideabiStripExecutable, \
        getCmakeVersion, CMAKE_BIN_DIR
except:
    from builders import getMakeExecutable, getPythonExecutable, getOpenExecutable, getHgExecutable, \
        getPyqtdeployBuildExecutable, getJavaExecutable, getXcodeSelectExecutable, getXcodebuildExecutable, \
        getCmakeExecutable, getAntExecutable, getUVExecutable, getGitExecutable, getXcrunExecutable, \
        getNdkBuildExecutable, getCurrentExecutable, getAdbExecutable, getPyqtdeploySysrootExecutable, \
        getArmLinuxAndroideabiStripExecutable, getCmakeVersion, CMAKE_BIN_DIR

__all__ = ['getMakeExecutable', 'getPythonExecutable', 'getOpenExecutable', 'getHgExecutable',
           'getPyqtdeployBuildExecutable', 'getJavaExecutable', 'getXcodeSelectExecutable', 'getXcodebuildExecutable',
           'getCmakeExecutable', 'getAntExecutable', 'getUVExecutable', 'getGitExecutable', 'getXcrunExecutable',
           'getNdkBuildExecutable', 'getCurrentExecutable', 'getAdbExecutable', 'getPyqtdeploySysrootExecutable',
           'getArmLinuxAndroideabiStripExecutable', 'getCmakeVersion', 'CMAKE_BIN_DIR']
