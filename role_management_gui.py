"""
role_management_gui.py — Role definition management panel for IsoWorks.
Provides RoleManagementWidget: create/edit/delete Role records, the set of
privileges each role grants (public.roleprivilege), and which top-level
modules a role can see (public.role_module_permission).

Mirrors web's Settings > Role Management (frontend/src/components/settings/
RoleManagement.tsx), except the privilege checklist is read live from
public.privilege (30 real keys, grouped by its own modulekey/label columns)
instead of a hardcoded 16-key list — web's own list is stale relative to
the DB, so this closes that gap rather than reproducing it.
"""
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLineEdit, QPushButton, QCheckBox, QLabel,
    QFormLayout, QMessageBox, QComboBox, QScrollArea,
)
from PyQt5.QtCore import Qt

from db_core import db_manager
from sqlalchemy import text
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name
from help_browser import make_help_button
from settings_style import BTN_SS, BTN_ADD_SS, BTN_DEL_SS, HDR_SS

log = logging.getLogger(__name__)


class RoleManagementWidget(QWidget):
    """CRUD for public.role + public.roleprivilege + public.role_module_permission."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_role_id = None
        self.is_new_record = False
        self.has_write = False
        self._priv_checks: dict[str, QCheckBox] = {}   # privilegekey -> checkbox
        self._module_checks: dict[int, QCheckBox] = {}  # moduleid -> checkbox

        self._check_privileges()

        main = QVBoxLayout(self)
        main.addLayout(self._create_action_bar())
        main.addWidget(self._create_selection_panel())
        main.addWidget(self._create_detail_panel(), 1)

        self._connect_signals()
        self._set_form_read_only(True)

        try:
            db_manager.get_engine()
            self._load_role_list()
            self._load_module_checklist()
        except Exception as exc:
            log.error("RoleManagementWidget startup: %s", exc)

    # ── privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write = check_employee_privilege(user, "superadmin")
        except Exception:
            self.has_write = False

    # ── UI ────────────────────────────────────────────────────────────────────

    def _create_action_bar(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        self.btnNew = QPushButton("New"); self.btnNew.setStyleSheet(BTN_ADD_SS)
        self.btnEdit = QPushButton("Edit"); self.btnEdit.setStyleSheet(BTN_SS)
        self.btnSave = QPushButton("Save"); self.btnSave.setStyleSheet(BTN_ADD_SS)
        self.btnCancel = QPushButton("Cancel"); self.btnCancel.setStyleSheet(BTN_SS)
        self.btnDelete = QPushButton("Delete"); self.btnDelete.setStyleSheet(BTN_DEL_SS)

        lay.addStretch(1)
        for b in (self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete):
            lay.addWidget(b)
        lay.addWidget(make_help_button(self, "role_mgmt"))
        return lay

    def _create_selection_panel(self) -> QGroupBox:
        g = QGroupBox("Selection")
        lay = QHBoxLayout(g)
        lay.addWidget(QLabel("Role:"))
        self.cmbRole = QComboBox()
        lay.addWidget(self.cmbRole, 1)
        return g

    def _create_detail_panel(self) -> QWidget:
        wrap = QWidget()
        wrap_lay = QVBoxLayout(wrap)
        wrap_lay.setContentsMargins(0, 0, 0, 0)

        form_grp = QGroupBox("Role")
        form = QFormLayout(form_grp)
        self.txtRoleName = QLineEdit()
        form.addRow("Role Name:", self.txtRoleName)
        wrap_lay.addWidget(form_grp)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_body = QWidget()
        scroll_lay = QVBoxLayout(scroll_body)

        priv_grp = QGroupBox("Privileges")
        self._priv_grid = QGridLayout(priv_grp)
        scroll_lay.addWidget(priv_grp)

        mod_grp = QGroupBox("Module Access")
        self._mod_grid = QGridLayout(mod_grp)
        scroll_lay.addWidget(mod_grp)

        scroll_lay.addStretch(1)
        scroll.setWidget(scroll_body)
        wrap_lay.addWidget(scroll, 1)

        self._load_privilege_checklist()
        return wrap

    def _connect_signals(self):
        self.cmbRole.currentIndexChanged.connect(self._on_role_selected)
        self.btnNew.clicked.connect(self.on_new)
        self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save)
        self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)

    # ── loading ───────────────────────────────────────────────────────────────

    def _load_role_list(self):
        self.cmbRole.blockSignals(True)
        self.cmbRole.clear()
        self.cmbRole.addItem("— Select —", None)
        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text(
                    "SELECT roleid, rolename FROM public.role ORDER BY rolename"
                )):
                    self.cmbRole.addItem(f"{r.rolename}", r.roleid)
        except Exception as exc:
            log.error("Load role list: %s", exc)
        self.cmbRole.blockSignals(False)

    def _load_privilege_checklist(self):
        """Group public.privilege rows by their own modulekey/label columns
        -- read live from the DB rather than a hardcoded list, so newly
        added privileges show up here automatically."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT privilegekey, modulekey, isadmin, label "
                    "FROM public.privilege ORDER BY modulekey, isadmin, label"
                )).fetchall()
        except Exception as exc:
            log.error("Load privilege list: %s", exc)
            rows = []

        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r.modulekey, []).append(r)

        row_idx = 0
        for modulekey in sorted(groups.keys()):
            hdr = QLabel(f"<b>{modulekey.upper()}</b>")
            self._priv_grid.addWidget(hdr, row_idx, 0, 1, 3)
            row_idx += 1
            col = 0
            for r in groups[modulekey]:
                chk = QCheckBox(r.label)
                self._priv_checks[r.privilegekey] = chk
                self._priv_grid.addWidget(chk, row_idx, col)
                col += 1
                if col >= 3:
                    col = 0
                    row_idx += 1
            if col != 0:
                row_idx += 1

    def _load_module_checklist(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT id, modulename FROM public.module ORDER BY id"
                )).fetchall()
        except Exception as exc:
            log.error("Load module list: %s", exc)
            rows = []

        for i, r in enumerate(rows):
            chk = QCheckBox(str(r.modulename).strip())
            self._module_checks[r.id] = chk
            self._mod_grid.addWidget(chk, i // 3, i % 3)

    def populate_form(self, role_id):
        for chk in self._priv_checks.values():
            chk.setChecked(False)
        for chk in self._module_checks.values():
            chk.setChecked(False)
        self.txtRoleName.clear()
        if role_id is None:
            return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text(
                    "SELECT rolename FROM public.role WHERE roleid = :rid"
                ), {"rid": role_id}).fetchone()
                if row:
                    self.txtRoleName.setText(row.rolename or "")

                for r in conn.execute(text(
                    "SELECT privilegename FROM public.roleprivilege WHERE roleid = :rid"
                ), {"rid": role_id}):
                    chk = self._priv_checks.get(r.privilegename)
                    if chk:
                        chk.setChecked(True)

                for r in conn.execute(text(
                    "SELECT moduleid FROM public.role_module_permission WHERE roleid = :rid"
                ), {"rid": role_id}):
                    chk = self._module_checks.get(r.moduleid)
                    if chk:
                        chk.setChecked(True)
        except Exception as exc:
            log.error("populate_form(%s): %s", role_id, exc)

    # ── interaction ───────────────────────────────────────────────────────────

    def _on_role_selected(self):
        self.is_new_record = False
        self.current_role_id = self.cmbRole.currentData()
        self.populate_form(self.current_role_id)
        self._set_form_read_only(True)

    def _set_form_read_only(self, read_only: bool):
        self.txtRoleName.setReadOnly(read_only)
        for chk in self._priv_checks.values():
            chk.setEnabled(not read_only)
        for chk in self._module_checks.values():
            chk.setEnabled(not read_only)
        self.cmbRole.setEnabled(read_only)
        self.btnNew.setEnabled(read_only and self.has_write)
        self.btnEdit.setEnabled(read_only and self.has_write and self.current_role_id is not None)
        self.btnDelete.setEnabled(read_only and self.has_write and self.current_role_id is not None)
        self.btnSave.setEnabled(not read_only)
        self.btnCancel.setEnabled(not read_only)

    # ── actions ───────────────────────────────────────────────────────────────

    def on_new(self):
        if not self.has_write:
            return
        self.is_new_record = True
        self.current_role_id = None
        self.cmbRole.setCurrentIndex(0)
        self.populate_form(None)
        self._set_form_read_only(False)

    def on_edit(self):
        if not self.has_write or self.current_role_id is None:
            return
        self.is_new_record = False
        self._set_form_read_only(False)

    def on_cancel(self):
        self._set_form_read_only(True)
        if self.is_new_record:
            self.current_role_id = None
            self.cmbRole.setCurrentIndex(0)
        self.populate_form(self.current_role_id)
        self.is_new_record = False

    def on_save(self):
        name = self.txtRoleName.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation", "Role name is required.")
            return
        user = getpass.getuser()
        now = datetime.now()
        checked_privs = [k for k, chk in self._priv_checks.items() if chk.isChecked()]
        checked_mods = [mid for mid, chk in self._module_checks.items() if chk.isChecked()]

        try:
            with db_manager.get_connection() as conn:
                if self.is_new_record:
                    role_id = conn.execute(text("""
                        INSERT INTO public.role
                            (rolename, createdatestamp, createuserstamp, modifdatestamp, modifuserstamp)
                        VALUES (:name, :now, :user, :now, :user)
                        RETURNING roleid
                    """), {"name": name, "now": now, "user": user}).scalar()
                    self.current_role_id = role_id
                else:
                    role_id = self.current_role_id
                    conn.execute(text("""
                        UPDATE public.role
                        SET rolename = :name, modifdatestamp = :now, modifuserstamp = :user
                        WHERE roleid = :rid
                    """), {"name": name, "now": now, "user": user, "rid": role_id})

                conn.execute(text(
                    "DELETE FROM public.roleprivilege WHERE roleid = :rid"
                ), {"rid": role_id})
                for priv in checked_privs:
                    conn.execute(text("""
                        INSERT INTO public.roleprivilege (roleid, privilegename)
                        VALUES (:rid, :priv)
                    """), {"rid": role_id, "priv": priv})

                conn.execute(text(
                    "DELETE FROM public.role_module_permission WHERE roleid = :rid"
                ), {"rid": role_id})
                for mid in checked_mods:
                    conn.execute(text("""
                        INSERT INTO public.role_module_permission (roleid, moduleid)
                        VALUES (:rid, :mid)
                    """), {"rid": role_id, "mid": mid})

                conn.commit()

            QMessageBox.information(self, "Saved", f"Role '{name}' saved.")
            self.is_new_record = False
            self._set_form_read_only(True)
            self._load_role_list()
            idx = self.cmbRole.findData(self.current_role_id)
            if idx >= 0:
                self.cmbRole.setCurrentIndex(idx)

        except Exception as exc:
            log.error("Save role failed: %s", exc)
            QMessageBox.critical(self, "Error", str(exc))

    def on_delete(self):
        if not self.has_write or self.current_role_id is None:
            return
        if QMessageBox.question(
            self, "Confirm Delete",
            f"Delete role '{self.txtRoleName.text()}'? This will remove it from "
            "all employees who currently hold it. This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                # roleprivilege/role_module_permission cascade on role delete,
                # but employee_role's FK does not (confirmed via pg_constraint,
                # confdeltype='a' -- NO ACTION) -- clear it explicitly first or
                # the DELETE below fails whenever any employee holds this role.
                conn.execute(text(
                    "DELETE FROM public.employee_role WHERE roleid = :rid"
                ), {"rid": self.current_role_id})
                conn.execute(text(
                    "DELETE FROM public.role WHERE roleid = :rid"
                ), {"rid": self.current_role_id})
                conn.commit()
            self.current_role_id = None
            self._load_role_list()
            self.populate_form(None)
        except Exception as exc:
            log.error("Delete role failed: %s", exc)
            QMessageBox.critical(self, "Error", str(exc))
