"""
employee_management_gui.py — Employee management panel for IsoWorks.
Provides EmployeeManagementWidget with full CRUD operations on the Employee
table, including role assignment, using SQLAlchemy via db_core.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout, QToolButton,
    QMessageBox, QFileDialog, QHeaderView, QSplitter, QTextEdit, QCompleter,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QTimer, QModelIndex, QAbstractItemModel

# --- Shared Manager ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

class EmployeeManagementWidget(QWidget):
    """
    A widget for full CRUD operations on the Employee table.
    Refactored to be an embeddable QWidget using db_core.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_employee_id = None
        self.is_new_record = False
        
        self.main_layout = QVBoxLayout(self)
        
        # --- UI Creation ---
        self.main_layout.addLayout(self._create_action_buttons())        
        self.main_layout.addWidget(self._create_selection_panel())
        self.main_layout.addSpacing(10)
        
        # Splitter for Details and Roles
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._create_detail_panel())
        self.roles_group = self._create_roles_group() 
        splitter.addWidget(self.roles_group)
        splitter.setSizes([350, 350])
        self.main_layout.addWidget(splitter, 1)
        
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_employees_list()
            self.load_all_combos()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
            
        self._set_form_read_only(True)
        self.btnSave.setEnabled(False)
        self.btnCancel.setEnabled(False)

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            elif dialect == "SQL_SERVER": parts.append(f"CAST({arg} AS NVARCHAR(255))")
            elif dialect == "POSTGRESQL": parts.append(f"CAST({arg} AS TEXT)")
            else: parts.append(str(arg))
        if dialect == "SQL_SERVER": sep = " + "
        elif dialect == "POSTGRESQL": sep = " || "
        else: sep = " || "
        return sep.join(parts)

    def _sql_bool(self, value):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        if dialect == "POSTGRESQL":
            return "true" if value else "false"
        return "1" if value else "0"

    def _sql_func(self, func_name, *args):
        func_name = func_name.upper()
        if func_name == "UPPER" and getattr(db_manager, 'dialect', '') == "ACCESS": return f"UCase({args[0]})"
        return f"{func_name}({', '.join(args)})"

    # --- UI Creation ---
    def _create_action_buttons(self):
        l = QHBoxLayout(); self.btnNew = QPushButton("New"); self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save"); self.btnCancel = QPushButton("Cancel"); self.btnDelete = QPushButton("Delete")
        l.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]: l.addWidget(b)
        l.addWidget(make_help_button(self, "employee_mgmt"))
        return l

    def _create_selection_panel(self):
        g = QGroupBox("Selection"); l = QHBoxLayout(g)
        l.addWidget(QLabel("Filter/Select Employee:"))
        self.cmbSelectionList = QComboBox()
        self.cmbSelectionList.setEditable(True)
        self.cmbSelectionList.setInsertPolicy(QComboBox.NoInsert)
        self.cmbSelectionList.completer().setCompletionMode(QCompleter.PopupCompletion)
        self.cmbSelectionList.completer().setFilterMode(Qt.MatchContains)
        l.addWidget(self.cmbSelectionList, 1)
        self.btnMoveBack = QToolButton(); self.btnMoveBack.setText("<"); self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack); l.addWidget(self.btnMoveForward)
        return g

    def _create_detail_panel(self):
        g = QGroupBox("Employee Details"); layout = QGridLayout(g)
        
        # Left Form
        f = QFormLayout()
        self.txtEmployeeID = QLineEdit(); self.txtEmployeeID.setReadOnly(True)
        self.txtLastName = QLineEdit(); self.txtFirstName = QLineEdit()
        self.txtLoginName = QLineEdit(); self.txtFunctionalTitle = QLineEdit()
        self.txtPhoneNumber = QLineEdit(); self.txtEmailAddress = QLineEdit()
        self.cmbDefaultJob = QComboBox(); self.chkIsObsolete = QCheckBox("Employee is Inactive")
        
        f.addRow("Employee ID:", self.txtEmployeeID)
        f.addRow("Last Name:", self.txtLastName)
        f.addRow("First Name:", self.txtFirstName)
        f.addRow("Login Name:", self.txtLoginName)
        f.addRow("Functional Title:", self.txtFunctionalTitle)
        f.addRow("Phone:", self.txtPhoneNumber)
        f.addRow("Email:", self.txtEmailAddress)
        f.addRow("Default Job:", self.cmbDefaultJob)
        f.addRow("", self.chkIsObsolete)
        
        # Right Form (History)
        sg = QGroupBox("Record History"); sf = QFormLayout(sg)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateUserStamp = QLineEdit()
        self.txtModifDateStamp = QLineEdit(); self.txtModifUserStamp = QLineEdit()
        # Make history read-only
        for w in [self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.setReadOnly(True)
        
        sf.addRow("Created:", self.txtCreateDateStamp); sf.addRow("By:", self.txtCreateUserStamp)
        sf.addRow("Modified:", self.txtModifDateStamp); sf.addRow("By:", self.txtModifUserStamp)
        
        layout.addLayout(f, 0, 0); layout.addWidget(sg, 0, 1, Qt.AlignTop)
        layout.setColumnStretch(0, 1); layout.setColumnStretch(1, 1)
        return g

    def _create_roles_group(self):
        g = QGroupBox("Employee Roles"); l = QVBoxLayout(g)
        self.roles_table = QTableView(); self.roles_table.setSelectionBehavior(QTableView.SelectRows)
        self.roles_table.setEditTriggers(QTableView.NoEditTriggers)
        self.roles_table_model = QStandardItemModel(); self.roles_table.setModel(self.roles_table_model)
        l.addWidget(self.roles_table)
        
        al = QHBoxLayout(); self.cmbAddRole = QComboBox(); self.btnAddRole = QPushButton("Add")
        al.addWidget(QLabel("Add Role:")); al.addWidget(self.cmbAddRole, 1); al.addWidget(self.btnAddRole)
        l.addLayout(al)
        self.btnRemoveRole = QPushButton("Remove Selected Role")
        l.addWidget(self.btnRemoveRole, 0, Qt.AlignRight)
        g.setEnabled(False) 
        return g

    def _connect_signals(self):
        self.cmbSelectionList.currentIndexChanged.connect(self.on_employee_selected)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete); self.btnAddRole.clicked.connect(self.on_add_role)
        self.btnRemoveRole.clicked.connect(self.on_remove_role)
        self.btnMoveBack.clicked.connect(self.on_move_back); self.btnMoveForward.clicked.connect(self.on_move_forward)

    # --- Data ---
    def load_employees_list(self):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT EmployeeID, {db_manager.sql_concat("LastName", "','", "FirstMiddleName")}, SystemLoginName FROM Employee WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY LastName"""
                for row in conn.execute(text(sql)):
                    self.cmbSelectionList.addItem(f"{row[1]} ({row[2] or 'No Login'})", row[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self.cmbSelectionList.blockSignals(False); self.on_employee_selected()

    def load_all_combos(self):
        try:
            logging.debug(f"load_all_combos: db_manager engine={db_manager._engine}, dialect={getattr(db_manager, 'dialect', 'N/A')}")
            with db_manager.get_connection() as conn:
                logging.debug(f"load_all_combos: connection acquired: {conn}")
                self.cmbDefaultJob.clear(); self.cmbDefaultJob.addItem("", None)
                jobs = list(conn.execute(text("SELECT ID, sName FROM Job_Procedure ORDER BY sName")))
                logging.debug(f"load_all_combos: Job_Procedure rows={len(jobs)}, sample={jobs[:3]}")
                for r in jobs:
                    self.cmbDefaultJob.addItem(r[1], r[0])
                self.cmbAddRole.clear(); self.cmbAddRole.addItem("- Select -", None)
                roles = list(conn.execute(text("SELECT RoleID, RoleName FROM Role ORDER BY RoleName")))
                logging.debug(f"load_all_combos: Role rows={len(roles)}, sample={roles[:3]}")
                for r in roles:
                    self.cmbAddRole.addItem(r[1], r[0])
        except Exception as e:
            logging.error(f"load_all_combos FAILED: {e}", exc_info=True)

    def on_employee_selected(self):
        eid = self.cmbSelectionList.currentData()
        self.populate_form(eid)

    def populate_form(self, eid):
        if eid is None: self.current_employee_id=None; self._clear_form_fields(); self._set_form_read_only(True); self.btnNew.setEnabled(True); return
        self.current_employee_id = eid
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("SELECT * FROM Employee WHERE EmployeeID = :eid"), {"eid": eid}).fetchone()
                if row:
                    self.txtEmployeeID.setText(str(row.employeeid or ""))
                    self.txtLastName.setText(row.lastname or "")
                    self.txtFirstName.setText(row.firstmiddlename or "")
                    self.txtLoginName.setText(row.systemloginname or "")
                    self.txtFunctionalTitle.setText(row.functionaltitle or "")
                    self.txtPhoneNumber.setText(row.phonenumber or "")
                    self.txtEmailAddress.setText(row.emailaddress or "")
                    self.cmbDefaultJob.setCurrentIndex(self.cmbDefaultJob.findData(row.defaultjobid))
                    self.chkIsObsolete.setChecked(bool(row.isobsolete))
                    self.txtCreateDateStamp.setText(str(row.createdatestamp) if row.createdatestamp else "")
                    self.txtCreateUserStamp.setText(row.createuserstamp or "")
                    self.txtModifDateStamp.setText(str(row.modifdatestamp) if row.modifdatestamp else "")
                    self.txtModifUserStamp.setText(row.modifuserstamp or "")
                    self.load_employee_roles(eid)
                    self.roles_group.setEnabled(True)
        except Exception as e: logging.error(f"Load failed: {e}"); self._set_form_read_only(True)

    def load_employee_roles(self, eid):
        self.roles_table_model.clear(); self.roles_table_model.setHorizontalHeaderLabels(["Role", "ID"])
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT er.ID, r.RoleName FROM Employee_Role er JOIN Role r ON er.RoleID=r.RoleID WHERE er.EmployeeID=:eid ORDER BY r.RoleName"), {"eid": eid})
                for row in res:
                    item = QStandardItem(row[1]); item.setData(row[0], Qt.UserRole)
                    self.roles_table_model.appendRow([item, QStandardItem(str(row[0]))])
            self.roles_table.setColumnHidden(1, True)
            self.roles_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def _set_form_read_only(self, ro):
        for w in [self.txtLastName, self.txtFirstName, self.txtLoginName, self.txtFunctionalTitle, self.txtPhoneNumber, self.txtEmailAddress]: w.setReadOnly(ro)
        for w in [self.chkIsObsolete, self.cmbDefaultJob, self.cmbAddRole, self.btnAddRole, self.btnRemoveRole]: w.setEnabled(not ro)
        self.cmbSelectionList.setEnabled(ro); self.btnNew.setEnabled(ro)
        
        # Explicit boolean check
        has_id = (self.current_employee_id is not None)
        self.btnEdit.setEnabled(ro and has_id)
        self.btnDelete.setEnabled(ro and has_id)
        
        self.btnSave.setEnabled(not ro); self.btnCancel.setEnabled(not ro)

    def _clear_form_fields(self):
        for w in [self.txtEmployeeID, self.txtLastName, self.txtFirstName, self.txtLoginName, self.txtFunctionalTitle, self.txtPhoneNumber, self.txtEmailAddress, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.chkIsObsolete.setChecked(False); self.cmbDefaultJob.setCurrentIndex(0)
        self.roles_table_model.clear(); self.roles_group.setEnabled(False)

    def on_new(self): self.is_new_record=True; self._clear_form_fields(); self._set_form_read_only(False); self.txtLastName.setFocus()
    def on_edit(self): self.is_new_record=False; self._set_form_read_only(False)
    def on_cancel(self): self.is_new_record=False; self.populate_form(self.current_employee_id); self._set_form_read_only(True)

    def on_save(self):
        if not self.txtLastName.text(): QMessageBox.warning(self, "Error", "Name required."); return
        try:
            with db_manager.get_connection() as conn:
                now = datetime.now(); user = getpass.getuser()
                params = {
                    "ln": self.txtLastName.text(), "fn": self.txtFirstName.text(), "log": self.txtLoginName.text(),
                    "tit": self.txtFunctionalTitle.text(), "ph": self.txtPhoneNumber.text(), "em": self.txtEmailAddress.text(),
                    "job": self.cmbDefaultJob.currentData(), "obs": self.chkIsObsolete.isChecked(),
                    "now": now, "user": user
                }
                if self.is_new_record:
                    sql = "INSERT INTO Employee (LastName, FirstMiddleName, SystemLoginName, FunctionalTitle, PhoneNumber, EmailAddress, DefaultJobID, IsObsolete, CreateDateStamp, CreateUserStamp, ModifDateStamp, ModifUserStamp) VALUES (:ln, :fn, :log, :tit, :ph, :em, :job, :obs, :now, :user, :now, :user) RETURNING EmployeeID"
                    self.current_employee_id = conn.execute(text(sql), params).scalar()
                    self.is_new_record = False
                else:
                    params["eid"] = self.current_employee_id
                    sql = "UPDATE Employee SET LastName=:ln, FirstMiddleName=:fn, SystemLoginName=:log, FunctionalTitle=:tit, PhoneNumber=:ph, EmailAddress=:em, DefaultJobID=:job, IsObsolete=:obs, ModifDateStamp=:now, ModifUserStamp=:user WHERE EmployeeID=:eid"
                    conn.execute(text(sql), params)
                conn.commit()
            show_message(self, "Success", "Saved."); self.load_employees_list()
            # Re-select current
            idx = self.cmbSelectionList.findData(self.current_employee_id) if not self.is_new_record else self.cmbSelectionList.count()-1
            self.cmbSelectionList.setCurrentIndex(idx if idx > -1 else 0)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete Employee?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.Yes:
            try:
                with db_manager.get_connection() as conn:
                    conn.execute(text("DELETE FROM Employee_Role WHERE EmployeeID=:eid"), {"eid": self.current_employee_id})
                    conn.execute(text("DELETE FROM Employee WHERE EmployeeID=:eid"), {"eid": self.current_employee_id})
                    conn.commit()
                self.current_employee_id = None; self.load_employees_list()
            except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_add_role(self):
        rid = self.cmbAddRole.currentData()
        if not rid or not self.current_employee_id: return
        try:
            with db_manager.get_connection() as conn:
                if not conn.execute(text("SELECT 1 FROM Employee_Role WHERE EmployeeID=:eid AND RoleID=:rid"), {"eid": self.current_employee_id, "rid": rid}).fetchone():
                    conn.execute(text("INSERT INTO Employee_Role (EmployeeID, RoleID) VALUES (:eid, :rid)"), {"eid": self.current_employee_id, "rid": rid})
                    conn.commit()
            self.load_employee_roles(self.current_employee_id)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_remove_role(self):
        rows = self.roles_table.selectionModel().selectedRows()
        if not rows: return
        er_id = self.roles_table_model.item(rows[0].row(), 0).data(Qt.UserRole)
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM Employee_Role WHERE ID=:id"), {"id": er_id}); conn.commit()
            self.load_employee_roles(self.current_employee_id)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_move_back(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i-1)
    def on_move_forward(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count()-1: self.cmbSelectionList.setCurrentIndex(i+1)


class EmployeeModuleWidget(QWidget):
    """Top-level widget for the Employee management launcher entry.

    Role/privilege management now lives in its own Settings entry
    (role_management_gui.py) -- this used to also embed a "Roles &&
    Privileges" tab here, but it was a stale duplicate (hardcoded to 16 of
    the 30 real privilege keys, no AMS group, no module-access grid) of
    that screen, so it was removed rather than kept in sync in two places.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(EmployeeManagementWidget())


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = EmployeeModuleWidget(); w.show()
    sys.exit(app.exec_())