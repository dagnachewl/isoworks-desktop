"""
ams_purification_create_run_gui.py — Purification batch creation dialog.

Sources available samples from public.sample_queue (workflowjob.jobname =
'Purification'). Assembles a positional batch layout and saves to
ams.purification_run + ams.purification_sample, consuming the corresponding
sample_queue rows.

Layout mirrors ams_physical_prep_create_run_gui: left=setup+available,
centre=buttons, right=batch layout. No batch-level method/catalyst field
(unlike Pretreatment/Graphitisation) — purification_method is set per-sample
in the Details grid instead.
"""
from __future__ import annotations
import logging
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QSplitter,
    QGroupBox, QLabel, QLineEdit, QComboBox, QTextEdit, QSpinBox,
    QPushButton, QTableView, QAbstractItemView, QHeaderView,
    QMessageBox, QWidget, QDateEdit,
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush
from PyQt5.QtCore import Qt, QDate

from db_core import db_manager
from sqlalchemy import text
from shared_utils import get_current_user_id, normalize_login_name
from gui_utils import show_message
from help_browser import make_help_button

log = logging.getLogger(__name__)

_FILLED_COLOR = QColor("#E8F5E9")   # mint-green — slot with sample assigned
_EMPTY_COLOR  = QColor("#FFFFFF")   # white — empty slot

_TABLE_SS = (
    "QTableView { gridline-color:#deeeff; border:1px solid #b3d4f5; }"
    "QTableView::item:selected { background:#e1f5fe; color:#01579b; }"
    "QHeaderView::section { background:#e8f4fd; color:#37474f;"
    " padding:2px 5px; border:none;"
    " border-right:1px solid #cce5ff; border-bottom:2px solid #90caf9;"
    " font-weight:600; }"
)
_BTN_CLEAR = (
    "QPushButton { background:#f5f5f5; color:#78909c; border:1px solid #e0e0e0;"
    " border-radius:3px; padding:0 5px; font-size:11px; }"
    "QPushButton:hover { background:#fff3e0; color:#e65100; border-color:#e65100; }"
)

# ── Available-samples table column indices ────────────────────────────────────
AV_TBAID  = 0   # queue_id (hidden in UserRole on this item)
AV_AID    = 1   # analysisid
AV_SREF   = 2   # prefix-sampleid
AV_SNAME  = 3   # sample name
AV_WF     = 4   # workflow name
AV_PRIO   = 5   # priority

# ── Batch-layout table column indices ─────────────────────────────────────────
LL_POS  = 0   # batch position number
LL_AID  = 1   # analysisid  (UserRole = analysisid int, UserRole+1 = queue_id)
LL_SREF = 2   # prefix-sampleid
LL_NAME = 3   # sample name
LL_BTN  = 4   # clear button widget


class PurificationCreateRunDialog(QDialog):
    """
    Assemble a new purification batch from the TBA queue.

    Left   — Run Setup + Available Samples
    Centre — transfer buttons (>> / >>> / < / <<)
    Right  — Batch layout (positions with sample assignments)

    Save inserts ams.purification_run + ams.purification_sample and deletes
    consumed sample_queue rows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Purification Batch")
        self.setModal(True)
        self.resize(1280, 820)

        self.new_batch_id: int | None = None
        self._n_positions: int = 16          # current batch size
        self._batch_rows: list[dict] = []    # {row, aid, tbaid, sref, name}

        self._init_ui()
        self._load_lookups()
        self._suggest_batch_code()
        self._build_positions()
        self._load_available_samples()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>Create Purification Batch</b>"))
        bar.addStretch()

        self.btnSave = QPushButton("Save Batch")
        self.btnSave.setStyleSheet(
            "QPushButton { background:#27ae60; color:white; font-weight:bold;"
            " border:none; padding:5px 16px; border-radius:4px; }"
            "QPushButton:hover { background:#219a52; }"
        )
        self.btnSave.clicked.connect(self._save)
        btnCancel = QPushButton("Cancel")
        btnCancel.clicked.connect(self.reject)

        for w in [self.btnSave, btnCancel,
                  make_help_button(self, "purification_create_run")]:
            bar.addWidget(w)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setSpacing(6)
        ll.addWidget(self._build_run_setup())
        ll.addWidget(self._build_available_group(), 1)
        splitter.addWidget(left)

        mid = QWidget()
        ml = QVBoxLayout(mid)
        ml.addStretch()

        def _btn(lbl, tip):
            b = QPushButton(lbl); b.setFixedWidth(44); b.setToolTip(tip); return b

        self.btnAdd       = _btn(">>",  "Assign selected to next empty position(s)")
        self.btnAddAll    = _btn(">>>", "Assign ALL available samples")
        self.btnRemove    = _btn("<",   "Clear selected position(s)")
        self.btnRemoveAll = _btn("<<",  "Clear all filled positions")

        self.btnAdd.clicked.connect(self._add_to_batch)
        self.btnAddAll.clicked.connect(self._add_all_to_batch)
        self.tblAvail.doubleClicked.connect(lambda _: self._add_to_batch())
        self.btnRemove.clicked.connect(self._clear_selected)
        self.btnRemoveAll.clicked.connect(self._clear_all)

        for w in [self.btnAdd, self.btnAddAll]:
            ml.addWidget(w)
        ml.addSpacing(12)
        for w in [self.btnRemove, self.btnRemoveAll]:
            ml.addWidget(w)
        ml.addStretch()
        splitter.addWidget(mid)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.addWidget(self._build_batch_group(), 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 5)
        root.addWidget(splitter, 1)

    def _build_run_setup(self) -> QGroupBox:
        grp = QGroupBox("Batch Setup")
        form = QFormLayout(grp)
        form.setSpacing(5)
        form.setContentsMargins(8, 6, 8, 6)

        self.txtBatchCode = QLineEdit()
        self.txtBatchCode.setMaxLength(50)
        self.txtBatchCode.setPlaceholderText("e.g. PU260115A")
        form.addRow("Batch Code *", self.txtBatchCode)

        self.dtBatchDate = QDateEdit(QDate.currentDate())
        self.dtBatchDate.setCalendarPopup(True)
        self.dtBatchDate.setDisplayFormat("yyyy-MM-dd")
        self.dtBatchDate.dateChanged.connect(self._suggest_batch_code)
        form.addRow("Date", self.dtBatchDate)

        self.cmbEquip = QComboBox()
        form.addRow("Equipment", self.cmbEquip)

        self.cmbTech = QComboBox()
        form.addRow("Technician", self.cmbTech)

        self.spnSize = QSpinBox()
        self.spnSize.setRange(1, 99)
        self.spnSize.setValue(16)
        self.spnSize.setFixedWidth(60)
        self.spnSize.valueChanged.connect(self._on_size_changed)
        form.addRow("Batch Size", self.spnSize)

        self.txtNotes = QTextEdit()
        self.txtNotes.setFixedHeight(52)
        self.txtNotes.setPlaceholderText("Optional remarks…")
        form.addRow("Notes", self.txtNotes)

        return grp

    def _build_available_group(self) -> QGroupBox:
        grp = QGroupBox("Available Samples  (TBA queue — Purification job)")
        vlay = QVBoxLayout(grp)
        vlay.setSpacing(4)
        vlay.setContentsMargins(6, 6, 6, 6)

        self.modelAvail = QStandardItemModel()
        self.modelAvail.setHorizontalHeaderLabels(
            ["TBA ID", "AnalysisID", "Sample Ref", "Sample Name", "Workflow", "Priority"]
        )
        self.tblAvail = QTableView()
        self.tblAvail.setModel(self.modelAvail)
        self.tblAvail.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblAvail.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvail.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblAvail.setAlternatingRowColors(True)
        self.tblAvail.verticalHeader().setVisible(False)
        self.tblAvail.verticalHeader().setDefaultSectionSize(22)
        self.tblAvail.horizontalHeader().setStretchLastSection(True)
        self.tblAvail.setStyleSheet(_TABLE_SS)
        vlay.addWidget(self.tblAvail, 1)

        bot = QHBoxLayout()
        self.lblAvailCount = QLabel("0 samples")
        bot.addWidget(self.lblAvailCount)
        bot.addStretch()

        bot.addWidget(QLabel("Workflow:"))
        self.cmbWfFilter = QComboBox()
        self.cmbWfFilter.addItem("— All —", None)
        self.cmbWfFilter.currentIndexChanged.connect(self._load_available_samples)
        bot.addWidget(self.cmbWfFilter)

        btnRefresh = QPushButton("Refresh")
        btnRefresh.clicked.connect(self._load_available_samples)
        bot.addWidget(btnRefresh)
        vlay.addLayout(bot)
        return grp

    def _build_batch_group(self) -> QGroupBox:
        grp = QGroupBox("Batch Layout")
        vlay = QVBoxLayout(grp)
        vlay.setSpacing(4)
        vlay.setContentsMargins(6, 6, 6, 6)

        self.modelBatch = QStandardItemModel()
        self.modelBatch.setHorizontalHeaderLabels(
            ["Pos", "AnalysisID", "Sample Ref", "Sample Name", ""]
        )
        self.tblBatch = QTableView()
        self.tblBatch.setModel(self.modelBatch)
        self.tblBatch.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblBatch.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblBatch.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblBatch.setAlternatingRowColors(False)
        self.tblBatch.verticalHeader().setVisible(False)
        self.tblBatch.verticalHeader().setDefaultSectionSize(26)
        self.tblBatch.setStyleSheet(_TABLE_SS)
        vlay.addWidget(self.tblBatch, 1)

        self.lblStats = QLabel("Positions: 0  |  Filled: 0")
        self.lblStats.setStyleSheet("color:#555; font-size:11px; padding:2px 4px;")
        vlay.addWidget(self.lblStats)
        return grp

    def _apply_batch_col_widths(self):
        h = self.tblBatch.horizontalHeader()
        h.setSectionResizeMode(LL_POS,  QHeaderView.Fixed);  self.tblBatch.setColumnWidth(LL_POS,  40)
        h.setSectionResizeMode(LL_AID,  QHeaderView.Fixed);  self.tblBatch.setColumnWidth(LL_AID,  80)
        h.setSectionResizeMode(LL_SREF, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(LL_NAME, QHeaderView.Stretch)
        h.setSectionResizeMode(LL_BTN,  QHeaderView.Fixed);  self.tblBatch.setColumnWidth(LL_BTN,  52)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_lookups(self):
        try:
            with db_manager.get_connection() as conn:
                self.cmbEquip.addItem("— Select —", None)
                # categoryid 17 = Purification (job_procedure renumber,
                # migration 080) -- scopes this picker to Purification
                # equipment only, matching every other AMS stage.
                for r in conn.execute(text(
                    "SELECT DISTINCT e.EquipmentID, e.EquipmentName FROM Equipment e "
                    "JOIN equipment_job_procedure ejp ON ejp.equipmentid = e.EquipmentID "
                    "JOIN job_procedure jp ON jp.id = ejp.categoryid "
                    "WHERE jp.id = 17 AND e.IsObsolete IS NOT TRUE "
                    "ORDER BY e.EquipmentName"
                )):
                    self.cmbEquip.addItem(r[1], r[0])

                cn = db_manager.sql_concat('LastName', "', '", 'FirstMiddleName')
                self.cmbTech.addItem("— unassigned —", None)
                for r in conn.execute(text(
                    f"SELECT EmployeeID, {cn} AS n FROM Employee "
                    "WHERE IsObsolete IS NOT TRUE ORDER BY LastName"
                )):
                    self.cmbTech.addItem(r[1], r[0])

                for r in conn.execute(text("""
                    SELECT DISTINCT w.workflowid, w.workflowname
                    FROM public.workflow w
                    JOIN public.workflowjob wj ON wj.workflowid = w.workflowid
                    WHERE wj.jobname = 'Purification' AND w.isobsolete = FALSE
                    ORDER BY w.workflowname
                """)):
                    self.cmbWfFilter.addItem(r[1], r[0])

        except Exception as exc:
            log.error("PurificationCreateRunDialog._load_lookups: %s", exc)

        user = normalize_login_name(get_current_user_id()).split("\\")[-1].lower()
        for i in range(self.cmbTech.count()):
            if user in self.cmbTech.itemText(i).lower():
                self.cmbTech.setCurrentIndex(i)
                break

    def _suggest_batch_code(self):
        cur = self.txtBatchCode.text().strip()
        dt  = self.dtBatchDate.date()
        sug = f"PU{dt.toString('yyMMdd')}A"
        if not cur or (cur.startswith("PU") and len(cur) == 9):
            self.txtBatchCode.setText(sug)

    def _load_available_samples(self):
        self.modelAvail.removeRows(0, self.modelAvail.rowCount())

        assigned_aids: set[int] = set()
        for info in self._batch_rows:
            if info.get("aid") is not None:
                assigned_aids.add(info["aid"])

        wf_id = self.cmbWfFilter.currentData()
        params: dict = {}
        clauses = ["wj.jobname = 'Purification'"]

        if wf_id:
            clauses.append("wj.workflowid = :wf")
            params["wf"] = wf_id
        if assigned_aids:
            clauses.append("sq.source_aid NOT IN :excl")
            params["excl"] = tuple(assigned_aids)

        where = " AND ".join(clauses)
        sql = f"""
            SELECT sq.queue_id,
                   sq.source_aid AS analysisid,
                   a.prefix || '-' || CAST(a.sampleid AS TEXT) AS sampleref,
                   s.sname,
                   w.workflowname,
                   sq.priorityid
            FROM   public.sample_queue sq
            JOIN   public.analysis    a  ON  a.analysisid  = sq.source_aid
            JOIN   public.sample      s  ON  s.sampleid    = a.sampleid
                                         AND s.prefix      = a.prefix
            JOIN   public.workflowjob wj ON  wj.workflowjobid = sq.workflowjobid
            JOIN   public.workflow    w  ON  w.workflowid   = wj.workflowid
            WHERE  sq.source_aid IS NOT NULL AND {where}
            ORDER  BY sq.priorityid NULLS LAST, sq.queue_id
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as exc:
            log.error("Load TBA samples: %s", exc)
            return

        for r in rows:
            tbaid, aid, sref, sname, wfname, prio = r
            items = [
                QStandardItem(str(tbaid)),
                QStandardItem(str(aid)),
                QStandardItem(sref or ""),
                QStandardItem(sname or ""),
                QStandardItem(wfname or ""),
                QStandardItem(str(prio) if prio is not None else ""),
            ]
            items[AV_TBAID].setData(tbaid, Qt.UserRole)
            items[AV_AID].setData(aid,   Qt.UserRole)
            self.modelAvail.appendRow(items)

        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) in queue")

        h = self.tblAvail.horizontalHeader()
        h.setSectionResizeMode(AV_TBAID, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_AID,   QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_SREF,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_SNAME, QHeaderView.Stretch)
        h.setSectionResizeMode(AV_WF,    QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_PRIO,  QHeaderView.ResizeToContents)

    # ── Batch position builder ─────────────────────────────────────────────────

    def _build_positions(self):
        preserved: dict[int, dict] = {}
        for info in self._batch_rows:
            if info.get("aid") is not None:
                preserved[info["pos"]] = info

        self.modelBatch.removeRows(0, self.modelBatch.rowCount())
        self._batch_rows.clear()

        for pos in range(1, self._n_positions + 1):
            prev = preserved.get(pos)
            aid   = prev["aid"]   if prev else None
            tbaid = prev["tbaid"] if prev else None
            sref  = prev["sref"]  if prev else ""
            name  = prev["name"]  if prev else ""
            filled = aid is not None

            bg = _FILLED_COLOR if filled else _EMPTY_COLOR

            pos_item  = QStandardItem(str(pos))
            aid_item  = QStandardItem(str(aid) if aid else "")
            sref_item = QStandardItem(sref)
            name_item = QStandardItem(name)
            btn_item  = QStandardItem("")

            for it in [pos_item, aid_item, sref_item, name_item, btn_item]:
                it.setBackground(QBrush(bg))
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)

            aid_item.setData(aid,   Qt.UserRole)
            aid_item.setData(tbaid, Qt.UserRole + 1)

            row_idx = self.modelBatch.rowCount()
            self.modelBatch.appendRow(
                [pos_item, aid_item, sref_item, name_item, btn_item]
            )

            btn = QPushButton("Clear")
            btn.setFixedHeight(22)
            btn.setStyleSheet(_BTN_CLEAR)
            btn.clicked.connect(lambda _, r=row_idx: self._clear_row(r))
            self.tblBatch.setIndexWidget(
                self.modelBatch.index(row_idx, LL_BTN), btn
            )

            self._batch_rows.append({
                "row": row_idx, "pos": pos,
                "aid": aid, "tbaid": tbaid,
                "sref": sref, "name": name,
            })

        self._apply_batch_col_widths()
        self._update_stats()

    def _on_size_changed(self, new_size: int):
        self._n_positions = new_size
        self._build_positions()

    # ── Transfer helpers ──────────────────────────────────────────────────────

    def _empty_batch_rows(self) -> list[int]:
        return [
            info["row"] for info in self._batch_rows
            if info.get("aid") is None
        ]

    def _assign_to_row(self, batch_row: int, avail_row: int):
        tbaid = self.modelAvail.item(avail_row, AV_TBAID).data(Qt.UserRole)
        aid   = self.modelAvail.item(avail_row, AV_AID).data(Qt.UserRole)
        sref  = self.modelAvail.item(avail_row, AV_SREF).text()
        name  = self.modelAvail.item(avail_row, AV_SNAME).text()

        self.modelBatch.item(batch_row, LL_AID).setText(str(aid))
        self.modelBatch.item(batch_row, LL_AID).setData(aid,   Qt.UserRole)
        self.modelBatch.item(batch_row, LL_AID).setData(tbaid, Qt.UserRole + 1)
        self.modelBatch.item(batch_row, LL_SREF).setText(sref)
        self.modelBatch.item(batch_row, LL_NAME).setText(name)

        fill = QBrush(_FILLED_COLOR)
        for col in range(self.modelBatch.columnCount()):
            it = self.modelBatch.item(batch_row, col)
            if it:
                it.setBackground(fill)

        for info in self._batch_rows:
            if info["row"] == batch_row:
                info["aid"]   = aid
                info["tbaid"] = tbaid
                info["sref"]  = sref
                info["name"]  = name
                break

        self.modelAvail.removeRow(avail_row)

    def _add_to_batch(self):
        sel = sorted({idx.row() for idx in self.tblAvail.selectionModel().selectedRows()})
        if not sel:
            sel = [0] if self.modelAvail.rowCount() else []
        empty = self._empty_batch_rows()
        removed = 0
        for batch_row, avail_row in zip(empty, sel):
            self._assign_to_row(batch_row, avail_row - removed)
            removed += 1
        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) in queue")

    def _add_all_to_batch(self):
        for batch_row in self._empty_batch_rows():
            if self.modelAvail.rowCount() == 0:
                break
            self._assign_to_row(batch_row, 0)
        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) in queue")

    def _clear_row(self, batch_row: int):
        aid_it = self.modelBatch.item(batch_row, LL_AID)
        if not aid_it or aid_it.data(Qt.UserRole) is None:
            return

        tbaid = aid_it.data(Qt.UserRole + 1)
        aid   = aid_it.data(Qt.UserRole)

        if tbaid is not None:
            self._reload_tba_to_avail(tbaid, aid)

        self.modelBatch.item(batch_row, LL_AID).setText("")
        self.modelBatch.item(batch_row, LL_AID).setData(None, Qt.UserRole)
        self.modelBatch.item(batch_row, LL_AID).setData(None, Qt.UserRole + 1)
        self.modelBatch.item(batch_row, LL_SREF).setText("")
        self.modelBatch.item(batch_row, LL_NAME).setText("")

        empty_brush = QBrush(_EMPTY_COLOR)
        for col in range(self.modelBatch.columnCount()):
            it = self.modelBatch.item(batch_row, col)
            if it:
                it.setBackground(empty_brush)

        for info in self._batch_rows:
            if info["row"] == batch_row:
                info["aid"] = info["tbaid"] = None
                info["sref"] = info["name"] = ""
                break

        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) in queue")

    def _clear_selected(self):
        rows = sorted(
            {idx.row() for idx in self.tblBatch.selectionModel().selectedRows()},
            reverse=True,
        )
        for r in rows:
            self._clear_row(r)

    def _clear_all(self):
        for info in list(self._batch_rows):
            if info.get("aid") is not None:
                self._clear_row(info["row"])

    def _reload_tba_to_avail(self, tbaid: int, aid: int):
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("""
                    SELECT sq.queue_id, sq.source_aid,
                           a.prefix || '-' || CAST(a.sampleid AS TEXT),
                           s.sname, w.workflowname, sq.priorityid
                    FROM   public.sample_queue sq
                    JOIN   public.analysis  a  ON  a.analysisid = sq.source_aid
                    JOIN   public.sample    s  ON  s.sampleid   = a.sampleid
                                               AND s.prefix     = a.prefix
                    JOIN   public.workflowjob wj ON wj.workflowjobid = sq.workflowjobid
                    JOIN   public.workflow   w  ON  w.workflowid = wj.workflowid
                    WHERE  sq.queue_id = :tid
                """), {"tid": tbaid}).fetchone()
        except Exception as exc:
            log.error("Reload TBA %d: %s", tbaid, exc)
            return
        if not r:
            return
        tid2, aid2, sref, sname, wfname, prio = r
        items = [
            QStandardItem(str(tid2)),
            QStandardItem(str(aid2)),
            QStandardItem(sref or ""),
            QStandardItem(sname or ""),
            QStandardItem(wfname or ""),
            QStandardItem(str(prio) if prio is not None else ""),
        ]
        items[AV_TBAID].setData(tid2, Qt.UserRole)
        items[AV_AID].setData(aid2,  Qt.UserRole)
        self.modelAvail.insertRow(0, items)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _update_stats(self):
        total  = len(self._batch_rows)
        filled = sum(1 for i in self._batch_rows if i.get("aid") is not None)
        self.lblStats.setText(f"Positions: {total}  |  Filled: {filled}")

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self):
        code = self.txtBatchCode.text().strip()
        if not code:
            show_message(self, "Validation", "Batch Code is required.", QMessageBox.Warning)
            return

        filled = [i for i in self._batch_rows if i.get("aid") is not None]
        if not filled:
            show_message(self, "Validation",
                         "Add at least one sample to the batch before saving.",
                         QMessageBox.Warning)
            return

        equip_id  = self.cmbEquip.currentData()
        tech_id   = self.cmbTech.currentData()
        batch_dt  = self.dtBatchDate.date().toPyDate()
        notes_txt = self.txtNotes.toPlainText().strip() or None
        now       = datetime.now()
        user      = normalize_login_name(get_current_user_id())

        try:
            with db_manager.get_connection() as conn:
                run_row = conn.execute(text("""
                    INSERT INTO ams.purification_run
                        (batchcode, batchdate, equipmentid, technicianid,
                         notes, runstatus, islocked,
                         createdatestamp, createuserstamp)
                    VALUES
                        (:code, :dt, :eid, :tid,
                         :notes, 0, FALSE,
                         :now, :user)
                    RETURNING purificationrunid
                """), {
                    "code": code, "dt": batch_dt,
                    "eid": equip_id, "tid": tech_id,
                    "notes": notes_txt,
                    "now": now, "user": user,
                }).fetchone()
                batch_id = run_row[0]

                consumed_tbaids: list[int] = []

                for info in filled:
                    tbaid = info.get("tbaid")

                    conn.execute(text("""
                        INSERT INTO ams.purification_sample
                            (purificationrunid, analysisid, isbypass, batchposition,
                             status, isaccepted,
                             createdatestamp, createuserstamp)
                        VALUES
                            (:bid, :aid, FALSE, :pos,
                             0, TRUE,
                             :now, :user)
                    """), {
                        "bid": batch_id,
                        "aid": info["aid"],
                        "pos": info["pos"],
                        "now": now,
                        "user": user,
                    })

                    if tbaid is not None:
                        consumed_tbaids.append(tbaid)

                if consumed_tbaids:
                    conn.execute(text(
                        "DELETE FROM public.sample_queue WHERE queue_id IN :ids"
                    ), {"ids": tuple(consumed_tbaids)})

                conn.commit()
                self.new_batch_id = batch_id

            log.info("Created purification batch %d (%s) with %d samples",
                     batch_id, code, len(filled))
            show_message(self, "Batch Created",
                         f"Batch {batch_id} ({code}) created with {len(filled)} sample(s).")
            self.accept()

        except Exception as exc:
            log.error("PurificationCreateRunDialog._save: %s", exc)
            if "uq_purification_run_batchcode" in str(exc).lower():
                show_message(self, "Duplicate Code",
                             f"Batch code '{code}' already exists.", QMessageBox.Warning)
            else:
                show_message(self, "Error", str(exc), QMessageBox.Critical)
