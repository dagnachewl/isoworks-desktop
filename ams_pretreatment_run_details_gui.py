"""
ams_pretreatment_run_details_gui.py — Pretreatment batch detail view.
Provides PretreatmentRunDetailsWindow (QDialog) for entering and editing
per-sample ABA pretreatment data (acid/base/acid steps, mass, QA acceptance).

Grid is scoped to the ABA-relevant fields (method-specific combustion/sparge/
fraction columns are out of scope for a first pass, matching the web's own
PretreatmentModule.tsx field list for method='ABA').
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
_METHODS = [
    "ABA", "combustion", "acid_hydrolysis", "water_dic", "water_poc_doc",
    "collagen_extraction", "bioapatite_hydrolysis", "hcl_surface_leaching",
    "organic_solvent_extraction",
]

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
    POS           = 0    # batchposition
    AID           = 1    # analysisid  (read-only)
    SAMPLE        = 2    # prefix-sampleid name (read-only)
    PREMASS       = 3    # pretreatment_mass_mg
    ACID1_REAGENT = 4    # acid1_reagent
    ACID1_CONC    = 5    # acid1_conc_pct
    ACID1_TEMP    = 6    # acid1_temp_c
    ACID1_DUR     = 7    # acid1_duration_min
    BASE_REAGENT  = 8    # base_reagent
    BASE_CONC     = 9    # base_conc_pct
    BASE_TEMP     = 10   # base_temp_c
    BASE_DUR      = 11   # base_duration_min
    BASE_CYCLES   = 12   # base_cycles
    BASE_CLEAR    = 13   # base_ran_clear (checkbox)
    ACID2_REAGENT = 14   # acid2_reagent
    ACID2_CONC    = 15   # acid2_conc_pct
    ACID2_TEMP    = 16   # acid2_temp_c
    ACID2_DUR     = 17   # acid2_duration_min
    RINSE_CYCLES  = 18   # rinse_cycles
    FINAL_PH      = 19   # final_rinse_ph
    DRY_TEMP      = 20   # drying_temp_c
    DRY_DUR       = 21   # drying_duration_h
    POSTMASS      = 22   # post_treatment_mass_mg
    ACCEPT        = 23   # isaccepted (checkbox)
    STATUS        = 24   # status (read-only)
    REJECT        = 25   # rejectreason
    NOTES         = 26   # notes

    HEADERS = [
        "Pos", "AnalysisID", "Sample",
        "Pre Mass\n(mg)",
        "Acid1\nReagent", "Acid1\nConc %", "Acid1\nTemp °C", "Acid1\nDur (min)",
        "Base\nReagent", "Base\nConc %", "Base\nTemp °C", "Base\nDur (min)",
        "Base\nCycles", "Ran\nClear?",
        "Acid2\nReagent", "Acid2\nConc %", "Acid2\nTemp °C", "Acid2\nDur (min)",
        "Rinse\nCycles", "Final\nRinse pH",
        "Dry Temp\n°C", "Dry Dur\n(h)",
        "Post Mass\n(mg)",
        "Accepted?", "Status", "Reject Reason", "Notes",
    ]

    READ_ONLY = {AID, SAMPLE, STATUS}
    NUMERIC   = {
        PREMASS, ACID1_CONC, ACID1_TEMP, ACID1_DUR,
        BASE_CONC, BASE_TEMP, BASE_DUR, BASE_CYCLES,
        ACID2_CONC, ACID2_TEMP, ACID2_DUR,
        RINSE_CYCLES, FINAL_PH, DRY_TEMP, DRY_DUR, POSTMASS,
    }
    CHECKBOX  = {BASE_CLEAR, ACCEPT}


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

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._collect_selection)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        self._search()

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

class PretreatmentRunDetailsWindow(QDialog):
    """
    Master-detail dialog for one pretreatment batch.
    Header = batch metadata (incl. Method);  Table = per-sample ABA data.
    """

    def __init__(self, batch_id: int, parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMaximizeButtonHint)
        self.batch_id    = batch_id
        self.is_editing  = False
        self.has_write   = False
        self._is_locked  = False
        self._updating   = False
        self._ptsid_map: dict[int, int] = {}   # row → pretreatmentsampleid PK

        self.setWindowTitle(f"Pretreatment Batch :: {batch_id}")
        self.resize(1700, 800)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        self._num_delegate = _NumDelegate(None, decimals=3, active_cols=C.NUMERIC)

        self._build_ui()

        try:
            self._check_privileges()
            self._load_lookups()
            self._load_header()
            self._load_detail()
            self._apply_read_only()
        except Exception as exc:
            log.error("PretreatmentRunDetailsWindow init: %s", exc, exc_info=True)
            set_status(self._status_lbl, f"Error: {exc}", "error")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)

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

        self._build_header_form()
        root.addWidget(self._hdr_grp)

        line = QFrame(); line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color:#d0d0d0;")
        root.addWidget(line)

        self._model = QStandardItemModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setShowGrid(True)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setStyleSheet(_TABLE_STYLE)
        for col in C.NUMERIC:
            self._table.setItemDelegateForColumn(col, self._num_delegate)
        root.addWidget(self._table, 1)

        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("padding:2px 4px; color:#555;")
        root.addWidget(self._status_lbl)

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
        self.cmbMethod    = QComboBox(); self.cmbMethod.addItems(_METHODS)
        self.txtBatchStatus = QLineEdit(); self.txtBatchStatus.setReadOnly(True)
        self.txtNotes     = QTextEdit(); self.txtNotes.setMaximumHeight(55)

        grid.addWidget(lbl("Batch ID:"),     0, 0); grid.addWidget(self.txtBatchID,     0, 1)
        grid.addWidget(lbl("Batch Code:"),   1, 0); grid.addWidget(self.txtBatchCode,   1, 1)
        grid.addWidget(lbl("Date:"),         0, 2); grid.addWidget(self.dateBatch,       0, 3)
        grid.addWidget(lbl("Equipment:"),    1, 2); grid.addWidget(self.cmbEquip,        1, 3)
        grid.addWidget(lbl("Technician:"),   0, 4); grid.addWidget(self.cmbTech,         0, 5)
        grid.addWidget(lbl("Method:"),       1, 4); grid.addWidget(self.cmbMethod,       1, 5)
        grid.addWidget(lbl("Batch Status:"), 2, 4); grid.addWidget(self.txtBatchStatus, 2, 5)
        grid.addWidget(lbl("Notes:"),        3, 0, Qt.AlignTop)
        grid.addWidget(self.txtNotes,        3, 1, 1, 5)

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
                SELECT pretreatmentrunid, batchcode, batchdate,
                       equipmentid, technicianid, runstatus, islocked, notes, method
                FROM ams.pretreatmentrun
                WHERE pretreatmentrunid = :bid
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
        idx = self.cmbMethod.findText(row[8] or "ABA")
        self.cmbMethod.setCurrentIndex(idx if idx >= 0 else 0)

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
                SELECT ps.pretreatmentsampleid,
                       ps.batchposition,
                       ps.analysisid,
                       a.prefix || '-' || CAST(a.sampleid AS TEXT) AS sampleref,
                       s.sname,
                       ps.pretreatment_mass_mg,
                       ps.acid1_reagent, ps.acid1_conc_pct, ps.acid1_temp_c, ps.acid1_duration_min,
                       ps.base_reagent, ps.base_conc_pct, ps.base_temp_c, ps.base_duration_min,
                       ps.base_cycles, ps.base_ran_clear,
                       ps.acid2_reagent, ps.acid2_conc_pct, ps.acid2_temp_c, ps.acid2_duration_min,
                       ps.rinse_cycles, ps.final_rinse_ph,
                       ps.drying_temp_c, ps.drying_duration_h,
                       ps.post_treatment_mass_mg,
                       ps.isaccepted, ps.status, ps.rejectreason, ps.notes
                FROM ams.pretreatmentsample ps
                JOIN public.analysis a ON a.analysisid = ps.analysisid
                JOIN public.sample   s ON s.sampleid   = a.sampleid AND s.prefix = a.prefix
                WHERE ps.pretreatmentrunid = :bid
                ORDER BY ps.batchposition, ps.pretreatmentsampleid
            """), {"bid": self.batch_id}).fetchall()

        self._updating = True
        try:
            self._model.clear()
            self._model.setHorizontalHeaderLabels(C.HEADERS)
            self._ptsid_map.clear()
            ro_brush = QBrush(QColor("#f5f5f5"))

            for r in rows:
                (ptsid, pos, aid, sref, sname,
                 premass,
                 acid1_reag, acid1_conc, acid1_temp, acid1_dur,
                 base_reag, base_conc, base_temp, base_dur, base_cycles, base_clear,
                 acid2_reag, acid2_conc, acid2_temp, acid2_dur,
                 rinse_cycles, final_ph,
                 dry_temp, dry_dur,
                 postmass,
                 accepted, status, reject, notes) = r

                sample_label = f"{sref}  {sname or ''}".strip()
                status_code  = int(status) if status is not None else 0

                items = [None] * len(C.HEADERS)
                items[C.POS]           = QStandardItem(str(pos) if pos is not None else "")
                items[C.AID]           = QStandardItem(str(aid))
                items[C.SAMPLE]        = QStandardItem(sample_label)
                items[C.PREMASS]       = QStandardItem(self._fmt(premass))
                items[C.ACID1_REAGENT] = QStandardItem(acid1_reag or "")
                items[C.ACID1_CONC]    = QStandardItem(self._fmt(acid1_conc))
                items[C.ACID1_TEMP]    = QStandardItem(self._fmt(acid1_temp))
                items[C.ACID1_DUR]     = QStandardItem(self._fmt(acid1_dur))
                items[C.BASE_REAGENT]  = QStandardItem(base_reag or "")
                items[C.BASE_CONC]     = QStandardItem(self._fmt(base_conc))
                items[C.BASE_TEMP]     = QStandardItem(self._fmt(base_temp))
                items[C.BASE_DUR]      = QStandardItem(self._fmt(base_dur))
                items[C.BASE_CYCLES]   = QStandardItem(self._fmt(base_cycles, 0))
                items[C.BASE_CLEAR]    = QStandardItem("")
                items[C.ACID2_REAGENT] = QStandardItem(acid2_reag or "")
                items[C.ACID2_CONC]    = QStandardItem(self._fmt(acid2_conc))
                items[C.ACID2_TEMP]    = QStandardItem(self._fmt(acid2_temp))
                items[C.ACID2_DUR]     = QStandardItem(self._fmt(acid2_dur))
                items[C.RINSE_CYCLES]  = QStandardItem(self._fmt(rinse_cycles, 0))
                items[C.FINAL_PH]      = QStandardItem(self._fmt(final_ph))
                items[C.DRY_TEMP]      = QStandardItem(self._fmt(dry_temp))
                items[C.DRY_DUR]       = QStandardItem(self._fmt(dry_dur))
                items[C.POSTMASS]      = QStandardItem(self._fmt(postmass))
                items[C.ACCEPT]        = QStandardItem("")
                items[C.STATUS]        = QStandardItem(_STATUS_LABELS.get(status_code, "?"))
                items[C.REJECT]        = QStandardItem(reject or "")
                items[C.NOTES]         = QStandardItem(notes or "")

                for c in C.READ_ONLY:
                    items[c].setEditable(False)
                    items[c].setBackground(ro_brush)
                    items[c].setFlags(items[c].flags() & ~Qt.ItemIsEditable)

                base_chk = items[C.BASE_CLEAR]
                base_chk.setCheckable(True)
                base_chk.setCheckState(Qt.Checked if (base_clear is True or base_clear == 1) else Qt.Unchecked)
                base_chk.setFlags(
                    (base_chk.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    & ~Qt.ItemIsEditable
                )

                acc_chk = items[C.ACCEPT]
                acc_chk.setCheckable(True)
                acc_chk.setCheckState(Qt.Checked if (accepted is True or accepted == 1) else Qt.Unchecked)
                acc_chk.setFlags(
                    (acc_chk.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    & ~Qt.ItemIsEditable
                )

                items[C.AID].setData(aid, Qt.UserRole)
                items[C.STATUS].setData(status_code, Qt.UserRole)

                row_idx = self._model.rowCount()
                self._model.appendRow(items)
                self._ptsid_map[row_idx] = ptsid

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
        self.cmbMethod.setEnabled(False)
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
        self.cmbMethod.setEnabled(True)

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
                    elif col in C.CHECKBOX:
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
            self._recalc_status(row)
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
                    UPDATE ams.pretreatmentrun SET
                        batchcode       = :code,
                        batchdate       = :dt,
                        equipmentid     = :eid,
                        technicianid    = :tid,
                        method          = :method,
                        notes           = :notes,
                        modifdatestamp  = :now,
                        modifuserstamp  = :user
                    WHERE pretreatmentrunid = :bid
                """), {
                    "code":   self.txtBatchCode.text().strip() or None,
                    "dt":     d.toPyDate(),
                    "eid":    self.cmbEquip.currentData(),
                    "tid":    self.cmbTech.currentData(),
                    "method": self.cmbMethod.currentText(),
                    "notes":  self.txtNotes.toPlainText().strip() or None,
                    "now":    datetime.now(),
                    "user":   user,
                    "bid":    self.batch_id,
                })
                conn.commit()
        except Exception as exc:
            log.error("Save pretreatmentrun header %d: %s", self.batch_id, exc)
            set_status(self._status_lbl, f"Header save failed: {exc}", "error")

    def _save_row(self, row: int):
        ptsid = self._ptsid_map.get(row)
        if ptsid is None:
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

        base_clear = self._model.item(row, C.BASE_CLEAR).checkState() == Qt.Checked
        accepted   = self._model.item(row, C.ACCEPT).checkState() == Qt.Checked
        status     = self._model.item(row, C.STATUS).data(Qt.UserRole)
        user       = normalize_login_name(get_current_user_id())

        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.pretreatmentsample SET
                        batchposition         = :pos,
                        pretreatment_mass_mg   = :premass,
                        acid1_reagent          = :acid1_reag,
                        acid1_conc_pct         = :acid1_conc,
                        acid1_temp_c           = :acid1_temp,
                        acid1_duration_min     = :acid1_dur,
                        base_reagent           = :base_reag,
                        base_conc_pct          = :base_conc,
                        base_temp_c            = :base_temp,
                        base_duration_min      = :base_dur,
                        base_cycles            = :base_cycles,
                        base_ran_clear         = :base_clear,
                        acid2_reagent          = :acid2_reag,
                        acid2_conc_pct         = :acid2_conc,
                        acid2_temp_c           = :acid2_temp,
                        acid2_duration_min     = :acid2_dur,
                        rinse_cycles           = :rinse_cycles,
                        final_rinse_ph         = :final_ph,
                        drying_temp_c          = :dry_temp,
                        drying_duration_h      = :dry_dur,
                        post_treatment_mass_mg = :postmass,
                        isaccepted             = :acc,
                        status                 = :st,
                        rejectreason           = :rej,
                        notes                  = :notes,
                        modifdatestamp         = :now,
                        modifuserstamp         = :user
                    WHERE pretreatmentsampleid = :ptsid
                """), {
                    "pos":          nt(C.POS),
                    "premass":      flt(C.PREMASS),
                    "acid1_reag":   txt(C.ACID1_REAGENT),
                    "acid1_conc":   flt(C.ACID1_CONC),
                    "acid1_temp":   flt(C.ACID1_TEMP),
                    "acid1_dur":    flt(C.ACID1_DUR),
                    "base_reag":    txt(C.BASE_REAGENT),
                    "base_conc":    flt(C.BASE_CONC),
                    "base_temp":    flt(C.BASE_TEMP),
                    "base_dur":     flt(C.BASE_DUR),
                    "base_cycles":  nt(C.BASE_CYCLES),
                    "base_clear":   base_clear,
                    "acid2_reag":   txt(C.ACID2_REAGENT),
                    "acid2_conc":   flt(C.ACID2_CONC),
                    "acid2_temp":   flt(C.ACID2_TEMP),
                    "acid2_dur":    flt(C.ACID2_DUR),
                    "rinse_cycles": nt(C.RINSE_CYCLES),
                    "final_ph":     flt(C.FINAL_PH),
                    "dry_temp":     flt(C.DRY_TEMP),
                    "dry_dur":      flt(C.DRY_DUR),
                    "postmass":     flt(C.POSTMASS),
                    "acc":          accepted,
                    "st":           status if status is not None else 0,
                    "rej":          txt(C.REJECT),
                    "notes":        txt(C.NOTES),
                    "now":          datetime.now(),
                    "user":         user,
                    "ptsid":        ptsid,
                })
                conn.commit()
        except Exception as exc:
            log.error("Save pretreatmentsample row %d (ptsid=%d): %s", row, ptsid, exc)
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

        user   = normalize_login_name(get_current_user_id())
        now    = datetime.now()
        method = self.cmbMethod.currentText()
        next_pos = self._model.rowCount() + 1

        try:
            with db_manager.get_connection() as conn:
                for i, aid in enumerate(dlg.selected_aids):
                    conn.execute(text("""
                        INSERT INTO ams.pretreatmentsample
                            (pretreatmentrunid, analysisid, isbypass, batchposition,
                             method, status, isaccepted,
                             createdatestamp, createuserstamp)
                        VALUES
                            (:bid, :aid, FALSE, :pos,
                             :method, 0, TRUE,
                             :now, :user)
                        ON CONFLICT DO NOTHING
                    """), {
                        "bid": self.batch_id,
                        "aid": aid,
                        "pos": next_pos + i,
                        "method": method,
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
                "Enter post-treatment mass (or mark as Failed) for all samples first.",
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
                    UPDATE ams.pretreatmentrun SET
                        runstatus      = 1,
                        modifdatestamp = :now,
                        modifuserstamp = :user
                    WHERE pretreatmentrunid = :bid
                """), {"now": datetime.now(), "user": user, "bid": self.batch_id})
                conn.commit()
            self.txtBatchStatus.setText("Complete")
            self._is_locked = False
            self.btnComplete.setEnabled(False)
            self.btnAdd.setEnabled(False)
            set_status(self._status_lbl, "Batch marked complete.", "success")
        except Exception as exc:
            log.error("Mark pretreatmentrun %d complete: %s", self.batch_id, exc)
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
