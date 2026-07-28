"""
customer_management_gui.py — Customer (client) management panel for IsoWorks.
Provides CustomerManagementWidget, a QWidget offering full CRUD operations
on the Customer/Client table via SQLAlchemy and db_core.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout, QToolButton,
    QMessageBox, QSplitter, QTextEdit, QCompleter
)
from PyQt5.QtCore import Qt, QTimer

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

class CustomerManagementWidget(QWidget):
    """
    Widget for Customer (Client) management.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_customer_id = None; self.is_new_record = False
        self.main_layout = QVBoxLayout(self)
        
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_selection_panel())
        self.main_layout.addSpacing(10)
        self.main_layout.addWidget(self._create_detail_panel(), 1)
        
        self._connect_signals()
        try:
            db_manager.get_engine(); self.load_customers_list(); self.load_country_combo()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_form_read_only(True); self.btnSave.setEnabled(False); self.btnCancel.setEnabled(False)

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)
    
    def _sql_func(self, func_name, *args):
        func_name = func_name.upper()
        if func_name == "UPPER" and getattr(db_manager, 'dialect', '') == "ACCESS": return f"UCase({args[0]})"
        return f"{func_name}({', '.join(args)})"
    
    def _sql_bool(self, value): return "1" if value else "0"

    # --- UI ---
    def _create_action_buttons(self):
        l = QHBoxLayout(); self.btnNew = QPushButton("New"); self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save"); self.btnCancel = QPushButton("Cancel"); self.btnDelete = QPushButton("Delete")
        l.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]: l.addWidget(b)
        l.addWidget(make_help_button(self, "customer_mgmt"))
        return l

    def _create_selection_panel(self):
        g = QGroupBox("Selection"); l = QHBoxLayout(g)
        l.addWidget(QLabel("Filter/Select Customer:"))
        self.cmbSelectionList = QComboBox(); self.cmbSelectionList.setEditable(True)
        self.cmbSelectionList.setInsertPolicy(QComboBox.NoInsert)
        self.cmbSelectionList.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.cmbSelectionList.completer().setFilterMode(Qt.MatchContains)
        l.addWidget(self.cmbSelectionList, 1)
        self.btnBack = QToolButton(); self.btnBack.setText("<"); self.btnFwd = QToolButton(); self.btnFwd.setText(">")
        l.addWidget(self.btnBack); l.addWidget(self.btnFwd)
        return g

    def _create_detail_panel(self):
        sp = QSplitter(Qt.Vertical)
        
        g1 = QGroupBox("Contact Information"); f1 = QFormLayout(g1)
        self.txtID = QLineEdit(); self.txtID.setReadOnly(True)
        self.txtLast = QLineEdit(); self.txtFirst = QLineEdit(); self.txtMid = QLineEdit()
        self.txtPh = QLineEdit(); self.txtFax = QLineEdit(); self.txtEm = QLineEdit()
        
        f1.addRow("Customer ID:", self.txtID)
        f1.addRow("Last Name:", self.txtLast); f1.addRow("First Name:", self.txtFirst)
        f1.addRow("Middle Name:", self.txtMid); f1.addRow("Phone:", self.txtPh)
        f1.addRow("Fax:", self.txtFax); f1.addRow("Email:", self.txtEm)
        sp.addWidget(g1)
        
        g2 = QGroupBox("Address Information"); f2 = QFormLayout(g2)
        self.txtInst = QLineEdit(); self.txtMail = QLineEdit(); self.txtStr = QLineEdit()
        self.txtCity = QLineEdit(); self.txtZip = QLineEdit(); self.cmbCtry = QComboBox()
        self.chkObs = QCheckBox("Customer is Inactive")
        
        f2.addRow("Institution:", self.txtInst); f2.addRow("Mail Stop:", self.txtMail)
        f2.addRow("Street:", self.txtStr); f2.addRow("City:", self.txtCity)
        f2.addRow("Zip/Postal:", self.txtZip); f2.addRow("Country:", self.cmbCtry)
        f2.addRow("", self.chkObs)
        sp.addWidget(g2)
        
        # History
        sg = QGroupBox("Record History"); sf = QFormLayout(sg)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        for w in [self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.setReadOnly(True)
        sf.addRow("Created:", self.txtCreateDateStamp); sf.addRow("By:", self.txtCreateUserStamp)
        sf.addRow("Modified:", self.txtModifDateStamp); sf.addRow("By:", self.txtModifUserStamp)
        sp.addWidget(sg)
        
        return sp

    def _connect_signals(self):
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete); self.btnBack.clicked.connect(self.on_back)
        self.btnFwd.clicked.connect(self.on_fwd)

    def load_customers_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT CustomerID, {db_manager.sql_concat("LastName", "','", "FirstName")}, InstitutionName FROM Customer WHERE IsObsolete={db_manager.sql_bool(False)} ORDER BY LastName"""
                for r in conn.execute(text(sql)): self.cmbSelectionList.addItem(f"{r[1]} ({r[2]})", r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self.cmbSelectionList.blockSignals(False); self.on_sel()

    def load_country_combo(self):
        if self.cmbCtry.count() > 1: return
        try:
            with db_manager.get_connection() as conn:
                self.cmbCtry.addItem("-", None)
                for r in conn.execute(text("SELECT CountryCode, sName FROM Country ORDER BY sName")):
                    self.cmbCtry.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_sel(self):
        cid = self.cmbSelectionList.currentData(); self.current_customer_id = cid
        if not cid: self._clear_form_fields(); self.btnNew.setEnabled(True); return
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM Customer WHERE CustomerID=:cid"), {"cid": cid}).fetchone()
                if r:
                    self.txtID.setText(str(r.CustomerID))
                    self.txtLast.setText(r.LastName or ""); self.txtFirst.setText(r.FirstName or "")
                    self.txtMid.setText(r.MiddleName or ""); self.txtPh.setText(r.PhoneNumber or "")
                    self.txtFax.setText(r.FaxNumber or ""); self.txtEm.setText(r.Email or "")
                    self.txtInst.setText(r.InstitutionName or ""); self.txtMail.setText(r.MailStop or "")
                    self.txtStr.setText(r.StreetName or ""); self.txtCity.setText(r.CityName or "")
                    self.txtZip.setText(r.PostalCode or ""); self.chkObs.setChecked(bool(r.IsObsolete))
                    self.cmbCtry.setCurrentIndex(self.cmbCtry.findData(r.CountryCode))
                    self.txtCreateDateStamp.setText(str(r.CreateDateStamp) if r.CreateDateStamp else "")
                    self.txtCreateUserStamp.setText(r.CreateUserStamp or ""); self.txtModifDateStamp.setText(str(r.ModifDateStamp) if r.ModifDateStamp else "")
                    self.txtModifUserStamp.setText(r.ModifUserStamp or "")
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self._set_form_read_only(True)

    def _set_form_read_only(self, ro):
        for w in [self.txtLast, self.txtFirst, self.txtMid, self.txtPh, self.txtFax, self.txtEm, self.txtInst, self.txtMail, self.txtStr, self.txtCity, self.txtZip]: w.setReadOnly(ro)
        self.cmbCtry.setEnabled(not ro); self.chkObs.setEnabled(not ro)
        self.btnSave.setEnabled(not ro); self.btnCancel.setEnabled(not ro)
        
        has_id = (self.current_customer_id is not None)
        self.btnEdit.setEnabled(ro and has_id)
        self.btnDelete.setEnabled(ro and has_id)
        self.cmbSelectionList.setEnabled(ro)
        self.btnNew.setEnabled(ro)

    def _clear_form_fields(self):
        for w in [self.txtID, self.txtLast, self.txtFirst, self.txtMid, self.txtPh, self.txtFax, self.txtEm, self.txtInst, self.txtMail, self.txtStr, self.txtCity, self.txtZip, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.chkObs.setChecked(False); self.cmbCtry.setCurrentIndex(0)

    def on_new(self): self.is_new_record=True; self._clear_form_fields(); self._set_form_read_only(False); self.txtLast.setFocus()
    def on_edit(self): self.is_new_record=False; self._set_form_read_only(False)
    def on_cancel(self): self.is_new_record=False; self.on_sel(); self._set_form_read_only(True)

    def on_save(self):
        if not self.txtLast.text(): return
        try:
            with db_manager.get_connection() as conn:
                now = datetime.now(); user = getpass.getuser()
                p = {
                    "ln": self.txtLast.text(), "fn": self.txtFirst.text(), "mn": self.txtMid.text(),
                    "inst": self.txtInst.text(), "ms": self.txtMail.text(), "st": self.txtStr.text(),
                    "ct": self.txtCity.text(), "zip": self.txtZip.text(), "cc": self.cmbCtry.currentData(),
                    "ph": self.txtPh.text(), "fx": self.txtFax.text(), "em": self.txtEm.text(),
                    "obs": self.chkObs.isChecked(), "now": now, "user": user
                }
                if self.is_new_record:
                    conn.execute(text("INSERT INTO Customer (LastName, FirstName, MiddleName, InstitutionName, MailStop, StreetName, CityName, PostalCode, CountryCode, PhoneNumber, FaxNumber, Email, IsObsolete, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:ln, :fn, :mn, :inst, :ms, :st, :ct, :zip, :cc, :ph, :fx, :em, :obs, :now, :user, :now, :user)"), p)
                else:
                    p["cid"] = self.current_customer_id
                    conn.execute(text("UPDATE Customer SET LastName=:ln, FirstName=:fn, MiddleName=:mn, InstitutionName=:inst, MailStop=:ms, StreetName=:st, CityName=:ct, PostalCode=:zip, CountryCode=:cc, PhoneNumber=:ph, FaxNumber=:fx, Email=:em, IsObsolete=:obs, ModifDateStamp=:now, ModifUserStamp=:user WHERE CustomerID=:cid"), p)
                conn.commit()
            show_message(self, "Success", "Saved."); self.load_customers_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM Customer WHERE CustomerID=:cid"), {"cid": self.current_customer_id}); conn.commit()
            self.load_customers_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_back(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i-1)
    def on_fwd(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count()-1: self.cmbSelectionList.setCurrentIndex(i+1)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = CustomerManagementWidget(); w.show()
    sys.exit(app.exec_())