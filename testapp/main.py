import sys
from PySide6.QtWidgets import QApplication, QLabel

def _run():                                  # pyside6-android-deploy convention
    app = QApplication(sys.argv)
    QLabel("Hello from Android!").show()
    sys.exit(app.exec())

if __name__ == "__main__":
    _run()
  
