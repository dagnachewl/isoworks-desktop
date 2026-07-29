"""
ams_physical_prep_run_details_gui.py — Physical Prep batch detail view.
Provides PhysicalPrepRunDetailsWindow (QDialog) for entering and editing
per-sample physical preparation data (prep method, pre/post mass, QA acceptance).
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QLineEdit, QTextEdit, QComboBox, QDateEdit,
    QPushButton, QTableView, QHeaderView, QStyledItemDelegate,
    QAbstractItemView, QMessageBox, QCheckBox, QFrame,
    QDialogButtonBox, QApplication,
)
from PyQt5.QtCore import Qt, QEvent, QDate
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QDoubleValidator,
    QIntValidator,
)

from db_core import db_manager
from sqlalchemy import text
from shared_utils import (
    check_employee_privilege, set_status, normalize_login_name,
    get_current_user_id,
)
from gui_utils import show_message

log = logging.getLogger(__name__)

_STATUS_LABELS = {0: "Pending", 1: "In Progress", 2: "Complete", 3: "Failed"}
_BATCH_STATUS  = {0: "Open", 1: "Complete", 2: "Approved", 3: "Locked"}
_PREP_METHODS  = ["none", "cleaned", "sieved", "crushed", "rotary_tool", "ultrasonic"]

_HEADER_STYLE = """
QGroupBox { background:#FAFAFC; border:1px solid #D9D9E3; border-radius:8px;
            margin-top:8px; font-weight:600; padding-top:10px; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 6px; color:#2D2D33; }
QLineEdit, QComboBox, QDateEdit, QTextEdit {
    background:#fff; border:1px solid #D0D0DD; border-radius:5px; padding:3px 5px; }
QLineEdit[readOnly="true"] { background:#F2F3F7; }
QDateEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
QDateEdit::drop-down:hover { background: #7986CB; }
QDateEdit::drop-down:pressed { background: #3949AB; }
QDateEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
"""

_TABLE_STYLE = """
QTableView { border:1px solid #d0d0d0; background:white;
             gridline-color:#cccccc; selection-background-color:#DDEEFF; }
QTableView::item { padding:4px; }
QHeaderView::section { background:#f0f0f0; font-weight:bold;
                        border:1px solid #d0d0d0; padding:4px; }
"""


# ── Column index constants ────────────────────────────────────────────────────

class C:
    POS      = 0   # batchposition
    AID      = 1   # analysisid  (read-only)
    SAMPLE   = 2   # prefix-sampleid name (read-only)
    METHOD   = 3   # prep_method
    PREMASS  = 4   # pre_mass_mg
    POSTMASS = 5   # post_mass_mg
    ACCEPT   = 6   # isaccepted (checkbox)
    STATUS   = 7   # status (read-only)
    REJECT   = 8   # rejectreason
    NOTES    = 9   # notes

    HEADERS = [
        "Pos", "AnalysisID", "Sample",
        "Prep Method", "Pre Mass\n(mg)", "Post Mass\n(mg)",
        "Accepted?", "Status", "Reject Reason", "Notes",
    ]

    READ_ONLY = {AID, SAMPLE, STATUS}
    NUMERIC   = {PREMASS, POSTMASS}


# ── Delegates ─────────────────────────────────────────────────────────────────

class _NumDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, decimals: int = 3, active_cols: set = None):
        super().__init__(parent)
        self.decimals = decimals
        self.active_cols: set = active_cols or set()

    def createEditor(self, parent, option, index):
        if index.column() not in self.active_cols:
            return None
        ed = QLineEdit(parent)
        ed.setValidator(QDoubleValidator(-1e9, 1e9, self.decimals))
        ed.installEventFilter(self)
        return ed

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.EditRole) or "")
        editor.selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text(), Qt.EditRole)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            view = editor.parent().parent()
            if isinstance(view, QTableView):
                r = view.currentIndex().row()
                if r + 1 < view.model().rowCount():
                    self.commitData.emit(editor)
                    self.closeEditor.emit(editor, QStyledItemDelegate.NoHint)
                    view.setCurrentIndex(view.model().index(r + 1, editor.parent().parent().currentIndex().column()))
                    return True
        return super().eventFilter(editor, event)


class _MethodDelegate(QStyledItemDelegate):
    """Inline prep-method combo (none / cleaned / sieved / crushed / rotary_tool / ultrasonic)."""

    def __init__(self, parent=None, active: bool = True):
        super().__init__(parent)
        self.active = active

    def createEditor(self, parent, option, index):
        if not self.active:
            return None
        cmb = QComboBox(parent)
        cmb.addItems(_PREP_METHODS)
        return cmb

    def setEditorData(self, editor, index):
        val = index.data(Qt.EditRole) or "none"
        idx = editor.findText(val)
        editor.setCurrentIndex(idx if idx >= 0 else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.EditRole)


# ── Add-samples dialog ────────────────────────────────────────────────────────

class _AddSamplesDialog(QDialog):
    """Search analyses and pick which ones to add to the batch."""

    def __init__(self, existing_aids: set[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Samples to Batch")
        self.setModal(True)
        self.resize(700, 480)
        self.existing_aids = existing_aids
        self.selected_aids: list[int] = []

        lay = QVBoxLayout(self)

        # search bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Search (ID / Sample):"))
        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("Analysis ID, prefix-SampleID or sample name…")
        self.txtSearch.returnPressed.connect(self._search)
        bar.addWidget(self.txtSearch, 1)
        btnSearch = QPushButton("Search")
        btnSearch.clicked.connect(self._search)
        bar.addWidget(btnSearch)
        lay.addLayout(bar)

        # results table
        self._model = QStandardItemModel()
        self._model.setHorizontalHeaderLabels(
            ["AnalysisID", "Sample ID", "Sample Name", "Workflow", "Status"]
        )
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self._table, 1)

        # buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._collect_selection)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._search()  # load all on open

    def _search(self):
        term = self.txtSearch.text().strip()
        params: dict = {}
        clauses: list[str] = []

        if self.existing_aids:
            clauses.append("a.analysisid NOT IN :excl")
            params["excl"] = tuple(self.existing_aids)

        if term:
            try:
                aid = int(term)
                clauses.append("a.analysisid = :aid")
                params["aid"] = aid
            except ValueError:
                clauses.append(
                    "(CAST(a.prefix AS TEXT) || '-' || CAST(a.sampleid AS TEXT) ILIKE :t "
                    " OR s.sname ILIKE :t)"
                )
                params["t"] = f"%{term}%"

        where = "WHERE " + " AND ".join(clauses) if clauses else ""

        sql = f"""
            SELECT a.analysisid,
                   a.prefix || '-' || a.sampleid  AS sampleref,
                   s.sname,
                   a.workflowid,
                   a.status
            FROM   public.analysis a
            JOIN   public.sample   s ON s.sampleid = a.sampleid AND s.prefix = a.prefix
            {where}
            ORDER  BY a.analysisid DESC
            LIMIT  200
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as exc:
            log.error("AddSamplesDialog search: %s", exc)
            return

        self._model.removeRows(0, self._model.rowCount())
        for r in rows:
            self._model.appendRow([
                QStandardItem(str(r[0])),
                QStandardItem(str(r[1])),
                QStandardItem(r[2] or ""),
                QStandardItem(str(r[3]) if r[3] is not None else ""),
                QStandardItem(str(r[4]) if r[4] is not None else ""),
            ])
        self._table.resizeColumnsToContents()

    def _collect_selection(self):
        selected_rows = {idx.row() for idx in self._table.selectionModel().selectedRows()}
        if not selected_rows:
            show_message(self, "No Selection", "Select at least one sample.")
            return
        self.selected_aids = [
            int(self._model.item(r, 0).text()) for r in selected_rows
        ]
        self.accept()


# ── Main details window ───────────────────────────────────────────────────────

class PhysicalPrepRunDetailsWindow(QDialog):
    """
    Master-detail dialog for one physical prep batch.
    Header = batch metadata;  Table = per-sample prep data.
    """

    def __init__(self, batch_id: int, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.batch_id    = batch_id
        self.is_editing  = False
        self.has_write   = False
        self._is_locked  = False
        self._updating   = False
        self._psid_map: dict[int, int] = {}   # row → physicalprepsampleid PK

        self.setWindowTitle(f"Physical Prep Batch :: {batch_id}")
        self.resize(1500, 800)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._num_delegate    = _NumDelegate(None, decimals=3, active_cols=C.NUMERIC)
        self._method_delegate = _MethodDelegate()

        self._build_ui()

        try:
            self._check_privileges()
            self._load_lookups()
            self._load_header()
            self._load_detail()
            self._apply_read_only()
        except Exception as exc:
            log.error("PhysicalPrepRunDetailsWindow init: %s", exc, exc_info=True)
            set_status(self._status_lbl, f"Error: {exc}", "error")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

        # top action bar
        bar = QHBoxLayout()
        self.btnEdit     = QPushButton("Edit")
        self.btnEdit.setCheckable(True)
        self.btnAdd      = QPushButton("Add Samples…")
        self.btnComplete = QPushButton("Mark Batch Complete")
        self.btnClose    = QPushButton("Close")

        for btn, color in [
            (self.btnAdd,      "#2980b9"),
            (self.btnComplete, "#27ae60"),
        ]:
            btn.setStyleSheet(
                f"QPushButton {{ background:{color}; color:white; font-weight:bold; "
                f"border:none; padding:5px 12px; border-radius:4px; }}"
                f"QPushButton:hover {{ opacity:0.9; }}"
                f"QPushButton:disabled {{ background:#aaa; color:#666; }}"
            )

        bar.addStretch()
        for w in [self.btnEdit, self.btnAdd, self.btnComplete, self.btnClose]:
            bar.addWidget(w)
        root.addLayout(bar)

        # header form
        self._build_header_form()
        root.addWidget(self._hdr_grp)

        # separator
        line = QFrame(); line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#d0d0d0;")
        root.addWidget(line)

        # detail table
        self._model = QStandardItemModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setShowGrid(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setStyleSheet(_TABLE_STYLE)
        self._table.setItemDelegateForColumn(C.METHOD, self._method_delegate)
        for col in C.NUMERIC:
            self._table.setItemDelegateForColumn(col, self._num_delegate)
        root.addWidget(self._table, 1)

        # status bar
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("padding:2px 4px; color:#555;")
        root.addWidget(self._status_lbl)

        # connections
        self.btnEdit.toggled.connect(self._toggle_edit)
        self.btnAdd.clicked.connect(self._add_samples)
        self.btnComplete.clicked.connect(self._mark_complete)
        self.btnClose.clicked.connect(self.accept)
        self._model.itemChanged.connect(self._on_item_changed)

    def _build_header_form(self):
        self._hdr_grp = QGroupBox("Batch Metadata")
        self._hdr_grp.setStyleSheet(_HEADER_STYLE)
        grid = QGridLayout(self._hdr_grp)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1); grid.setColumnStretch(5, 1)

        def lbl(t):
            l = QLabel(t)
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        self.txtBatchID   = QLineEdit(); self.txtBatchID.setReadOnly(True); self.txtBatchID.setFixedWidth(80)
        self.txtBatchCode = QLineEdit()
        self.dateBatch    = QDateEdit(); self.dateBatch.setCalendarPopup(True)
        self.dateBatch.setDisplayFormat("yyyy-MM-dd")
        self.cmbEquip     = QComboBox()
        self.cmbTech      = QComboBox()
        self.txtBatchStatus = QLineEdit(); self.txtBatchStatus.setReadOnly(True)
        self.txtNotes     = QTextEdit(); self.txtNotes.setMaximumHeight(55)

        grid.addWidget(lbl("Batch ID:"),     0, 0); grid.addWidget(self.txtBatchID,     0, 1)
        grid.addWidget(lbl("Batch Code:"),   1, 0); grid.addWidget(self.txtBatchCode,   1, 1)
        grid.addWidget(lbl("Date:"),         0, 2); grid.addWidget(self.dateBatch,       0, 3)
        grid.addWidget(lbl("Equipment:"),    1, 2); grid.addWidget(self.cmbEquip,        1, 3)
        grid.addWidget(lbl("Technician:"),   0, 4); grid.addWidget(self.cmbTech,         0, 5)
        grid.addWidget(lbl("Batch Status:"), 1, 4); grid.addWidget(self.txtBatchStatus, 1, 5)
        grid.addWidget(lbl("Notes:"),        2, 0, Qt.AlignTop)
        grid.addWidget(self.txtNotes,        2, 1, 1, 5)

    # ── Privileges ────────────────────────────────────────────────────────────

    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write = check_employee_privilege(user, "accessams")
        except Exception:
            self.has_write = False

    # ── Lookups ───────────────────────────────────────────────────────────────

    def _load_lookups(self):
        with db_manager.get_connection() as conn:
            self.cmbEquip.addItem("", None)
            for r in conn.execute(text(
                "SELECT EquipmentID, EquipmentName FROM Equipment ORDER BY EquipmentName"
            )):
                self.cmbEquip.addItem(r[1], r[0])

            self.cmbTech.addItem("", None)
            for r in conn.execute(text(
                "SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY LastName"
            )):
                name = f"{r[1]}, {r[2]}" if r[2] else r[1]
                self.cmbTech.addItem(name, r[0])

    # ── Header ────────────────────────────────────────────────────────────────

    def _load_header(self):
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT physicalpreprunid, batchcode, batchdate,
                       equipmentid, technicianid, runstatus, islocked, notes
                FROM ams.physicalprep_run
                WHERE physicalpreprunid = :bid
            """), {"bid": self.batch_id}).fetchone()

        if not row:
            set_status(self._status_lbl, "Batch not found.", "error")
            return

        self.txtBatchID.setText(str(row[0]))
        self.txtBatchCode.setText(row[1] or "")
        if row[2]:
            self.dateBatch.setDate(QDate(row[2].year, row[2].month, row[2].day))
        self._set_combo(self.cmbEquip, row[3])
        self._set_combo(self.cmbTech,  row[4])
        status_code = int(row[5]) if row[5] is not None else 0
        self.txtBatchStatus.setText(_BATCH_STATUS.get(status_code, str(status_code)))
        self._is_locked = bool(row[6]) if row[6] is not None else False
        self.txtNotes.setPlainText(row[7] or "")

    def _set_combo(self, cmb: QComboBox, val):
        if val is None:
            cmb.setCurrentIndex(0)
            return
        idx = cmb.findData(int(val))
        cmb.setCurrentIndex(idx if idx >= 0 else 0)

    # ── Detail table ──────────────────────────────────────────────────────────

    def _load_detail(self):
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT ps.physicalprepsampleid,
                       ps.batchposition,
                       ps.analysisid,
                       a.prefix || '-' || CAST(a.sampleid AS TEXT) AS sampleref,
                       s.sname,
                       ps.prep_method,
                       ps.pre_mass_mg,
                       ps.post_mass_mg,
                       ps.isaccepted,
                       ps.status,
                       ps.rejectreason,
                       ps.notes
                FROM ams.physicalprep_sample ps
                JOIN public.analysis a ON a.analysisid = ps.analysisid
                JOIN public.sample   s ON s.sampleid   = a.sampleid AND s.prefix = a.prefix
                WHERE ps.physicalpreprunid = :bid
                ORDER BY ps.batchposition, ps.physicalprepsampleid
            """), {"bid": self.batch_id}).fetchall()

        self._updating = True
        try:
            self._model.clear()
            self._model.setHorizontalHeaderLabels(C.HEADERS)
            self._psid_map.clear()
            ro_brush = QBrush(QColor("#f5f5f5"))

            for r in rows:
                (psid, pos, aid, sref, sname,
                 method, premass, postmass, accepted, status, reject, notes) = r

                sample_label = f"{sref}  {sname or ''}".strip()
                status_code  = int(status) if status is not None else 0

                items = [
                    QStandardItem(str(pos) if pos is not None else ""),   # C.POS
                    QStandardItem(str(aid)),                               # C.AID
                    QStandardItem(sample_label),                           # C.SAMPLE
                    QStandardItem(method or "none"),                       # C.METHOD
                    QStandardItem(self._fmt(premass)),                     # C.PREMASS
                    QStandardItem(self._fmt(postmass)),                    # C.POSTMASS
                    QStandardItem(""),                                     # C.ACCEPT (checkbox)
                    QStandardItem(_STATUS_LABELS.get(status_code, "?")),   # C.STATUS
                    QStandardItem(reject or ""),                           # C.REJECT
                    QStandardItem(notes or ""),                            # C.NOTES
                ]

                # read-only columns
                for c in C.READ_ONLY:
                    items[c].setEditable(False)
                    items[c].setBackground(ro_brush)
                    items[c].setFlags(items[c].flags() & ~Qt.ItemIsEditable)

                # accepted checkbox
                chk = items[C.ACCEPT]
                chk.setCheckable(True)
                chk.setCheckState(Qt.Checked if (accepted is True or accepted == 1) else Qt.Unchecked)
                chk.setFlags(
                    (chk.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    & ~Qt.ItemIsEditable
                )

                # store PKs
                items[C.AID].setData(aid,        Qt.UserRole)
                items[C.STATUS].setData(status_code, Qt.UserRole)

                row_idx = self._model.rowCount()
                self._model.appendRow(items)
                self._psid_map[row_idx] = psid

            self._table.resizeColumnsToContents()
            h = self._table.horizontalHeader()
            h.setSectionResizeMode(C.SAMPLE, QHeaderView.Stretch)
            h.setSectionResizeMode(C.NOTES,  QHeaderView.Stretch)
            self._table.resizeRowsToContents()
        finally:
            self._updating = False

        self._update_cell_flags()

    # ── Edit mode ─────────────────────────────────────────────────────────────

    def _toggle_edit(self, checked: bool):
        if not self.has_write:
            self.btnEdit.setChecked(False)
            show_message(self, "Access Denied",
                         "You do not have write access to AMS.", QMessageBox.Warning)
            return
        if self._is_locked:
            self.btnEdit.setChecked(False)
            show_message(self, "Batch Locked", "This batch is locked and cannot be edited.")
            return

        self.is_editing = checked
        if checked:
            self.btnEdit.setText("Stop Edit")
            self._table.setEditTriggers(QAbstractItemView.AllEditTriggers)
            set_status(self._status_lbl, "Edit Mode — modify values, then Stop Edit to save.", "processing")
        else:
            self._save_header()
            self.btnEdit.setText("Edit")
            self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            set_status(self._status_lbl, "Saved.", "success")

        self._update_cell_flags()

    def _apply_read_only(self):
        for w in [self.txtBatchCode, self.txtNotes]:
            w.setReadOnly(True)
        self.dateBatch.setReadOnly(True)
        self.cmbEquip.setEnabled(False)
        self.cmbTech.setEnabled(False)
        self.btnEdit.setEnabled(self.has_write and not self._is_locked)
        self.btnAdd.setEnabled(self.has_write and not self._is_locked)
        self.btnComplete.setEnabled(
            self.has_write and not self._is_locked
            and self.txtBatchStatus.text() == "Open"
        )

    def _apply_edit_state(self):
        for w in [self.txtBatchCode, self.txtNotes]:
            w.setReadOnly(False)
        self.dateBatch.setReadOnly(False)
        self.cmbEquip.setEnabled(True)
        self.cmbTech.setEnabled(True)

    def _update_cell_flags(self):
        self._model.blockSignals(True)
        try:
            for row in range(self._model.rowCount()):
                for col in range(self._model.columnCount()):
                    it = self._model.item(row, col)
                    if it is None:
                        continue
                    if col in C.READ_ONLY:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                    elif col == C.ACCEPT:
                        if self.is_editing:
                            it.setFlags(
                                (it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                                & ~Qt.ItemIsEditable
                            )
                        else:
                            it.setFlags(it.flags() & ~Qt.ItemIsUserCheckable & ~Qt.ItemIsEditable)
                    elif self.is_editing:
                        it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    else:
                        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        finally:
            self._model.blockSignals(False)

        self._table.viewport().update()
        if self.is_editing:
            self._apply_edit_state()
        else:
            self._apply_read_only()

    # ── Item changed → recalc + save ─────────────────────────────────────────

    def _on_item_changed(self, item):
        if self._updating or not self.is_editing:
            return
        self._updating = True
        try:
            row, col = item.row(), item.column()
            # Auto-update sample status
            self._recalc_status(row)
            # Persist
            self._save_row(row)
        finally:
            self._updating = False

    def _recalc_status(self, row: int):
        premass  = self._float(self._model.item(row, C.PREMASS))
        postmass = self._float(self._model.item(row, C.POSTMASS))

        if postmass is not None:
            code = 2   # complete
        elif premass is not None:
            code = 1   # in progress
        else:
            code = 0   # pending

        it = self._model.item(row, C.STATUS)
        it.setText(_STATUS_LABELS.get(code, "?"))
        it.setData(code, Qt.UserRole)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _save_header(self):
        user = normalize_login_name(get_current_user_id())
        d = self.dateBatch.date()
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.physicalprep_run SET
                        batchcode       = :code,
                        batchdate       = :dt,
                        equipmentid     = :eid,
                        technicianid    = :tid,
                        notes           = :notes,
                        modifdatestamp  = :now,
                        modifuserstamp  = :user
                    WHERE physicalpreprunid = :bid
                """), {
                    "code":  self.txtBatchCode.text().strip() or None,
                    "dt":    d.toPyDate(),
                    "eid":   self.cmbEquip.currentData(),
                    "tid":   self.cmbTech.currentData(),
                    "notes": self.txtNotes.toPlainText().strip() or None,
                    "now":   datetime.now(),
                    "user":  user,
                    "bid":   self.batch_id,
                })
                conn.commit()
        except Exception as exc:
            log.error("Save physicalprep_run header %d: %s", self.batch_id, exc)
            set_status(self._status_lbl, f"Header save failed: {exc}", "error")

    def _save_row(self, row: int):
        psid = self._psid_map.get(row)
        if psid is None:
            return

        def txt(col): return self._model.item(row, col).text().strip() or None
        def flt(col):
            v = txt(col)
            try:
                return float(v) if v else None
            except ValueError:
                return None
        def nt(col):
            v = txt(col)
            try:
                return int(float(v)) if v else None
            except ValueError:
                return None

        accepted = self._model.item(row, C.ACCEPT).checkState() == Qt.Checked
        status   = self._model.item(row, C.STATUS).data(Qt.UserRole)
        user     = normalize_login_name(get_current_user_id())

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.physicalprep_sample SET
                        batchposition   = :pos,
                        prep_method     = :method,
                        pre_mass_mg     = :premass,
                        post_mass_mg    = :postmass,
                        isaccepted      = :acc,
                        status          = :st,
                        rejectreason    = :rej,
                        notes           = :notes,
                        modifdatestamp  = :now,
                        modifuserstamp  = :user
                    WHERE physicalprepsampleid = :psid
                """), {
                    "pos":      nt(C.POS),
                    "method":   txt(C.METHOD) or "none",
                    "premass":  flt(C.PREMASS),
                    "postmass": flt(C.POSTMASS),
                    "acc":      accepted,
                    "st":       status if status is not None else 0,
                    "rej":      txt(C.REJECT),
                    "notes":    txt(C.NOTES),
                    "now":      datetime.now(),
                    "user":     user,
                    "psid":     psid,
                })
                conn.commit()
        except Exception as exc:
            log.error("Save physicalprep_sample row %d (psid=%d): %s", row, psid, exc)
            set_status(self._status_lbl, f"Row save failed: {exc}", "error")

    # ── Add samples ───────────────────────────────────────────────────────────

    def _add_samples(self):
        existing_aids: set[int] = set()
        for row in range(self._model.rowCount()):
            it = self._model.item(row, C.AID)
            if it:
                existing_aids.add(it.data(Qt.UserRole))

        dlg = _AddSamplesDialog(existing_aids=existing_aids, parent=self)
        if dlg.exec_() != QDialog.Accepted or not dlg.selected_aids:
            return

        user = normalize_login_name(get_current_user_id())
        now  = datetime.now()
        next_pos = self._model.rowCount() + 1

        try:
            with db_manager.get_connection() as conn:
                for i, aid in enumerate(dlg.selected_aids):
                    conn.execute(text("""
                        INSERT INTO ams.physicalprep_sample
                            (physicalpreprunid, analysisid, isbypass, batchposition,
                             status, isaccepted,
                             createdatestamp, createuserstamp)
                        VALUES
                            (:bid, :aid, FALSE, :pos,
                             0, TRUE,
                             :now, :user)
                        ON CONFLICT DO NOTHING
                    """), {
                        "bid": self.batch_id,
                        "aid": aid,
                        "pos": next_pos + i,
                        "now": now,
                        "user": user,
                    })
                conn.commit()
            self._load_detail()
            set_status(self._status_lbl,
                       f"Added {len(dlg.selected_aids)} sample(s).", "success")
        except Exception as exc:
            log.error("Add samples to batch %d: %s", self.batch_id, exc)
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    # ── Complete batch ────────────────────────────────────────────────────────

    def _mark_complete(self):
        # Check all samples are complete or failed
        incomplete = []
        for row in range(self._model.rowCount()):
            it = self._model.item(row, C.STATUS)
            code = it.data(Qt.UserRole) if it else 0
            if code not in (2, 3):
                aid_it = self._model.item(row, C.AID)
                incomplete.append(str(aid_it.data(Qt.UserRole)) if aid_it else str(row))

        if incomplete:
            show_message(
                self, "Incomplete Samples",
                f"The following analyses are not yet complete or failed:\n"
                f"{', '.join(incomplete)}\n\n"
                "Enter post-prep mass (or mark as Failed) for all samples first.",
                QMessageBox.Warning,
            )
            return

        if QMessageBox.question(
            self, "Mark Batch Complete",
            "Mark this batch as Complete?\n"
            "All accepted samples will become available for the next AMS stage.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) == QMessageBox.No:
            return

        user = normalize_login_name(get_current_user_id())
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.physicalprep_run SET
                        runstatus      = 1,
                        modifdatestamp = :now,
                        modifuserstamp = :user
                    WHERE physicalpreprunid = :bid
                """), {"now": datetime.now(), "user": user, "bid": self.batch_id})
                conn.commit()
            self.txtBatchStatus.setText("Complete")
            self._is_locked = False
            self.btnComplete.setEnabled(False)
            self.btnAdd.setEnabled(False)
            set_status(self._status_lbl, "Batch marked complete.", "success")
        except Exception as exc:
            log.error("Mark physicalprep_run %d complete: %s", self.batch_id, exc)
            show_message(self, "Error", str(exc), QMessageBox.Critical)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt(val, decimals: int = 3) -> str:
        if val is None:
            return ""
        try:
            return f"{float(val):.{decimals}f}"
        except (TypeError, ValueError):
            return str(val)

    @staticmethod
    def _float(item: Optional[QStandardItem]) -> Optional[float]:
        if item is None:
            return None
        try:
            return float(item.text())
        except (TypeError, ValueError):
            return None
