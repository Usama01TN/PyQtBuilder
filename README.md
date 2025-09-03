Below is a **single, self‑contained Python script** that automates the entire process you described—installing the right packages, fetching the pyqtdeploy demo, applying all the documented fixes (Apple Silicon arch handling, `-Wno-implicit-function-declaration` for Python, SIP’s case‑insensitive glob), tweaking `sysroot.toml` (PyQt 5.15.11, comment modules from PyQt3D to QScintilla, Qt 5.15.18), patching the demo’s `pyqt-demo.py` to avoid the crash, downloading the exact **PyQt5‑5.15.11** sdist into the demo, and finally invoking `build-demo.py` to produce an Xcode project for **iOS (ios‑64)**.

It also supports compiling **your own PyQt5 app** by passing `--app-entry /path/to/your_app.py` (it will wrap it so the demo’s build script uses your code). Backups are created for any file the script patches.

---

### Quick start

1. Save this as `build_pyqt_ios.py`
2. Ensure **Xcode** and **Qt 5.15.18 (iOS kit)** are installed.
3. Run:

```bash
python3 build_pyqt_ios.py \
  --qt-ios-qmake "$HOME/Qt/5.15.18/ios/bin/qmake" \
  --workdir "$HOME/pyqt_ios_build"
```

Optional (use your app instead of the demo):

```bash
python3 build_pyqt_ios.py \
  --qt-ios-qmake "$HOME/Qt/5.15.18/ios/bin/qmake" \
  --workdir "$HOME/pyqt_ios_build" \
  --app-entry /path/to/your_main.py
```

When it finishes, open the generated Xcode project it prints (e.g. `build-ios-64/pyqt-demo.xcodeproj`) and **Run** on the iOS Simulator (or on device if you have signing/profiles set).

---

### What this does for you (in plain terms)

* **Packages:** Installs `PyQt5==5.15.11`, `PyQt5_sip`, `pyqtdeploy`, `pyqt-builder`.
* **Demo:** Pulls the official **pyqtdeploy demo** from the pyqtdeploy source sdist (which is how Riverbank ships it).
* **sdist:** Places **PyQt5‑5.15.11.tar.gz** inside the demo directory (required for the sysroot stage).
* **Patches (exactly as in your memo):**

  * `pyqtdeploy/platforms.py` → adds **Apple Silicon (`arm64`)** recognition so it won’t mis-detect as `macos-32`.
  * `pyqtdeploy/sysroot/plugins/Python/configurations/python.pro` → adds **`-Wno-implicit-function-declaration`** to fix the `getentropy` compile error.
  * `pyqtdeploy/sysroot/plugins/SIP.py` → makes **glob case-insensitive** so `pyqt5_sip-*.tar.gz` is found even when case mismatches happen.
* **sysroot.toml edits:**

  * **PyQt = 5.15.11**
  * **Qt = 5.15.18**
  * **Comments out** modules **from `PyQt3D` through `QScintilla`** (to simplify/accelerate).
* **Demo crash fix:** Replaces `view.setText(get_source_code(...))` with `"don't give up..."`.
* **Your app (optional):** Pass `--app-entry` to compile *your* PyQt5 app instead of the demo; the script swaps the demo’s entry so pyqtdeploy builds your code.
* **Build:** Runs `build-demo.py --target ios-64 --qmake <your iOS qmake> --verbose` and prints the resulting `.xcodeproj`.

---

### Notes & tips

* The script targets **PyQt5** (per your memo). If you later migrate to PyQt6, the demo recipe and sysroot will differ.
* For **device** deployment, open the generated Xcode project, set your **Team** and **Bundle Identifier**, and build to your iPhone; the script itself focuses on producing a buildable Xcode project.
* Re‑running the script is safe: it makes **`.bak` backups** and applies **idempotent** patches.

Just run it as shown; it will do all the steps for you.
