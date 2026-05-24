# -*- coding: utf-8 -*-
import sys
from PySide import QtGui

class MainWindow(QtGui.QWidget):
    def __init__(self):
        super(MainWindow, self).__init__()

        self.label = QtGui.QLabel("Hello from PySide 1", self)
        self.button = QtGui.QPushButton("Click me", self)

        self.button.clicked.connect(self.on_click)

        layout = QtGui.QVBoxLayout()
        layout.addWidget(self.label)
        layout.addWidget(self.button)
        self.setLayout(layout)

        self.setWindowTitle("PySide 1 Example")
        self.resize(300, 120)

    def on_click(self):
        self.label.setText("Button clicked!")

if __name__ == '__main__':
    app = QtGui.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
