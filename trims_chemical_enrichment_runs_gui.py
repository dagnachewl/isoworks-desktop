"""
trims_chemical_enrichment_runs_gui.py — TRIMS Chemical Enrichment run list panel.

Provides ChemEnrRunsWindow (filterable run list) and
ChemEnrCreateRunDialog (lightweight run creation wizard) for
non-tritium LSC pre-concentration workflows (35S, 14C, etc.).

Follows the same structural conventions as trims_electrolysis_runs_gui.py.
"""
from __future__ import annotations

import getpass
import logging
import sys
from datetime import datetime
from typing import Optional

from PyQt5.QtCore import Qt, QCoreApplication, QRect, pyqtSignal
from PyQt5.QtGui import (
    QBrush, QColor, QPainter, QStandardItem, QStandardItemModel,
)
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox,
    QDateTimeEdit, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter, QStyledItemDelegate,
    QTableView, QTextEdit, QVBoxLayout, QWidget,
)
from sqlalchemy import text

from db_core import db_manager
from gui_utils import show_message
from shared_utils import (
    check_employee_privilege, get_current_user_id, normalize_login_name, set_status,
)

try:
    from help_browser import make_help_button
    _HAS_HELP = True
except ImportError:
    _HAS_HELP = False

from trims_chemical_enrichment_gui import (
    ChemicalEnrichmentWindow,
    METH_GRAVIMETRIC, METH_SPIKE_RECOVERY, METH_VOLUMETRIC, METH_LABELS,
)

# ---------------------------------------------------------------------------
# Shared widget styles
# ---------------------------------------------------------------------------
_GRP_STYLE = """
    QGroupBox { background:#FAFAFC; border:1px solid #D9D9E3;
                border-radius:8px; margin-top:8px; font-weight:600;
                padding-top:10px; }
    QGroupBox::title { subcontrol-origin:margin; left:10px;
                       padding:0 6px; color:#2D2D33; font-size:13px; }
    QLabel  { color:#3A3A44; font-size:12.5px; }
    QLineEdit, QComboBox, QDateTimeEdit { background:#FFF;
        border:1px solid #D0D0DD; border-radius:6px;
        padding:4px 6px; font-size:12.5px; }
"""

_TBL_STYLE = (
    "QTableView { gridline-color:#deeeff; border:1px solid #b3d4f5; }"
    "QTableView::item:selected { background:#e1f5fe; color:#01579b; }"
    "QHeaderView::section { background:#e8f4fd; color:#37474f;"
    " padding:2px 5px; border:none;"
    " border-right:1px solid #cce5ff; border-bottom:1px solid #90caf9;"
    " font-weight:600; }"
)

# ---------------------------------------------------------------------------
# Status dot delegate  (mirrors electrolysis runs panel)
# ---------------------------------------------------------------------------
class _StatusDelegate(QStyledItemDelegate):
    _COLORS = {
        "Ongoing":  QColor(255, 165,  0),
        "Complete": QColor( 50, 200, 50),
        "Pending":  QColor(255,   0,  0),
    }

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        status = index.data(Qt.UserRole + 1) or ""
        color  = self._COLORS.get(status, QColor(200, 200, 200))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        sz = 12
        rect = QRect(
            option.rect.x() + (option.rect.width()  - sz) // 2,
            option.rect.y() + (option.rect.height() - sz) // 2,
            sz, sz,
        )
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(rect)
        painter.restore()


# ---------------------------------------------------------------------------
# Create-run dialog  (mirrors TrimsElectrolysisCreateRunWidget layout)
# ---------------------------------------------------------------------------
class ChemEnrCreateRunDialog(QDialog):
    """
    Create-run dialog that matches the electrolysis create-run style:
    • Workflow selection auto-fills Isotope (Workflow → Media → Measurables)
      and Procedure (WorkflowJob → AnalysisProcedure).
    • Left panel : Run Setup form + Available Samples (Status=3, matching workflow).
    • Middle      : >> / >>> / < / << transfer buttons.
    • Right panel : Staged Samples to include in the run.
    """

    runCreated = pyqtSignal(int)

    # Shared column layout for both available and staged models
    _COL_LAB   = 0   # OurLabID  (carries AnalysisID in Qt.UserRole)
    _COL_NAME  = 1   # Sample Name
    _COL_AID   = 2   # AnalysisID (text)
    _COL_DATE  = 3   # Collection Date  (hidden in staged view)
    _COL_SUB   = 4   # Submission ID    (hidden in staged view)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Chemical Enrichment Run")
        self.resize(1060, 680)

        self._avail_model  = QStandardItemModel()
        self._staged_model = QStandardItemModel()
        self._measurable_id   = None
        self._measurable_name = ""
        self._procedure_name  = ""

        self._build_ui()
        self._load_static_combos()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Top bar ────────────────────────────────────────────────────
        top = QHBoxLayout()
        top.addWidget(QLabel("<b>Create Chemical Enrichment Run</b>"))
        top.addStretch()
        self.btnCreate = QPushButton("Create Run")
        self.btnCreate.setEnabled(False)
        self.btnCreate.setStyleSheet("""
            QPushButton          { background:#27ae60; color:white; font-weight:bold;
                                   border:none; padding:6px 18px; border-radius:4px; }
            QPushButton:hover    { background:#219a52; }
            QPushButton:disabled { background:#a9dfbf; }
        """)
        self.btnCreate.clicked.connect(self._create_run)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        top.addWidget(self.btnCreate)
        top.addWidget(btn_cancel)
        root.addLayout(top)

        # ── Splitter: left | mid | right ──────────────────────────────
        splitter = QSplitter(Qt.Horizontal)

        # ── LEFT: setup form + available samples ──────────────────────
        left_w  = QWidget()
        left_ly = QVBoxLayout(left_w)
        left_ly.setContentsMargins(0, 0, 4, 0)

        setup_grp = QGroupBox("Run Setup")
        setup_grp.setStyleSheet(_GRP_STYLE)
        frm = QFormLayout()
        frm.setHorizontalSpacing(12)
        frm.setVerticalSpacing(8)

        self.cmbWorkflow   = QComboBox()
        self.txtIsotope    = QLineEdit()
        self.txtIsotope.setReadOnly(True)
        self.txtIsotope.setStyleSheet("background:#f0f0f0; color:#555;")
        self.txtProcedure  = QLineEdit()
        self.txtProcedure.setReadOnly(True)
        self.txtProcedure.setStyleSheet("background:#f0f0f0; color:#555;")
        self.cmbMethod     = QComboBox()
        for code, label in METH_LABELS.items():
            self.cmbMethod.addItem(label, code)
        self.dtDate        = QDateTimeEdit(datetime.now())
        self.dtDate.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.cmbTechnician = QComboBox()
        self.txtRemarks    = QLineEdit()
        self.txtRemarks.setPlaceholderText("Optional run remarks …")

        frm.addRow("Workflow:",   self.cmbWorkflow)
        frm.addRow("Isotope:",    self.txtIsotope)
        frm.addRow("Procedure:",  self.txtProcedure)
        frm.addRow("Method:",     self.cmbMethod)
        frm.addRow("Date:",       self.dtDate)
        frm.addRow("Technician:", self.cmbTechnician)
        frm.addRow("Remarks:",    self.txtRemarks)
        setup_grp.setLayout(frm)
        left_ly.addWidget(setup_grp)

        avail_grp = QGroupBox("Available Samples (Ready for Enrichment)")
        avail_grp.setStyleSheet(_GRP_STYLE)
        v_avail = QVBoxLayout()
        self.tblAvailable = QTableView()
        self.tblAvailable.setModel(self._avail_model)
        self.tblAvailable.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblAvailable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvailable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblAvailable.setAlternatingRowColors(True)
        self.tblAvailable.verticalHeader().setVisible(False)
        self.tblAvailable.verticalHeader().setDefaultSectionSize(22)
        self.tblAvailable.setStyleSheet(_TBL_STYLE)
        self.tblAvailable.doubleClicked.connect(lambda _: self._add_selected())
        v_avail.addWidget(self.tblAvailable)
        avail_grp.setLayout(v_avail)
        left_ly.addWidget(avail_grp, 1)
        splitter.addWidget(left_w)

        # ── MIDDLE: arrow buttons ──────────────────────────────────────
        mid_w  = QWidget()
        mid_ly = QVBoxLayout(mid_w)
        mid_ly.addStretch()
        for label, tip, slot in [
            (">>",  "Add selected",    self._add_selected),
            (">>>", "Add all",         self._add_all),
            ("<",   "Remove selected", self._remove_selected),
            ("<<",  "Remove all",      self._remove_all),
        ]:
            btn = QPushButton(label)
            btn.setFixedWidth(40)
            btn.setToolTip(tip)
            btn.clicked.connect(slot)
            mid_ly.addWidget(btn)
            if label == ">>>":
                mid_ly.addSpacing(10)
        mid_ly.addStretch()
        splitter.addWidget(mid_w)

        # ── RIGHT: staged samples ──────────────────────────────────────
        right_w  = QWidget()
        right_ly = QVBoxLayout(right_w)
        right_ly.setContentsMargins(4, 0, 0, 0)

        staged_grp = QGroupBox("Staged Samples")
        staged_grp.setStyleSheet(_GRP_STYLE)
        v_staged = QVBoxLayout()
        self.lblStaged = QLabel("0 samples staged")
        v_staged.addWidget(self.lblStaged)
        self.tblStaged = QTableView()
        self.tblStaged.setModel(self._staged_model)
        self.tblStaged.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tblStaged.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblStaged.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblStaged.setAlternatingRowColors(True)
        self.tblStaged.verticalHeader().setVisible(False)
        self.tblStaged.verticalHeader().setDefaultSectionSize(22)
        self.tblStaged.setStyleSheet(_TBL_STYLE)
        self.tblStaged.doubleClicked.connect(lambda _: self._remove_selected())
        v_staged.addWidget(self.tblStaged)
        staged_grp.setLayout(v_staged)
        right_ly.addWidget(staged_grp, 1)
        splitter.addWidget(right_w)

        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 3)
        root.addWidget(splitter, 1)

        # ── Status bar ─────────────────────────────────────────────────
        self.status_label = QLabel("Select a workflow to begin.")
        root.addWidget(self.status_label)

        # Model headers (same 5 cols; staged hides cols 3-4)
        _hdrs = ["OurLabID", "Sample Name", "AnalysisID", "Collection Date", "Submission"]
        self._avail_model.setHorizontalHeaderLabels(_hdrs)
        self._staged_model.setHorizontalHeaderLabels(_hdrs)
        self.tblStaged.setColumnHidden(self._COL_DATE, True)
        self.tblStaged.setColumnHidden(self._COL_SUB,  True)

    # ------------------------------------------------------------------
    def _load_static_combos(self):
        try:
            with db_manager.get_connection() as conn:
                # Workflows with non-H3 media
                self.cmbWorkflow.addItem("— select workflow —", None)
                for r in conn.execute(text("""
                    SELECT DISTINCT w.WorkflowID,
                           CAST(w.WorkflowID AS TEXT) || ' - ' || w.WorkflowName AS label
                    FROM Workflow w
                    INNER JOIN Media m ON m.MediaID = w.MediaID
                    WHERE m.MediaID <> 200
                    ORDER BY w.WorkflowID
                """)):
                    self.cmbWorkflow.addItem(r.label, r.WorkflowID)
                self.cmbWorkflow.currentIndexChanged.connect(self._on_workflow_changed)

                # Technicians
                self.cmbTechnician.addItem("", None)
                for r in conn.execute(text(
                    "SELECT EmployeeID, LastName || ', ' || FirstMiddleName AS n "
                    "FROM Employee ORDER BY LastName"
                )):
                    self.cmbTechnician.addItem(r.n, r.EmployeeID)

        except Exception as exc:
            logging.error(f"ChemEnrCreateRunDialog._load_static_combos: {exc}")

    # ------------------------------------------------------------------
    def _on_workflow_changed(self):
        wid = self.cmbWorkflow.currentData()
        self.txtIsotope.clear()
        self.txtProcedure.clear()
        self._measurable_id   = None
        self._measurable_name = ""
        self._procedure_name  = ""
        self._avail_model.removeRows(0, self._avail_model.rowCount())
        self._update_staged_count()

        if not wid:
            self.status_label.setText("Select a workflow to begin.")
            return

        try:
            with db_manager.get_connection() as conn:
                # Isotope: Workflow → Media → Measurables (ILIKE for PG case-insensitivity)
                iso_row = conn.execute(text("""
                    SELECT m2.AnalyteID AS MeasurableID, m2.AnalyteName AS MeasurableName
                    FROM Workflow w
                    INNER JOIN Media     m1 ON m1.MediaID = w.MediaID
                    INNER JOIN Analytes  m2
                           ON m2.AnalyteName ILIKE '%' || m1.abbreviation || '%'
                    WHERE w.WorkflowID = :wid
                    LIMIT 1
                """), {"wid": wid}).fetchone()
                if iso_row:
                    self._measurable_id   = int(iso_row.MeasurableID)
                    self._measurable_name = iso_row.MeasurableName
                    self.txtIsotope.setText(iso_row.MeasurableName)

                # Procedure: first matching WorkflowJob step
                proc_row = conn.execute(text("""
                    SELECT wj.ProcedureID, p.ProcedureName
                    FROM WorkflowJob wj
                    INNER JOIN AnalysisProcedure p ON p.ProcedureID = wj.ProcedureID
                    WHERE wj.WorkflowID = :wid
                    ORDER BY wj.RunSequence
                    LIMIT 1
                """), {"wid": wid}).fetchone()
                if proc_row:
                    self._procedure_name = proc_row.ProcedureName
                    self.txtProcedure.setText(proc_row.ProcedureName)

        except Exception as exc:
            logging.error(f"_on_workflow_changed: {exc}")
            set_status(self.status_label, f"Error resolving workflow: {exc}", "error")
            return

        self._load_available_samples(wid)

    def _load_available_samples(self, wid: int):
        self._avail_model.removeRows(0, self._avail_model.rowCount())

        # Exclude AnalysisIDs already staged
        staged_aids: set = set()
        for r in range(self._staged_model.rowCount()):
            try:
                staged_aids.add(int(self._staged_model.item(r, self._COL_AID).text()))
            except (ValueError, AttributeError):
                pass

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT s.Prefix || '-' || CAST(s.SampleID AS TEXT) AS lab_id,
                           s.sName, a.AnalysisID,
                           s.CollectionDate, sub.SubmissionID
                    FROM Analysis a
                    INNER JOIN Sample     s   ON s.SampleID  = a.SampleID
                                             AND s.Prefix    = a.Prefix
                    INNER JOIN Submission sub ON sub.SubmissionID = s.SubmissionID
                    WHERE a.WorkflowID = :wid
                      AND a.Status     = 3
                    ORDER BY sub.PriorityID DESC, a.AnalysisID
                """), {"wid": wid}).fetchall()

            for r in rows:
                if r.AnalysisID in staged_aids:
                    continue
                col_date = r.CollectionDate.strftime("%Y-%m-%d") if r.CollectionDate else ""
                items = [
                    QStandardItem(r.lab_id or ""),
                    QStandardItem(r.sName  or ""),
                    QStandardItem(str(r.AnalysisID)),
                    QStandardItem(col_date),
                    QStandardItem(str(r.SubmissionID or "")),
                ]
                items[self._COL_LAB].setData(r.AnalysisID, Qt.UserRole)
                self._avail_model.appendRow(items)

            h = self.tblAvailable.horizontalHeader()
            for i in range(4):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(4, QHeaderView.Stretch)

            n = self._avail_model.rowCount()
            set_status(self.status_label,
                       f"{n} sample(s) available for this workflow.", "ok")

        except Exception as exc:
            logging.error(f"_load_available_samples: {exc}")
            set_status(self.status_label, f"Sample load error: {exc}", "error")

    # ------------------------------------------------------------------
    # Arrow-button transfer helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _take_row(model: QStandardItemModel, row: int) -> list:
        """Remove row from model and return list of cloned QStandardItems."""
        items = []
        for c in range(model.columnCount()):
            src = model.item(row, c)
            it  = QStandardItem(src.text() if src else "")
            if src:
                for role in (Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2):
                    val = src.data(role)
                    if val is not None:
                        it.setData(val, role)
            items.append(it)
        model.removeRow(row)
        return items

    def _add_selected(self):
        rows = sorted({i.row() for i in self.tblAvailable.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self._staged_model.appendRow(self._take_row(self._avail_model, r))
        self._resize_staged()
        self._update_staged_count()

    def _add_all(self):
        while self._avail_model.rowCount():
            self._staged_model.appendRow(self._take_row(self._avail_model, 0))
        self._resize_staged()
        self._update_staged_count()

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.tblStaged.selectedIndexes()},
                      reverse=True)
        for r in rows:
            self._avail_model.appendRow(self._take_row(self._staged_model, r))
        self._resize_available()
        self._update_staged_count()

    def _remove_all(self):
        while self._staged_model.rowCount():
            self._avail_model.appendRow(self._take_row(self._staged_model, 0))
        self._resize_available()
        self._update_staged_count()

    def _resize_available(self):
        h = self.tblAvailable.horizontalHeader()
        for i in range(4):
            h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.Stretch)

    def _resize_staged(self):
        h = self.tblStaged.horizontalHeader()
        h.setSectionResizeMode(self._COL_LAB,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(self._COL_NAME, QHeaderView.Stretch)
        h.setSectionResizeMode(self._COL_AID,  QHeaderView.ResizeToContents)

    def _update_staged_count(self):
        n = self._staged_model.rowCount()
        self.lblStaged.setText(f"{n} sample(s) staged")
        self.btnCreate.setEnabled(n > 0 and self._measurable_id is not None)

    # ------------------------------------------------------------------
    def _create_run(self):
        mid     = self._measurable_id
        meth    = self.cmbMethod.currentData()
        tech_id = self.cmbTechnician.currentData()
        run_dt  = self.dtDate.dateTime().toPyDateTime()
        remarks = self.txtRemarks.text().strip()[:255]
        user    = getpass.getuser()
        now     = datetime.now()

        if not mid:
            show_message(self, "Isotope required",
                         "Select a workflow with a recognised isotope first.", "warning")
            return

        n = self._staged_model.rowCount()
        if n == 0:
            show_message(self, "No samples staged",
                         "Move at least one sample to the staged panel first.", "warning")
            return

        analysis_ids = []
        for r in range(n):
            item = self._staged_model.item(r, self._COL_AID)
            if item:
                try:
                    analysis_ids.append(int(item.text()))
                except ValueError:
                    pass

        try:
            with db_manager.get_connection() as conn:
                # RETURNING lets PostgreSQL hand back the generated runid
                res = conn.execute(text("""
                    INSERT INTO trims.chemenrrun
                        (measurableid, enrichmentmethod, rundate, isfinished,
                         technicianid, remarks, createdatestamp, createuserstamp)
                    VALUES (:mid, :meth, :dt, FALSE,
                            :tech, :rem, :now, :usr)
                    RETURNING runid
                """), {
                    "mid": mid, "meth": meth, "dt": run_dt,
                    "tech": tech_id, "rem": remarks, "now": now, "usr": user,
                })
                new_rid = res.fetchone()[0]

                for aid in analysis_ids:
                    conn.execute(text("""
                        INSERT INTO trims.chemicalenrichment
                            (runid, analysisid, measurableid, enrichmentmethod,
                             isignored, createdatestamp, createuserstamp)
                        VALUES (:rid, :aid, :mid, :meth, FALSE, :now, :usr)
                    """), {
                        "rid": new_rid, "aid": aid, "mid": mid,
                        "meth": meth, "now": now, "usr": user,
                    })
                    conn.execute(text(
                        "UPDATE analysis SET status = 4 WHERE analysisid = :aid"
                    ), {"aid": aid})

                conn.commit()

            show_message(self, "Success",
                         f"Chemical Enrichment Run {new_rid} created with "
                         f"{len(analysis_ids)} sample(s).", "information")
            self.runCreated.emit(new_rid)
            self.accept()

        except Exception as exc:
            logging.error(f"_create_run: {exc}")
            show_message(self, "Error", f"Failed to create run:\n{exc}", "critical")


# ---------------------------------------------------------------------------
# Runs list panel
# ---------------------------------------------------------------------------
class ChemEnrRunsWindow(QWidget):
    """
    Filterable list of Chemical Enrichment runs.
    Mirrors TrimsElectrolysisRunsWindow in layout and behaviour.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.has_write_priv = False
        self.has_admin_priv = False
        self._build_ui()
        self._check_privileges()
        self._update_ui_state()
        try:
            db_manager.get_engine()
            self.load_run_list()
        except Exception as exc:
            self.setEnabled(False)
            self.main_layout.addWidget(QLabel(f"DB Error: {exc}"))

    # ------------------------------------------------------------------
    def _build_ui(self):
        self.main_layout = QVBoxLayout(self)

        # Top bar
        top = QHBoxLayout()
        top.addStretch()
        self.btnNew = QPushButton("Create New Run")
        self.btnNew.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover    { background:#219a52; }
            QPushButton:pressed  { background:#1e8449; }
            QPushButton:disabled { background:#a9dfbf; color:#7f8c8d; }
        """)
        self.btnNew.clicked.connect(self._create_new_run)
        top.addWidget(self.btnNew)
        self.btnClose = QPushButton("Close Module")
        self.btnClose.clicked.connect(self._close_module)
        top.addWidget(self.btnClose)
        if _HAS_HELP:
            from help_browser import make_help_button
            top.addWidget(make_help_button(self, "trims_chem_enrichment"))
        self.main_layout.addLayout(top)
        self.main_layout.addSpacing(6)

        # Filter group
        self.main_layout.addWidget(self._build_filter_group())
        self.main_layout.addSpacing(8)

        # Table
        self.run_table = QTableView()
        self._apply_table_styles()
        self.run_table.setItemDelegateForColumn(0, _StatusDelegate())
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True)
        self.model = QStandardItemModel()
        self.run_table.setModel(self.model)
        self.run_table.clicked.connect(self._on_click)
        self.run_table.doubleClicked.connect(self._on_dblclick)
        self.main_layout.addWidget(self.run_table, 1)

    def _build_filter_group(self):
        grp = QGroupBox("Filter / Search Chemical Enrichment Runs")
        layout = QVBoxLayout()

        # Row 1
        r1 = QHBoxLayout()
        self.chkOngoing = QCheckBox("Show ongoing runs only")
        self.chkOngoing.setChecked(True)
        self.chkOngoing.toggled.connect(self.load_run_list)
        self.cmbIsotopeFilter = QComboBox()
        self.cmbIsotopeFilter.addItem("All isotopes", None)
        r1.addWidget(self.chkOngoing)
        r1.addSpacing(20)
        r1.addWidget(QLabel("Isotope:"))
        r1.addWidget(self.cmbIsotopeFilter)
        r1.addStretch()
        layout.addLayout(r1)

        # Row 2
        r2 = QHBoxLayout()
        r2.addWidget(QLabel("Search by:"))
        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Run ID", "Sample ID", "Sample Name"])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        r2.addWidget(self.cmbSearchType)

        self.txtSearch = QLineEdit()
        self.txtSearch.setPlaceholderText("Enter search term…")
        self.txtSearch.returnPressed.connect(self._on_search_or_open)
        r2.addWidget(self.txtSearch)

        self.btnSearch = QPushButton("Open Run")
        self.btnSearch.clicked.connect(self._on_search_or_open)
        r2.addWidget(self.btnSearch)

        self.btnReset = QPushButton("Reset")
        self.btnReset.clicked.connect(self._reset_filters)
        r2.addWidget(self.btnReset)
        r2.addSpacing(20)

        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background:#c0392b; color:white; font-weight:bold;
                          border:none; padding:5px 14px; border-radius:4px; }
            QPushButton:hover    { background:#a93226; }
            QPushButton:pressed  { background:#922b21; }
            QPushButton:disabled { background:#f1948a; color:#7f8c8d; }
        """)
        self.btnDelete.clicked.connect(self._delete_run)
        r2.addWidget(self.btnDelete)
        layout.addLayout(r2)
        grp.setLayout(layout)
        return grp

    def _apply_table_styles(self):
        self.run_table.setShowGrid(False)
        self.run_table.setStyleSheet("""
            QTableView { border:none; background:white; gridline-color:none; }
            QTableView::item { padding:6px 8px; border:none; background:white; color:#333; }
            QTableView::item:alternate { background:#F3F7FA; }
            QTableView::item:selected  { background:#DDEEFF; color:#000; }
            QHeaderView::section { background:white; color:#7F8BB5; font-weight:bold;
                                   padding:6px 8px; border:none;
                                   border-bottom:2px solid #7F8BB5; }
            QHeaderView::section:hover { background:#DDEEFF; color:#000; }
        """)
        self.run_table.setAlternatingRowColors(True)
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    # ------------------------------------------------------------------
    def _check_privileges(self):
        try:
            user = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user, "AccessEnrichment")
            self.has_admin_priv = check_employee_privilege(user, "AdminTRIMS")
        except Exception as exc:
            logging.error(f"_check_privileges: {exc}")

    def _update_ui_state(self):
        self.btnNew.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    def _close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()

    # ------------------------------------------------------------------
    def _load_isotope_filter(self):
        """Populate isotope filter combo from existing runs."""
        current = self.cmbIsotopeFilter.currentData()
        self.cmbIsotopeFilter.blockSignals(True)
        self.cmbIsotopeFilter.clear()
        self.cmbIsotopeFilter.addItem("All isotopes", None)
        try:
            with db_manager.get_connection() as conn:
                for r in conn.execute(text(
                    "SELECT DISTINCT m.AnalyteID AS MeasurableID, m.AnalyteName AS MeasurableName "
                    "FROM TRIMS.ChemEnrRun cr "
                    "INNER JOIN Analytes m ON m.AnalyteID = cr.MeasurableID "
                    "ORDER BY m.AnalyteName"
                )):
                    self.cmbIsotopeFilter.addItem(r.MeasurableName, r.MeasurableID)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        # Restore selection
        i = self.cmbIsotopeFilter.findData(current)
        self.cmbIsotopeFilter.setCurrentIndex(max(0, i))
        self.cmbIsotopeFilter.blockSignals(False)

    def load_run_list(self):
        self._load_isotope_filter()
        self.model.clear()
        self.model.setHorizontalHeaderLabels(
            ["Status", "Run ID", "Isotope", "Method", "Date", "Finished?", "Technician", "Remarks"])

        sql, params = self._build_query()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()

            for r in rows:
                is_done = bool(r.IsFinished)
                status  = "Complete" if is_done else "Ongoing"

                st_item = QStandardItem(status)
                st_item.setData(status, Qt.UserRole + 1)

                id_item = QStandardItem(str(r.RunID))
                id_item.setData(r.RunID, Qt.UserRole)

                meth_label = METH_LABELS.get(int(r.EnrichmentMethod or METH_GRAVIMETRIC), "")
                date_str   = r.RunDate.strftime("%Y-%m-%d") if r.RunDate else ""
                tech_str   = r.TechName or str(r.TechnicianID or "")

                self.model.appendRow([
                    st_item,
                    id_item,
                    QStandardItem(r.MeasurableName or ""),
                    QStandardItem(meth_label),
                    QStandardItem(date_str),
                    QStandardItem("Yes" if is_done else ""),
                    QStandardItem(tech_str),
                    QStandardItem(r.Remarks or ""),
                ])

            h = self.run_table.horizontalHeader()
            for i in range(7):
                h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(7, QHeaderView.Stretch)

        except Exception as exc:
            logging.error(f"load_run_list: {exc}")
            show_message(self, "Error", f"Failed to load runs:\n{exc}", "warning")

    def _build_query(self):
        params: dict = {}
        wheres = []

        if self.chkOngoing.isChecked():
            wheres.append("cr.IsFinished = FALSE")

        mid = self.cmbIsotopeFilter.currentData()
        if mid:
            wheres.append("cr.MeasurableID = :mid")
            params["mid"] = mid

        s_val  = self.txtSearch.text().strip()
        s_type = self.cmbSearchType.currentText()
        if s_val:
            if s_type == "Run ID" and s_val.isdigit():
                wheres.append("cr.RunID = :rid")
                params["rid"] = int(s_val)
            elif s_type == "Sample ID" and s_val.isdigit():
                wheres.append("""cr.RunID IN (
                    SELECT ce.RunID FROM TRIMS.ChemicalEnrichment ce
                    JOIN Analysis a ON a.AnalysisID = ce.AnalysisID
                    WHERE a.SampleID = :sid)""")
                params["sid"] = int(s_val)
            elif s_type == "Sample Name":
                wheres.append("""cr.RunID IN (
                    SELECT ce.RunID FROM TRIMS.ChemicalEnrichment ce
                    JOIN Analysis a ON a.AnalysisID = ce.AnalysisID
                    JOIN Sample   s ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
                    WHERE s.sName LIKE :sname)""")
                params["sname"] = f"%{s_val}%"

        where_clause = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        sql = f"""
            SELECT cr.RunID, cr.MeasurableID, cr.EnrichmentMethod,
                   cr.RunDate, cr.IsFinished, cr.TechnicianID, cr.Remarks,
                   m.AnalyteName AS MeasurableName,
                   e.LastName || ', ' || e.FirstMiddleName AS TechName
            FROM TRIMS.ChemEnrRun cr
            LEFT JOIN Analytes m     ON m.AnalyteID  = cr.MeasurableID
            LEFT JOIN Employee    e  ON e.EmployeeID     = cr.TechnicianID
            {where_clause}
            ORDER BY cr.RunDate DESC, cr.RunID DESC
        """
        return sql, params

    # ------------------------------------------------------------------
    def _on_search_type_changed(self, text: str):
        self.btnSearch.setText("Open Run" if text == "Run ID" else "Search")

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Run ID":
            val = self.txtSearch.text().strip()
            if not val:
                self.load_run_list()
                return
            if not val.isdigit():
                show_message(self, "Invalid", "Run ID must be a number.", "warning")
                return
            self._open_run_details(int(val))
        else:
            self.load_run_list()

    def _reset_filters(self):
        self.chkOngoing.setChecked(True)
        self.txtSearch.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.cmbIsotopeFilter.setCurrentIndex(0)
        self.load_run_list()

    def _on_click(self, index):
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item:
            rid = item.data(Qt.UserRole)
            if rid is not None:
                self.cmbSearchType.setCurrentText("Run ID")
                self.txtSearch.setText(str(rid))

    def _on_dblclick(self, index):
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item:
            self._open_run_details(item.data(Qt.UserRole))

    def _open_run_details(self, run_id: int):
        dlg = ChemicalEnrichmentWindow(run_id=run_id, parent=self)
        dlg.finished.connect(self._on_details_closed)
        dlg.show()

    def _on_details_closed(self, _result):
        self._reset_filters()

    def _create_new_run(self):
        dlg = ChemEnrCreateRunDialog(parent=self)
        dlg.runCreated.connect(lambda rid: self._open_run_details(rid))
        dlg.exec_()
        self.load_run_list()

    def _delete_run(self):
        rows = self.run_table.selectionModel().selectedRows()
        if not rows:
            show_message(self, "No Selection", "Select a run to delete.", "warning")
            return
        rid = self.model.item(rows[0].row(), 1).data(Qt.UserRole)
        if QMessageBox.question(
            self, "Delete",
            f"Delete Chemical Enrichment Run {rid} and all its sample rows?\n"
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            with db_manager.get_connection() as conn:
                # Revert Analysis.Status back to 3 (ready for enrichment)
                conn.execute(text("""
                    UPDATE Analysis SET Status = 3
                    WHERE AnalysisID IN (
                        SELECT AnalysisID FROM TRIMS.ChemicalEnrichment WHERE RunID = :rid
                    ) AND Status = 4
                """), {"rid": rid})
                conn.execute(text(
                    "DELETE FROM TRIMS.ChemicalEnrichment WHERE RunID = :rid"
                ), {"rid": rid})
                conn.execute(text(
                    "DELETE FROM TRIMS.ChemEnrRun WHERE RunID = :rid"
                ), {"rid": rid})
                conn.commit()
            self.load_run_list()
        except Exception as exc:
            logging.error(f"_delete_run: {exc}")
            show_message(self, "Error", f"Failed to delete run:\n{exc}", "critical")


# ---------------------------------------------------------------------------
# Launcher helper
# ---------------------------------------------------------------------------
def load_trims_chem_enrichment():
    return ChemEnrRunsWindow()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = ChemEnrRunsWindow()
    w.show()
    sys.exit(app.exec_())
