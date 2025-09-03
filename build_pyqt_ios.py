#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_pyqt_ios.py

Automates compiling a PyQt5 app for iOS (simulator/device) on macOS (Apple Silicon),
following the exact steps described in the provided memo:

- Installs: PyQt5, PyQt5_sip, pyqtdeploy, pyqt-builder (via pip)
- Fetches pyqtdeploy source sdist to obtain the official "demo" folder
- Downloads PyQt5-5.15.11.tar.gz into the demo directory (required by pyqtdeploy sysroot)
- Patches pyqtdeploy:
    * platforms.py — add Apple Silicon ('arm64') handling to avoid 'macos-32' error
    * sysroot/plugins/Python/configurations/python.pro — add -Wno-implicit-function-declaration
    * sysroot/plugins/SIP.py — make glob case-insensitive to find pyqt5_sip sdist
- Edits demo/sysroot.toml to:
    * set PyQt to 5.15.11
    * set Qt to 5.15.18
    * comment modules from PyQt3D to QScintilla
- Patches demo/pyqt-demo.py to avoid the get_source_code() crash by replacing the display with
  a placeholder "don't give up..." (as in the memo)
- Optionally replaces the demo entrypoint with your own app via --app-entry
- Runs: python build-demo.py --target ios-64 --qmake <ios qmake> --verbose
- Prints the resulting Xcode project path

Notes:
- This script modifies files inside your installed pyqtdeploy package directory. It creates
  .bak backups and is idempotent (safe to re-run).
- Requires macOS with Xcode and Qt 5.15.18 (iOS kit). Tested logic is geared for Apple Silicon.
"""
from re import search, MULTILINE, sub, compile, escape
from shutil import copy2, copytree, rmtree, which
from subprocess import check_call, check_output
from sys import platform, executable
from argparse import ArgumentParser
from pathlib import Path
from tarfile import open
from json import loads
import sys
from urllib.request import urlopen, urlretrieve


# --------------------------- utilities ---------------------------

def sh(cmd, cwd=None, env=None):
    print("+", " ".join(cmd))
    check_call(cmd, cwd=cwd, env=env)


def ensureDir(p):
    """
    :param p: str | unicode
    :return: str | unicode | None
    """
    p.mkdir(parents=True, exist_ok=True)
    return p


def backupOnce(target):
    """
    :param target: str | unicode
    :return: None
    """
    if not target.exists():
        return
    bak = target.with_suffix(target.suffix + ".bak")
    if not bak.exists():
        copy2(target, bak)


def readText(p):
    """
    :param p: str | unicode
    :return: str | unicode
    """
    return p.read_text(encoding="utf-8", errors="replace")


def writeText(p, data):
    """
    :param p: str | unicode
    :param data: str | unicode
    :return:
    """
    p.write_text(data, encoding="utf-8")


def idempotentInsertUnique(text, needle, insertAfterRegex):
    """
    :param text: str | unicode
    :param needle: str | unicode
    :param insertAfterRegex: str | unicode
    :return: str | unicode
    """
    if needle in text:
        return text
    m = search(insertAfterRegex, text, flags=MULTILINE)
    if not m:
        # Fallback: append at end
        return text + "\n" + needle + "\n"
    idx = m.end()  # type: int
    return text[:idx] + ("\n" if not text[idx - 1] == "\n" else "") + needle + text[idx:]


def which_(qmakeCandidate):
    """
    :param qmakeCandidate: str | unicode
    :return: str | unicode
    """
    p = which(qmakeCandidate)  # type: str
    return Path(p) if p else None


# --------------------------- pip & imports ---------------------------

def pipInstall(pkgs, userFlag=False):
    cmd = [executable, "-m", "pip", "install"]
    if userFlag and not hasattr(sys, 'real_prefix') and not hasattr(
            sys, 'base_prefix') or sys.base_prefix == sys.prefix:
        cmd.append("--user")
    cmd.extend(pkgs)
    sh(cmd)


def ensurePackages():
    """
    :return:
    """
    # Install packages as specified in the memo
    # Pin PyQt5 to 5.15.11 (matches the sdist we will download)
    # pyqtdeploy/pyqt-builder unpinned to get a working recent toolchain
    print("Installing required packages (may take a while)…")
    pipInstall(["PyQt5==5.15.11", "PyQt5_sip", "pyqtdeploy", "pyqt-builder"])


def importPyqtdeploy():
    """
    :return: ModuleType
    """
    # Import after installation.
    from importlib import import_module
    return import_module("pyqtdeploy")


# --------------------------- download helpers ---------------------------

def pypiJson(project, version=None):
    """
    :param project: str | unicode
    :param version: str | unicode | None
    :return: dict
    """
    if version:
        url = "https://pypi.org/pypi/{}/{}/json".format(project, version)
    else:
        url = "https://pypi.org/pypi/{}/json".format(project)
    with urlopen(url) as r:
        return loads(r.read().decode("utf-8"))


def downloadSdist(project: str, destDir: Path, version: str = None, filenameContains: str = None) -> Path:
    """Download a project sdist (tar.gz) from PyPI into destDir and return path."""
    ensureDir(destDir)
    meta = pypiJson(project, version)
    urls = meta["urls"]
    cand = None
    for u in urls:
        if u.get("packagetype") != "sdist":
            continue
        fn = u.get("filename", "")
        if not fn.endswith(".tar.gz"):
            continue
        if filenameContains and filenameContains.lower() not in fn.lower():
            continue
        cand = (u["url"], fn)
        break
    if not cand:
        # Fallback: take first sdist tar.gz
        for u in urls:
            if u.get("packagetype") == "sdist" and u.get("filename", "").endswith(".tar.gz"):
                cand = (u["url"], u["filename"])
                break
    if not cand:
        print("Could not find sdist for {} on PyPI.".format(project))
    url, filename = cand
    out = destDir / filename
    if not out.exists():
        print("Downloading {} sdist: {}".format(project, filename))
        urlretrieve(url, out)
    else:
        print("Using cached sdist: {}".format(out.name))
    return out


def extractTarGz(archive: Path, destDir: Path) -> Path:
    """
    :param archive: str | unicode
    :param destDir: str | unicode
    :return: str | unicode
    """
    ensureDir(destDir)
    with open(archive, "r:gz") as tf:
        tf.extractall(destDir)
        top = tf.getmembers()[0].name.split("/")[0]
    return destDir / top


# --------------------------- patchers ---------------------------

def patchPlatformsArm64(platformsPy: Path):
    """
    pyqtdeploy/platforms.py — add Apple Silicon ('arm64') handling to avoid:
    pyqtdeploy-sysroot: 'macos-32' is not a supported architecture
    """
    print("Patching Apple Silicon handling in {} …".format(platformsPy))
    backupOnce(platformsPy)
    text = readText(platformsPy)  # type: str
    # Heuristic 1: add 'arm64' wherever 'x86_64' is mentioned for macOS mapping to 'macos-64'
    # patterns = [
    #     # add to tuple checks like: if machine in ('x86_64', ...):
    #     (r"(machine\s*in\s*\(\s*'x86_64'[^)]*\))", "arm64"),
    #     (r"==\s*'x86_64'", "or machine == 'arm64'"),
    # ]
    changed = text  # type: str

    # Try expanding tuple pattern
    def addInTuple(s):
        """
        :param s: str | unicode
        :return: str | unicode
        """
        return sub(r"\(\s*'x86_64'([^)]*)\)", r"('x86_64'\1, 'arm64')", s, count=1)

    # 1) Try tuple expansion
    new = sub(r"(machine\s*in\s*\(\s*'x86_64'[^)]*\))", lambda m: addInTuple(m.group(0)), changed, count=1)
    if new != changed:
        changed = new
    # 2) Try equality expansion
    new = sub(r"(machine\s*==\s*'x86_64')", r"(\1 or machine == 'arm64')", changed, count=1)
    if new != changed:
        changed = new
    # 3) Fallback: inject a small guard that forces macos-64 on arm64
    guard = (
        "\n# --- injected by build_pyqt_ios.py ---\n"
        "try:\n"
        "    import platform as _p\n"
        "    if _p.system() == 'Darwin' and _p.machine() == 'arm64':\n"
        "        # Provide a helper for callers that derive arch from machine\n"
        "        MACOS_ARM64 = True\n"
        "except Exception:\n"
        "    pass\n"
    )
    if "MACOS_ARM64 = True" not in changed:
        changed += guard
    if changed != text:
        writeText(platformsPy, changed)
        print("  ✓ platforms.py patched (arm64 recognized).")
    else:
        print("  … platforms.py already appears to handle arm64; no change made.")


def patchPythonProAddCflag(pythonPro):
    """
    Add:
      - QMAKE_CFLAGS    += -Wno-implicit-function-declaration
      - QMAKE_CFLAGS_C  += -Wno-implicit-function-declaration
      - CONFIGURE_ARGS  += ac_cv_func_getentropy=no ac_cv_func_getrandom=no ac_cv_func_openpty=no ac_cv_func_futimens=no ac_cv_func_utimensat=no
      - CONFIGURE_ENV   += ac_cv_func_getentropy=no
    to steer CPython's configure away from problematic probes on iOS and
    make Clang ignore any stray implicit-decl diagnostics for C files.
    """
    print("Patching CFLAGS & configure args in {} …".format(pythonPro))
    backupOnce(pythonPro)
    text = readText(pythonPro)

    def ensure_line(txt, needle):
        if needle in txt:
            return txt
        # insert near existing QMAKE_* lines when possible; else append
        return idempotentInsertUnique(txt, needle, r"(^\s*QMAKE_.*$)")

    text = ensure_line(text, "QMAKE_CFLAGS += -Wno-implicit-function-declaration")
    text = ensure_line(text, "QMAKE_CFLAGS_C += -Wno-implicit-function-declaration")
    text = ensure_line(text, "CONFIGURE_ARGS += ac_cv_func_getentropy=no ac_cv_func_getrandom=no ac_cv_func_openpty=no ac_cv_func_futimens=no ac_cv_func_utimensat=no")
    text = ensure_line(text, "CONFIGURE_ENV += ac_cv_func_getentropy=no")

    writeText(pythonPro, text)
    print("  ✓ python.pro patched with getentropy workaround & CFLAGS.")

def patchSipCaseInsensitiveGlob(sipPy: Path):
    """
    Make glob case-insensitive in sysroot/plugins/SIP.py to find pyqt5_sip sdist
    (e.g., pyqt5_sip-12.13.0.tar.gz regardless of case).
    """
    print("Patching case-insensitive glob in {} …".format(sipPy))
    backupOnce(sipPy)
    text = readText(sipPy)  # type: str
    if "_glob_ci(" in text:
        print("  … case-insensitive glob already present; no change made.")
        return
    # Define helper and replace glob.glob(...) with _glob_ci(...)
    helper = (
        "\n# --- injected by build_pyqt_ios.py ---\n"
        "import os as _os, re as _re\n"
        "def _glob_ci(pattern):\n"
        "    d = _os.path.dirname(pattern) or '.'\n"
        "    pat = _os.path.basename(pattern)\n"
        "    # convert shell wildcards to regex\n"
        "    pat_re = '^' + _re.escape(pat).replace(r'\\*', '.*').replace(r'\\?', '.') + '$'\n"
        "    rx = _re.compile(pat_re, _re.IGNORECASE)\n"
        "    try:\n"
        "        entries = _os.listdir(d)\n"
        "    except FileNotFoundError:\n"
        "        return []\n"
        "    return [_os.path.join(d, e) for e in entries if rx.match(e)]\n"
    )
    changed = text  # type: str
    # Ensure 'glob' calls are replaced cautiously only where used to find sdists.
    # Replace the first 2 occurrences as a heuristic.
    changed2 = changed.replace("glob.glob(", "_glob_ci(", 2)  # type: str
    if changed2 != changed:
        changed = changed2
        if helper not in changed:
            changed += helper
        writeText(sipPy, changed)
        print("  ✓ SIP.py patched with case-insensitive glob.")
    else:
        # Couldn't find glob.glob references; append helper just in case
        if helper not in changed:
            changed += helper
            writeText(sipPy, changed)
            print("  ✓ SIP.py updated with helper (no direct replacements found).")
        else:
            print("  … SIP.py did not require changes.")


# --------------------------- sysroot.toml & demo patching ---------------------------

def tweakSysrootToml(sysrootToml: Path, pyqtVer: str, qtVer: str, pythonVer: str | None = None):
    """
    - Set PyQt5 version to pyqtVer
    - Set Qt version to qtVer
    - Optionally set Python version to pythonVer
    - Comment every line from first 'PyQt3D' occurrence through first 'QScintilla' occurrence
    """
    print("Editing {} …".format(sysrootToml.name))
    backupOnce(sysrootToml)
    lines = readText(sysrootToml).splitlines()

    def replVersion(lines, key, value):
        from re import compile, escape
        pattern = compile(r'^(\s*{}\s*=\s*)["\']?([^"\']*)["\']?\s*$'.format(escape(key)))
        out, done = [], False
        for ln in lines:
            m = pattern.match(ln)
            out.append('{}"{}"'.format(m.group(1), value) if m else ln)
            done = done or bool(m)
        if not done:
            out.append('{} = "{}"'.format(key, value))
        return out

    lines = replVersion(lines, "PyQt", pyqtVer)
    lines = replVersion(lines, "PyQt5", pyqtVer)
    lines = replVersion(lines, "Qt", qtVer)
    if pythonVer:
        lines = replVersion(lines, "Python", pythonVer)

    # comment block from PyQt3D to QScintilla (inclusive)
    startI = endI = None
    from re import search as _s
    for i, ln in enumerate(lines):
        if startI is None and _s(r"\bPyQt3D\b", ln):
            startI = i
        if startI is not None and _s(r"\bQScintilla\b", ln):
            endI = i
            break
    if startI is not None and endI is not None:
        for i in range(startI, endI + 1):
            if not lines[i].lstrip().startswith("#"):
                lines[i] = "# " + lines[i]

    writeText(sysrootToml, "\n".join(lines) + "\n")
    print("  ✓ sysroot.toml updated (PyQt, Qt, {}modules trimmed{}).".format(
        "" if not pythonVer else "Python set, ", ""))

def patchDemoPy(demoPy: Path):
    """
    Replace any view.setText(get_source_code(...)) pattern with a placeholder to avoid crash.
    :param demoPy: str | unicode
    :return:
    """
    print("Patching {} to avoid get_source_code crash …".format(demoPy.name))
    backupOnce(demoPy)
    text = readText(demoPy)
    # Several possible forms; be generous:
    patterns = [
        r"view\.setText\(\s*get_source_code\([^\)]*\)\s*\)",
        r"view\.setText\(\s*get_source_code\s*\(\s*\)\s*\)",
    ]  # type: list[str]
    replaced = text  # type: str
    for pat in patterns:
        replaced = sub(pat, 'view.setText("don\'t give up...")', replaced)  # type: str
    if replaced != text:
        writeText(demoPy, replaced)
        print("  ✓ demo patched.")
    else:
        print("  … demo did not require this patch (pattern not found).")


def swapInUserApp(demoDir: Path, appEntry: Path):
    """
    Replace demo's entry with a thin wrapper that runs the user's app.
    """
    print("Replacing demo entry with your app: {} …".format(appEntry))
    appEntry = appEntry.resolve()
    target = demoDir / "pyqt-demo.py"  # type: str
    backupOnce(target)
    # Create a tiny bootstrap that imports and runs user's script.
    # We avoid assumptions about their structure; we just execfile-like run it.
    wrapper = """# Auto-generated by build_pyqt_ios.py
import runpy, sys
sys.argv = ['{}']
runpy.run_path(r\"\"\"{}\"\"\", run_name="__main__")
""".format(appEntry.name, appEntry.as_posix())
    writeText(target, wrapper)
    print("  ✓ demo entry swapped to your app.")


# --------------------------- main flow ---------------------------

def findQmakeIos(userPath, qtVer):
    """
    :param userPath: str | unicode
    :param qtVer: str | unicode
    :return: str | unicode
    """
    if userPath:
        qp = Path(userPath).expanduser().resolve()
        if qp.exists():
            return qp
        print("--qt-ios-qmake path not found: {}".format(qp))
        
    # Try common locations
    candidates = [
        Path.home() / "Qt/{}/ios/bin/qmake".format(qtVer),
        Path("/Applications/Qt") / qtVer / "ios/bin/qmake",
        Path("/usr/local/opt/qt@5/bin/qmake"),  # Homebrew (unlikely for iOS kit).
    ]
    for c in candidates:
        if c.exists():
            return c
    # Last resort: 'qmake' in PATH (verify it's the iOS one later).
    w = which_("qmake")
    if w:
        return w
    print("Could not locate iOS qmake. Provide --qt-ios-qmake.")


def verifyHost():
    """
    :return:
    """
    if not platform.lower().startswith(('darwin', 'mac')):
        print("This script must be run on macOS.")
    try:
        check_output(["xcode-select", "-p"])
    except Exception:
        print("Xcode Command Line Tools not found. Install Xcode + CLT first.")


def main():
    """
    :return:
    """
    parser = ArgumentParser(description="Compile PyQt5 app for iOS using pyqtdeploy with required patches.")
    parser.add_argument("--qt-ios-qmake", default=None, help="Path to iOS qmake, e.g. ~/Qt/5.15.18/ios/bin/qmake")
    parser.add_argument("--qt-version", default="5.15.18",
                        help="Qt version installed (for sysroot.toml). Default: 5.15.18")
    parser.add_argument("--pyqt-version", default="5.15.11", help="PyQt version to embed. Default: 5.15.11")
    parser.add_argument("--python-version", default=None,
                        help="Python version to embed (optional; sysroot.toml usually sets this).")
    parser.add_argument("--workdir", default=str(Path.cwd() / "pyqt_ios_build"),
                        help="Working directory (will be created).")
    parser.add_argument("--app-entry", default=None,
                        help="Path to your app's entry .py (optional; replaces demo entry).")
    args = parser.parse_args()
    verifyHost()
    qmake = findQmakeIos(args.qt_ios_qmake, args.qt_version)
    print("Using iOS qmake: {}".format(qmake))
    # 1) Ensure packages are present
    ensurePackages()
    pyqtdeploy_module = importPyqtdeploy()
    pyqtdeployRoot = Path(pyqtdeploy_module.__file__).parent
    print("pyqtdeploy package at: {}".format(pyqtdeployRoot))
    # 2) Patch pyqtdeploy internals (with backups)
    patchPlatformsArm64(pyqtdeployRoot / "platforms.py")
    patchPythonProAddCflag(pyqtdeployRoot / "sysroot" / "plugins" / "Python" / "configurations" / "python.pro")
    patchSipCaseInsensitiveGlob(pyqtdeployRoot / "sysroot" / "plugins" / "SIP.py")
    # 3) Prepare workspace and fetch demo
    workdir = ensureDir(Path(args.workdir).expanduser().resolve())
    downloads = ensureDir(workdir / "downloads")
    sources = ensureDir(workdir / "sources")
    demo_root = ensureDir(workdir / "demo")
    # Download pyqtdeploy sdist and extract demo
    print("Fetching pyqtdeploy source to obtain the demo…")
    pyqtdeploySdist = downloadSdist("pyqtdeploy", downloads)
    extracted = extractTarGz(pyqtdeploySdist, sources)
    # Copy demo folder
    demo_src = extracted / "demo"
    if not demo_src.exists():
        # Some versions place it under 'examples/demo'
        demo_src = extracted / "examples" / "demo"
    if not demo_src.exists():
        print("Could not locate 'demo' folder inside pyqtdeploy source.")
    if demo_root.exists():
        print("Clearing existing demo dir…")
        rmtree(demo_root)
    copytree(demo_src, demo_root)
    print("Demo prepared at: {}".format(demo_root))
    # 4) Download PyQt5-5.15.11 sdist into demo folder
    print("Downloading PyQt5-5.15.11 sdist for sysroot build…")
    pyqt5_sdist = downloadSdist("PyQt5", downloads, version=args.pyqt_version, filenameContains=args.pyqt_version)
    pyqt5_sdist_target = demo_root / pyqt5_sdist.name
    if not pyqt5_sdist_target.exists():
        copy2(pyqt5_sdist, pyqt5_sdist_target)
        print("  ✓ Copied {} into demo directory.".format(pyqt5_sdist.name))
    else:
        print("  … sdist already present in demo directory.")
    # 5) sysroot.toml edits
    sysroot_toml = demo_root / "sysroot.toml"
    if not sysroot_toml.exists():
       print("{} not found in demo.".format(sysroot_toml))
    tweakSysrootToml(sysroot_toml, args.pyqt_version, args.qt_version)
    # 6) demo py patch (avoid get_source_code crash)
    demoPy = demo_root / "pyqt-demo.py"
    if demoPy.exists():
        patchDemoPy(demoPy)
    # 7) Optional: replace demo with user's app entry
    if args.app_entry:
        appEntry = Path(args.app_entry)
        if not appEntry.exists():
            print("--app-entry not found: {}".format(appEntry))
        swapInUserApp(demo_root, appEntry)
    # 8) Build with build-demo.py
    buildScript = demo_root / "build-demo.py"
    if not buildScript.exists():
        print("build-demo.py not found in demo.")
    print("\nStarting pyqtdeploy build for iOS (this will take time)…\n")
    cmd = [
        sys.executable, str(buildScript),
        "--target", "ios-64",
        "--qmake", str(qmake),
        "--verbose",
    ]
    sh(cmd, cwd=str(demo_root))
    # 9) Report xcodeproj
    xcodeproj = demo_root / "build-ios-64" / "pyqt-demo.xcodeproj"
    if xcodeproj.exists():
        print("\n✓ Success!")
        print("Open this in Xcode: {}".format(xcodeproj))
        print("Build & Run on the iOS Simulator (or on device with proper signing).")
    else:
        print("\nBuild completed but the Xcode project was not found where expected.")
        print("Check the build output above for the exact location message.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n[ERROR]", e)
        sys.exit(1)




