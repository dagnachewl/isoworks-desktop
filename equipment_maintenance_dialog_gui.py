"""
equipment_maintenance_dialog_gui.py — Equipment maintenance history dialog for IsoWorks.
Provides EquipmentMaintenanceDialog (QDialog) for creating, editing, and
reviewing maintenance records for a given piece of equipment via db_core.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QGroupBox, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QToolButton, QTextEdit, QDialogButtonBox
)
from PyQt5.QtCore import Qt, QTimer

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message

class EquipmentMaintenanceDialog(QDialog):
    """
    Port of frmEditEquipmentMaintenance for managing maintenance records.
    Refactored for db_manager/SQLAlchemy.
    """
    
    def __init__(self, equipment_id, equipment_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Maintenance History for: {equipment_name}")
        self.setGeometry(200, 200, 900, 600)
        self.setModal(True)
        
        self.current_equipment_id = equipment_id
        self.current_maintenance_id = None
        self.is_new_record = False
        self.has_privileges = True 
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_selection_panel())
        self.main_layout.addWidget(self._create_detail_panel(), 1)
        
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_maintenance_type_combo()
            self.load_selection_list()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
            
        self._set_edit_mode(False)

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    # --- UI Creation ---
    def _create_action_buttons(self):
        l = QHBoxLayout(); self.btnNew = QPushButton("New"); self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save"); self.btnCancel = QPushButton("Cancel"); self.btnDelete = QPushButton("Delete")
        self.btnExit = QPushButton("Close")
        l.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete, self.btnExit]: l.addWidget(b)
        return l
        
    def _create_selection_panel(self):
        g = QGroupBox("Selection"); l = QHBoxLayout(g)
        l.addWidget(QLabel("Select Record:")); self.cmbSelectionList = QComboBox(); l.addWidget(self.cmbSelectionList, 1)
        self.btnMoveBack = QToolButton(); self.btnMoveBack.setText("<"); self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack); l.addWidget(self.btnMoveForward)
        return g
        
    def _create_detail_panel(self):
        g = QGroupBox("Details"); l = QVBoxLayout(g); f = QFormLayout()
        self.txtMaintenanceID = QLineEdit(); self.txtMaintenanceID.setReadOnly(True)
        self.cmbMaintenanceType = QComboBox()
        self.dteMaintenanceDate = QLineEdit(); self.dteMaintenanceDate.setPlaceholderText("yyyy-mm-dd")
        
        op_lay = QHBoxLayout(); self.txtOperator = QLineEdit(); self.btnSetOperator = QToolButton(); self.btnSetOperator.setText("*")
        op_lay.addWidget(self.txtOperator); op_lay.addWidget(self.btnSetOperator)
        
        self.txtComments = QTextEdit(); self.txtComments.setMinimumHeight(100)
        self.chkIsRecordClosed = QCheckBox("Record Closed"); self.chkUpdateAlert = QCheckBox("Update Alert")
        
        f.addRow("ID:", self.txtMaintenanceID); f.addRow("Type:", self.cmbMaintenanceType)
        f.addRow("Date:", self.dteMaintenanceDate); f.addRow("Operator:", op_lay)
        f.addRow("Comments:", self.txtComments); f.addRow("", self.chkIsRecordClosed); f.addRow("", self.chkUpdateAlert)
        l.addLayout(f)
        
        sg = QGroupBox("History"); sf = QFormLayout(sg)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        for w in [self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.setReadOnly(True)
        sf.addRow("Created:", self.txtCreateDateStamp); sf.addRow("By:", self.txtCreateUserStamp)
        sf.addRow("Modified:", self.txtModifDateStamp); sf.addRow("By:", self.txtModifUserStamp)
        l.addWidget(sg); l.addStretch(1)
        return g

    def _connect_signals(self):
        self.cmbSelectionList.currentIndexChanged.connect(self.on_selection_changed)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete); self.btnExit.clicked.connect(self.reject)
        self.btnMoveBack.clicked.connect(self.on_move_back); self.btnMoveForward.clicked.connect(self.on_move_forward)
        self.btnSetOperator.clicked.connect(lambda: self.txtOperator.setText(getpass.getuser()))

    # --- Logic ---
    def _set_edit_mode(self, edit):
        browse = not edit
        self.cmbSelectionList.setEnabled(browse); self.btnMoveBack.setEnabled(browse); self.btnMoveForward.setEnabled(browse)
        self.btnNew.setEnabled(browse); self.btnEdit.setEnabled(browse and self.current_maintenance_id is not None)
        self.btnDelete.setEnabled(browse and self.current_maintenance_id is not None)
        self.btnSave.setEnabled(edit); self.btnCancel.setEnabled(edit)
        self.cmbMaintenanceType.setEnabled(edit); self.dteMaintenanceDate.setReadOnly(browse)
        self.txtOperator.setReadOnly(browse); self.btnSetOperator.setEnabled(edit)
        self.txtComments.setReadOnly(browse); self.chkIsRecordClosed.setEnabled(edit); self.chkUpdateAlert.setEnabled(edit)

    def load_maintenance_type_combo(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT TypeID, MaintenanceType FROM GUIEquipmentMaintenanceType ORDER BY TypeID"))
                self.cmbMaintenanceType.addItem("", None)
                for r in res: self.cmbMaintenanceType.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def load_selection_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes ---
                sql = f"""SELECT MaintenanceID, {db_manager.sql_concat("GUIEquipmentMaintenanceType.MaintenanceType", "'--'", "MaintenanceDate")} AS strSortName FROM EquipmentMaintenance INNER JOIN GUIEquipmentMaintenanceType ON GUIEquipmentMaintenanceType.TypeID = EquipmentMaintenance.MaintenanceType WHERE EquipmentID = :eid ORDER BY MaintenanceDate DESC"""
                for r in conn.execute(text(sql), {"eid": self.current_equipment_id}):
                    self.cmbSelectionList.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        finally: 
            self.cmbSelectionList.blockSignals(False)
            if not self.is_new_record: 
                self.cmbSelectionList.setCurrentIndex(0); self.on_selection_changed()

    def on_selection_changed(self):
        mid = self.cmbSelectionList.currentData()
        self.populate_form(mid)

    def populate_form(self, mid):
        if not mid: self.current_maintenance_id=None; self._clear_form_fields(); self._set_edit_mode(False); self.btnNew.setEnabled(True); return
        self.current_maintenance_id = mid
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM EquipmentMaintenance WHERE MaintenanceID = :mid"), {"mid": mid}).fetchone()
                if r:
                    self.txtMaintenanceID.setText(str(r.MaintenanceID))
                    self.cmbMaintenanceType.setCurrentIndex(self.cmbMaintenanceType.findData(r.MaintenanceType))
                    self.dteMaintenanceDate.setText(str(r.MaintenanceDate.date()) if r.MaintenanceDate else "")
                    self.txtOperator.setText(r.Operator or ""); self.txtComments.setText(r.Comments or "")
                    self.chkIsRecordClosed.setChecked(bool(r.IsRecordClosed))
                    self.txtCreateDateStamp.setText(str(r.CreateDateStamp) if r.CreateDateStamp else "")
                    self.txtCreateUserStamp.setText(r.CreateUserStamp or ""); self.txtModifDateStamp.setText(str(r.ModifDateStamp) if r.ModifDateStamp else "")
                    self.txtModifUserStamp.setText(r.ModifUserStamp or "")
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_edit_mode(False)

    def _clear_form_fields(self):
        for w in [self.txtMaintenanceID, self.txtOperator, self.txtComments, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.cmbMaintenanceType.setCurrentIndex(0); self.dteMaintenanceDate.setText(datetime.now().date().isoformat())
        self.chkIsRecordClosed.setChecked(False); self.chkUpdateAlert.setChecked(False)

    def on_new(self):
        self.is_new_record=True; self._clear_form_fields()
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(MaintenanceID) FROM EquipmentMaintenance")).fetchone()
                self.txtMaintenanceID.setText(str((res[0] or 0) + 1))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self.txtOperator.setText(getpass.getuser()); self._set_edit_mode(True)

    def on_edit(self): 
        if self.chkIsRecordClosed.isChecked(): QMessageBox.warning(self, "Closed", "Record is closed."); return
        self.is_new_record=False; self._set_edit_mode(True)
        
    def on_cancel(self): self._set_edit_mode(False); self.populate_form(self.current_maintenance_id)

    def on_save(self):
        if not self.cmbMaintenanceType.currentData(): return
        try:
            with db_manager.get_connection() as conn:
                p = {
                    "eid": self.current_equipment_id, "typ": self.cmbMaintenanceType.currentData(),
                    "dat": self.dteMaintenanceDate.text(), "op": self.txtOperator.text(),
                    "com": self.txtComments.toPlainText(), "alert": self.chkUpdateAlert.isChecked(),
                    "cls": self.chkIsRecordClosed.isChecked(), "now": datetime.now(), "user": getpass.getuser()
                }
                if self.is_new_record:
                    p["mid"] = int(self.txtMaintenanceID.text()); self.current_maintenance_id = p["mid"]
                    conn.execute(text("INSERT INTO EquipmentMaintenance (EquipmentID, MaintenanceType, MaintenanceDate, Operator, Comments, chkUpdateAlert, IsRecordClosed, CreateDateStamp, CreateUserStamp, MaintenanceID, ModifDateStamp, ModifUserStamp) VALUES (:eid, :typ, :dat, :op, :com, :alert, :cls, :now, :user, :mid, :now, :user)"), p)
                else:
                    p["mid"] = self.current_maintenance_id
                    conn.execute(text("UPDATE EquipmentMaintenance SET EquipmentID=:eid, MaintenanceType=:typ, MaintenanceDate=:dat, Operator=:op, Comments=:com, chkUpdateAlert=:alert, IsRecordClosed=:cls, ModifDateStamp=:now, ModifUserStamp=:user WHERE MaintenanceID=:mid"), p)
                conn.commit()
            self.load_selection_list(); self.populate_form(self.current_maintenance_id)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete record?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM EquipmentMaintenance WHERE MaintenanceID=:mid"), {"mid": self.current_maintenance_id})
                conn.commit()
            self.load_selection_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_move_back(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i-1)
    def on_move_forward(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count()-1: self.cmbSelectionList.setCurrentIndex(i+1)