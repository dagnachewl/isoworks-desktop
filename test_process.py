import sys, os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer
from siam_processor.views.main_window import StandaloneProcessorWindow
import logging

logging.basicConfig(level=logging.DEBUG)

app = QApplication(sys.argv)
window = StandaloneProcessorWindow()
window.show()

def click_process():
    print("Clicking process data...")
    try:
        window.processor_widget.start_processing()
    except Exception as e:
        print(f"CRASH: {e}")
        app.quit()
        
    print("Started processing.")

QTimer.singleShot(1000, click_process)
QTimer.singleShot(5000, app.quit) # timeout

app.exec_()
