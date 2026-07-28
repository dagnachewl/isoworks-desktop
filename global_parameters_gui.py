"""
global_parameters_gui.py — Global parameters management panel for IsoWorks.
Provides GlobalParametersWidget with full CRUD operations on the GlobalValue
table (application-wide key/value configuration) via db_core.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QTextEdit, QToolButton, QCompleter
)
from PyQt5.QtCore import Qt

# --- Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

class GlobalParametersWidget(QWidget):
    """
    A widget for full CRUD operations on the GlobalValue table.
    Refactored to use db_core.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_param_id = None
        self.is_new_record = False
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_selection_panel())
        self.main_layout.addSpacing(20)
        self.main_layout.addWidget(self._create_detail_panel(), 1)
        
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_parameters_list()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
            
        self._set_form_read_only(True)
        self.btnSave.setEnabled(False); self.btnCancel.setEnabled(False)

    # --- SQL Helpers ---
    def _sql_bool(self, value):
        return "1" if value else "0"

    # --- UI Creation ---
    def _create_action_buttons(self):
        l = QHBoxLayout(); self.btnNew = QPushButton("New"); self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save"); self.btnCancel = QPushButton("Cancel")
        l.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel]: l.addWidget(b)
        l.addWidget(make_help_button(self, "global_params"))
        return l

    def _create_selection_panel(self):
        g = QGroupBox("Selection"); l = QHBoxLayout(g)
        l.addWidget(QLabel("Filter/Select Parameter:"))
        self.cmbSelectionList = QComboBox(); self.cmbSelectionList.setEditable(True)
        self.cmbSelectionList.setInsertPolicy(QComboBox.NoInsert)
        self.cmbSelectionList.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.cmbSelectionList.completer().setFilterMode(Qt.MatchContains)
        l.addWidget(self.cmbSelectionList, 1)
        self.btnMoveBack = QToolButton(); self.btnMoveBack.setText("<")
        self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack); l.addWidget(self.btnMoveForward)
        return g
        
    def _create_detail_panel(self):
        g = QGroupBox("Details"); l = QVBoxLayout(g); f = QFormLayout()
        self.txtID = QLineEdit(); self.txtID.setReadOnly(True)
        self.txtToken = QLineEdit(); self.txtTokenValue = QLineEdit()
        self.txtDescription = QTextEdit(); self.txtDescription.setFixedHeight(100)
        self.chkIsObsolete = QCheckBox("Obsolete")
        f.addRow("ID:", self.txtID); f.addRow("Token:", self.txtToken)
        f.addRow("Value:", self.txtTokenValue); f.addRow("Desc:", self.txtDescription); f.addRow(self.chkIsObsolete)
        l.addLayout(f)
        
        sg = QGroupBox("History"); sf = QFormLayout(sg)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        for w in [self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.setReadOnly(True)
        sf.addRow("Created:", self.txtCreateDateStamp); sf.addRow("By:", self.txtCreateUserStamp)
        sf.addRow("Modified:", self.txtModifDateStamp); sf.addRow("By:", self.txtModifUserStamp)
        l.addWidget(sg, 0, Qt.AlignTop); l.addStretch(1)
        return g

    def _connect_signals(self):
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnMoveBack.clicked.connect(self.on_back); self.btnMoveForward.clicked.connect(self.on_fwd)

    # --- Data ---
    def load_parameters_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes not strictly needed here but good practice ---
                sql = f"""SELECT ID, Token, TokenValue FROM GlobalValue WHERE IsObsolete = {db_manager.sql_bool(False)} OR IsObsolete IS NULL ORDER BY Token"""
                for row in conn.execute(text(sql)): self.cmbSelectionList.addItem(f"{row[1]} = {row[2]}", row[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        finally: self.cmbSelectionList.blockSignals(False); self.on_sel()
            
    def on_sel(self): self.populate_form(self.cmbSelectionList.currentData())
            
    def populate_form(self, pid):
        if not pid: self.current_param_id=None; self._clear_form_fields(); self._set_form_read_only(True); self.btnNew.setEnabled(True); return
        self.current_param_id = pid
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM GlobalValue WHERE ID = :pid"), {"pid": pid}).fetchone()
                if r:
                    self.txtID.setText(str(r.ID)); self.txtToken.setText(r.Token or "")
                    self.txtTokenValue.setText(r.TokenValue or ""); self.txtDescription.setText(r.Description or "")
                    self.chkIsObsolete.setChecked(bool(r.IsObsolete))
                    self.txtCreateDateStamp.setText(str(r.CreateDateStamp) if r.CreateDateStamp else "")
                    self.txtCreateUserStamp.setText(r.CreateUserStamp or ""); self.txtModifDateStamp.setText(str(r.ModifDateStamp) if r.ModifDateStamp else "")
                    self.txtModifUserStamp.setText(r.ModifUserStamp or "")
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_form_read_only(True)
            
    def _set_form_read_only(self, ro):
        edit = not ro
        self.cmbSelectionList.setEnabled(ro); self.btnMoveBack.setEnabled(ro); self.btnMoveForward.setEnabled(ro)
        self.btnNew.setEnabled(ro); self.btnEdit.setEnabled(ro and self.current_param_id is not None)
        self.btnSave.setEnabled(edit); self.btnCancel.setEnabled(edit)
        self.txtToken.setReadOnly(not self.is_new_record if edit else True) # Token only editable on new
        self.txtTokenValue.setReadOnly(ro); self.txtDescription.setReadOnly(ro); self.chkIsObsolete.setEnabled(edit)

    def _clear_form_fields(self):
        for w in [self.txtID, self.txtToken, self.txtTokenValue, self.txtDescription, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.chkIsObsolete.setChecked(False)

    def on_new(self): self.is_new_record=True; self._clear_form_fields(); self._set_form_read_only(False); self.txtToken.setFocus()
    def on_edit(self): self.is_new_record=False; self._set_form_read_only(False)
    def on_cancel(self): self.is_new_record=False; self.populate_form(self.current_param_id); self._set_form_read_only(True)
            
    def on_save(self):
        if not self.txtToken.text() or not self.txtTokenValue.text(): return
        try:
            with db_manager.get_connection() as conn:
                now = datetime.now(); user = getpass.getuser()
                p = {"tok": self.txtToken.text(), "val": self.txtTokenValue.text(), "desc": self.txtDescription.toPlainText(), "obs": self.chkIsObsolete.isChecked(), "now": now, "user": user}
                
                if self.is_new_record:
                    res = conn.execute(text("SELECT MAX(ID) FROM GlobalValue")).fetchone()
                    pid = (res[0] or 0) + 1; p["pid"] = pid; self.current_param_id = pid
                    conn.execute(text("INSERT INTO GlobalValue (ID, Token, TokenValue, Description, IsObsolete, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:pid, :tok, :val, :desc, :obs, :now, :user, :now, :user)"), p)
                else:
                    p["pid"] = self.current_param_id
                    conn.execute(text("UPDATE GlobalValue SET TokenValue=:val, Description=:desc, IsObsolete=:obs, ModifDateStamp=:now, ModifUserStamp=:user WHERE ID=:pid"), p)
                conn.commit()
            QMessageBox.information(self, "Success", "Saved."); self.load_parameters_list()
            idx = self.cmbSelectionList.findData(self.current_param_id)
            self.cmbSelectionList.setCurrentIndex(idx if idx > -1 else 0)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_back(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i-1)
    def on_fwd(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count()-1: self.cmbSelectionList.setCurrentIndex(i+1)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = GlobalParametersWidget(); w.show()
    sys.exit(app.exec_())