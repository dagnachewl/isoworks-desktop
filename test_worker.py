import sys, os
from PyQt5.QtWidgets import QApplication
from siam_processor.views.main_window import StandaloneProcessorWindow
from siam_processor.workers.worker import Worker
from PyQt5.QtCore import QTimer

app = QApplication(sys.argv)

window = StandaloneProcessorWindow()
window.show()

def check_status():
    print("STATUS:", window.processor_widget.status_label.text())

QTimer.singleShot(1000, lambda: window.processor_widget.start_processing())
QTimer.singleShot(2000, check_status)
QTimer.singleShot(4000, check_status)
QTimer.singleShot(6000, app.quit)

app.exec_()
