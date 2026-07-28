import sys, os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QProcess, QTimer

app = QApplication(sys.argv)
proc = QProcess()

def on_finished(exitCode, exitStatus):
    print("EXIT CODE:", exitCode)
    print("STDERR:", proc.readAllStandardError().data().decode())
    print("STDOUT:", proc.readAllStandardOutput().data().decode())
    app.quit()

proc.finished.connect(on_finished)
proc.start(sys.executable, ["-m", "siam_processor.views.main_window", "1", "test_dsn", "SQL_SERVER"])

# wait 2 seconds, then simulate button click via applescript or similar?
# I can't easily click it via QProcess. But wait! I can modify main_window.py to auto-click it!

