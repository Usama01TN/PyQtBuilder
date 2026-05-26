# -*- coding: utf-8 -*-
import sys
import traceback

print("=== main.py starting ===", flush=True)

try:
    from PyQt5.QtCore import qVersion, QTimer, Qt
    from PyQt5.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

    print(f"Qt {qVersion()} / Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
          flush=True)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)

    # IMPORTANT: keep references at module scope or as attributes,
    # otherwise Python's GC will reap them before show() takes effect.
    window = QWidget()
    window.setWindowTitle("PyQt5 on Android")
    layout = QVBoxLayout(window)
    layout.setAlignment(Qt.AlignCenter)
    layout.addWidget(QLabel("Hello from PyQt5 on Android!"))
    layout.addWidget(QLabel(f"Qt {qVersion()}"))
    window.resize(400, 300)
    window.show()                     # NOT QLabel("..").show() — keep the ref!

    counter = {'n': 0}
    def tick():
        counter['n'] += 1
        print(f"[heartbeat] tick {counter['n']}", flush=True)
    timer = QTimer()                  # again, keep a reference
    timer.timeout.connect(tick)
    timer.start(1000)

    print("Entering event loop...", flush=True)
    rc = app.exec_()
    print(f"Event loop exited with rc={rc}", flush=True)
    sys.exit(rc)

except Exception:
    print("=== FATAL EXCEPTION ===", flush=True)
    traceback.print_exc()
    sys.exit(1)
