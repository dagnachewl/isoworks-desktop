"""
trims_lsc_create_run_gui.py — TRIMS LSC run creation dialog for IsoWorks.
Provides a QDialog for assembling a new LSC liquid scintillation counting run
by selecting a procedure, assigning sample types to tray positions, and saving.
"""
from __future__ import annotations
import sys
import csv
import logging
from datetime import datetime

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QComboBox, QLineEdit, QPushButton,
    QTableView, QAbstractItemView, QHeaderView,
    QMessageBox, QSplitter, QWidget, QFileDialog,
    QFormLayout, QCheckBox
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush
from PyQt5.QtCore import Qt

from sqlalchemy import text
from db_core import db_manager
from shared_utils import get_current_user_id
from gui_utils import show_message
from help_browser import make_help_button

# ---------------------------------------------------------------------------
# Sample type constants — sourced from tblSampleType (MediaCode=200)
# strShortDescription used as TYPE_NAMES values.
# TYPE_UNKNOWN (0) is the load-list sentinel for an unfilled position.
# ---------------------------------------------------------------------------
TYPE_UNKNOWN = 0
TYPE_NSTD2   = -3   # D2 Diluted NIST Standard
TYPE_NSTD1   = -2   # D1 Diluted NIST Standard
TYPE_NIST    = -1   # NIST Parent Standard
TYPE_DW      =  1   # Dead Water / Blank
TYPE_STD     =  2   # Standard
TYPE_SPKEN   =  3   # Spike for Enrichment
TYPE_SPKUN   =  4   # Spike for LSC
TYPE_AIR     =  5   # Lab Air moisture
TYPE_DWEN    =  6   # Dead Water for Enrichment
TYPE_TAPW    =  7   # Tap Water
TYPE_HDXAS   =  8   # HIDEX Active Standard
TYPE_HDXBG   =  9   # Hidex Blank Sample
TYPE_CTRL    = 10   # QC Sample

# Background colour per type — unknowns filled turn water-blue at runtime
TYPE_COLORS = {
    TYPE_UNKNOWN: "#FFFFFF",   # white          — empty slot
    TYPE_NSTD2:   "#EDE7F6",   # deep-lavender  — diluted NIST D2
    TYPE_NSTD1:   "#E8EAF6",   # indigo-tint    — diluted NIST D1
    TYPE_NIST:    "#E3F2FD",   # sky-blue       — NIST parent
    TYPE_DW:      "#BBDEFB",   # blue           — Dead Water / Blank
    TYPE_STD:     "#FFF3E0",   # amber-tint     — standard
    TYPE_SPKEN:   "#FCE4EC",   # rose           — enrichment spike
    TYPE_SPKUN:   "#F3E5F5",   # lavender       — LSC spike
    TYPE_AIR:     "#E8F5E9",   # mint-green     — lab air
    TYPE_DWEN:    "#E0F7FA",   # cyan-tint      — DW for enrichment
    TYPE_TAPW:    "#F1F8E9",   # light-lime     — tap water
    TYPE_HDXAS:   "#FFFDE7",   # pale-yellow    — HIDEX active std
    TYPE_HDXBG:   "#F5F5F5",   # light-grey     — HIDEX blank
    TYPE_CTRL:    "#E0F2F1",   # teal-tint      — QC control
}

# Display names — strShortDescription from tblSampleType
TYPE_NAMES = {
    TYPE_UNKNOWN: "Unknown",
    TYPE_NSTD2:   "NSTD2",
    TYPE_NSTD1:   "NSTD1",
    TYPE_NIST:    "NIST",
    TYPE_DW:      "DW",
    TYPE_STD:     "STD",
    TYPE_SPKEN:   "SPKEN",
    TYPE_SPKUN:   "SPKUN",
    TYPE_AIR:     "AIR",
    TYPE_DWEN:    "DWEN",
    TYPE_TAPW:    "TAPW",
    TYPE_HDXAS:   "HDXAS",
    TYPE_HDXBG:   "HDXBG",
    TYPE_CTRL:    "CTRL",
}

# Load-list column indices
# Pos | Tray | SampleID (J-10001) | AnalysisID | Display Name | (btn)
LL_COL_POS  = 0   # Run order (sequential position)
LL_COL_TRAY = 1   # Vial # = TrayNumber-PositionInTray
LL_COL_SID  = 2   # formatted sample ID, e.g. "J-10001"
LL_COL_AID  = 3   # AnalysisID (blank for controls)
LL_COL_NAME = 4   # "Type Sample Name"; t_type stored in Qt.UserRole
LL_COL_BTN  = 5   # per-row Clear / Delete widget

# Shared table stylesheet (mirrors electrolysis_create_run)
_TABLE_SS = (
    "QTableView { gridline-color: #deeeff; border: 1px solid #b3d4f5; }"
    "QTableView::item:selected { background: #e1f5fe; color: #01579b; }"
    "QHeaderView::section { background: #e8f4fd; color: #37474f;"
    " padding: 2px 5px; border: none;"
    " border-right: 1px solid #cce5ff;"
    " border-bottom: 2px solid #90caf9;"
    " font-weight: 600; }"
)

# Per-row button styles (mirror electrolysis_create_run)
_BTN_BASE = (
    "QPushButton { background:#f5f5f5; color:#78909c;"
    " border:1px solid #e0e0e0; border-radius:3px;"
    " padding:0 5px; font-size:11px; font-weight:500; }"
)
_BTN_CLEAR_H = (
    "QPushButton:hover { background:#fff3e0; color:#e65100; border-color:#e65100; }"
    "QPushButton:pressed { background:#ffe0b2; color:#bf360c; border-color:#bf360c; }"
)
_BTN_DELETE_H = (
    "QPushButton:hover { background:#ffebee; color:#c62828; border-color:#ef9a9a; }"
    "QPushButton:pressed { background:#ffcdd2; color:#b71c1c; border-color:#c62828; }"
)


class TrimsLSCCreateRunDialog(QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create LSC Run")
        self.resize(1200, 780)

        self.run_id = None
        self.load_list_data = []   # list of dicts representing the tray
        self._vial_capacity = 20   # per-tray vial count, updated when procedure/counter is set

        self._init_ui()
        self._load_lookups()
        self._generate_next_run_id()
        self._load_available_samples()

    # =========================================================================
    # UI Construction
    # =========================================================================
    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(6)

        # ── Top bar: title + RunID (read-only) on left, buttons on right ─────
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("<b>Create LSC Run:</b>"))

        self.txtRunID = QLineEdit()
        self.txtRunID.setReadOnly(True)
        self.txtRunID.setFixedWidth(90)
        self.txtRunID.setStyleSheet("font-weight: bold; background: #ecf0f1;")
        top_bar.addWidget(self.txtRunID)
        top_bar.addStretch()

        self.btnReset = QPushButton("Reset Template")
        self.btnReset.clicked.connect(self._on_procedure_changed)

        self.btnSave = QPushButton("Create Run && Export CSV")
        self.btnSave.setStyleSheet(
            "background-color: #27ae60; color: white;"
            " font-weight: bold; padding: 4px 12px;"
        )
        self.btnSave.clicked.connect(self._save_run)
        self.btnSave.setEnabled(False)

        self.btnClose = QPushButton("Close")
        self.btnClose.clicked.connect(self.reject)

        for btn in (self.btnReset, self.btnSave, self.btnClose):
            top_bar.addWidget(btn)
        top_bar.addWidget(make_help_button(self, "trims_create_lsc"))

        main_layout.addLayout(top_bar)

        # ── Main splitter ─────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Horizontal)

        # --- LEFT: Run Setup + Available Samples (equal width to right) ------
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setSpacing(6)

        # Run Setup
        setup_grp = QGroupBox("Run Setup")
        setup_form = QFormLayout(setup_grp)
        setup_form.setSpacing(4)
        setup_form.setContentsMargins(8, 6, 8, 6)

        self.cmbWorkflow  = QComboBox()
        self.cmbProcedure = QComboBox()
        self.cmbCounter   = QComboBox()
        self.cmbStatus    = QComboBox()

        self.cmbWorkflow.currentIndexChanged.connect(self._on_workflow_changed)
        self.cmbProcedure.currentIndexChanged.connect(self._on_procedure_changed)
        self.cmbCounter.currentIndexChanged.connect(self._on_procedure_changed)
        self.cmbStatus.currentIndexChanged.connect(self._on_status_filter_changed)

        setup_form.addRow("Workflow:",  self.cmbWorkflow)
        setup_form.addRow("Procedure:", self.cmbProcedure)
        setup_form.addRow("Counter:",   self.cmbCounter)
        setup_form.addRow("Samples:",   self.cmbStatus)

        left_layout.addWidget(setup_grp)

        # Available Samples
        avail_grp  = QGroupBox("Available Samples")
        avail_vbox = QVBoxLayout(avail_grp)
        avail_vbox.setSpacing(4)
        avail_vbox.setContentsMargins(6, 6, 6, 6)

        self.tblAvailable   = QTableView()
        self.modelAvailable = QStandardItemModel()
        self.modelAvailable.setHorizontalHeaderLabels(
            ["Sample ID", "Sample Name", "Analysis ID", "Status"]
        )
        self.tblAvailable.setModel(self.modelAvailable)
        self.tblAvailable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblAvailable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvailable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblAvailable.setAlternatingRowColors(True)
        self.tblAvailable.verticalHeader().setVisible(False)
        self.tblAvailable.verticalHeader().setDefaultSectionSize(22)
        self.tblAvailable.horizontalHeader().setStretchLastSection(True)
        self.tblAvailable.setStyleSheet(_TABLE_SS)
        avail_vbox.addWidget(self.tblAvailable, 1)

        avail_bottom = QHBoxLayout()
        self.lblAvailCount = QLabel("0 samples")
        avail_bottom.addWidget(self.lblAvailCount)
        avail_bottom.addStretch()
        self.btnRefresh = QPushButton("Refresh")
        self.btnRefresh.clicked.connect(self._load_available_samples)
        avail_bottom.addWidget(self.btnRefresh)
        avail_vbox.addLayout(avail_bottom)

        left_layout.addWidget(avail_grp, 1)
        splitter.addWidget(left_widget)

        # --- CENTER: Action buttons ------------------------------------------
        center_widget  = QWidget()
        center_layout  = QVBoxLayout(center_widget)
        center_layout.addStretch()

        def _mid_btn(label, tip):
            b = QPushButton(label)
            b.setFixedWidth(44)
            b.setToolTip(tip)
            return b

        self.btnAdd       = _mid_btn(">",  "Add selected samples to first empty slots")
        self.btnAddAll    = _mid_btn(">>", "Add ALL available samples to empty slots")
        self.btnRemove    = _mid_btn("<",   "Clear selected load-list slots")
        self.btnRemoveAll = _mid_btn("<<",  "Clear ALL filled Unknown slots")

        self.btnAdd.clicked.connect(self._add_samples_to_loadlist)
        self.btnAddAll.clicked.connect(self._add_all_samples)
        self.tblAvailable.doubleClicked.connect(lambda _: self._add_samples_to_loadlist())
        self.btnRemove.clicked.connect(self._clear_selected_slots)
        self.btnRemoveAll.clicked.connect(self._clear_all_unknowns)

        center_layout.addWidget(self.btnAdd)
        center_layout.addWidget(self.btnAddAll)
        center_layout.addSpacing(16)
        center_layout.addWidget(self.btnRemove)
        center_layout.addWidget(self.btnRemoveAll)
        center_layout.addStretch()
        splitter.addWidget(center_widget)

        # --- RIGHT: Load List (equal width to left) ---------------------------
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setSpacing(4)

        load_grp  = QGroupBox("Load List / Tray Configuration")
        load_vbox = QVBoxLayout(load_grp)
        load_vbox.setSpacing(4)
        load_vbox.setContentsMargins(6, 6, 6, 6)

        self.tblLoadList   = QTableView()
        self.modelLoadList = QStandardItemModel()
        self.modelLoadList.setHorizontalHeaderLabels(
            ["Order", "Vial #", "Sample ID", "Analysis ID", "Name", ""]
        )
        self.tblLoadList.setModel(self.modelLoadList)
        self.tblLoadList.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblLoadList.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblLoadList.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblLoadList.setAlternatingRowColors(False)
        self.tblLoadList.verticalHeader().setVisible(False)
        self.tblLoadList.verticalHeader().setDefaultSectionSize(22)
        self.tblLoadList.setStyleSheet(_TABLE_SS)
        load_vbox.addWidget(self.tblLoadList, 1)

        self.btnAddCtrl = QPushButton("Add Ctrl Sample…")
        self.btnAddCtrl.setToolTip(
            "Assign an active Reference Control to a position in the load list."
        )
        self.btnAddCtrl.clicked.connect(self.open_add_ctrl_dialog)
        self.btnAddCtrl.setEnabled(False)
        load_vbox.addWidget(self.btnAddCtrl)

        load_bottom = QHBoxLayout()
        self.lblStats  = QLabel("Total: 0 | Unknowns: 0 | Filled: 0")
        self.lblStatus = QLabel("Select a Procedure to initialise the Load List.")
        load_bottom.addWidget(self.lblStats)
        load_bottom.addStretch()
        load_bottom.addWidget(self.lblStatus)
        load_vbox.addLayout(load_bottom)

        right_layout.addWidget(load_grp, 1)
        splitter.addWidget(right_widget)

        # Equal stretch for left and right; center collapses to minimum
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 4)
        main_layout.addWidget(splitter, 1)

    # =========================================================================
    # Load-list column widths  (called after template rebuild)
    # =========================================================================
    def _apply_loadlist_column_widths(self):
        hdr = self.tblLoadList.horizontalHeader()
        hdr.setSectionResizeMode(LL_COL_POS,  QHeaderView.Fixed)
        self.tblLoadList.setColumnWidth(LL_COL_POS,  46)
        hdr.setSectionResizeMode(LL_COL_TRAY, QHeaderView.Fixed)
        self.tblLoadList.setColumnWidth(LL_COL_TRAY, 52)
        hdr.setSectionResizeMode(LL_COL_SID,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(LL_COL_AID,  QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(LL_COL_NAME, QHeaderView.Stretch)
        hdr.setSectionResizeMode(LL_COL_BTN,  QHeaderView.Fixed)
        self.tblLoadList.setColumnWidth(LL_COL_BTN, 60)

    # =========================================================================
    # Data Loading
    # =========================================================================
    def _load_lookups(self):
        try:
            with db_manager.get_connection() as conn:
                self.cmbWorkflow.addItem("- Select -", None)
                sql_wf = """
                    SELECT DISTINCT w.WorkflowID, w.WorkflowName
                    FROM Workflow w
                    JOIN WorkflowJob wj ON w.WorkflowID = wj.WorkflowID
                    WHERE wj.JobName LIKE 'LSC%'
                    ORDER BY w.WorkflowID
                """
                for r in conn.execute(text(sql_wf)):
                    self.cmbWorkflow.addItem(r.WorkflowName, r.WorkflowID)

                if self.cmbWorkflow.count() > 1:
                    self.cmbWorkflow.setCurrentIndex(1)

            # Sample-source filter — no "All" option; only meaningful statuses
            self.cmbStatus.addItem("5 - Ready (Enriched)",  5)
            self.cmbStatus.addItem("3 - Distilled (Direct)", 3)
            self.cmbStatus.addItem("From existing run…",     0)

        except Exception as e:
            logging.error(f"_load_lookups error: {e}")

    def _on_workflow_changed(self):
        wf_id = self.cmbWorkflow.currentData()
        if not wf_id:
            return

        self.cmbCounter.clear()
        self.cmbProcedure.clear()

        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text(
                    "SELECT EquipmentID, EquipmentName "
                    "FROM Equipment "
                    f"WHERE CategoryID=3 AND IsObsolete = {db_manager.sql_bool(False)}"
                )):
                    self.cmbCounter.addItem(r.EquipmentName, r.EquipmentID)

                for r in conn.execute(text(
                    "SELECT ProcedureID, ProcedureName "
                    "FROM AnalysisProcedure "
                    f"WHERE CategoryID=3 AND IsObsolete = {db_manager.sql_bool(False)}"
                )):
                    self.cmbProcedure.addItem(r.ProcedureName, r.ProcedureID)

                row = conn.execute(text("""
                    SELECT ProcedureID
                    FROM WorkflowJob
                    WHERE WorkflowID = :wid
                      AND JobName LIKE 'LSC%'
                """), {"wid": wf_id}).fetchone()

            if row and row[0]:
                idx = self.cmbProcedure.findData(row[0])
                if idx >= 0:
                    self.cmbProcedure.blockSignals(True)
                    self.cmbProcedure.setCurrentIndex(idx)
                    self.cmbProcedure.blockSignals(False)

            self._on_procedure_changed()

        except Exception as e:
            logging.error(f"Workflow change error: {e}")
            self._load_available_samples()

    def _on_status_filter_changed(self):
        mode = self.cmbStatus.currentData()
        if mode == 0:
            self._load_samples_from_run()
        else:
            self._load_available_samples()

    def _generate_next_run_id(self):
        """Shows estimated next RunID (informational only; actual ID assigned by DB at save time)."""
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(
                    text("SELECT MAX(RunID) FROM TRIMS.LSCRun")
                ).scalar()
            self.txtRunID.setText(f"~{(res or 1000) + 1}")
        except Exception as e:
            logging.error(f"_generate_next_run_id: {e}")
            self.txtRunID.setText("~1001")

    def _load_available_samples(self):
        """Populate Available table based on cmbStatus filter.

        Column order: Sample ID | Sample Name | Analysis ID | Status (numeric)
        Status=3 (Distilled/Direct) excludes samples whose workflow has an
        Electrolysis_Enrichment job — they are not direct-count samples.
        """
        self.modelAvailable.removeRows(0, self.modelAvailable.rowCount())

        status = self.cmbStatus.currentData()
        if status == 0:
            return   # "From existing run" — filled by _load_samples_from_run

        try:
            with db_manager.get_connection() as conn:
                if status == 3:
                    # Direct-count distilled: exclude enrichment workflows
                    sql = """
                        SELECT a.AnalysisID, s.sName, s.Prefix, a.SampleID, a.Status
                        FROM Analysis a
                        JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                        WHERE a.Status = 3
                          AND (a.WorkflowID IS NULL OR a.WorkflowID NOT IN (
                              SELECT DISTINCT WorkflowID FROM WorkflowJob
                              WHERE JobName LIKE 'Electrolysis_Enrichment%'
                          ))
                        ORDER BY s.Prefix, a.SampleID, s.sName, a.AnalysisID
                    """
                    rows = conn.execute(text(sql)).fetchall()
                else:
                    sql = """
                        SELECT a.AnalysisID, s.sName, s.Prefix, a.SampleID, a.Status
                        FROM Analysis a
                        JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                        WHERE a.Status = :st
                        ORDER BY s.Prefix, a.SampleID, s.sName, a.AnalysisID
                    """
                    rows = conn.execute(text(sql), {"st": status}).fetchall()

            for r in rows:
                full_id = f"{r.Prefix}-{r.SampleID}"
                items = [
                    QStandardItem(full_id),           # Sample ID  (col 0)
                    QStandardItem(r.sName),            # Sample Name
                    QStandardItem(str(r.AnalysisID)),  # Analysis ID
                    QStandardItem(str(r.Status)),      # Status numeric
                ]
                # UserRole data on col 0 (read by _fill_slots)
                items[0].setData(r.AnalysisID, Qt.UserRole)       # AnalysisID
                items[0].setData(full_id,       Qt.UserRole + 1)   # formatted SID
                items[0].setData(r.sName,       Qt.UserRole + 2)   # sample name
                self.modelAvailable.appendRow(items)

            self.tblAvailable.resizeColumnsToContents()
            self._update_avail_count()

        except Exception as e:
            logging.error(f"Error loading available samples: {e}")
            self.lblStatus.setText("Error loading samples.")

    def _load_samples_from_run(self):
        """Ask for an existing RunID and populate Available with every sample
        that was in that run and may need to be re-run:

        • TYPE_UNKNOWN slots that had an AnalysisID assigned (the common case)
        • Non-Unknown slots whose Analysis was enriched (Status ≥ 5) or
          was run as unknown for whatever reason (i.e. any assigned sample,
          regardless of slot type)

        The new RunID is NOT changed — it always stays as the auto-generated
        next ID.  UserRole+3 stores the original LSCLoadList SampleType so
        the caller can decide whether to treat it as Unknown or keep its type.
        """
        self.modelAvailable.removeRows(0, self.modelAvailable.rowCount())

        run_id, ok = _ask_run_id(self)
        if not ok or run_id is None:
            return

        try:
            with db_manager.get_connection() as conn:
                # Two groups of samples to repeat:
                #   1. TYPE_UNKNOWN slots — the regular unknown samples
                #   2. Non-Unknown slots (DWEN, SPKEN, …) that were enriched,
                #      identified by a matching TRIMS.Electrolysis row
                rows = conn.execute(text("""
                    SELECT a.AnalysisID, s.sName, s.Prefix, a.SampleID,
                           a.Status, ll.SampleType AS orig_type
                    FROM   TRIMS.LSCLoadList ll
                    JOIN   Analysis a ON ll.AnalysisID = a.AnalysisID
                    JOIN   Sample   s ON a.SampleID = s.SampleID
                                     AND a.Prefix   = s.Prefix
                    LEFT JOIN TRIMS.Electrolysis e ON e.AnalysisID = a.AnalysisID
                    WHERE  ll.RunID        = :rid
                      AND  ll.AnalysisID IS NOT NULL
                      AND (
                            ll.SampleType = :unk              -- all unknown samples
                          OR e.AnalysisID IS NOT NULL          -- enriched non-unknowns
                          )
                    ORDER  BY ll.SampleType, s.Prefix, a.SampleID, s.sName
                """), {"rid": run_id, "unk": TYPE_UNKNOWN}).fetchall()

            unk_count   = 0
            other_count = 0
            for r in rows:
                full_id   = f"{r.Prefix}-{r.SampleID}"
                orig_type = r.orig_type   # preserve for display / tooltip

                # Source label: "Unk" or the type short name
                if orig_type == TYPE_UNKNOWN:
                    src_label = f"Run {run_id} UNK"
                    unk_count += 1
                else:
                    type_name = TYPE_NAMES.get(orig_type, str(orig_type))
                    src_label = f"Run {run_id} [{type_name}]"
                    other_count += 1

                items = [
                    QStandardItem(full_id),             # Sample ID
                    QStandardItem(r.sName),             # Sample Name
                    QStandardItem(str(r.AnalysisID)),   # Analysis ID
                    QStandardItem(src_label),           # source / origin
                ]
                items[0].setData(r.AnalysisID, Qt.UserRole)
                items[0].setData(full_id,       Qt.UserRole + 1)
                items[0].setData(r.sName,       Qt.UserRole + 2)
                items[0].setData(orig_type,     Qt.UserRole + 3)  # original slot type
                self.modelAvailable.appendRow(items)

            self.tblAvailable.resizeColumnsToContents()
            self._update_avail_count()
            self.lblStatus.setText(
                f"Run {run_id}: {unk_count} unknown + {other_count} control sample(s) loaded."
            )

        except Exception as e:
            logging.error(f"_load_samples_from_run: {e}")

    def _update_avail_count(self):
        n = self.modelAvailable.rowCount()
        self.lblAvailCount.setText(f"{n} sample{'s' if n != 1 else ''}")

    # =========================================================================
    # Template Logic
    # =========================================================================
    def _load_reference_controls(self, conn):
        """Return {intSampleType: (SampleID, Prefix, sName)} for every active
        ReferenceControl with Prefix='J'.  One entry per type (first found)."""
        ctrl_map = {}
        try:
            rows = conn.execute(text("""
                SELECT s.SampleType, rc.SampleID, rc.Prefix, s.sName
                FROM   ReferenceControl rc
                JOIN   Sample s ON rc.SampleID = s.SampleID AND rc.Prefix = s.Prefix
                WHERE  rc.Prefix = 'J'
                  AND  rc.AvailableDateTo IS NULL
                ORDER  BY s.SampleType, rc.SampleID
            """)).fetchall()
            for r in rows:
                if r.SampleType not in ctrl_map:          # keep first / lowest ID
                    ctrl_map[r.SampleType] = (r.SampleID, r.Prefix, r.sName)
        except Exception as e:
            logging.error(f"_load_reference_controls: {e}")
        return ctrl_map

    def _on_procedure_changed(self):
        proc_id = self.cmbProcedure.currentData()
        if not proc_id:
            return

        sample_size   = None
        ctrl_map      = {}
        vial_capacity = 20
        tmpl_rows     = []
        try:
            with db_manager.get_connection() as conn:
                proc_row = conn.execute(text("""
                    SELECT lp.SampleSize
                    FROM   AnalysisProcedure ap
                    LEFT JOIN LSCProcedure lp ON lp.ProcedureID = ap.ProcedureID
                    WHERE  ap.ProcedureID = :pid
                """), {"pid": proc_id}).fetchone()
                if proc_row:
                    sample_size = proc_row.SampleSize
                eid = self.cmbCounter.currentData()
                if eid:
                    _tc = conn.execute(
                        text('SELECT vialcapacity FROM public.equipmenttrayconfig WHERE equipmentid=:eid'),
                        {"eid": eid}
                    ).fetchone()
                    if _tc:
                        vial_capacity = int(_tc.vialcapacity)
                tmpl_rows = conn.execute(text("""
                    SELECT ordinalposition, sampletype, portrefempty, trayno, vialno
                    FROM   public.analysisprocedure_template
                    WHERE  procedureid = :pid
                      AND  (sampletype IS NOT NULL OR portrefempty IS NOT NULL)
                    ORDER BY ordinalposition
                """), {"pid": proc_id}).fetchall()
                ctrl_map = self._load_reference_controls(conn)
        except Exception as e:
            logging.error(f"_on_procedure_changed DB error: {e}")
            return

        self._vial_capacity = vial_capacity

        if not tmpl_rows:
            self.lblStatus.setText("No template configured for this procedure — use the LSC Template Editor.")
            return

        self.load_list_data = []
        self.modelLoadList.clear()
        self.modelLoadList.setHorizontalHeaderLabels(
            ["Order", "Vial #", "Sample ID", "Analysis ID", "Name", ""]
        )

        for r in tmpl_rows:
            t_type      = r.sampletype if r.sampletype is not None else (r.portrefempty or 0)
            pos_counter = r.ordinalposition
            tray_num    = r.trayno    if r.trayno    is not None else (pos_counter - 1) // vial_capacity + 1
            pos_in_tray = r.vialno   if r.vialno    is not None else (pos_counter - 1) % vial_capacity + 1
            row_data = {
                "pos":         pos_counter,
                "tray_num":    tray_num,
                "pos_in_tray": pos_in_tray,
                "type":        t_type,
                "analysis_id": None,
                "sample_name": "",
                "full_sid":    None,
                "ctrl_sid":    None,
                "ctrl_prefix": None,
                "sample_size": sample_size,
            }
            if t_type != TYPE_UNKNOWN:
                if t_type in ctrl_map:
                    sid, pfx, sname = ctrl_map[t_type]
                    row_data["sample_name"] = sname
                    row_data["ctrl_sid"]    = sid
                    row_data["ctrl_prefix"] = pfx
                else:
                    row_data["sample_name"] = f"[{TYPE_NAMES.get(t_type, 'Control')}]"

            self.load_list_data.append(row_data)
            self._append_loadlist_row(row_data)

        self._apply_loadlist_column_widths()
        self._refresh_all_buttons()
        self.btnSave.setEnabled(True)
        self.btnAddCtrl.setEnabled(True)
        self._update_stats()
        ctrl_filled = sum(1 for r in self.load_list_data
                          if r["type"] != TYPE_UNKNOWN and r["ctrl_sid"] is not None)
        self.lblStatus.setText(
            f"Template loaded — {len(tmpl_rows)} positions, "
            f"{ctrl_filled} control(s) auto-populated."
        )

    def _append_loadlist_row(self, r_data):
        t_type    = r_data["type"]
        bg_color  = QColor(TYPE_COLORS.get(t_type, "#FFFFFF"))
        brush     = QBrush(bg_color)
        type_tag  = TYPE_NAMES.get(t_type, str(t_type))

        # SampleID column: "J-SID" for controls, empty for unknown until filled
        if t_type != TYPE_UNKNOWN and r_data.get("ctrl_sid") is not None:
            sid_str = f"{r_data['ctrl_prefix']}-{r_data['ctrl_sid']}"
        else:
            sid_str = ""

        # Display name: "Type Sample Name" for controls; sample name for unknowns
        if t_type != TYPE_UNKNOWN:
            display = f"[{type_tag}] {r_data['sample_name']}".strip()
        else:
            display = r_data["sample_name"]   # empty until filled

        name_item = QStandardItem(display)
        name_item.setData(t_type, Qt.UserRole)   # type stored for downstream use

        vial_str = f"{r_data.get('tray_num', 1)}-{r_data.get('pos_in_tray', r_data['pos'])}"
        items = [
            QStandardItem(str(r_data["pos"])),
            QStandardItem(vial_str),
            QStandardItem(sid_str),
            QStandardItem(str(r_data["analysis_id"] or "")),
            name_item,
            QStandardItem(""),   # placeholder for button widget
        ]
        for item in items:
            item.setBackground(brush)
            item.setEditable(False)
        self.modelLoadList.appendRow(items)

    def _update_row_colors(self, row_idx: int):
        """Repaint row background to reflect filled/empty state."""
        row_data = self.load_list_data[row_idx]
        t_type   = row_data["type"]
        filled   = row_data["analysis_id"] is not None
        # Filled Unknown → light water-blue; otherwise original type colour
        if t_type == TYPE_UNKNOWN and filled:
            color = QColor("#b3e5fc")
        else:
            color = QColor(TYPE_COLORS.get(t_type, "#FFFFFF"))
        brush = QBrush(color)
        for col in range(LL_COL_BTN):
            it = self.modelLoadList.item(row_idx, col)
            if it:
                it.setBackground(brush)

    def open_add_ctrl_dialog(self):
        dlg = LscAddCtrlSampleDialog(self.load_list_data, self.modelLoadList, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            # Refresh colours and buttons for every touched row
            for r in range(len(self.load_list_data)):
                self._update_row_colors(r)
                self._refresh_row_button(r)
            self._update_stats()

    def _update_stats(self):
        total    = len(self.load_list_data)
        unknowns = sum(1 for r in self.load_list_data if r["type"] == TYPE_UNKNOWN)
        filled   = sum(
            1 for r in self.load_list_data
            if r["type"] == TYPE_UNKNOWN and r["analysis_id"] is not None
        )
        ctrls    = sum(
            1 for r in self.load_list_data
            if r["type"] != TYPE_UNKNOWN and r.get("ctrl_sid") is not None
        )
        self.lblStats.setText(
            f"Total: {total} | Unknowns: {unknowns} | Filled: {filled} | Controls: {ctrls}"
        )

    # =========================================================================
    # Per-row Clear / Delete buttons  (two-stage, mirrors electrolysis)
    # =========================================================================
    def _refresh_row_button(self, row_idx: int):
        """
        Two-stage semantics:
          • Slot is filled (analysis_id OR ctrl_sid set) → "Clear" (orange)
            Unknown slots: returns sample to Available.
            Control slots: resets name to placeholder; does NOT touch Available.
          • Slot is empty → "Delete" (red)
            Asks for confirmation, removes the row from the template.
            Reset Template restores all rows.
        """
        row_data = self.load_list_data[row_idx]
        has_samp = (row_data["analysis_id"] is not None
                    or row_data.get("ctrl_sid") is not None)

        btn = QPushButton()
        btn.setFixedHeight(20)

        if has_samp:
            btn.setText("Clear")
            btn.setStyleSheet(_BTN_BASE + _BTN_CLEAR_H)
            btn.setToolTip(
                "Clear this slot assignment.\n"
                "Unknown samples are returned to the Available list.\n"
                "Control slots revert to their placeholder."
            )
            btn.clicked.connect(
                lambda _checked, r=row_idx: self._on_row_clear(r)
            )
        else:
            btn.setText("Delete")
            btn.setStyleSheet(_BTN_BASE + _BTN_DELETE_H)
            btn.setToolTip(
                "Remove this slot from the run template.\n"
                "Reset Template restores it."
            )
            btn.clicked.connect(
                lambda _checked, r=row_idx: self._on_row_delete(r)
            )

        self.tblLoadList.setIndexWidget(
            self.modelLoadList.index(row_idx, LL_COL_BTN), btn
        )

    def _refresh_all_buttons(self):
        for r in range(len(self.load_list_data)):
            self._refresh_row_button(r)

    def _on_row_clear(self, row_idx: int):
        row_data = self.load_list_data[row_idx]

        if row_data["analysis_id"] is not None:
            # Unknown sample — return to Available list
            self._return_to_available(row_data)
            row_data["analysis_id"] = None
            row_data["sample_name"] = ""
            row_data["full_sid"]    = None
            self.modelLoadList.item(row_idx, LL_COL_SID).setText("")
            self.modelLoadList.item(row_idx, LL_COL_AID).setText("")
            self.modelLoadList.item(row_idx, LL_COL_NAME).setText("")

        elif row_data.get("ctrl_sid") is not None:
            # Control slot — revert to placeholder; do NOT touch Available
            row_data["ctrl_sid"]    = None
            row_data["ctrl_prefix"] = None
            placeholder = f"[{TYPE_NAMES.get(row_data['type'], 'Control')}]"
            row_data["sample_name"] = placeholder
            self.modelLoadList.item(row_idx, LL_COL_SID).setText("")
            self.modelLoadList.item(row_idx, LL_COL_NAME).setText(placeholder)
        else:
            return   # nothing to clear
        self._update_row_colors(row_idx)
        self._refresh_row_button(row_idx)
        self._update_avail_count()
        self._update_stats()

    def _on_row_delete(self, row_idx: int):
        t_type = self.load_list_data[row_idx]["type"]
        label  = TYPE_NAMES.get(t_type, f"Type {t_type}")
        pos    = self.load_list_data[row_idx]["pos"]

        if QMessageBox.question(
            self, "Confirm Delete",
            f"Remove position {pos} ({label}) from the run template?\n"
            "Reset Template restores it.",
            QMessageBox.Yes | QMessageBox.No
        ) == QMessageBox.No:
            return

        self.load_list_data.pop(row_idx)
        self.modelLoadList.removeRow(row_idx)
        # Re-number positions and recompute tray/vial assignments
        vc = self._vial_capacity or 20
        for i, rd in enumerate(self.load_list_data):
            new_pos        = i + 1
            rd["pos"]      = new_pos
            rd["tray_num"] = (new_pos - 1) // vc + 1
            rd["pos_in_tray"] = (new_pos - 1) % vc + 1
            vial_str = f"{rd['tray_num']}-{rd['pos_in_tray']}"
            self.modelLoadList.item(i, LL_COL_POS).setText(str(new_pos))
            self.modelLoadList.item(i, LL_COL_TRAY).setText(vial_str)
        # Recreate all buttons so lambda row indices are correct
        self._refresh_all_buttons()
        self._update_stats()

    # =========================================================================
    # Actions: Add / Remove (bulk)
    # =========================================================================
    def _add_samples_to_loadlist(self):
        indexes = self.tblAvailable.selectionModel().selectedRows()
        if not indexes:
            return
        self._fill_slots(indexes)

    def _add_all_samples(self):
        indexes = [
            self.modelAvailable.index(r, 0)
            for r in range(self.modelAvailable.rowCount())
        ]
        if not indexes:
            return
        self._fill_slots(indexes)

    def _fill_slots(self, indexes):
        avail_idx     = 0
        samples_added = 0

        for i, row_data in enumerate(self.load_list_data):
            if avail_idx >= len(indexes):
                break
            if row_data["type"] == TYPE_UNKNOWN and row_data["analysis_id"] is None:
                src_row  = indexes[avail_idx].row()
                aid      = self.modelAvailable.item(src_row, 0).data(Qt.UserRole)
                full_sid = self.modelAvailable.item(src_row, 0).data(Qt.UserRole + 1)
                sname    = self.modelAvailable.item(src_row, 0).data(Qt.UserRole + 2)

                row_data["analysis_id"] = aid
                row_data["sample_name"] = sname
                row_data["full_sid"]    = full_sid
                row_data["ctrl_sid"]    = None   # unknown slots never hold a ctrl ref

                self.modelLoadList.item(i, LL_COL_SID).setText(full_sid or "")
                self.modelLoadList.item(i, LL_COL_AID).setText(str(aid))
                self.modelLoadList.item(i, LL_COL_NAME).setText(sname)
                self._update_row_colors(i)
                self._refresh_row_button(i)

                avail_idx     += 1
                samples_added += 1

        used_rows = sorted(
            {indexes[i].row() for i in range(avail_idx)}, reverse=True
        )
        for row_idx in used_rows:
            self.modelAvailable.removeRow(row_idx)

        self._update_avail_count()
        self._update_stats()
        self.lblStatus.setText(f"Added {samples_added} sample(s) to the run.")

    def _clear_selected_slots(self):
        indexes = self.tblLoadList.selectionModel().selectedRows()
        if not indexes:
            return

        restored = 0
        for idx in sorted(indexes, key=lambda x: x.row()):
            row_idx  = idx.row()
            row_data = self.load_list_data[row_idx]
            if row_data["type"] == TYPE_UNKNOWN and row_data["analysis_id"] is not None:
                self._on_row_clear(row_idx)
                restored += 1

        self.lblStatus.setText(f"Cleared {restored} slot(s).")

    def _clear_all_unknowns(self):
        restored = 0
        for i, row_data in enumerate(self.load_list_data):
            if row_data["type"] == TYPE_UNKNOWN and row_data["analysis_id"] is not None:
                self._return_to_available(row_data)
                row_data["analysis_id"] = None
                row_data["sample_name"] = ""
                row_data["full_sid"]    = None
                self.modelLoadList.item(i, LL_COL_SID).setText("")
                self.modelLoadList.item(i, LL_COL_AID).setText("")
                self.modelLoadList.item(i, LL_COL_NAME).setText("")
                self._update_row_colors(i)
                self._refresh_row_button(i)
                restored += 1

        self._update_avail_count()
        self._update_stats()
        self.lblStatus.setText(f"Cleared all {restored} Unknown slot(s).")

    def _return_to_available(self, row_data):
        aid      = row_data["analysis_id"]
        full_sid = row_data.get("full_sid") or ""
        sname    = row_data["sample_name"]

        items = [
            QStandardItem(full_sid),          # Sample ID
            QStandardItem(sname),             # Sample Name
            QStandardItem(str(aid)),          # Analysis ID
            QStandardItem("Returned"),        # Status
        ]
        items[0].setData(aid,      Qt.UserRole)
        items[0].setData(full_sid, Qt.UserRole + 1)
        items[0].setData(sname,    Qt.UserRole + 2)
        self.modelAvailable.appendRow(items)

    # =========================================================================
    # Saving
    # =========================================================================
    def _save_run(self):
        try:
            run_id = int(self.txtRunID.text())
        except ValueError:
            show_message(self, "Error", "Invalid Run ID.", QMessageBox.Critical)
            return

        # Warn if no unknown samples have been assigned
        if not any(
            r["analysis_id"] for r in self.load_list_data if r["type"] == TYPE_UNKNOWN
        ):
            if QMessageBox.question(
                self, "Empty Run",
                "No unknown samples assigned. Create run anyway?",
                QMessageBox.Yes | QMessageBox.No
            ) == QMessageBox.No:
                return

        wid  = self.cmbWorkflow.currentData()
        pid  = self.cmbProcedure.currentData()
        eid  = self.cmbCounter.currentData()
        user = get_current_user_id()
        now  = datetime.now()

        try:
            with db_manager.get_connection() as conn:
                # Look up tray capacity for this instrument
                _tc = conn.execute(
                    text('SELECT vialcapacity FROM public.equipmenttrayconfig WHERE equipmentid=:eid'),
                    {"eid": eid}
                ).fetchone()
                vial_capacity = int(_tc.vialcapacity) if _tc else 20

                # ── Resolve an AnalysisID for every load-list position ─────────
                # Rows that already have analysis_id: use it directly (unknown
                # samples from the distillation / repeat-run path).
                # Rows without analysis_id (ctrl_sid set): INSERT a new Analysis row.
                resolved = []   # list of (row_data, final_aid)

                for rd in self.load_list_data:
                    aid = rd["analysis_id"]

                    if aid is not None:
                        # ── Existing Analysis (unknown sample or repeated sample) ─
                        conn.execute(
                            text("UPDATE Analysis SET Status=6 WHERE AnalysisID=:aid"),
                            {"aid": aid}
                        )
                        resolved.append((rd, aid))

                    elif rd.get("ctrl_sid") is not None:
                        # ── Control / standard: create Analysis row, sequence assigns ID ─
                        sid = rd["ctrl_sid"]
                        pfx = rd["ctrl_prefix"]
                        new_aid = conn.execute(text("""
                            INSERT INTO Analysis
                                (SampleID, Prefix, Status, Repeats,
                                 WorkflowID, CreateDateStamp, CreateUserStamp)
                            VALUES (:sid, :pfx, 6, 1, :wid, :now, :user)
                            RETURNING AnalysisID
                        """), {
                            "sid":  sid,
                            "pfx":  pfx,
                            "wid":  wid,
                            "now":  now,
                            "user": user,
                        }).scalar()
                        resolved.append((rd, new_aid))

                    else:
                        # ── Empty/unfilled slot — skip (no sample, no control) ───
                        resolved.append((rd, None))

                # ── Write LSCRun header — sequence assigns RunID ───────────────
                run_id = conn.execute(text("""
                    INSERT INTO TRIMS.LSCRun
                        (WorkflowID, ProcedureID, EquipmentID,
                         RunStartTime, RunStatus, CreateDateStamp, CreateUserStamp)
                    VALUES (:wid, :pid, :eid, :start, :stat, :now, :user)
                    RETURNING RunID
                """), {
                    "wid":   wid,
                    "pid":   pid,
                    "eid":   eid,
                    "start": now,
                    "stat":  4,       # Ongoing
                    "now":   now,
                    "user":  user,
                }).scalar()

                # ── Write LSCLoadList rows ─────────────────────────────────────
                for rd, final_aid in resolved:
                    if final_aid is None:
                        # Truly empty slot — write with NULL only if DB allows it,
                        # otherwise skip.  LSCLoadList requires NOT NULL, so skip.
                        continue
                    pos = rd["pos"]
                    tray_num    = (pos - 1) // vial_capacity + 1
                    pos_in_tray = (pos - 1) % vial_capacity + 1
                    conn.execute(text("""
                        INSERT INTO TRIMS.LSCLoadList
                            (RunID, PositionInRun, TrayNumber, PositionInTray,
                             SampleType, AnalysisID,
                             SampleAmount, CreateDateStamp, CreateUserStamp)
                        VALUES (:rid, :pos, :tray, :pit, :type, :aid, :amt, :now, :user)
                    """), {
                        "rid":  run_id,
                        "pos":  pos,
                        "tray": tray_num,
                        "pit":  pos_in_tray,
                        "type": rd["type"],
                        "aid":  final_aid,
                        "amt":  rd.get("sample_size"),
                        "now":  now,
                        "user": user,
                    })

                conn.commit()

                # Update the model so the AID column reflects newly assigned IDs
                for i, (rd, final_aid) in enumerate(resolved):
                    if final_aid is not None and rd["analysis_id"] is None:
                        rd["analysis_id"] = final_aid
                        self.modelLoadList.item(i, LL_COL_AID).setText(str(final_aid))

            self._export_csv(run_id)
            show_message(
                self, "Success",
                f"Run {run_id} created successfully.",
                QMessageBox.Information
            )
            self.accept()

        except Exception as e:
            logging.error(f"Save Run Failed: {e}")
            show_message(self, "Error", f"Failed to create run:\n{e}", QMessageBox.Critical)

    def _export_csv(self, run_id):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Load List",
            f"RUN_{run_id}_LoadList.csv",
            "CSV Files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Order", "Vial#", "AnalysisID", "SampleName"])
                for r in self.load_list_data:
                    vial = f"{r.get('tray_num', 1)}-{r.get('pos_in_tray', r['pos'])}"
                    aid  = r["analysis_id"] if r["analysis_id"] else ""
                    writer.writerow([r["pos"], vial, aid, r["sample_name"]])
        except Exception as e:
            show_message(
                self, "Warning",
                f"Run saved, but CSV export failed:\n{e}",
                QMessageBox.Warning
            )


# ---------------------------------------------------------------------------
# Add Control Sample dialog — LSC variant
# Source: ReferenceControl WHERE Prefix='J' AND AvailableDateTo IS NULL
# ---------------------------------------------------------------------------
class LscAddCtrlSampleDialog(QDialog):
    """
    Assign an active Reference Control sample to one or more positions
    in the LSC load list.

    "Add as Unknown" mode: the selected reference sample is placed in the
    slot as a TYPE_UNKNOWN entry (analysis_id path, not ctrl_sid).  This is
    for cases where a control sample must be counted blind / without its type
    being recorded in the tray.

    Position range operates on Pos numbers (1-based integers from the template).
    "Fill all non-Unknown empty" fills every control slot that still shows
    the placeholder name (no ctrl_sid assigned yet).
    """

    def __init__(self, load_list_data, load_model, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Control Sample")
        self.resize(520, 280)
        self.load_list_data = load_list_data
        self.load_model     = load_model

        self._init_ui()
        self._load_controls()
        self._load_positions()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setSpacing(6)

        # "Add as Unknown" checkbox sits inline before the sample combo
        self.chkAsUnknown = QCheckBox("Add as Unknown")
        self.chkAsUnknown.setToolTip(
            "Place the selected reference sample into the slot as TYPE_UNKNOWN.\n"
            "Use when a control must be counted blind or without its type recorded."
        )
        self.cmbSample  = QComboBox()
        self.cmbPosFrom = QComboBox()
        self.cmbPosTo   = QComboBox()
        self.chkAll     = QCheckBox("Fill all non-Unknown empty slots")

        # Inline row: chkAsUnknown  cmbSample
        sample_row = QHBoxLayout()
        sample_row.setSpacing(6)
        sample_row.addWidget(self.chkAsUnknown)
        sample_row.addWidget(self.cmbSample, 1)

        form.addRow("Control Sample:", sample_row)
        form.addRow("Position From:",  self.cmbPosFrom)
        form.addRow("Position To:",    self.cmbPosTo)
        form.addRow("",                self.chkAll)
        layout.addLayout(form)
        layout.addStretch()

        btns = QHBoxLayout()
        self.btnApply = QPushButton("Apply")
        self.btnApply.clicked.connect(self._apply)
        self.btnClose = QPushButton("Close")
        self.btnClose.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.btnApply)
        btns.addWidget(self.btnClose)
        layout.addLayout(btns)

        self.chkAll.toggled.connect(self._toggle_pos)
        self.chkAsUnknown.toggled.connect(self._toggle_as_unknown)

    def _toggle_pos(self, checked):
        self.cmbPosFrom.setEnabled(not checked)
        self.cmbPosTo.setEnabled(not checked)

    def _toggle_as_unknown(self, checked):
        """When 'Add as Unknown' is on, 'Fill all' makes no sense — disable it."""
        if checked:
            self.chkAll.setChecked(False)
        self.chkAll.setEnabled(not checked)

    def _load_controls(self):
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT rc.SampleID, rc.Prefix, s.sName, s.SampleType,
                           st.strShortDescription
                    FROM   ReferenceControl rc
                    JOIN   Sample s
                             ON rc.SampleID = s.SampleID AND rc.Prefix = s.Prefix
                    LEFT JOIN tblSampleType st
                             ON st.intSampleType = s.SampleType
                    WHERE  rc.Prefix = 'J'
                      AND  rc.AvailableDateTo IS NULL
                    ORDER  BY s.SampleType, rc.SampleID
                """)).fetchall()

            for r in rows:
                type_lbl = r.strShortDescription or str(r.SampleType)
                label    = f"{r.Prefix}-{r.SampleID}  {r.sName}  [{type_lbl}]"
                self.cmbSample.addItem(label, {
                    "sid":    r.SampleID,
                    "prefix": r.Prefix,
                    "sname":  r.sName,
                    "type":   r.SampleType,
                })
        except Exception as e:
            logging.error(f"LscAddCtrlSampleDialog._load_controls: {e}")

    def _load_positions(self):
        self.cmbPosFrom.clear()
        self.cmbPosTo.clear()
        for rd in self.load_list_data:
            pos_str = str(rd["pos"])
            self.cmbPosFrom.addItem(pos_str)
            self.cmbPosTo.addItem(pos_str)
        if self.cmbPosTo.count() > 0:
            self.cmbPosTo.setCurrentIndex(self.cmbPosTo.count() - 1)

    def _apply(self):
        data = self.cmbSample.currentData()
        if not data:
            return

        sid       = data["sid"]
        pfx       = data["prefix"]
        sname     = data["sname"]
        s_type    = data["type"]
        as_unk    = self.chkAsUnknown.isChecked()

        fill_all = self.chkAll.isChecked()
        try:
            p_from = int(self.cmbPosFrom.currentText()) if not fill_all else 1
            p_to   = int(self.cmbPosTo.currentText())   if not fill_all else 999999
        except ValueError:
            p_from, p_to = 1, 999999

        updated = 0
        for i, rd in enumerate(self.load_list_data):
            pos = rd["pos"]
            if not (p_from <= pos <= p_to):
                continue

            if as_unk:
                # ── "Add as Unknown" path ────────────────────────────────────
                # Only fill TYPE_UNKNOWN slots that are currently empty.
                # analysis_id stays NULL — the slot has no real Analysis record;
                # the reference sample is identified via ctrl_sid/ctrl_prefix only.
                if rd["type"] != TYPE_UNKNOWN or rd["analysis_id"] is not None:
                    continue
                full_sid = f"{pfx}-{sid}"
                rd["analysis_id"] = None     # intentionally NULL — no Analysis row
                rd["sample_name"] = sname
                rd["full_sid"]    = full_sid
                rd["ctrl_sid"]    = sid      # used for save / display
                rd["ctrl_prefix"] = pfx
                self.load_model.item(i, LL_COL_SID).setText(full_sid)
                self.load_model.item(i, LL_COL_AID).setText("")     # NULL
                self.load_model.item(i, LL_COL_NAME).setText(sname)
            else:
                # ── Normal control assignment path ───────────────────────────
                if fill_all:
                    # Only touch non-Unknown slots that have no ctrl yet
                    if rd["type"] == TYPE_UNKNOWN or rd.get("ctrl_sid") is not None:
                        continue
                # else: fill any position in range regardless of current type

                rd["ctrl_sid"]    = sid
                rd["ctrl_prefix"] = pfx
                rd["sample_name"] = sname
                rd["type"]        = s_type
                type_tag          = TYPE_NAMES.get(s_type, str(s_type))
                self.load_model.item(i, LL_COL_SID).setText(f"{pfx}-{sid}")
                self.load_model.item(i, LL_COL_NAME).setText(f"[{type_tag}] {sname}")

            updated += 1

        if updated:
            mode_lbl = "as Unknown" if as_unk else "as control"
            QMessageBox.information(
                self, "Done",
                f"Applied {mode_lbl} to {updated} position(s)."
            )
            self.accept()
        else:
            QMessageBox.warning(self, "No match", "No positions matched the selection.")


# ---------------------------------------------------------------------------
# Helper: small input dialog for "load from existing run"
# ---------------------------------------------------------------------------
def _ask_run_id(parent):
    from PyQt5.QtWidgets import QInputDialog
    run_id_str, ok = QInputDialog.getText(
        parent,
        "Load from Existing Run",
        "Enter the RunID to load unassigned unknowns from:"
    )
    if not ok or not run_id_str.strip():
        return None, False
    try:
        return int(run_id_str.strip()), True
    except ValueError:
        QMessageBox.warning(parent, "Invalid", "Run ID must be a number.")
        return None, False


if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = TrimsLSCCreateRunDialog()
    w.show()
    sys.exit(app.exec_())