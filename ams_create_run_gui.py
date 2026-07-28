"""
ams_create_run_gui.py — AMS run creation dialog for IsoWorks.
Redesigned: wheel-template-based assembly with Available Samples panel
(sourced from completed ams.graphsample records) and transfer buttons,
mirroring the TRIMS LSC create-run pattern.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from typing import Optional

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

# ── Target type display ────────────────────────────────────────────────────────
_TYPE_LABELS = {
    "unknown":        "Sample",
    "OXI":            "OXI (Primary Std)",
    "OXII":           "OXII (Secondary Std)",
    "process_blank":  "Process Blank",
    "graphite_blank": "Graphite Blank",
    "secondary_std":  "Secondary Std",
    "other":          "Other",
    "gas_unknown":    "Gas Sample (GIS)",
    "gas_blank":      "Gas Blank (GIS)",
}
_TYPE_COLORS = {
    "unknown":        "#FFFFFF",   # white       — empty sample slot
    "OXI":            "#E3F2FD",   # blue-tint   — primary standard
    "OXII":           "#E8EAF6",   # indigo      — secondary standard
    "process_blank":  "#FFF3E0",   # amber       — process blank
    "graphite_blank": "#FCE4EC",   # rose        — graphite blank
    "secondary_std":  "#F3E5F5",   # lavender    — other standard
    "other":          "#F5F5F5",   # grey        — other
    "gas_unknown":    "#E0F7FA",   # cyan-tint   — GIS gas sample
    "gas_blank":      "#FFF8E1",   # pale-yellow — GIS gas blank
}
_SAMPLE_FILLED_COLOR = "#E8F5E9"   # mint-green when an unknown slot is filled

# ── Table column indices ───────────────────────────────────────────────────────
# Available samples panel
AV_GSID  = 0
AV_AID   = 1
AV_SREF  = 2
AV_SNAME = 3
AV_MASS  = 4
AV_PATH  = 5   # "Direct" or "Graphitised"

# Wheel layout panel
WL_POS   = 0
WL_TYPE  = 1
WL_AID   = 2
WL_SREF  = 3
WL_MASS  = 4
WL_BTN   = 5   # Clear button

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


class AMSCreateRunDialog(QDialog):
    """
    Wheel-template-based AMS run creation dialog.

    Left  — Run Setup (metadata) + Available Samples (from graphitisation pool)
    Centre — transfer buttons
    Right  — Wheel layout (template + sample assignments)

    On save: inserts ams.amsrun + ams.amswheel + ams.amstarget rows.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create New AMS Run")
        self.setModal(True)
        self.resize(1280, 820)

        self.new_run_id: int | None = None
        # row → graphsampleid stored in UserRole on AV_GSID column
        self._templates: list[dict] = []   # [{id, name, npos, layout}, ...]
        self._wheel_layout: list[dict] = []  # [{pos, type, gsid, aid, sref, mass}]

        self._init_ui()
        self._load_lookups()
        self._suggest_run_code()
        self._load_available_samples()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # top bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("<b>Create AMS Run</b>"))
        bar.addStretch()

        self.btnSave = QPushButton("Save Run")
        self.btnSave.setEnabled(False)
        self.btnSave.setStyleSheet(
            "QPushButton { background:#27ae60; color:white; font-weight:bold;"
            " border:none; padding:5px 16px; border-radius:4px; }"
            "QPushButton:hover { background:#219a52; }"
            "QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }"
        )
        self.btnSave.clicked.connect(self._save_run)
        btnClose = QPushButton("Cancel")
        btnClose.clicked.connect(self.reject)

        for w in [self.btnSave, btnClose,
                  make_help_button(self, "ams_create_run")]:
            bar.addWidget(w)
        root.addLayout(bar)

        # splitter
        splitter = QSplitter(Qt.Horizontal)

        # ── LEFT ──────────────────────────────────────────────────────────────
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setSpacing(6)
        left_lay.addWidget(self._build_run_setup())
        left_lay.addWidget(self._build_template_group())
        left_lay.addWidget(self._build_available_group(), 1)
        splitter.addWidget(left)

        # ── CENTRE ────────────────────────────────────────────────────────────
        mid = QWidget()
        mid_lay = QVBoxLayout(mid)
        mid_lay.addStretch()

        def _btn(lbl, tip):
            b = QPushButton(lbl)
            b.setFixedWidth(44)
            b.setToolTip(tip)
            return b

        self.btnAdd       = _btn(">>",  "Assign selected samples to next empty slot(s)")
        self.btnAddAll    = _btn(">>>", "Assign ALL available samples to empty slots")
        self.btnRemove    = _btn("<",   "Clear selected wheel slot(s)")
        self.btnRemoveAll = _btn("<<",  "Clear all filled sample slots")

        self.btnAdd.clicked.connect(self._add_to_wheel)
        self.btnAddAll.clicked.connect(self._add_all_to_wheel)
        self.tblAvail.doubleClicked.connect(lambda _: self._add_to_wheel())
        self.btnRemove.clicked.connect(self._clear_selected)
        self.btnRemoveAll.clicked.connect(self._clear_all)

        for w in [self.btnAdd, self.btnAddAll]:
            mid_lay.addWidget(w)
        mid_lay.addSpacing(12)
        for w in [self.btnRemove, self.btnRemoveAll]:
            mid_lay.addWidget(w)
        mid_lay.addStretch()
        splitter.addWidget(mid)

        # ── RIGHT ─────────────────────────────────────────────────────────────
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.addWidget(self._build_wheel_group(), 1)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 5)
        root.addWidget(splitter, 1)

    def _build_run_setup(self) -> QGroupBox:
        grp = QGroupBox("Run Setup")
        form = QFormLayout(grp)
        form.setSpacing(5)
        form.setContentsMargins(8, 6, 8, 6)

        self.txtRunCode = QLineEdit()
        self.txtRunCode.setMaxLength(50)
        self.txtRunCode.setPlaceholderText("e.g. R260115A")
        form.addRow("Run Code *", self.txtRunCode)

        self.dtRunDate = QDateEdit(QDate.currentDate())
        self.dtRunDate.setCalendarPopup(True)
        self.dtRunDate.setDisplayFormat("yyyy-MM-dd")
        self.dtRunDate.dateChanged.connect(self._suggest_run_code)
        form.addRow("Run Date", self.dtRunDate)

        self.cmbEquip = QComboBox()
        form.addRow("AMS Machine *", self.cmbEquip)

        self.cmbTech = QComboBox()
        form.addRow("Technician", self.cmbTech)

        self.spnWheel = QSpinBox()
        self.spnWheel.setRange(1, 99)
        self.spnWheel.setValue(1)
        form.addRow("Wheel #", self.spnWheel)

        self.txtNotes = QTextEdit()
        self.txtNotes.setFixedHeight(52)
        self.txtNotes.setPlaceholderText("Optional remarks…")
        form.addRow("Notes", self.txtNotes)

        return grp

    def _build_template_group(self) -> QGroupBox:
        grp = QGroupBox("Wheel Template")
        lay = QHBoxLayout(grp)
        lay.setContentsMargins(8, 6, 8, 6)

        self.cmbTemplate = QComboBox()
        self.cmbTemplate.addItem("— select template —", None)
        self.cmbTemplate.currentIndexChanged.connect(self._on_template_changed)

        btnReset = QPushButton("Reset")
        btnReset.setToolTip("Re-apply selected template, clearing all assignments")
        btnReset.clicked.connect(self._on_template_changed)

        lay.addWidget(QLabel("Template:"))
        lay.addWidget(self.cmbTemplate, 1)
        lay.addWidget(btnReset)
        return grp

    def _build_available_group(self) -> QGroupBox:
        grp = QGroupBox("Available Samples  (graphitised, not yet assigned)")
        vlay = QVBoxLayout(grp)
        vlay.setSpacing(4)
        vlay.setContentsMargins(6, 6, 6, 6)

        self.modelAvail = QStandardItemModel()
        self.modelAvail.setHorizontalHeaderLabels(
            ["GraphSampleID", "AnalysisID", "Sample Ref", "Sample Name", "Mass (mg)", "Path"]
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

        # Workflow filter: all, or AMS-workflow only
        bot.addWidget(QLabel("Workflow:"))
        self.cmbWfFilter = QComboBox()
        self.cmbWfFilter.addItem("All ready samples", None)
        self.cmbWfFilter.currentIndexChanged.connect(self._load_available_samples)
        bot.addWidget(self.cmbWfFilter)

        btnRefresh = QPushButton("Refresh")
        btnRefresh.clicked.connect(self._load_available_samples)
        bot.addWidget(btnRefresh)
        vlay.addLayout(bot)
        return grp

    def _build_wheel_group(self) -> QGroupBox:
        grp = QGroupBox("Wheel Layout")
        vlay = QVBoxLayout(grp)
        vlay.setSpacing(4)
        vlay.setContentsMargins(6, 6, 6, 6)

        self.modelWheel = QStandardItemModel()
        self.modelWheel.setHorizontalHeaderLabels(
            ["Pos", "Type", "AnalysisID", "Sample Ref / Label", "Graphite Mass (mg)", ""]
        )
        self.tblWheel = QTableView()
        self.tblWheel.setModel(self.modelWheel)
        self.tblWheel.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblWheel.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblWheel.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblWheel.setAlternatingRowColors(False)
        self.tblWheel.verticalHeader().setVisible(False)
        self.tblWheel.verticalHeader().setDefaultSectionSize(22)
        self.tblWheel.setStyleSheet(_TABLE_SS)
        vlay.addWidget(self.tblWheel, 1)

        self.lblStats = QLabel("Load a template to begin.")
        self.lblStats.setStyleSheet("color:#555; font-size:11px; padding:2px 4px;")
        vlay.addWidget(self.lblStats)
        return grp

    def _apply_wheel_col_widths(self):
        h = self.tblWheel.horizontalHeader()
        h.setSectionResizeMode(WL_POS,  QHeaderView.Fixed);  self.tblWheel.setColumnWidth(WL_POS,  40)
        h.setSectionResizeMode(WL_TYPE, QHeaderView.Fixed);  self.tblWheel.setColumnWidth(WL_TYPE, 130)
        h.setSectionResizeMode(WL_AID,  QHeaderView.Fixed);  self.tblWheel.setColumnWidth(WL_AID,  80)
        h.setSectionResizeMode(WL_SREF, QHeaderView.Stretch)
        h.setSectionResizeMode(WL_MASS, QHeaderView.Fixed);  self.tblWheel.setColumnWidth(WL_MASS, 110)
        h.setSectionResizeMode(WL_BTN,  QHeaderView.Fixed);  self.tblWheel.setColumnWidth(WL_BTN,  52)

    # ── Data loading ──────────────────────────────────────────────────────────

    def _load_lookups(self):
        try:
            with db_manager.get_connection() as conn:
                # AMS machines (CategoryID TBD — show all non-obsolete for now)
                for r in conn.execute(text(
                    "SELECT EquipmentID, EquipmentName FROM Equipment "
                    "WHERE IsObsolete IS NOT TRUE ORDER BY EquipmentName"
                )):
                    self.cmbEquip.addItem(r[1], r[0])

                # Employees
                cn = db_manager.sql_concat('LastName', "', '", 'FirstMiddleName')
                for r in conn.execute(text(
                    f"SELECT EmployeeID, {cn} AS n FROM Employee "
                    "WHERE IsObsolete IS NOT TRUE ORDER BY LastName"
                )):
                    self.cmbTech.addItem(r[1], r[0])
                self.cmbTech.insertItem(0, "— unassigned —", None)

                # Templates
                for r in conn.execute(text(
                    "SELECT templateid, templatename, npositions, layoutjson "
                    "FROM ams.wheeltemplate WHERE isactive ORDER BY templatename"
                )):
                    self.cmbTemplate.addItem(f"{r[1]} ({r[2]} pos)", r[0])
                    self._templates.append({
                        "id": r[0], "name": r[1],
                        "npos": r[2], "layout": json.loads(r[3]),
                    })

                # Workflow filter options (AMS workflows only)
                self.cmbWfFilter.addItem("— All —", None)
                for r in conn.execute(text("""
                    SELECT DISTINCT w.workflowid, w.workflowname
                    FROM public.workflow w
                    JOIN public.workflowjob wj ON wj.workflowid = w.workflowid
                    WHERE wj.jobname = 'AMS' AND w.isobsolete = FALSE
                    ORDER BY w.workflowname
                """)):
                    self.cmbWfFilter.addItem(r[1], r[0])

        except Exception as exc:
            log.error("AMSCreateRunDialog._load_lookups: %s", exc)

        # Default technician to current user
        user = normalize_login_name(get_current_user_id()).split("\\")[-1].lower()
        for i in range(self.cmbTech.count()):
            if user in self.cmbTech.itemText(i).lower():
                self.cmbTech.setCurrentIndex(i)
                break

    def _suggest_run_code(self):
        cur = self.txtRunCode.text().strip()
        dt  = self.dtRunDate.date()
        sug = f"R{dt.toString('yyMMdd')}A"
        if not cur or (cur.startswith("R") and len(cur) == 8):
            self.txtRunCode.setText(sug)

    def _load_available_samples(self):
        self.modelAvail.removeRows(0, self.modelAvail.rowCount())

        wf_id = self.cmbWfFilter.currentData()
        # Collect graphsampleids already assigned in the wheel table
        assigned_gsids: set[int] = set()
        for row in range(self.modelWheel.rowCount()):
            it = self.modelWheel.item(row, WL_AID)
            gsid = it.data(Qt.UserRole + 1) if it else None
            if gsid:
                assigned_gsids.add(gsid)

        params: dict = {}
        extra = ""
        if wf_id:
            extra = "AND a.workflowid = :wf"
            params["wf"] = wf_id

        excl = ""
        if assigned_gsids:
            excl = "AND gs.graphsampleid NOT IN :excl"
            params["excl"] = tuple(assigned_gsids)

        sql = f"""
            SELECT gs.graphsampleid,
                   gs.analysisid,
                   a.prefix || '-' || CAST(a.sampleid AS TEXT) AS sampleref,
                   s.sname,
                   gs.graphitemass_mg,
                   gs.isbypass
            FROM ams.graphsample gs
            JOIN public.analysis a ON a.analysisid = gs.analysisid
            JOIN public.sample   s ON s.sampleid   = a.sampleid AND s.prefix = a.prefix
            WHERE gs.status      = 2
              AND gs.isaccepted  = TRUE
              AND gs.graphitemass_mg IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ams.amstarget t WHERE t.graphsampleid = gs.graphsampleid
              )
              {excl}
              {extra}
            ORDER BY gs.graphsampleid DESC
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as exc:
            log.error("Load available samples: %s", exc)
            return

        for r in rows:
            gsid, aid, sref, sname, mass, bypass = r
            path_label = "Direct" if bypass else "Graphitised"
            items = [
                QStandardItem(str(gsid)),
                QStandardItem(str(aid)),
                QStandardItem(sref or ""),
                QStandardItem(sname or ""),
                QStandardItem(f"{float(mass):.3f}" if mass else ""),
                QStandardItem(path_label),
            ]
            items[AV_GSID].setData(gsid, Qt.UserRole)
            items[AV_AID].setData(aid,  Qt.UserRole)
            self.modelAvail.appendRow(items)

        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) available")

        h = self.tblAvail.horizontalHeader()
        h.setSectionResizeMode(AV_GSID,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_AID,   QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_SREF,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_SNAME, QHeaderView.Stretch)
        h.setSectionResizeMode(AV_MASS,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(AV_PATH,  QHeaderView.ResizeToContents)

    # ── Template ──────────────────────────────────────────────────────────────

    def _on_template_changed(self):
        tmpl_id = self.cmbTemplate.currentData()
        tmpl = next((t for t in self._templates if t["id"] == tmpl_id), None)
        if tmpl is None:
            return

        self.modelWheel.removeRows(0, self.modelWheel.rowCount())
        self._wheel_layout.clear()

        type_counters: dict[str, int] = {}  # for auto-labelling standards

        for entry in sorted(tmpl["layout"], key=lambda e: e["pos"]):
            pos  = entry["pos"]
            ttype = entry["type"]
            type_counters[ttype] = type_counters.get(ttype, 0) + 1
            n = type_counters[ttype]

            is_sample = ttype == "unknown"
            color     = QColor(_TYPE_COLORS.get(ttype, "#FFFFFF"))
            brush     = QBrush(color)

            if is_sample:
                label = ""       # filled when sample is assigned
                auto_label = f"SAMPLE-{pos}"
            else:
                lbl_map = {
                    "OXI": "OXI", "OXII": "OXII",
                    "process_blank": "PB", "graphite_blank": "GB",
                    "secondary_std": "STD", "other": "OTHER",
                    "gas_unknown": "GAS", "gas_blank": "GBLK",
                }
                auto_label = f"{lbl_map.get(ttype, ttype)}-{n}"
                label = auto_label

            pos_item  = QStandardItem(str(pos))
            type_item = QStandardItem(_TYPE_LABELS.get(ttype, ttype))
            aid_item  = QStandardItem("")
            sref_item = QStandardItem(label)
            mass_item = QStandardItem("")
            btn_item  = QStandardItem("")

            for it in [pos_item, type_item, aid_item, sref_item, mass_item, btn_item]:
                it.setBackground(brush)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)

            # Store metadata in UserRole
            type_item.setData(ttype,       Qt.UserRole)      # targettype key
            aid_item.setData(None,         Qt.UserRole)      # analysisid
            aid_item.setData(None,         Qt.UserRole + 1)  # graphsampleid
            sref_item.setData(auto_label,  Qt.UserRole)      # auto-label fallback
            mass_item.setData(None,        Qt.UserRole)      # graphitemass_mg

            row_idx = self.modelWheel.rowCount()
            self.modelWheel.appendRow(
                [pos_item, type_item, aid_item, sref_item, mass_item, btn_item]
            )
            self._wheel_layout.append({"pos": pos, "type": ttype, "row": row_idx})

            # Add Clear button for sample slots
            if is_sample:
                btn = QPushButton("Clear")
                btn.setFixedHeight(20)
                btn.setStyleSheet(_BTN_CLEAR)
                btn.clicked.connect(lambda _, r=row_idx: self._clear_row(r))
                self.tblWheel.setIndexWidget(
                    self.modelWheel.index(row_idx, WL_BTN), btn
                )

        self._apply_wheel_col_widths()
        self._update_stats()
        self.btnSave.setEnabled(True)
        self._load_available_samples()

    # ── Transfer buttons ──────────────────────────────────────────────────────

    def _selected_avail_rows(self) -> list[int]:
        return sorted({idx.row() for idx in self.tblAvail.selectionModel().selectedRows()})

    def _empty_sample_rows(self) -> list[int]:
        result = []
        for info in self._wheel_layout:
            if info["type"] != "unknown":
                continue
            row = info["row"]
            it  = self.modelWheel.item(row, WL_AID)
            if it and it.data(Qt.UserRole) is None:
                result.append(row)
        return result

    def _assign_sample_to_row(self, wheel_row: int, avail_row: int):
        gsid = self.modelAvail.item(avail_row, AV_GSID).data(Qt.UserRole)
        aid  = self.modelAvail.item(avail_row, AV_AID).data(Qt.UserRole)
        sref = self.modelAvail.item(avail_row, AV_SREF).text()
        sname= self.modelAvail.item(avail_row, AV_SNAME).text()
        mass = self.modelAvail.item(avail_row, AV_MASS).text()

        label = f"{sref}  {sname}".strip()
        fill  = QBrush(QColor(_SAMPLE_FILLED_COLOR))

        self.modelWheel.item(wheel_row, WL_AID).setText(str(aid))
        self.modelWheel.item(wheel_row, WL_AID).setData(aid,  Qt.UserRole)
        self.modelWheel.item(wheel_row, WL_AID).setData(gsid, Qt.UserRole + 1)
        self.modelWheel.item(wheel_row, WL_SREF).setText(label)
        self.modelWheel.item(wheel_row, WL_MASS).setText(mass)
        self.modelWheel.item(wheel_row, WL_MASS).setData(
            float(mass) if mass else None, Qt.UserRole
        )
        for col in range(self.modelWheel.columnCount()):
            it = self.modelWheel.item(wheel_row, col)
            if it:
                it.setBackground(fill)

        self.modelAvail.removeRow(avail_row)

    def _add_to_wheel(self):
        sel_avail = self._selected_avail_rows()
        if not sel_avail:
            sel_avail = [0] if self.modelAvail.rowCount() else []
        empty_slots = self._empty_sample_rows()
        pairs = list(zip(empty_slots, sel_avail))
        if not pairs:
            return
        for wheel_row, avail_row in pairs:
            # adjust avail_row for deletions already done
            actual = avail_row - sum(
                1 for _, ar in pairs[:pairs.index((wheel_row, avail_row))]
                if ar < avail_row
            )
            self._assign_sample_to_row(wheel_row, actual)
        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) available")

    def _add_all_to_wheel(self):
        empty_slots = self._empty_sample_rows()
        for i, wheel_row in enumerate(empty_slots):
            if self.modelAvail.rowCount() == 0:
                break
            self._assign_sample_to_row(wheel_row, 0)  # always take first row
        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) available")

    def _clear_row(self, wheel_row: int):
        aid_it = self.modelWheel.item(wheel_row, WL_AID)
        if not aid_it or aid_it.data(Qt.UserRole) is None:
            return   # already empty

        gsid = aid_it.data(Qt.UserRole + 1)
        aid  = aid_it.data(Qt.UserRole)

        # Reload that sample back into available panel
        if gsid is not None:
            self._reload_graphsample_to_avail(gsid, aid)

        # Reset wheel row
        ttype = self.modelWheel.item(wheel_row, WL_TYPE).data(Qt.UserRole)
        auto_label = self.modelWheel.item(wheel_row, WL_SREF).data(Qt.UserRole)
        empty_brush = QBrush(QColor(_TYPE_COLORS.get(ttype, "#FFFFFF")))

        self.modelWheel.item(wheel_row, WL_AID).setText("")
        self.modelWheel.item(wheel_row, WL_AID).setData(None, Qt.UserRole)
        self.modelWheel.item(wheel_row, WL_AID).setData(None, Qt.UserRole + 1)
        self.modelWheel.item(wheel_row, WL_SREF).setText("")
        self.modelWheel.item(wheel_row, WL_MASS).setText("")
        self.modelWheel.item(wheel_row, WL_MASS).setData(None, Qt.UserRole)
        for col in range(self.modelWheel.columnCount()):
            it = self.modelWheel.item(wheel_row, col)
            if it:
                it.setBackground(empty_brush)

        self._update_stats()
        self.lblAvailCount.setText(f"{self.modelAvail.rowCount()} sample(s) available")

    def _clear_selected(self):
        rows = sorted(
            {idx.row() for idx in self.tblWheel.selectionModel().selectedRows()},
            reverse=True,
        )
        for r in rows:
            ttype = self.modelWheel.item(r, WL_TYPE).data(Qt.UserRole) if self.modelWheel.item(r, WL_TYPE) else ""
            if ttype == "unknown":
                self._clear_row(r)

    def _clear_all(self):
        for info in self._wheel_layout:
            if info["type"] == "unknown":
                self._clear_row(info["row"])

    def _reload_graphsample_to_avail(self, gsid: int, aid: int):
        """Re-fetch one graphsample record and add it back to the available table."""
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("""
                    SELECT gs.graphsampleid, gs.analysisid,
                           a.prefix || '-' || CAST(a.sampleid AS TEXT),
                           s.sname, gs.graphitemass_mg, gs.isbypass
                    FROM ams.graphsample gs
                    JOIN public.analysis a ON a.analysisid = gs.analysisid
                    JOIN public.sample   s ON s.sampleid = a.sampleid AND s.prefix = a.prefix
                    WHERE gs.graphsampleid = :gsid
                """), {"gsid": gsid}).fetchone()
        except Exception as exc:
            log.error("Reload graphsample %d: %s", gsid, exc)
            return
        if not r:
            return
        gsid2, aid2, sref, sname, mass, bypass = r
        items = [
            QStandardItem(str(gsid2)),
            QStandardItem(str(aid2)),
            QStandardItem(sref or ""),
            QStandardItem(sname or ""),
            QStandardItem(f"{float(mass):.3f}" if mass else ""),
            QStandardItem("Direct" if bypass else "Graphitised"),
        ]
        items[AV_GSID].setData(gsid2, Qt.UserRole)
        items[AV_AID].setData(aid2,  Qt.UserRole)
        self.modelAvail.insertRow(0, items)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def _update_stats(self):
        total    = self.modelWheel.rowCount()
        unknowns = sum(1 for inf in self._wheel_layout if inf["type"] == "unknown")
        filled   = sum(
            1 for inf in self._wheel_layout
            if inf["type"] == "unknown"
            and self.modelWheel.item(inf["row"], WL_AID)
            and self.modelWheel.item(inf["row"], WL_AID).data(Qt.UserRole) is not None
        )
        self.lblStats.setText(
            f"Total: {total} positions  |  Sample slots: {unknowns}  |  Filled: {filled}"
        )

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save_run(self):
        code = self.txtRunCode.text().strip()
        if not code:
            show_message(self, "Validation", "Run Code is required.", QMessageBox.Warning)
            return
        eq_id = self.cmbEquip.currentData()
        if eq_id is None:
            show_message(self, "Validation", "Please select an AMS machine.", QMessageBox.Warning)
            return
        if self.modelWheel.rowCount() == 0:
            show_message(self, "Validation", "Load a wheel template first.", QMessageBox.Warning)
            return

        # Collect wheel rows
        targets: list[dict] = []
        type_counters: dict[str, int] = {}
        for info in self._wheel_layout:
            row   = info["row"]
            pos   = info["pos"]
            ttype = info["type"]
            type_counters[ttype] = type_counters.get(ttype, 0) + 1

            aid_it  = self.modelWheel.item(row, WL_AID)
            mass_it = self.modelWheel.item(row, WL_MASS)

            aid   = aid_it.data(Qt.UserRole)  if aid_it  else None
            gsid  = aid_it.data(Qt.UserRole + 1) if aid_it else None
            mass  = mass_it.data(Qt.UserRole) if mass_it else None
            sref_it = self.modelWheel.item(row, WL_SREF)
            label = sref_it.data(Qt.UserRole) if sref_it else f"POS-{pos}"
            if sref_it and sref_it.text():
                label = sref_it.text().split("  ")[0].strip() or label

            targets.append({
                "pos": pos, "type": ttype, "label": label,
                "aid": aid, "gsid": gsid, "mass": mass,
            })

        run_date  = self.dtRunDate.date().toPyDate()
        tech_id   = self.cmbTech.currentData()
        notes_txt = self.txtNotes.toPlainText().strip() or None
        wheel_num = self.spnWheel.value()
        now       = datetime.now()
        user      = normalize_login_name(get_current_user_id())

        try:
            with db_manager.get_connection() as conn:
                # 1. ams.amsrun
                run_row = conn.execute(text("""
                    INSERT INTO ams.amsrun
                        (runcode, rundate, equipmentid, technicianid,
                         notes, runstatus, islocked,
                         createdatestamp, createuserstamp)
                    VALUES
                        (:code, :dt, :eid, :tid,
                         :notes, 0, FALSE,
                         :now, :user)
                    RETURNING amsrunid
                """), {
                    "code": code, "dt": run_date,
                    "eid": eq_id, "tid": tech_id,
                    "notes": notes_txt, "now": now, "user": user,
                }).fetchone()
                run_id = run_row[0]

                # 2. ams.amswheel
                wheel_row = conn.execute(text("""
                    INSERT INTO ams.amswheel
                        (amsrunid, wheelnumber, wheellabel, loaddate)
                    VALUES
                        (:rid, :wnum, :wlabel, :dt)
                    RETURNING amswheelid
                """), {
                    "rid": run_id, "wnum": wheel_num,
                    "wlabel": f"Wheel-{wheel_num}", "dt": run_date,
                }).fetchone()
                wheel_id = wheel_row[0]

                # 3. ams.amstarget  (one per position)
                for t in targets:
                    conn.execute(text("""
                        INSERT INTO ams.amstarget
                            (amswheelid, wheelposition, targetlabel, targettype,
                             analysisid, graphsampleid, samplemass_mg)
                        VALUES
                            (:wid, :pos, :lbl, :typ,
                             :aid, :gsid, :mass)
                    """), {
                        "wid":  wheel_id,
                        "pos":  t["pos"],
                        "lbl":  t["label"],
                        "typ":  t["type"],
                        "aid":  t["aid"],
                        "gsid": t["gsid"],
                        "mass": t["mass"],
                    })

                conn.commit()
                self.new_run_id = run_id

            log.info("Created AMS run %d (%s) with %d targets", run_id, code, len(targets))
            show_message(self, "Run Created",
                         f"AMS Run {run_id} ({code}) created with {len(targets)} positions.")
            self.accept()

        except Exception as exc:
            log.error("AMSCreateRunDialog._save_run: %s", exc)
            if "uq_amsrun_runcode" in str(exc).lower():
                show_message(self, "Duplicate Code",
                             f"Run code '{code}' already exists.", QMessageBox.Warning)
            else:
                show_message(self, "Error", str(exc), QMessageBox.Critical)
