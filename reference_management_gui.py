"""
reference_management_gui.py — Reference material management panel for IsoWorks.
Provides ReferenceManagementWidget with CRUD operations on the ReferenceControl
table and embeds ReferenceDataSubform for editing associated isotopic values.
"""
import sys
import logging
import getpass
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QSplitter, QToolButton, QFrame,
    QDialog, QDialogButtonBox, QSpinBox
)
from PyQt5.QtCore import Qt

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

try: from reference_data_subform_gui import ReferenceDataSubform
except ImportError: ReferenceDataSubform = None

class ReferenceManagementWidget(QWidget):
    """
    CRUD for ReferenceControl. Refactored for db_core.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_reference_id = None
        self.is_new_record = False
        self.current_prefix = "W"
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons())
        self.main_layout.addWidget(self._create_filter_group())
        self.main_layout.addWidget(self._create_selection_group())
        self.main_layout.addSpacing(20)
        
        s = QSplitter(Qt.Vertical); s.addWidget(self._create_details_group())
        s.addWidget(self._create_subform_group()); s.setSizes([450, 300])
        self.main_layout.addWidget(s, 1)
        
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_category_combo()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        self._set_edit_mode(False)

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = db_manager.dialect
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _sql_bool(self, value): return "1" if value else "0"

    # --- UI ---
    def _create_action_buttons(self):
        l = QHBoxLayout(); self.btnNew = QPushButton("New"); self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save"); self.btnCancel = QPushButton("Cancel"); self.btnDelete = QPushButton("Delete")
        l.addStretch(1)
        for b in [self.btnNew, self.btnEdit, self.btnSave, self.btnCancel, self.btnDelete]: l.addWidget(b)
        l.addWidget(make_help_button(self, "reference_mgmt"))
        return l
        
    def _create_filter_group(self):
        g = QGroupBox("Filter"); f = QFormLayout(g)
        self.cmbCategoryID = QComboBox(); self.cmbMedia = QComboBox(); self.media_label = QLabel("Media:")
        f.addRow("Category:", self.cmbCategoryID); f.addRow(self.media_label, self.cmbMedia)
        return g
        
    def _create_selection_group(self):
        g = QGroupBox("Selection"); l = QHBoxLayout(g)
        l.addWidget(QLabel("Select:")); self.cmbSelectionList = QComboBox(); l.addWidget(self.cmbSelectionList, 1)
        self.btnMoveBack = QToolButton(); self.btnMoveBack.setText("<"); self.btnMoveForward = QToolButton(); self.btnMoveForward.setText(">")
        l.addWidget(self.btnMoveBack); l.addWidget(self.btnMoveForward)
        return g
        
    def _create_details_group(self):
        g = QGroupBox("Details")
        grid = QGridLayout(g)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.txtReferenceID = QLineEdit(); self.txtReferenceID.setReadOnly(True)
        self.cmbSampleType = QComboBox()
        self.cmbSampleID = QComboBox()
        self.btnNewSample = QPushButton("+")
        self.btnNewSample.setFixedSize(24, 24)
        self.btnNewSample.setToolTip("Quick-create a new sample for this type")
        self.btnNewSample.setEnabled(False)
        self.txtDescription = QLineEdit()
        self.dteAvailableDateTo = QLineEdit()
        self.chkIsObsolete = QCheckBox("Obsolete")
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateDateStamp.setReadOnly(True)
        self.txtCreateUserStamp = QLineEdit(); self.txtCreateUserStamp.setReadOnly(True)
        self.txtModifDateStamp  = QLineEdit(); self.txtModifDateStamp.setReadOnly(True)
        self.txtModifUserStamp  = QLineEdit(); self.txtModifUserStamp.setReadOnly(True)

        # Row 0: ID | Type
        grid.addWidget(QLabel("Ref. ID:"),     0, 0); grid.addWidget(self.txtReferenceID, 0, 1)
        grid.addWidget(QLabel("Type:"),   0, 2); grid.addWidget(self.cmbSampleType,  0, 3)
        # Row 1: Sample | Description
        _samp_w = QWidget()
        _samp_lay = QHBoxLayout(_samp_w)
        _samp_lay.setContentsMargins(0, 0, 0, 0); _samp_lay.setSpacing(4)
        _samp_lay.addWidget(self.cmbSampleID, 1); _samp_lay.addWidget(self.btnNewSample)
        grid.addWidget(QLabel("Sample:"), 1, 0); grid.addWidget(_samp_w,             1, 1)
        grid.addWidget(QLabel("Desc:"),   1, 2); grid.addWidget(self.txtDescription, 1, 3)
        # Row 2: Avail To  (IsObsolete column not present in PostgreSQL schema — checkbox hidden)
        grid.addWidget(QLabel("Avail To:"), 2, 0); grid.addWidget(self.dteAvailableDateTo, 2, 1)
        self.chkIsObsolete.setVisible(False)

        # Row 3: Created | Modified  (audit info at top so it's always visible)
        grid.addWidget(QLabel("Created:"),  3, 0); grid.addWidget(self.txtCreateDateStamp, 3, 1)
        grid.addWidget(QLabel("Modified:"), 3, 2); grid.addWidget(self.txtModifDateStamp,  3, 3)
        # Row 4: By | By
        grid.addWidget(QLabel("By:"), 4, 0); grid.addWidget(self.txtCreateUserStamp, 4, 1)
        grid.addWidget(QLabel("By:"), 4, 2); grid.addWidget(self.txtModifUserStamp,  4, 3)

        return g
        
    def _create_subform_group(self):
        g = QGroupBox("Values"); l = QVBoxLayout(g)
        if ReferenceDataSubform: self.sub_form = ReferenceDataSubform(); l.addWidget(self.sub_form)
        else: l.addWidget(QLabel("Subform missing"))
        return g

    def _connect_signals(self):
        self.cmbCategoryID.currentIndexChanged.connect(self.on_cat); self.cmbMedia.currentIndexChanged.connect(self.on_med)
        self.cmbSelectionList.currentIndexChanged.connect(self.on_sel); self.cmbSampleType.currentIndexChanged.connect(self.on_type)
        self.cmbSampleID.currentIndexChanged.connect(self.on_samp)
        self.btnNew.clicked.connect(self.on_new); self.btnEdit.clicked.connect(self.on_edit)
        self.btnSave.clicked.connect(self.on_save); self.btnCancel.clicked.connect(self.on_cancel)
        self.btnDelete.clicked.connect(self.on_delete)
        self.btnMoveBack.clicked.connect(self.on_move_back)
        self.btnMoveForward.clicked.connect(self.on_move_forward)
        self.btnNewSample.clicked.connect(self.on_quick_create_sample)

    # --- Logic ---
    def _set_edit_mode(self, edit):
        browse = not edit
        self.cmbCategoryID.setEnabled(browse); self.cmbMedia.setEnabled(browse); self.cmbSelectionList.setEnabled(browse)
        self.btnNew.setEnabled(browse)
        
        # FIX: Explicitly check is not None to ensure boolean result
        has_id = (self.current_reference_id is not None)
        self.btnEdit.setEnabled(browse and has_id)
        self.btnDelete.setEnabled(browse and has_id)
        
        self.btnSave.setEnabled(edit); self.btnCancel.setEnabled(edit)
        self.cmbSampleType.setEnabled(edit); self.cmbSampleID.setEnabled(edit); self.txtDescription.setReadOnly(browse)
        self.dteAvailableDateTo.setReadOnly(browse)
        self.btnNewSample.setEnabled(edit and self.cmbSampleType.currentData() is not None)
        if ReferenceDataSubform:
            self.sub_form.btnAdd.setEnabled(browse and has_id)

    def load_category_combo(self):
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes ---
                sql = f"""SELECT ID, {db_manager.sql_concat('ID', "': '", 'sName')} FROM Job_Procedure WHERE ID In (3, 4, 5, 11, 14) ORDER BY ID"""
                self.cmbCategoryID.blockSignals(True); self.cmbCategoryID.clear()
                for r in conn.execute(text(sql)): self.cmbCategoryID.addItem(r[1], r[0])
                idx = self.cmbCategoryID.findData(5); self.cmbCategoryID.setCurrentIndex(idx if idx>-1 else 0)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        finally: self.cmbCategoryID.blockSignals(False); self.on_cat()

    def on_cat(self):
        cid = self.cmbCategoryID.currentData()
        if not cid: return
        show_media = (cid == 5)
        self.cmbMedia.setVisible(show_media); self.media_label.setVisible(show_media)
        if show_media: self.load_media_combo()
        self.on_med()  # always call on_med to populate selection list

    def load_media_combo(self):
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes ---
                sql = f"""SELECT MediaID, {db_manager.sql_concat('Prefix', "' : '", 'medianame')}, Prefix FROM Media WHERE MediaID=1 OR MediaID=58 ORDER BY Prefix DESC"""
                self.cmbMedia.blockSignals(True); self.cmbMedia.clear()
                for r in conn.execute(text(sql)): 
                    self.cmbMedia.addItem(r[1], r[0]); self.cmbMedia.setItemData(self.cmbMedia.count()-1, r[2], Qt.UserRole)
                self.cmbMedia.setCurrentIndex(0)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        finally: self.cmbMedia.blockSignals(False)

    def on_med(self):
        cid = self.cmbCategoryID.currentData()
        if cid in [1,2,3,4,10,11]: self.current_prefix = "J"
        elif cid == 5: self.current_prefix = self.cmbMedia.itemData(self.cmbMedia.currentIndex(), Qt.UserRole) or "W"
        elif cid in [12,13,14]: self.current_prefix = "G"
        else: self.current_prefix = "W"
        self.load_sample_type_combo(self.current_prefix); self.load_selection_list(self.current_prefix)

    def load_sample_type_combo(self, pfx):
        self.cmbSampleType.blockSignals(True); self.cmbSampleType.clear()
        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text("SELECT intSampleType, strLongDescription FROM tblSampleType WHERE intSampleType<>0 AND strPrefix=:p ORDER BY intSampleType"), {"p": pfx}):
                    self.cmbSampleType.addItem(f"{r[0]}: {r[1]}", r[0])
            self.cmbSampleType.setCurrentIndex(0)
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        finally: self.cmbSampleType.blockSignals(False)

    def load_selection_list(self, pfx):
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear()
        try:
            with db_manager.get_connection() as conn:
                # --- FIXED SYNTAX: Triple quotes ---
                sql = f"""SELECT ReferenceID, {db_manager.sql_concat('Prefix', "'-'", 'SampleID', "': '", 'Description')} FROM ReferenceControl WHERE Prefix=:p ORDER BY SampleID"""
                for r in conn.execute(text(sql), {"p": pfx}): self.cmbSelectionList.addItem(r[1], r[0])
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        finally: self.cmbSelectionList.blockSignals(False); self.cmbSelectionList.setCurrentIndex(0); self.on_sel()

    def on_sel(self): self.populate_form(self.cmbSelectionList.currentData())

    def populate_form(self, rid):
        if not rid: self.current_reference_id=None; self._clear_form(); self._set_edit_mode(False); return
        self.current_reference_id = rid
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT * FROM ReferenceControl WHERE ReferenceID=:rid"), {"rid": rid}).fetchone()
                if r:
                    self.txtReferenceID.setText(str(r.ReferenceID))
                    self.txtDescription.setText(r.Description or "")
                    self.dteAvailableDateTo.setText(str(r.AvailableDateTo.date()) if r.AvailableDateTo else "")
                    self.cmbSampleType.blockSignals(True)
                    self.cmbSampleType.setCurrentIndex(self.cmbSampleType.findData(r.SampleType))
                    self.cmbSampleType.blockSignals(False)
                    self.load_sample_id_combo(pfx=r.Prefix)
                    self._select_sample(rid)
                    self.txtCreateDateStamp.setText(str(r.CreateDateStamp.date()) if r.CreateDateStamp else "")
                    self.txtCreateUserStamp.setText(r.CreateUserStamp or "")
                    self.txtModifDateStamp.setText(str(r.ModifDateStamp.date()) if r.ModifDateStamp else "")
                    self.txtModifUserStamp.setText(r.ModifUserStamp or "")
        except Exception as _e: logging.error(f"populate_form: {_e}", exc_info=True)
        try:
            if ReferenceDataSubform: self.sub_form.load_data(rid)
        except Exception as _e: logging.error(f"populate_form subform: {_e}", exc_info=True)
        self._set_edit_mode(False)

    def _clear_form(self):
        for w in [self.txtReferenceID, self.txtDescription, self.dteAvailableDateTo, self.txtCreateDateStamp, self.txtCreateUserStamp, self.txtModifDateStamp, self.txtModifUserStamp]: w.clear()
        self.chkIsObsolete.setChecked(False); self.cmbSampleType.setCurrentIndex(-1); self.cmbSampleID.clear()
        if ReferenceDataSubform: self.sub_form.load_data(None)

    def load_sample_id_combo(self, pfx=None, stype=None):
        """Populate the Sample ID combo. When both pfx and stype given, filters by both."""
        self.cmbSampleID.blockSignals(True); self.cmbSampleID.clear()
        try:
            with db_manager.get_connection() as conn:
                label_expr = db_manager.sql_concat('SampleID', "': '", 'sName')
                if pfx is not None and stype is not None:
                    sql = f"SELECT SampleID, {label_expr}, Prefix FROM Sample WHERE Prefix=:p AND SampleType=:t ORDER BY SampleID"
                    items = conn.execute(text(sql), {"p": pfx, "t": stype}).fetchall()
                elif pfx is not None:
                    sql = f"SELECT SampleID, {label_expr}, Prefix FROM Sample WHERE Prefix=:p ORDER BY SampleID"
                    items = conn.execute(text(sql), {"p": pfx}).fetchall()
                else:
                    sql = f"SELECT SampleID, {label_expr}, Prefix FROM Sample WHERE SampleType=:t ORDER BY SampleID"
                    items = conn.execute(text(sql), {"t": stype}).fetchall()
                for r in items:
                    self.cmbSampleID.addItem(r[1], r[0])
                    self.cmbSampleID.setItemData(self.cmbSampleID.count() - 1, r[2], Qt.UserRole + 1)
        except Exception as _e: logging.error(f"load_sample_id_combo: {_e}", exc_info=True)
        finally: self.cmbSampleID.blockSignals(False)

    def _select_sample(self, rid):
        """Set cmbSampleID selection by looking up SampleID from ReferenceControl."""
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(
                    text("SELECT Prefix, SampleID FROM ReferenceControl WHERE ReferenceID=:rid"),
                    {"rid": rid}
                ).fetchone()
                if row:
                    sid = row[1]   # positional — avoids named-attribute case sensitivity issues
                    for i in range(self.cmbSampleID.count()):
                        if int(self.cmbSampleID.itemData(i)) == int(sid):
                            self.cmbSampleID.setCurrentIndex(i)
                            return
        except Exception as _e: logging.error(f"_select_sample: {_e}", exc_info=True)

    def on_type(self):
        stype = self.cmbSampleType.currentData()
        if stype is not None:
            self.load_sample_id_combo(pfx=self.current_prefix, stype=stype)
            if not self.is_new_record and self.current_reference_id:
                self._select_sample(self.current_reference_id)
        in_edit = self.btnSave.isEnabled()
        self.btnNewSample.setEnabled(in_edit and stype is not None)
        self.on_samp()  # index may not have changed during blocked load — set desc explicitly
        
    def on_samp(self):
        if not self.is_new_record:
            return
        pfx = self.cmbSampleID.itemData(self.cmbSampleID.currentIndex(), Qt.UserRole + 1)
        sid = self.cmbSampleID.currentData()
        if pfx is None or sid is None:
            return
        st = self.cmbSampleType.currentText().split(': ')[-1]
        self.txtDescription.setText(f"{st}_{pfx}-{sid}")

    def on_quick_create_sample(self):
        pfx = self.current_prefix
        stype = self.cmbSampleType.currentData()
        stype_label = self.cmbSampleType.currentText()
        dlg = QuickCreateSampleDialog(pfx, stype, stype_label, self)
        if dlg.exec_() == QDialog.Accepted and dlg.created_sample_id is not None:
            self.load_sample_id_combo(pfx=pfx, stype=stype)
            for i in range(self.cmbSampleID.count()):
                if self.cmbSampleID.itemData(i) == dlg.created_sample_id:
                    self.cmbSampleID.setCurrentIndex(i)
                    break
            self.on_samp()  # force description even if index didn't change

    def on_new(self):
        self.is_new_record=True; self._clear_form()
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MAX(ReferenceID) FROM ReferenceControl")).fetchone()
                self.txtReferenceID.setText(str((res[0] or 10000)+1))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=True)
        self._set_edit_mode(True)

    def on_edit(self): self.is_new_record=False; self._set_edit_mode(True)
    def on_cancel(self): self._set_edit_mode(False); self.populate_form(self.current_reference_id)

    def on_move_back(self):
        i = self.cmbSelectionList.currentIndex()
        if i > 0: self.cmbSelectionList.setCurrentIndex(i - 1)

    def on_move_forward(self):
        i = self.cmbSelectionList.currentIndex()
        if i < self.cmbSelectionList.count() - 1: self.cmbSelectionList.setCurrentIndex(i + 1)

    def on_save(self):
        if not self.cmbSampleID.currentData(): return
        try:
            with db_manager.get_connection() as conn:
                p = {
                    "desc": self.txtDescription.text(), "pfx": self.cmbSampleID.itemData(self.cmbSampleID.currentIndex(), Qt.UserRole + 1),
                    "sid": self.cmbSampleID.currentData(), "typ": self.cmbSampleType.currentData(),
                    "av": self.dteAvailableDateTo.text() or None,
                    "now": datetime.now(), "usr": getpass.getuser()
                }
                if self.is_new_record:
                    p["rid"] = int(self.txtReferenceID.text()); self.current_reference_id = p["rid"]
                    conn.execute(text("INSERT INTO ReferenceControl (Description, Prefix, SampleID, SampleType, AvailableDateTo, ModifDateStamp, ModifUserStamp, CreateDateStamp, CreateUserStamp, ReferenceID) VALUES (:desc, :pfx, :sid, :typ, :av, :now, :usr, :now, :usr, :rid)"), p)
                else:
                    p["rid"] = self.current_reference_id
                    conn.execute(text("UPDATE ReferenceControl SET Description=:desc, Prefix=:pfx, SampleID=:sid, SampleType=:typ, AvailableDateTo=:av, ModifDateStamp=:now, ModifUserStamp=:usr WHERE ReferenceID=:rid"), p)
                conn.commit()
            QMessageBox.information(self, "Success", "Saved."); self.load_selection_list(self.current_prefix)
            self.cmbSelectionList.setCurrentIndex(self.cmbSelectionList.findData(self.current_reference_id))
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def on_delete(self):
        if QMessageBox.question(self, "Delete", "Delete reference?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM ReferenceControlData WHERE ReferenceID=:rid"), {"rid": self.current_reference_id})
                conn.execute(text("DELETE FROM ReferenceControl WHERE ReferenceID=:rid"), {"rid": self.current_reference_id})
                conn.commit()
            self.load_selection_list(self.current_prefix)
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

class QuickCreateSampleDialog(QDialog):
    """
    Quick-create a new reference/control Sample row for use in ReferenceControl.
    Ports the SampleID suggestion logic from dlgQuickCreateSample.cls.
    """

    def __init__(self, prefix: str, stype: int, stype_label: str, parent=None):
        super().__init__(parent)
        self.prefix = prefix
        self.stype = int(stype)
        self.created_sample_id = None

        self.setWindowTitle("Quick Create Sample")
        self.setModal(True)
        self.setMinimumWidth(420)

        l = QVBoxLayout(self)
        f = QFormLayout()
        f.setLabelAlignment(Qt.AlignRight)

        # Read-only context labels
        f.addRow("Prefix:", QLabel(f"<b>{prefix}</b>"))
        f.addRow("Type:", QLabel(stype_label))

        # Submission (optional)
        self.cmbSubmission = QComboBox()
        f.addRow("Submission:", self.cmbSubmission)

        # Sample ID — suggested, user-editable
        self.spnSampleID = QSpinBox()
        self.spnSampleID.setRange(1, 9_999_999)
        self.spnSampleID.setGroupSeparatorShown(False)
        f.addRow("Sample ID:", self.spnSampleID)

        # Sample Name
        self.txtName = QLineEdit()
        self.txtName.setPlaceholderText("e.g. IAEA-14")
        f.addRow("Sample Name:", self.txtName)

        # Collection Date (optional)
        self.txtDate = QLineEdit()
        self.txtDate.setPlaceholderText("YYYY-MM-DD  (optional)")
        f.addRow("Collection Date:", self.txtDate)

        l.addLayout(f)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._on_save)
        bb.rejected.connect(self.reject)
        l.addWidget(bb)

        self._load_submissions()
        self._suggest_id()

    def _load_submissions(self):
        try:
            with db_manager.get_connection() as conn:
                label_expr = db_manager.sql_concat('SubmissionID', "'--'", 'SubmissionName')
                sql = f"SELECT SubmissionID, {label_expr} FROM Submission WHERE SubmissionType=1 ORDER BY SubmissionID DESC"
                self.cmbSubmission.addItem("(none)", None)
                for r in conn.execute(text(sql)):
                    self.cmbSubmission.addItem(r[1], r[0])
        except Exception as exc:
            logging.error("QuickCreateSampleDialog._load_submissions: %s", exc)

    def _suggest_id(self):
        try:
            with db_manager.get_connection() as conn:
                self.spnSampleID.setValue(self._compute_next_id(conn))
        except Exception as exc:
            logging.error("QuickCreateSampleDialog._suggest_id: %s", exc)

    def _compute_next_id(self, conn) -> int:
        """Port of dlgQuickCreateSample cmbSampleType_AfterUpdate SampleID range logic."""
        t, p = self.stype, self.prefix
        if 1 <= t <= 10:
            default, extra = t * 100, ""
        elif 21 <= t <= 30:
            default, extra = (t % 10) * 1200, "AND SampleID > 200"
        elif 31 <= t <= 40:
            default, extra = (t % 10) * 1300, ""
        elif 41 <= t <= 60:
            default, extra = (t % 10) * 1000, ""
        else:
            default, extra = 100_000, ""
        where = f"SampleType=:t AND Prefix=:p {extra}".strip()
        row = conn.execute(text(f"SELECT MAX(SampleID) FROM Sample WHERE {where}"),
                           {"t": t, "p": p}).scalar()
        return (row or default) + 1

    def _on_save(self):
        sid  = self.spnSampleID.value()
        name = self.txtName.text().strip()
        date_str = self.txtDate.text().strip() or None
        sub_id   = self.cmbSubmission.currentData()

        if not name:
            QMessageBox.warning(self, "Validation", "Sample name is required.")
            return

        try:
            with db_manager.get_connection() as conn:
                dup = conn.execute(
                    text("SELECT COUNT(*) FROM Sample WHERE SampleID=:s AND Prefix=:p"),
                    {"s": sid, "p": self.prefix}
                ).scalar()
                if dup:
                    QMessageBox.warning(
                        self, "Duplicate ID",
                        f"{self.prefix}-{sid} already exists. Choose a different Sample ID."
                    )
                    return

                media = conn.execute(
                    text("SELECT MediaID FROM Media WHERE Prefix=:p ORDER BY MediaID"),
                    {"p": self.prefix}
                ).scalar()

                conn.execute(text("""
                    INSERT INTO Sample (SampleID, Prefix, MediaID, SubmissionID, SampleType, sName, CollectionDate)
                    VALUES (:sid, :pfx, :mid, :sub, :typ, :name, :dt)
                """), {
                    "sid": sid, "pfx": self.prefix, "mid": media,
                    "sub": sub_id, "typ": self.stype,
                    "name": name, "dt": date_str
                })
                conn.commit()

            self.created_sample_id = sid
            QMessageBox.information(
                self, "Created",
                f"Sample {self.prefix}-{sid} ({name}) created successfully."
            )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Error", str(exc))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = ReferenceManagementWidget(); w.show()
    sys.exit(app.exec_())