"""
siam_processor_config_gui.py — SIAM processor configuration dialogs for IsoWorks.
Provides MeasurablesEditorDialog and PostProcessingDialog for configuring the
measurables and post-processing parameters of an analysis procedure.
"""
import logging
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QTableWidget, QTableWidgetItem, QPushButton,
    QComboBox, QFormLayout, QGroupBox, QLineEdit, QMessageBox, QHeaderView,
    QHBoxLayout, QAbstractItemView, QToolButton, QTextEdit, QWidgetAction
)
from PyQt5.QtCore import Qt, QTimer
from db_core import db_manager
from sqlalchemy import text

class MeasurablesEditorDialog(QDialog):
    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.setWindowTitle(f"Edit Measurables - Procedure {pid}")
        self.resize(800, 500)
        self.setup_ui()
        try: self.load_data()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def setup_ui(self):
        l = QVBoxLayout(self)
        g = QGroupBox("Add"); f = QFormLayout(g)
        self.cmbMeas = QComboBox(); self.txtAcc = QLineEdit(); self.txtRep = QLineEdit(); self.txtPct = QLineEdit()
        self.btnAdd = QPushButton("Add/Update")
        f.addRow("Measurable:", self.cmbMeas); f.addRow("Accuracy:", self.txtAcc); f.addRow("Repeats:", self.txtRep); f.addRow("Acceptance %:", self.txtPct); f.addRow(self.btnAdd)
        self.btnAdd.clicked.connect(self.add_meas)
        l.addWidget(g)
        
        self.tbl = QTableWidget(0, 5); self.tbl.setHorizontalHeaderLabels(["Name", "Accuracy", "Repeats", "Pct", "ID"])
        self.tbl.hideColumn(4); self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows); self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.cellClicked.connect(self.on_click)
        l.addWidget(self.tbl)
        
        b = QHBoxLayout(); self.delBtn = QPushButton("Remove"); self.clsBtn = QPushButton("Close")
        b.addWidget(self.delBtn); b.addStretch(); b.addWidget(self.clsBtn)
        self.delBtn.clicked.connect(self.delete); self.clsBtn.clicked.connect(self.accept)
        l.addLayout(b)
        self._load_combo()

    def _load_combo(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MeasurableID, MeasurableName FROM Measurables ORDER BY MeasurableID"))
                self.cmbMeas.addItem("- Select -", None)
                for r in res: self.cmbMeas.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def load_data(self):
        self.tbl.setRowCount(0)
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT M.MeasurableName, AP.AccuracyLimit, AP.Repeats, AP.RepeatAcceptancePercent, M.MeasurableID FROM AnalysisProcedure_Measurable AP JOIN Measurables M ON AP.MeasurableID=M.MeasurableID WHERE AP.ProcedureID=:pid"), {"pid": self.pid})
                for r in res:
                    row = self.tbl.rowCount(); self.tbl.insertRow(row)
                    self.tbl.setItem(row, 0, QTableWidgetItem(r[0]))
                    self.tbl.setItem(row, 1, QTableWidgetItem(str(r[1] or "")))
                    self.tbl.setItem(row, 2, QTableWidgetItem(str(r[2] or "")))
                    self.tbl.setItem(row, 3, QTableWidgetItem(str(r[3] or "")))
                    self.tbl.setItem(row, 4, QTableWidgetItem(str(r[4])))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_click(self, row, col):
        mid = int(self.tbl.item(row, 4).text())
        self.cmbMeas.setCurrentIndex(self.cmbMeas.findData(mid))
        self.txtAcc.setText(self.tbl.item(row, 1).text())
        self.txtRep.setText(self.tbl.item(row, 2).text())
        self.txtPct.setText(self.tbl.item(row, 3).text())

    def add_meas(self):
        mid = self.cmbMeas.currentData()
        if not mid: return
        try:
            with db_manager.get_connection() as conn:
                exists = conn.execute(text("SELECT 1 FROM AnalysisProcedure_Measurable WHERE ProcedureID=:pid AND MeasurableID=:mid"), {"pid": self.pid, "mid": mid}).fetchone()
                params = {"acc": self.txtAcc.text(), "rep": self.txtRep.text(), "pct": self.txtPct.text(), "pid": self.pid, "mid": mid}
                if exists:
                    conn.execute(text("UPDATE AnalysisProcedure_Measurable SET AccuracyLimit=:acc, Repeats=:rep, RepeatAcceptancePercent=:pct WHERE ProcedureID=:pid AND MeasurableID=:mid"), params)
                else:
                    conn.execute(text("INSERT INTO AnalysisProcedure_Measurable (ProcedureID, MeasurableID, AccuracyLimit, Repeats, RepeatAcceptancePercent) VALUES (:pid, :mid, :acc, :rep, :pct)"), params)
                conn.commit()
            self.load_data()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def delete(self):
        r = self.tbl.currentRow()
        if r < 0: return
        try:
            with db_manager.get_connection() as conn:
                mid = self.tbl.item(r, 4).text()
                conn.execute(text("DELETE FROM AnalysisProcedure_Measurable WHERE ProcedureID=:pid AND MeasurableID=:mid"), {"pid": self.pid, "mid": mid})
                conn.commit()
            self.load_data()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

class PostProcessingDialog(QDialog):
    def __init__(self, pid, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.editing_id = None
        self.resize(700, 500)
        self.setup_ui()
        try: self.load_data()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def setup_ui(self):
        l = QVBoxLayout(self)
        g = QGroupBox("Step"); f = QFormLayout(g)
        self.cmbType = QComboBox(); self.cmbSub = QComboBox(); self.txtSeq = QLineEdit(); self.txtLab = QLineEdit()
        self.btnAdd = QPushButton("Add"); self.btnAdd.clicked.connect(lambda: self.save(True))
        self.btnUpd = QPushButton("Update"); self.btnUpd.clicked.connect(lambda: self.save(False)); self.btnUpd.setVisible(False)
        f.addRow("Type:", self.cmbType); f.addRow("Subtype:", self.cmbSub); f.addRow("Seq:", self.txtSeq); f.addRow("Lab ID:", self.txtLab)
        l.addWidget(g); l.addWidget(self.btnAdd); l.addWidget(self.btnUpd)
        
        self.cmbType.currentIndexChanged.connect(self.load_sub)
        
        self.tbl = QTableWidget(0, 6); self.tbl.setHorizontalHeaderLabels(["Seq", "Type", "Sub", "ID", "Rem", "PK"])
        self.tbl.hideColumn(5); self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.cellClicked.connect(self.on_click)
        l.addWidget(self.tbl)
        
        self.delBtn = QPushButton("Delete"); self.delBtn.clicked.connect(self.delete)
        l.addWidget(self.delBtn)
        self._load_type()

    def _load_type(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT DISTINCT CorrectionType, CorrectionName FROM GUItblSICorrections ORDER BY CorrectionType"))
                self.cmbType.addItem("-", None)
                for r in res: self.cmbType.addItem(r[1] or str(r[0]), r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def load_sub(self):
        tid = self.cmbType.currentData(); self.cmbSub.clear(); self.cmbSub.addItem("-", None)
        if not tid: return
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT CorrectionSubType, SubTypeName FROM GUItblSICorrectionSubType WHERE CorrectionType=:tid"), {"tid": tid})
                for r in res: self.cmbSub.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def load_data(self):
        self.tbl.setRowCount(0)
        try:
            with db_manager.get_connection() as conn:
                sql = """SELECT APP.RunSequence, APP.CorrectionName, ST.SubTypeName, APP.OurlabID, APP.Remarks, APP.ID, APP.CorrectionType, APP.CorrectionSubType FROM AnalysisProcedure_PostProcessing APP LEFT JOIN GUItblSICorrectionSubType ST ON APP.CorrectionType = ST.CorrectionType AND APP.CorrectionSubType = ST.CorrectionSubType WHERE APP.ProcedureID = :pid ORDER BY APP.RunSequence"""
                res = conn.execute(text(sql), {"pid": self.pid})
                for r in res:
                    row = self.tbl.rowCount(); self.tbl.insertRow(row)
                    for i in range(6): self.tbl.setItem(row, i, QTableWidgetItem(str(r[i] or "")))
                    # Store raw IDs in user role if needed, but here we reload on click
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_click(self, row, col):
        self.editing_id = int(self.tbl.item(row, 5).text())
        self.btnUpd.setVisible(True); self.btnAdd.setVisible(False)
        # Reload exact values from DB or Parse table
        # Simplified: just set sequence and IDs
        self.txtSeq.setText(self.tbl.item(row, 0).text())
        self.txtLab.setText(self.tbl.item(row, 3).text())

    def save(self, is_new):
        try:
            with db_manager.get_connection() as conn:
                params = {
                    "pid": self.pid, "typ": self.cmbType.currentData(), "sub": self.cmbSub.currentData(),
                    "seq": self.txtSeq.text(), "lab": self.txtLab.text(), "name": self.cmbType.currentText(), "rem": None
                }
                if is_new:
                    # Manually get ID if needed, or let identity handle it. Assuming Identity column ID
                    conn.execute(text("INSERT INTO AnalysisProcedure_PostProcessing (ProcedureID, CorrectionType, CorrectionSubType, RunSequence, OurlabID, CorrectionName, Remarks) VALUES (:pid, :typ, :sub, :seq, :lab, :name, :rem)"), params)
                else:
                    params["id"] = self.editing_id
                    conn.execute(text("UPDATE AnalysisProcedure_PostProcessing SET CorrectionType=:typ, CorrectionSubType=:sub, RunSequence=:seq, OurlabID=:lab WHERE ID=:id"), params)
                conn.commit()
            self.load_data(); self.btnUpd.setVisible(False); self.btnAdd.setVisible(True)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def delete(self):
        r = self.tbl.currentRow()
        if r < 0: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM AnalysisProcedure_PostProcessing WHERE ID=:id"), {"id": self.tbl.item(r, 5).text()})
                conn.commit()
            self.load_data()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)