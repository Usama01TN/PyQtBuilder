# -*- coding: utf-8 -*-
"""
None
"""
from PySide6.QtWidgets import QApplication, QMainWindow, QLabel
from sys import argv, exit

app = QApplication(argv)  # type: QApplication
w = QMainWindow()  # type: QMainWindow
lbl = QLabel('Hello World!')
w.setCentralWidget(lbl)
w.show()
exit(app.exec_() if hasattr(app, 'exec_') else app.exec())
