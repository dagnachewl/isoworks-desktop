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
    QTabWidget, QListWidget, QListWidgetItem, QFrame, QScrollArea
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont
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

class RoleManagementWidget(QWidget):
    """
    CRUD panel for the Role table and its RolePrivilege grants.

    Left pane  — role list with New / Delete buttons.
    Right pane — role name, and a Privileges section driven by checkboxes grouped
                 by category, mimicking the web-based role privilege manager.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_role_id: int | None = None
        self._is_new = False
        
        self._checkbox_groups = [
            {
                "title": "Administration",
                "perms": [
                    ("superadmin", "Super Administrator"),
                    ("adminworkflow", "Workflow Admin"),
                    ("adminsubmission", "Submission Admin"),
                    ("isresponsibleofficer", "Responsible Officer"),
                ],
            },
            {
                "title": "SIAM (Stable Isotopes)",
                "perms": [
                    ("accesssiam", "SIAM Access"),
                    ("siamadmin", "SIAM Admin"),
                ],
            },
            {
                "title": "NGAM (Noble Gas)",
                "perms": [
                    ("ngamaccess", "NGAM Access"),
                    ("ngamadmin", "NGAM Admin"),
                ],
            },
            {
                "title": "TRIMS (Tritium)",
                "perms": [
                    ("admintrims", "TRIMS Admin"),
                    ("accesscounting", "Access Counting"),
                    ("accessevaluation", "Access Evaluation"),
                    ("accessdistillation", "Access Distillation"),
                    ("accessenrichment", "Access Enrichment"),
                ],
            },
            {
                "title": "Reporting",
                "perms": [
                    ("accessreporting", "Access Reporting"),
                ],
            },
        ]
        
        self._build_ui()
        self._load_role_list()
        self._set_editor_enabled(False)

    # ── UI construction ────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QHBoxLayout(self)
        outer.setSpacing(10)

        # Left: role list
        left_w = QWidget()
        left_w.setFixedWidth(220)
        left = QVBoxLayout(left_w)
        left.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Roles")
        lbl.setFont(QFont("", -1, QFont.Bold))
        left.addWidget(lbl)
        self._role_list = QListWidget()
        self._role_list.setAlternatingRowColors(True)
        left.addWidget(self._role_list, 1)
        btn_row = QHBoxLayout()
        self._btn_new_role = QPushButton("New Role")
        self._btn_del_role = QPushButton("Delete")
        self._btn_del_role.setStyleSheet("color: #c0392b;")
        btn_row.addWidget(self._btn_new_role)
        btn_row.addWidget(self._btn_del_role)
        left.addLayout(btn_row)
        outer.addWidget(left_w)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep)

        # Right: editor
        right = QVBoxLayout()
        right.setSpacing(8)

        # Role name + save/cancel
        name_grp = QGroupBox("Role Details")
        name_form = QFormLayout(name_grp)
        self._txt_name = QLineEdit()
        self._txt_name.setPlaceholderText("Enter role name…")
        name_form.addRow("Role Name *:", self._txt_name)
        save_row = QHBoxLayout()
        self._btn_save = QPushButton("Save")
        self._btn_save.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;font-weight:bold;"
            "padding:5px 18px;border-radius:4px;}"
            "QPushButton:hover{background:#219a52;}"
        )
        self._btn_cancel = QPushButton("Cancel")
        save_row.addStretch()
        save_row.addWidget(self._btn_save)
        save_row.addWidget(self._btn_cancel)
        name_form.addRow("", save_row)
        right.addWidget(name_grp)

        # Privileges Group
        priv_grp = QGroupBox("Privileges")
        priv_v = QVBoxLayout(priv_grp)

        # Scroll Area for checkboxes
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll_widget = QWidget()
        scroll_layout = QGridLayout(scroll_widget)
        scroll_layout.setSpacing(15)

        self._checkboxes = {}

        # Add checkboxes inside categorized group boxes
        col = 0
        row = 0
        for group in self._checkbox_groups:
            grp_box = QGroupBox(group["title"])
            grp_layout = QVBoxLayout(grp_box)
            grp_layout.setSpacing(6)
            for key, label in group["perms"]:
                cb = QCheckBox(label)
                grp_layout.addWidget(cb)
                self._checkboxes[key] = cb
            
            scroll_layout.addWidget(grp_box, row, col)
            col += 1
            if col > 1:  # 2 columns layout
                col = 0
                row += 1

        scroll.setWidget(scroll_widget)
        priv_v.addWidget(scroll)
        right.addWidget(priv_grp, 1)

        outer.addLayout(right, 1)

        # Signals
        self._role_list.currentRowChanged.connect(self._on_role_selected)
        self._btn_new_role.clicked.connect(self._on_new_role)
        self._btn_del_role.clicked.connect(self._on_delete_role)
        self._btn_save.clicked.connect(self._on_save_role)
        self._btn_cancel.clicked.connect(self._on_cancel_role)

    # ── Data loading ───────────────────────────────────────────────────────

    def _load_role_list(self):
        self._role_list.blockSignals(True)
        self._role_list.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT RoleID, RoleName FROM Role ORDER BY RoleName"
                )).fetchall()
            for r in rows:
                item = QListWidgetItem(r[1] or f"Role {r[0]}")
                item.setData(Qt.UserRole, r[0])
                self._role_list.addItem(item)
        except Exception as exc:
            logging.error("RoleManagementWidget._load_role_list: %s", exc)
        self._role_list.blockSignals(False)

    def _load_role(self, role_id: int):
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT RoleName FROM Role WHERE RoleID = :rid"
                ), {"rid": role_id}).fetchone()
                if not row:
                    return
                self._txt_name.setText(row[0] or "")
                
                # Fetch all privilege names granted to this role
                priv_rows = conn.execute(text("""
                    SELECT privilegename FROM RolePrivilege WHERE roleid = :rid
                """), {"rid": role_id}).fetchall()
                granted = {r[0].lower() for r in priv_rows}
                
                # Update checkboxes
                for key, cb in self._checkboxes.items():
                    cb.setChecked(key.lower() in granted)
        except Exception as exc:
            logging.error("RoleManagementWidget._load_role: %s", exc)

    # ── Slot handlers ──────────────────────────────────────────────────────

    def _on_role_selected(self, row: int):
        if row < 0:
            self._current_role_id = None
            self._set_editor_enabled(False)
            self._clear_checkboxes()
            return
        item = self._role_list.item(row)
        self._current_role_id = item.data(Qt.UserRole)
        self._is_new = False
        self._load_role(self._current_role_id)
        self._set_editor_enabled(True)

    def _on_new_role(self):
        self._current_role_id = None
        self._is_new = True
        self._txt_name.clear()
        self._clear_checkboxes()
        self._role_list.clearSelection()
        self._set_editor_enabled(True)
        self._txt_name.setFocus()

    def _on_delete_role(self):
        if self._current_role_id is None:
            return
        name = self._txt_name.text().strip() or str(self._current_role_id)
        if QMessageBox.question(
            self, "Delete Role",
            f"Delete role '{name}'?\n\nThis will remove it from all employees.",
            QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM Employee_Role WHERE RoleID = :rid"),
                             {"rid": self._current_role_id})
                conn.execute(text("DELETE FROM RolePrivilege WHERE RoleID = :rid"),
                             {"rid": self._current_role_id})
                conn.execute(text("DELETE FROM Role WHERE RoleID = :rid"),
                             {"rid": self._current_role_id})
                conn.commit()
            self._current_role_id = None
            self._is_new = False
            self._load_role_list()
            self._set_editor_enabled(False)
        except Exception as exc:
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    def _on_save_role(self):
        name = self._txt_name.text().strip()
        if not name:
            show_message(self, "Validation", "Role Name is required.", QMessageBox.Warning)
            return
        now = datetime.now()
        user = getpass.getuser()
        try:
            with db_manager.get_connection() as conn:
                if self._is_new:
                    new_id = conn.execute(text(
                        "SELECT COALESCE(MAX(RoleID), 0) + 1 FROM Role"
                    )).scalar()
                    conn.execute(text("""
                        INSERT INTO Role
                            (RoleID, RoleName,
                             CreateDateStamp, CreateUserStamp,
                             ModifDateStamp,  ModifUserStamp)
                        VALUES (:rid, :name, :now, :user, :now, :user)
                    """), {"rid": new_id, "name": name, "now": now, "user": user})
                    self._current_role_id = new_id
                    self._is_new = False
                else:
                    conn.execute(text("""
                        UPDATE Role
                        SET RoleName=:name, ModifDateStamp=:now, ModifUserStamp=:user
                        WHERE RoleID=:rid
                    """), {"rid": self._current_role_id, "name": name,
                           "now": now, "user": user})
                
                # Update privileges
                conn.execute(text("DELETE FROM RolePrivilege WHERE roleid = :rid"), {"rid": self._current_role_id})
                for key, cb in self._checkboxes.items():
                    if cb.isChecked():
                        conn.execute(text("""
                            INSERT INTO RolePrivilege (roleid, privilegename)
                            VALUES (:rid, :priv)
                        """), {"rid": self._current_role_id, "priv": key})
                
                conn.commit()
            show_message(self, "Saved", "Role saved successfully.")
            self._load_role_list()
            for i in range(self._role_list.count()):
                if self._role_list.item(i).data(Qt.UserRole) == self._current_role_id:
                    self._role_list.blockSignals(True)
                    self._role_list.setCurrentRow(i)
                    self._role_list.blockSignals(False)
                    break
        except Exception as exc:
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    def _on_cancel_role(self):
        if self._is_new:
            self._is_new = False
            self._set_editor_enabled(False)
        elif self._current_role_id is not None:
            self._load_role(self._current_role_id)

    def _clear_checkboxes(self):
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    def _set_editor_enabled(self, enabled: bool):
        self._txt_name.setEnabled(enabled)
        self._btn_save.setEnabled(enabled)
        self._btn_cancel.setEnabled(enabled)
        self._btn_del_role.setEnabled(enabled and not self._is_new and self._current_role_id is not None)
        for cb in self._checkboxes.values():
            cb.setEnabled(enabled)


class EmployeeModuleWidget(QWidget):
    """Top-level widget for the Employee management launcher entry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        tabs.addTab(EmployeeManagementWidget(), "Employees")
        tabs.addTab(RoleManagementWidget(), "Roles && Privileges")
        layout.addWidget(tabs)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = EmployeeModuleWidget(); w.show()
    sys.exit(app.exec_())