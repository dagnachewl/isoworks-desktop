from PyQt5.QtCore import QProcess, QCoreApplication
import sys
app = QCoreApplication(sys.argv)
proc = QProcess()
def on_err():
    print("STDERR:", proc.readAllStandardError().data().decode())
def on_out():
    print("STDOUT:", proc.readAllStandardOutput().data().decode())
proc.readyReadStandardError.connect(on_err)
proc.readyReadStandardOutput.connect(on_out)
proc.start(sys.executable, ["-c", "print('hello'); raise Exception('crash')"])
proc.waitForFinished()
