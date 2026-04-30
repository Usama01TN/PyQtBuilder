# PyQtBuilder

> Automated cross-compilation pipelines for packaging **PyQt5 / PyQt6 / PySide / PySide6** applications as native **Android APKs** and **iOS apps**.

PyQtBuilder is a curated collection of single-file Python build scripts. Each script wraps an end-to-end toolchain (Qt + SIP/Shiboken + Python + NDK/Xcode + `pyqtdeploy` or `pyside6-android-deploy`) and exposes a friendly CLI so you can go from `myapp.py` to a deployable mobile artifact with a single command.

Because every "official" pipeline (Riverbank `pyqtdeploy`, the Plashless 2014 series, kviktor's recipe, achille-martin's `pyqt-crom`, Qt's own `pyside6-android-deploy`, Patrick Kidd's `pyside6-ios`, oforoshima's PyQt6 fork, …) targets a *different* combination of versions, host OS and target ABI, the repository ships **one self-contained builder per pipeline** instead of trying to merge them. Pick the script that matches your stack.

---

## Table of Contents:

- [Repository layout](#repository-layout)
- [Pipeline matrix](#pipeline-matrix)
- [Common workflow](#common-workflow)
- [Builder reference](#builder-reference)
  - [pyqt5_android_builder.py](#pyqt5_android_builderpy)
  - [pyqt5_android_kviktor.py](#pyqt5_android_kviktorpy)
  - [pyqt5_android_plashless.py](#pyqt5_android_plashlesspy)
  - [pyqt5_ios_builder.py](#pyqt5_ios_builderpy)
  - [pyqt5_ios_plashless.py](#pyqt5_ios_plashlesspy)
  - [pyqt6_ios_builder.py](#pyqt6_ios_builderpy)
  - [pyside6_android_builder.py](#pyside6_android_builderpy)
  - [pyside6_ios_builder.py](#pyside6_ios_builderpy)
  - [pyside_android_builder_py27.py](#pyside_android_builder_py27py)
- [`builders.py` helper module](#buildersp-y-helper-module)
- [Project layout expected by the builders](#project-layout-expected-by-the-builders)
- [Common flags](#common-flags)
- [Troubleshooting](#troubleshooting)
- [Credits](#credits)

---

## Repository layout:

```
PyQtBuilder/
├── builders.py                     # Shared executable-discovery helpers
├── pyqt5_android_builder.py        # PyQt5  -> Android (modern, Qt 5.15.2)
├── pyqt5_android_kviktor.py        # PyQt5  -> Android (kviktor pipeline)
├── pyqt5_android_plashless.py      # PyQt5  -> Android (Plashless 2014)
├── pyqt5_ios_builder.py            # PyQt5  -> iOS (modern, Qt 5.15.18)
├── pyqt5_ios_plashless.py          # PyQt5  -> iOS (Plashless 2014 trilogy)
├── pyqt6_ios_builder.py            # PyQt6  -> iOS (oforoshima fork)
├── pyside6_android_builder.py      # PySide6 -> Android (pyside6-android-deploy)
├── pyside6_ios_builder.py          # PySide6 -> iOS (patrickkidd architecture)
└── pyside_android_builder_py27.py  # PySide  -> Android (Necessitas, Python 2.7)
```

Every script is **standalone** — you can copy a single file out of the repository and use it on its own, with the only hard dependency being [`builders.py`](#buildersp-y-helper-module).

---

## Pipeline matrix:

| Script | Framework | Target | Host OS | Python | Qt | Notes |
|---|---|---|---|---|---|---|
| `pyqt5_android_builder.py` | PyQt5 5.15.10 | Android APK | Ubuntu 22.04 | 3.10.14 | 5.15.2 | NDK r21e, API 28, `pyqtdeploy` 3.3 |
| `pyqt5_android_kviktor.py` | PyQt5 5.15.1 | Android APK | Ubuntu 20.04 | 3.7.7 | 5.13.2 | SIP 4.19.24, `pyqtdeploy` 2.5.1, NDK r20b |
| `pyqt5_android_plashless.py` | PyQt5 5.3 | Android APK | Ubuntu 14.04 | 3.4.0 | 5.3 | NDK r10, `pyqtdeploy` 0.5/0.6, **Py3.4-only syntax** |
| `pyqt5_ios_builder.py` | PyQt5 5.15.x | iOS `.xcodeproj` | macOS | 3.10–3.12 | 5.15.18 | `pyqtdeploy` 3.3.x, includes 3 patches |
| `pyqt5_ios_plashless.py` | PyQt5 5.3.1 | iOS `.xcodeproj` | macOS 10.9.4+ | 3.4.0 | 5.3.0/5.3.1 | Xcode 5.1.1, ARM64 device |
| `pyqt6_ios_builder.py` | PyQt6 | iOS app | macOS | 3.x (≥3.9 recommended) | 6.9.x | Modified `pyqtdeploy` from oforoshima |
| `pyside6_android_builder.py` | PySide6 6.10.2 | Android APK/AAB | Linux/macOS | 3.10 or 3.11 | 6.x | Official `pyside6-android-deploy` |
| `pyside6_ios_builder.py` | PySide6 6.8.3 | iOS app | macOS Apple Silicon | 3.13 | 6.8.3 | Patrick Kidd `QtRuntime.framework` approach |
| `pyside_android_builder_py27.py` | PySide (legacy) | Android APK | Ubuntu | **2.7** | Necessitas 4.8 | M4rtinK pipeline, Shiboken |

> **Pick the newest one that matches your framework choice unless you have an explicit reason to reproduce a historical setup.** For new projects use `pyside6_android_builder.py` / `pyside6_ios_builder.py` (PySide6) or `pyqt5_android_builder.py` / `pyqt5_ios_builder.py` (PyQt5).

---

## Common workflow:

The high-level recipe is the same for every script:

1. **Get the script.** Either clone the repo or download a single `*.py` file plus `builders.py`.
   ```bash
   git clone https://github.com/Usama01TN/PyQtBuilder.git
   cd PyQtBuilder
   ```
2. **Prepare your application.** Make sure it has a clean entry point (typically `main.py`) and works on the host with a regular `python main.py`.
3. **Run the builder** with `--project-dir` pointing at your app:
   ```bash
   python pyqt5_android_builder.py --project-dir /path/to/myapp
   ```
4. **Wait.** The first run downloads Qt + NDK + Python sources and can take 30–90 minutes plus 15–50 GB of disk. Subsequent runs are incremental.
5. **Collect the artifact** — `.apk`, `.aab`, `.xcodeproj` or `.app` — printed in the final summary.

All builders support `--dry-run` (print commands, run nothing), `--verbose` (debug logs), and `--keep-build` (preserve intermediate files for inspection). When in doubt, run with `-v --dry-run` first.

---

## Builder reference:

### `pyqt5_android_builder.py`

Modern PyQt5 → Android APK pipeline using `pyqtdeploy` 3.3, Qt 5.15.2 and NDK r21e.

**Pipeline (8 steps).** Preflight → Env setup → Toolchain (SDK API 28 + NDK r21e + JDK 11 + Qt 5.15.2) → Source download (Python 3.10.14, SIP 6.8.3, PyQt5 5.15.10) → Sysroot cross-compile → `pyqtdeploy` configuration → `qmake` + `make` + `androiddeployqt` → optional `adb install`.

**Requirements.** Ubuntu 22.04 LTS (compatible Linuxes work), Python 3.10, ≥40 GB free disk, internet access on first run.

**Usage.**
```bash
# Full automated build (arm64 — most modern devices):
python pyqt5_android_builder.py --project-dir ./myapp --arch android-64

# 32-bit ARM (older devices):
python pyqt5_android_builder.py --project-dir ./myapp --arch android-32

# Use a pre-installed Qt / SDK / NDK to skip the long downloads:
python pyqt5_android_builder.py --project-dir ./myapp --qt-dir  ~/Qt5.15.2/5.15.2/android_arm64_v8a --ndk-path ~/Android/Sdk/ndk/21.4.7075529 --sdk-path ~/Android/Sdk

# Sysroot only (good for CI caching):
python pyqt5_android_builder.py --project-dir ./myapp --only-sysroot

# Build + install on the connected device:
python pyqt5_android_builder.py --project-dir ./myapp --install-apk

# Add extra Qt modules:
python pyqt5_android_builder.py --project-dir ./myapp --extra-pyqt-modules QtSql,QtBluetooth
```

**Key flags.**

| Flag | Default | Description |
|---|---|---|
| `--project-dir` | *required* | Path to your PyQt5 project (must contain a `.pdt` file). |
| `--app-name` | basename of project dir | Application label. |
| `--arch` | `android-64` | One of `android-32`, `android-64`, `android-x86`, `android-x86_64`. |
| `--qt-version` | `5.15.2` | Qt version override. |
| `--pyqt-version` | `5.15.10` | PyQt5 GPL version. |
| `--sip-version` | `6.8.3` | SIP version (must match PyQt5). |
| `--python-version` | `3.10.14` | Cross-compiled Python version. |
| `--qt-dir`, `--ndk-path`, `--sdk-path` | auto | Use pre-existing toolchains. |
| `--extra-pyqt-modules` | (empty) | Comma-separated Qt modules. |
| `--only-sysroot` | off | Stop after sysroot is built. |
| `--install-apk` | off | `adb install` to first connected device. |
| `--keep-build` | off | Keep intermediate build directory. |
| `--dry-run` / `-v` | off | Print-only / debug logging. |

---

### `pyqt5_android_kviktor.py`

PyQt5 5.15.1 → Android APK following [kviktor/pyqtdeploy-android-build](https://github.com/kviktor/pyqtdeploy-android-build) **exactly**, including the historical version pins.

**Pinned versions.** Python 3.7.7, Qt 5.13.2 (Android arm64-v8a), PyQt5 5.15.1, SIP 4.19.24, `pyqtdeploy` 2.5.1, OpenSSL 1.0.2r, NDK r20b, Android API 28 (platform) / 29 (deploy target), JDK 8, Ubuntu 20.04 LTS.

**Pipeline (13 steps).** Preflight → virtualenv with `pyqtdeploy` 2.5.1 → Qt validation → SDK/NDK check → source download → `sysroot.json` generation → `app.pdy` generation → Android assets (`AndroidManifest.xml`, `CustomActivity.java`) → sysroot build → app build → APK packaging via `androiddeployqt --gradle` → ADB install → summary.

**Usage.**
```bash
python pyqt5_android_kviktor.py --project-dir ./myapp --app-name MyApp --package-name com.example.myapp --qt-dir ~/Qt5.13.2/5.13.2/android_arm64_v8a --ndk-path ~/Android/Sdk/ndk/20.1.5948944 --sdk-path ~/Android/Sdk
```

**Key flags.** `--project-dir` (required), `--app-name`, `--package-name`, `--qt-dir`, `--ndk-path`, `--sdk-path`, `--jobs` (default 2 — kviktor uses `-j2`), `--extra-stdlib`, `--skip-sysroot`, `--install-apk`, `--keep-build`, `--dry-run`, `-v`.

---

### `pyqt5_android_plashless.py`

PyQt5 5.3 → Android using the [Plashless 2014](https://plashless.wordpress.com/2014/08/19/) pipeline with `pyqtdeploy` 0.5/0.6 and NDK r10.

> **Caveat:** this script is written so that it runs under **Python 3.4.0 exactly**. It deliberately avoids f-strings, `subprocess.run`, type annotations, dataclasses, the walrus operator, etc. Use it only when reproducing the historical setup.

**Pipeline (15 steps)** including patches to Python's `SYS_getdents64`, `epoll_create1`, `log2`, `python.pro`, and `config.c`.

**Usage.**
```bash
python3.4 pyqt5_android_plashless.py --project-dir ./myapp --ndk-root ~/android-ndk-r10 --qt-dir   ~/Qt5.3/5.3/android_armv7
```

**Key flags.** `--project-dir`, `--app-name`, `--ndk-root`, `--qt-dir`, `--sysroot`, `--work-dir`, `--python-src`, `--sip-src`, `--pyqt5-src`, `--jobs` (default 2), `--skip-static-build`, `--install-apk`, `--keep-build`, `--dry-run`, `-v`.

---

### `pyqt5_ios_builder.py`

Modern PyQt5 → iOS `.xcodeproj` builder for macOS, using `pyqtdeploy` 3.3.x.

**Pipeline (9 steps).** Preflight (macOS, Xcode, Python 3.10–3.12) → host venv (`pyqtdeploy` + PyQt5 + `pyqt-builder`) → toolchain validation → source download → patch three known `pyqtdeploy` issues (Apple-Silicon `arm64`, `getentropy()` declaration on iOS, case-sensitive `pyqt5_sip` glob) → `sysroot.toml` generation → sysroot build → app build → optional Xcode launch / Simulator run.

**Usage.**
```bash
# Generate Xcode project:
python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake

# Build and open in Xcode automatically:
python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --open-xcode

# Build and run on the iOS Simulator:
python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --run-simulator

# Sysroot only (cache it for CI):
python pyqt5_ios_builder.py --project-dir ./myapp --qmake ~/Qt/5.15.18/ios/bin/qmake --only-sysroot
```

**Key flags.** `--project-dir` (required), `--qmake`, `--qt-version`, `--pyqt-version`, `--python-version`, `--extra-modules`, `--sysroot`, `--only-sysroot`, `--open-xcode`, `--run-simulator`, `--keep-build`, `--dry-run`, `-v`.

---

### `pyqt5_ios_plashless.py`

PyQt5 5.3.1 → iOS following the [Plashless macOS / iOS trilogy](https://plashless.wordpress.com/2014/09/10/).

**Pinned versions.** macOS 10.9.4 Mavericks (or later), Xcode 5.1.1+, Qt 5.3.0/5.3.1, Qt Creator 3.1.1+, Python 3.4.0, PyQt5 5.3.1, SIP 4.16.1, `pyqtdeploy` 0.5/0.6, target ABI `ios-64`.

**Pipeline (14 steps).** Preflight → directory layout (`~/ios/iRoot`, `~/ios/Downloads`, `~/ios/pensoolBuild`) → environment + `env.sh` → `pyqtdeploy` from Mercurial → host SIP → host PyQt5 → sources → static Python → static SIP → `pyqt5-ios.cfg` patch → optional `qgraphicsvideoitem.sip` patch (QML) → static PyQt5 → `pyqtdeploy` build → `qmake` + open in Xcode.

**Usage.**
```bash
python pyqt5_ios_plashless.py --project-dir ./myapp --qt-dir ~/Qt5.3.1/5.3/ios --python-home ~/python3.4-ios --sysroot ~/ios/iRoot --downloads-dir ~/ios/Downloads --work-dir ~/ios/pensoolBuild
```

**Key flags.** `--project-dir`, `--app-name`, `--qt-dir`, `--python-home`, `--sysroot`, `--downloads-dir`, `--work-dir`, `--python-src`, `--sip-src`, `--pyqt5-src`, `--pyqtdeploy-src`, `--extra-qt-modules`, `--use-qml`, `--simulator`, `--skip-static`, `--jobs` (default 4), `--keep-build`, `--dry-run`, `-v`.

---

### `pyqt6_ios_builder.py`

PyQt6 → iOS pipeline based on [oforoshima's modified `pyqtdeploy`](https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment).

**Defaults.** Qt 6.9.1, Python 3.12.0. Requires macOS with Xcode, Qt 6.9.x **including the iOS module + Qt5 Compatibility Module**, and the modified `pyqtdeploy`.

**Usage.**
```bash
# Sanity-check prerequisites first:
python pyqt6_ios_builder.py --check-deps

# Install the modified pyqtdeploy fork into the active Python:
python pyqt6_ios_builder.py --install-pyqtdeploy

# Build:
python pyqt6_ios_builder.py --app myapp.py --qmake /path/to/Qt/6.9.1/ios/bin/qmake

# Override versions:
python pyqt6_ios_builder.py --app myapp.py --qmake /path/to/qmake --qt-version 6.9.2 --python-version 3.12.0
```

**Key flags (mutually exclusive actions).** `--app SCRIPT`, `--check-deps`, `--install-pyqtdeploy`. **Other flags.** `--qmake`, `--qt-version`, `--python-version`, `--pdt FILE`, `--pyqt-tarball FILE`, `--work-dir`, `--verbose` / `--quiet`, `--debug`.

---

### `pyside6_android_builder.py`

PySide6 → Android using Qt's official **`pyside6-android-deploy`** toolchain.

**Defaults.** PySide 6.10.2, Python 3.10 or 3.11, Linux (Ubuntu 22.04+) or macOS, ~15 GB free disk.

**Pipeline.** Preflight → SDK/NDK download (or auto-detect) → optional pre-downloaded wheels → `pyside6-android-deploy` invocation → APK/AAB output → optional `adb install`.

**Usage.**
```bash
# Full automated build (aarch64):
python pyside6_android_builder.py --project-dir ./myapp --arch aarch64

# Download SDK/NDK only:
python pyside6_android_builder.py --project-dir ./myapp --only-setup-env

# Install APK after build:
python pyside6_android_builder.py --project-dir ./myapp --arch aarch64 --install-apk

# Use pre-downloaded PySide6 + Shiboken Android wheels:
python pyside6_android_builder.py --project-dir ./myapp --wheel-pyside   /path/to/PySide6-...-android_aarch64.whl --wheel-shiboken /path/to/shiboken6-...-android_aarch64.whl

# Debug build with intermediate files retained:
python pyside6_android_builder.py --project-dir ./myapp --keep-build-files --verbose
```

**Key flags.** `--project-dir`, `--app-name`, `--arch` (`aarch64` / `armv7a` / `x86_64` / `i686`), `--python-version` (`3.10` or `3.11`), `--mode` (`debug` / `release`), `--ndk-path`, `--sdk-path`, `--wheel-pyside`, `--wheel-shiboken`, `--only-setup-env`, `--install-apk`, `--keep-build-files`, `--dry-run`, `-v`.

---

### `pyside6_ios_builder.py`

PySide6 → iOS using [patrickkidd/pyside6-ios](https://github.com/patrickkidd/pyside6-ios). Works around the upstream block (PYSIDE-2352) by:

1. Merging every Qt static lib into one dynamic `QtRuntime.framework` (clearing N_PEXT bits so symbols re-export).
2. Cross-compiling PySide6 modules (QtCore, QtGui, QtWidgets, …) as **static** libs linked into the host executable.
3. Registering each module via `PyImport_AppendInittab` so CPython treats them as built-ins (no dynamic loading on iOS).
4. Driving everything from an ObjC++ `main.mm` that owns `UIApplicationMain` and hands control to `QIOSEventDispatcher`.

**Defaults.** Qt 6.8.3, PySide 6.8.3, Python 3.13 (BeeWare `python-apple-support` tag `3.13-b13`). Requires macOS Apple Silicon, Xcode 16+, [`uv`](https://docs.astral.sh/uv/).

**Usage.**
```bash
# Check everything is in place:
python pyside6_ios_builder.py --check-deps

# Pick up where you are: bootstrap toolchain, then build everything:
python pyside6_ios_builder.py --bootstrap
python pyside6_ios_builder.py --build-all --app /path/to/myapp --app-name "My PySide6 App" --bundle-id com.example.myapp --team-id ABC123XYZ
```

**Pipeline actions (mutually exclusive).** `--check-deps`, `--bootstrap`, `--build-qtruntime`, `--build-support`, `--build-all`, `--generate-only`, `--list-devices`.

**App configuration.** `--app DIR`, `--app-name NAME`, `--bundle-id ID`, `--app-module MOD` (Python module containing `_run()`, default `main`), `--toml FILE`, `--team-id`, `--modules MOD ...`.

**Versions.** `--qt-version`, `--pyside-version`, `--python-version`, `--python-support-tag`.

**Paths.** `--root DIR`, `--qt-ios DIR`.

---

### `pyside_android_builder_py27.py`

PySide → Android using the **Necessitas SDK** + M4rtinK's [android-pyside-build-scripts](https://github.com/M4rtinK/android-pyside-build-scripts), [shiboken-android](https://github.com/M4rtinK/shiboken-android), [pyside-android](https://github.com/M4rtinK/pyside-android).

> **Python 2.7 is required** — this builder targets the legacy modRana stack.

**Pipeline (14 steps).** Preflight (Ubuntu host, Python 2.7, cmake, git, API-14) → clone source forks → environment / `env.sh` equivalents → download pre-built Android Python 2.7 → cmake-cross-compile Shiboken (ARM) → cmake-cross-compile PySide (ARM) → strip `.so` files with `arm-linux-androideabi-strip` → app packaging (`my_python_project.zip` + `python27.zip`) → C++ wrapper (`main.h` / `main.cpp`) → project scaffold (clone + rename example project) → sed-based rename → `ant debug` (Necessitas) → optional `adb install` → summary.

**Usage.**
```bash
python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas --app-name MyApp --unique-name com.example.MyApp

# Use pre-built PySide libs (skip the long Shiboken/PySide compile):
python2.7 pyside_android_builder_py27.py --project-dir ./myapp --necessitas-sdk ~/necessitas --pyside-stage ~/prebuilt-pyside-android
```

**Key flags.** `--project-dir`, `--necessitas-sdk`, `--app-name`, `--unique-name`, `--pyside-stage`, `--skip-build`, `--install-apk`, `--keep-build`, `--dry-run`, `-v`.

---

## `builders.py` helper module:

`builders.py` is the only shared dependency between the builders. It provides cross-platform helpers for locating executables on `PATH` (with fallbacks for ancient Python that lacks `shutil.which` or `subprocess.run`):

```python
from builders import (
    getCmakeExecutable,         # cmake / cmake3
    getMakeExecutable,          # make
    getGitExecutable,           # git
    getAntExecutable,           # ant
    getAdbExecutable,           # adb
    getNdkBuildExecutable,      # ndk-build
    getArmLinuxAndroideabiStripExecutable,
    getUVExecutable,            # uv (Astral)
    getXcodebuildExecutable,    # xcodebuild
    getXcrunExecutable,         # xcrun
    getXcodeSelectExecutable,   # xcode-select
    getPythonExecutable,        # python / python3
    getPyqtdeploySysrootExecutable,
    getPyqtdeployBuildExecutable,
    getHgExecutable,            # mercurial (used by Plashless builders)
    getOpenExecutable,          # open (macOS)
    getJavaExecutable,          # java
)
```

You can reuse it in your own scripts the same way the builders do:

```python
from builders import getCmakeExecutable, getMakeExecutable
cmake = getCmakeExecutable()
make  = getMakeExecutable()
```

It depends on the [`cmake`](https://pypi.org/project/cmake/) PyPI package for `CMAKE_BIN_DIR`. Install it with:

```bash
pip install cmake
```

---

## Project layout expected by the builders:

A "well-formed" project that the builders accept usually looks like:

```
myapp/
├── main.py                # Entry point (or whatever you point pyqtdeploy at)
├── ui/                    # Your modules
├── resources/             # Icons, QML, translations…
└── myapp.pdt              # pyqtdeploy project file (PyQt5/PyQt6 builders)
```

For PySide6 builders the `.pdt` is replaced by a `pyproject.toml` (Android) or a `pyside6-ios.toml` (iOS); each builder will auto-generate one if you don't provide it.

---

## Common flags:

Every builder exposes these (sometimes under slightly different names):

| Flag | Meaning |
|---|---|
| `--project-dir DIR` | Path to your application — **always required**. |
| `--app-name NAME` | Override the human-readable application name. Defaults to the project directory name. |
| `--keep-build` / `--keep-build-files` | Keep intermediate build artifacts for debugging. |
| `--dry-run` | Print every shell command without running it. |
| `-v` / `--verbose` / `--debug` | Enable DEBUG-level logging. |
| `--install-apk` | (Android only) `adb install` the APK to the first connected device. |
| `--only-sysroot` / `--only-setup-env` / `--skip-build` | Stop after toolchain/sysroot phase — useful for CI caching. |

---

## Troubleshooting:

A few recurring issues, with the script that most often surfaces them:

- **"Disk full" during sysroot build** — every Android builder needs 15–50 GB. `pyqt5_android_builder.py` enforces a 40 GB minimum upfront.
- **`pyqtdeploy` complains about Apple Silicon** — `pyqt5_ios_builder.py` patches this automatically; if you run a different builder on `arm64` Macs you may need to apply the same patch manually (see the docstring at the top of `pyqt5_ios_builder.py`).
- **`getentropy() undeclared` on iOS** — same as above, patched automatically by `pyqt5_ios_builder.py`.
- **`pyqt5_sip` not found at link time** — the iOS builders fix a case-sensitive glob in `SIP.py`; if you're using a custom `pyqtdeploy` you may need to apply that patch yourself.
- **Plashless / kviktor builders fail on a modern host** — they target Ubuntu 14.04 / 20.04 with specific NDK/SDK versions. Use a container or VM that matches the documented host OS.
- **PySide6 iOS linker errors about duplicated Qt symbols** — that's PYSIDE-2352. Use `pyside6_ios_builder.py` (which builds `QtRuntime.framework`) rather than rolling your own.
- **`adb` cannot find the device** after `--install-apk` — make sure USB debugging is enabled and the device is authorized; `adb devices` should list it before you run the builder.

For all builders, run with `-v --dry-run` first to inspect the commands; then re-run without `--dry-run` once you're satisfied.

---

## Credits:

PyQtBuilder is a single-author repository ([@Usama01TN](https://github.com/Usama01TN)) that codifies — script by script — the cross-compilation pipelines documented by:

- Riverbank Computing's [`pyqtdeploy` documentation](https://www.riverbankcomputing.com/static/Docs/pyqtdeploy/).
- The Plashless [Android trilogy](https://plashless.wordpress.com/2014/08/19/) and [macOS/iOS trilogy](https://plashless.wordpress.com/2014/09/10/).
- [kviktor/pyqtdeploy-android-build](https://github.com/kviktor/pyqtdeploy-android-build).
- [achille-martin/pyqt-crom](https://github.com/achille-martin/pyqt-crom).
- [oforoshima](https://oforoshima.medium.com/) — PyQt5 + PyQt6 iOS write-ups and the [`pyqtdeploy-pyqt6ios-experiment`](https://github.com/0sh1ma/pyqtdeploy-pyqt6ios-experiment) fork.
- Qt's official [`pyside6-android-deploy` documentation](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-android-deploy.html) and [EchterAlsFake/PySide6-to-Android](https://github.com/EchterAlsFake/PySide6-to-Android).
- [patrickkidd/pyside6-ios](https://github.com/patrickkidd/pyside6-ios) — the static-Qt + `QtRuntime.framework` architecture.
- M4rtinK's [android-pyside-build-scripts](https://github.com/M4rtinK/android-pyside-build-scripts) (Necessitas pipeline, used in the Python 2.7 builder).

Each script's header lists the exact references it follows.
