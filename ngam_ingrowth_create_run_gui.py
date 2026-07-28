"""
ngam_ingrowth_create_run_gui.py
================================
NGAM – Unified Create Run Dialog

Handles four modes via the `mode` parameter:

mode='ingrowth' (default):
  • Available samples from public.sample_queue (initial queue)
  • Inserts into ngam.ng3heingrowthrun + ngam.ng3heingrowthdata

mode='extraction':
  • Available samples from public.sample_queue, jobs LIKE '%extraction%'
  • Inserts into ngam.ngextractionrun + ngam.ngextractiondata

mode='sequence':
  • Template loaded from public.ngseqtemplate (ProcedureID)
  • Available samples from public.sample_queue WHERE source_aid IS NOT NULL (post-ingrowth)
  • Inserts into ngam.ng3hesequencerun + ngam.ng3hesequenceloadlist

mode='ng_sequence':
  • Same template-based UI as 'sequence' mode
  • Workflows filtered by MediaID=300 (Noble Gas / Mass Spec)
  • Available samples from public.sample_queue WHERE source_aid IS NOT NULL (post-extraction)
  • Inserts into ngam.ngsequencerun + ngam.ngpreparations
"""
from __future__ import annotations
import sys
import logging
from datetime import datetime
from typing import List, Optional

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QComboBox, QTextEdit, QCheckBox, QFormLayout,
    QPushButton, QDateTimeEdit,
    QMessageBox, QHeaderView, QWidget, QInputDialog,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QFrame,
    QDialogButtonBox,
)
from PyQt5.QtCore import Qt, QDateTime, QTimer
from PyQt5.QtGui import QColor, QBrush

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button
from shared_utils import get_current_user_id, normalize_login_name, get_standard_activity
from sample_type_registry import registry, STYPE_SAMPLE_SLOT, STYPE_NG_DUMMY

# ── palette ───────────────────────────────────────────────────────────────────
_PANEL_BG  = "#F5F7FA"   # left panel  – near-white, subtle tint
_PANEL_MID = "#EAEDF2"   # centre divider strip
_TBL_HDR   = "#3D6A9E"   # table column headers – softer navy
_BTN_BLUE  = "#3A6EA8"
_BTN_GRAY  = "#7F8C9A"

_PANEL_SS = f"background-color:{_PANEL_BG};"
_TBLHDR_SS = (
    f"QHeaderView::section {{ background-color:{_TBL_HDR}; color:white; "
    f"font-weight:bold; padding:3px 6px; border:none; "
    f"border-right:1px solid #2E527A; }}"
    "QHeaderView::section:last-section { border-right:none; }"
)

# ── sequence slot colours ─────────────────────────────────────────────────────
_COL_SAMPLE_EMPTY  = QColor("#FFFFFF")
_COL_SAMPLE_FILLED = QColor("#C8E6C9")
_COL_BLANK         = QColor("#BBDEFB")
_COL_REPRO         = QColor("#E8F5E9")
_COL_LIN           = QColor("#FFF3E0")
_COL_REF           = QColor("#EDE7F6")
_COL_CTRL          = QColor("#E0F2F1")   # teal tint — control sample slot


def _make_table(cols: list) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(False)
    t.setStyleSheet(_TBLHDR_SS)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _section_label(text_: str) -> QLabel:
    """Minimal section label – bold text, no background fill."""
    lbl = QLabel(text_)
    lbl.setStyleSheet(
        f"font-weight:bold; color:{_TBL_HDR}; font-size:11px; padding:4px 2px 2px 2px;"
    )
    return lbl


# ─────────────────────────────────────────────────────────────────────────────
class NGAMCreateRunDialog(QDialog):
    """Unified Create-Run dialog for all NGAM sub-modules."""

    DEFAULT_SAMPLE_STATUS = 222

    def __init__(self, parent=None, mode: str = "ingrowth"):
        super().__init__(parent)
        self._mode = mode

        titles = {
            "ingrowth":    "Create 3He Ingrowth Run",
            "extraction":  "Create NG Extraction Run",
            "sequence":    "Create 3He Sequence/Measurement Run",
            "ng_sequence": "Create Noble Gas MS Sequence Run",
        }
        self.setWindowTitle(titles.get(mode, "Create Run"))

        self.resize(1200, 720)
        self.setMinimumSize(960, 600)

        self._batch_no: int = 0
        self._wj_id: Optional[int] = None
        self._proc_num_samples: int = 0
        self._proc_repeats: int = 1
        self._all_available: List[dict] = []

        # sequence-mode template
        self._load_list_data: List[dict] = []
        self._has_template: bool = False

        self._build_ui()
        self._load_batch_no()
        self._load_technicians()
        if mode == "ingrowth":
            self._load_status_combo()
        self._load_workflows()

    # ═══════════════════════════════════════════════════════ UI construction ══

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        root.addLayout(body, 1)
        body.addWidget(self._build_left(),   38)
        body.addWidget(self._build_centre(),  5)
        body.addWidget(self._build_right(),  57)

        ftr_w = QWidget()
        ftr_w.setStyleSheet("background:#F0F2F6; border-top:1px solid #CDD3DC;")
        ftr = QHBoxLayout(ftr_w)
        ftr.setContentsMargins(10, 6, 10, 6)
        self.lblStatus = QLabel("")
        self.lblStatus.setStyleSheet("color:#444;")
        btnSave = QPushButton("✔  Create Run")
        btnSave.setFixedHeight(32)
        btnSave.setStyleSheet("""
            QPushButton { background:#27ae60; color:white; font-weight:bold;
                          border:none; padding:4px 20px; border-radius:4px; }
            QPushButton:hover { background:#1e8449; }
        """)
        btnSave.clicked.connect(self._save_run)
        btnCancel = QPushButton("✖  Cancel")
        btnCancel.setFixedHeight(32)
        btnCancel.setStyleSheet("""
            QPushButton { background:#c0392b; color:white; font-weight:bold;
                          border:none; padding:4px 20px; border-radius:4px; }
            QPushButton:hover { background:#a93226; }
        """)
        btnCancel.clicked.connect(self.reject)
        ftr.addWidget(self.lblStatus, 1)
        ftr.addWidget(btnSave)
        ftr.addSpacing(8)
        ftr.addWidget(btnCancel)

        help_topic_map = {
            "ingrowth":    "ngam_create_ingrowth",
            "extraction":  "ngam_create_extraction",
            "sequence":    "ngam_create_sequence",
            "ng_sequence": "ngam_create_ng_sequence",
        }
        ftr.addWidget(make_help_button(self, help_topic_map.get(self._mode, "ngam_create_ingrowth")))
        root.addWidget(ftr_w)

    # ── left panel ────────────────────────────────────────────────────────────
    def _build_left(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(_PANEL_SS)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 6, 10)
        lay.setSpacing(6)

        ms_lbl = QLabel("Method / System")
        ms_lbl.setStyleSheet(f"font-weight:bold; color:{_TBL_HDR}; font-size:12px;")
        lay.addWidget(ms_lbl)

        grid = QGridLayout()
        grid.setVerticalSpacing(5)
        grid.setHorizontalSpacing(8)
        grid.setColumnMinimumWidth(0, 84)
        grid.setColumnStretch(1, 1)

        def rlbl(t):
            l = QLabel(t)
            l.setStyleSheet("font-weight:bold; color:#444;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        self.cmbWorkflow = QComboBox()
        self.cmbWorkflow.currentIndexChanged.connect(self._on_workflow_changed)
        grid.addWidget(rlbl("Workflow:"), 0, 0)
        grid.addWidget(self.cmbWorkflow, 0, 1)

        self.cmbWorkflowJob = QComboBox()
        self.cmbWorkflowJob.setEnabled(False)
        self.cmbWorkflowJob.currentIndexChanged.connect(self._on_workflow_job_changed)
        grid.addWidget(rlbl("Job:"), 1, 0)
        grid.addWidget(self.cmbWorkflowJob, 1, 1)

        self.cmbProcedure = QComboBox()
        self.cmbProcedure.setEnabled(False)
        self.cmbProcedure.currentIndexChanged.connect(self._on_procedure_changed)
        grid.addWidget(rlbl("Procedure:"), 2, 0)
        grid.addWidget(self.cmbProcedure, 2, 1)

        self.cmbEquipment = QComboBox()
        self.cmbEquipment.setEnabled(False)
        grid.addWidget(rlbl("Device:"), 3, 0)
        grid.addWidget(self.cmbEquipment, 3, 1)

        lay.addLayout(grid)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#CDD3DC;")
        lay.addWidget(sep)

        meta = QGridLayout()
        meta.setVerticalSpacing(4)
        meta.setHorizontalSpacing(8)
        meta.setColumnMinimumWidth(0, 84)
        meta.setColumnStretch(1, 1)

        def mlbl(t):
            l = QLabel(t)
            l.setStyleSheet("color:#555;")
            l.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            return l

        row = 0
        # Status filter — ingrowth only
        self._lbl_status_filter = mlbl("Status:")
        self.cmbSampleStatus = QComboBox()
        self.cmbSampleStatus.currentIndexChanged.connect(self._load_available_samples)
        meta.addWidget(self._lbl_status_filter, row, 0)
        meta.addWidget(self.cmbSampleStatus, row, 1)
        if self._mode in ("sequence", "ng_sequence", "extraction"):
            self._lbl_status_filter.hide()
            self.cmbSampleStatus.hide()
        row += 1

        self.cmbTech = QComboBox()
        meta.addWidget(mlbl("Technician:"), row, 0)
        meta.addWidget(self.cmbTech, row, 1)
        row += 1

        self.dtStart = QDateTimeEdit(QDateTime.currentDateTime())
        self.dtStart.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dtStart.setCalendarPopup(True)
        btnNow = QPushButton("Now")
        btnNow.setFixedWidth(42)
        btnNow.setStyleSheet(
            f"background:{_BTN_GRAY}; color:white; border:none; border-radius:3px;")
        btnNow.clicked.connect(lambda: self.dtStart.setDateTime(QDateTime.currentDateTime()))
        st_row = QHBoxLayout()
        st_row.addWidget(self.dtStart, 1)
        st_row.addWidget(btnNow)
        meta.addWidget(mlbl("Start Time:"), row, 0)
        meta.addLayout(st_row, row, 1)
        row += 1

        self.txtRemarks = QTextEdit()
        self.txtRemarks.setMaximumHeight(48)
        self.txtRemarks.setPlaceholderText("Remarks…")
        meta.addWidget(mlbl("Remarks:"), row, 0, Qt.AlignTop)
        meta.addWidget(self.txtRemarks, row, 1)

        lay.addLayout(meta)

        # Edit Template button — sequence / ng_sequence modes
        if self._mode in ("sequence", "ng_sequence"):
            self.btnEditTemplate = QPushButton("Edit Load List Template…")
            self.btnEditTemplate.setFixedHeight(26)
            self.btnEditTemplate.setEnabled(False)
            self.btnEditTemplate.setStyleSheet("""
                QPushButton {
                    background: #1A8FA3; color: white; font-weight: bold;
                    border: 2px solid transparent; border-radius: 3px; padding: 2px 8px;
                }
                QPushButton:hover   { background: #147D8F; }
                QPushButton:pressed { background: #0F6A79; }
                QPushButton:focus   { border: 2px solid #A0DDE8; }
                QPushButton:disabled { background: #A0C8D0; color: #D0E8EC; }
            """)
            self.btnEditTemplate.clicked.connect(self._edit_ng_template)
            lay.addWidget(self.btnEditTemplate)

        # Available samples section
        if self._mode == "sequence":
            avail_lbl  = "Available Ingrowth Samples"
            avail_cols = ["Submission", "Lab ID", "sName", "AnalysisID", "Ingrowth Run"]
        elif self._mode == "ng_sequence":
            avail_lbl  = "Available Extracted Samples"
            avail_cols = ["Submission", "Lab ID", "sName", "AnalysisID", "Extraction Run"]
        elif self._mode == "extraction":
            avail_lbl  = "Available Samples  (status 222)"
            avail_cols = ["Submission", "Lab ID", "Sample Name"]
        else:
            avail_lbl  = "Available Samples"
            avail_cols = ["Submission", "LabID", "sName", "Status", "ParamValue"]

        lay.addWidget(_section_label(avail_lbl))

        self.tblAvailable = _make_table(avail_cols)
        self.tblAvailable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvailable.itemDoubleClicked.connect(self._add_selected)
        hh = self.tblAvailable.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        if self._mode not in ("extraction",):
            hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        lay.addWidget(self.tblAvailable, 1)
        return w

    # ── centre transfer buttons ────────────────────────────────────────────────
    def _build_centre(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background-color:{_PANEL_MID};")
        w.setFixedWidth(66)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(6)
        lay.addStretch(2)

        def btn(label, tip, color=_BTN_BLUE):
            b = QPushButton(label)
            b.setFixedSize(52, 30)
            b.setToolTip(tip)
            b.setStyleSheet(f"""
                QPushButton {{ background:{color}; color:white; font-weight:bold;
                              border:none; border-radius:4px; font-size:13px; }}
                QPushButton:hover   {{ background-color:#2A5E98; }}
                QPushButton:pressed {{ background-color:#1A4E88; }}
            """)
            return b

        self.btnAddAll    = btn(">>",  "Add all visible samples")
        self.btnAddSel    = btn(">",   "Add selected  (double-click also works)")
        self.btnRemSel    = btn("<",   "Remove selected from run", "#7F8C9A")
        self.btnRemoveAll = btn("<<",  "Remove all from run",      "#7F8C9A")

        self.btnAddAll.clicked.connect(self._add_all)
        self.btnAddSel.clicked.connect(self._add_selected)
        self.btnRemSel.clicked.connect(self._remove_selected)
        self.btnRemoveAll.clicked.connect(self._remove_all)

        lay.addWidget(self.btnAddAll)
        lay.addWidget(self.btnAddSel)
        lay.addSpacing(10)
        lay.addWidget(self.btnRemSel)
        lay.addWidget(self.btnRemoveAll)
        lay.addSpacing(20)

        self.btnAddCtrl = QPushButton("Add Ctrl…")
        self.btnAddCtrl.setToolTip(
            "Assign a Reference Control sample to one or more empty sample slots."
        )
        self.btnAddCtrl.setEnabled(False)
        self.btnAddCtrl.clicked.connect(self._open_add_ctrl_dialog)
        lay.addWidget(self.btnAddCtrl)

        lay.addStretch(3)
        return w

    # ── right panel ───────────────────────────────────────────────────────────
    def _build_right(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background-color:#FFFFFF;")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 10, 10, 10)
        lay.setSpacing(6)

        pos_row = QHBoxLayout()
        if self._mode in ("sequence", "ng_sequence"):
            pos_row.addWidget(QLabel("Sample slots remaining:"))
        elif self._mode == "extraction":
            pos_row.addWidget(QLabel("Samples in run:"))
        else:
            pos_row.addWidget(QLabel("Available Positions in System:"))
        self.lblPosAvail = QLabel("—")
        self.lblPosAvail.setStyleSheet(
            f"font-weight:bold; color:{_TBL_HDR}; border:1px solid #9AAFC8; "
            "padding:2px 10px; background:white; min-width:36px;")
        pos_row.addWidget(self.lblPosAvail)
        pos_row.addWidget(QLabel("of"))
        self.lblPosTotal = QLabel("—")
        self.lblPosTotal.setStyleSheet(
            f"font-weight:bold; color:{_TBL_HDR}; border:1px solid #9AAFC8; "
            "padding:2px 10px; background:white; min-width:36px;")
        pos_row.addWidget(self.lblPosTotal)
        pos_row.addStretch()
        lay.addLayout(pos_row)

        if self._mode == "ng_sequence":
            sel_hdr_lbl = "Load List  (template)"
            sel_cols    = ["Inlet", "Port", "Type", "Lab ID", "Sample Name", "Amount (ccSTP)", ""]
        elif self._mode == "sequence":
            sel_hdr_lbl = "Load List  (template)"
            sel_cols    = ["Inlet", "Type", "Lab ID", "Sample Name", "Port", "Amount (ccSTP)", ""]
        elif self._mode == "extraction":
            sel_hdr_lbl = "Samples Selected for this Run"
            sel_cols    = ["Inlet", "Lab ID", "Sample Name"]
        else:
            sel_hdr_lbl = "Selected Samples for this Run"
            sel_cols    = ["Inlet", "Analysis ID", "Sample Name", "Status", "Parameter"]

        lay.addWidget(_section_label(sel_hdr_lbl))

        self.tblSelected = _make_table(sel_cols)
        if self._mode in ("sequence", "ng_sequence"):
            self.tblSelected.setDragDropMode(QAbstractItemView.NoDragDrop)
            self.tblSelected.setEditTriggers(
                QAbstractItemView.CurrentChanged | QAbstractItemView.SelectedClicked
            )
            self.tblSelected.itemChanged.connect(self._on_template_port_changed)
        else:
            self.tblSelected.setDragDropMode(QAbstractItemView.InternalMove)
            self.tblSelected.model().rowsMoved.connect(self._renumber_positions)
        self.tblSelected.setSelectionMode(QAbstractItemView.ExtendedSelection)

        hh = self.tblSelected.horizontalHeader()
        n_cols = self.tblSelected.columnCount()
        for i in range(n_cols):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        # Stretch the sample name column (last col for ingrowth/extraction,
        # fixed index for sequence modes: col 4 for ng_sequence, col 3 for sequence)
        if self._mode == "ng_sequence":
            stretch_col = 4
        elif self._mode == "sequence":
            stretch_col = 3
        else:
            stretch_col = n_cols - 1
        hh.setSectionResizeMode(stretch_col, QHeaderView.Stretch)

        # Give Type and Lab ID a generous starting width so short values
        # (e.g. "Sample", "Blank", short lab IDs) don't feel cramped.
        if self._mode == "ng_sequence":
            _type_col, _labid_col = 2, 3
        elif self._mode == "sequence":
            _type_col, _labid_col = 1, 2
        else:
            _type_col, _labid_col = None, None
        if _type_col is not None:
            hh.setSectionResizeMode(_type_col,  QHeaderView.Interactive)
            hh.setSectionResizeMode(_labid_col, QHeaderView.Interactive)
            self.tblSelected.setColumnWidth(_type_col,  100)
            self.tblSelected.setColumnWidth(_labid_col, 110)

        lay.addWidget(self.tblSelected, 1)


        return w

    # ═════════════════════════════════════════════════════════ Data loading ═══

    def _load_batch_no(self):
        # (max_sql, default_when_empty, title_fmt)
        sqls = {
            "ingrowth":    ("SELECT MAX(runid) FROM ngam.ng3heingrowthrun",
                            9001, "Create 3He Ingrowth Run  —  Batch No. {}"),
            "extraction":  ("SELECT MAX(runid) FROM ngam.ngextractionrun",
                            1000, "Create NG Extraction Run  —  Run No. {}"),
            "sequence":    ("SELECT MAX(runid) FROM ngam.msrun WHERE measurement_mode = 'IG'",
                            0,    "Create 3He Sequence/Measurement Run  —  Batch No. {}"),
            "ng_sequence": ("SELECT MAX(runid) FROM ngam.msrun WHERE measurement_mode = 'NG'",
                            0,    "Create Noble Gas MS Sequence Run  —  Batch No. {}"),
        }
        sql, default, title_fmt = sqls.get(self._mode, sqls["ingrowth"])
        try:
            with db_manager.get_connection() as conn:
                max_val = conn.execute(text(sql)).scalar()

            if max_val is None:
                # Table is empty — ask user for a custom starting number so they
                # can align with any runs that were imported or created externally.
                proposed = default + 1
                val, ok = QInputDialog.getInt(
                    self, "First Run — Set Starting Number",
                    "No existing runs found in the database.\n\n"
                    "Enter the starting Run / Batch number\n"
                    "(useful when importing runs from another system):",
                    proposed, 1, 9_999_999
                )
                self._batch_no = val if ok else proposed
            else:
                self._batch_no = int(max_val) + 1

            self.setWindowTitle(title_fmt.format(self._batch_no))
        except Exception as e:
            logging.error(f"Batch No load failed: {e}")

    def _load_technicians(self):
        self.cmbTech.clear()
        self.cmbTech.addItem("— Select Technician —", None)
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT employeeid, lastname, firstmiddlename "
                    "FROM public.employee ORDER BY lastname"
                )).fetchall()
            for r in rows:
                self.cmbTech.addItem(f"{r[1]}, {r[2]}", r[0])
        except Exception as e:
            logging.error(f"Technicians load failed: {e}")

    def _load_status_combo(self):
        self.cmbSampleStatus.blockSignals(True)
        self.cmbSampleStatus.clear()
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT status, description FROM public.statuslookup
                    WHERE status IN (200, 222, -9) ORDER BY status
                """)).fetchall()
            for r in rows:
                self.cmbSampleStatus.addItem(f"{r[0]}---{r[1]}", r[0])
        except Exception as e:
            logging.error(f"Status combo load failed: {e}")
            self.cmbSampleStatus.addItem("222---TBA", 222)
        idx = self.cmbSampleStatus.findData(self.DEFAULT_SAMPLE_STATUS)
        if idx >= 0:
            self.cmbSampleStatus.setCurrentIndex(idx)
        self.cmbSampleStatus.blockSignals(False)

    def _load_workflows(self):
        self.cmbWorkflow.blockSignals(True)
        self.cmbWorkflow.clear()
        self.cmbWorkflow.addItem("— Select Workflow —", None)
        try:
            with db_manager.get_connection() as conn:
                if self._mode == "ng_sequence":
                    rows = conn.execute(text("""
                        SELECT DISTINCT w.workflowid,
                               CAST(w.workflowid AS VARCHAR) || ' - ' || w.workflowname
                        FROM public.workflow w
                        JOIN public.workflowjob wj ON wj.workflowid = w.workflowid
                        WHERE w.mediaid = 300
                          AND COALESCE(w.isobsolete, FALSE) = FALSE
                        ORDER BY w.workflowid
                    """)).fetchall()
                elif self._mode == "extraction":
                    rows = conn.execute(text("""
                        SELECT DISTINCT w.workflowid,
                               CAST(w.workflowid AS VARCHAR) || ' - ' || w.workflowname
                        FROM public.workflow w
                        JOIN public.workflowjob wj ON wj.workflowid = w.workflowid
                        WHERE LOWER(wj.jobname) LIKE '%extraction%'
                          AND COALESCE(w.isobsolete, FALSE) = FALSE
                        ORDER BY w.workflowid
                    """)).fetchall()
                else:
                    rows = conn.execute(text("""
                        SELECT DISTINCT w.workflowid,
                               CAST(w.workflowid AS VARCHAR) || ' - ' || w.workflowname
                        FROM public.workflow w
                        JOIN public.workflowjob wj ON wj.workflowid = w.workflowid
                        WHERE (wj.jobname ILIKE '%3He%Ingrowth%' OR wj.jobname ILIKE '%Ingrowth%3He%')
                          AND COALESCE(w.isobsolete, FALSE) = FALSE
                        ORDER BY w.workflowid
                    """)).fetchall()
            for r in rows:
                self.cmbWorkflow.addItem(str(r[1]), int(r[0]))
        except Exception as e:
            logging.error(f"Workflows load failed: {e}")
        self.cmbWorkflow.blockSignals(False)

    # ═════════════════════════════════════════════ Workflow → Procedure cascade

    def _on_workflow_changed(self):
        wf_id = self.cmbWorkflow.currentData()

        self._wj_id = None
        self.cmbWorkflowJob.blockSignals(True)
        self.cmbWorkflowJob.clear()
        self.cmbWorkflowJob.setEnabled(False)
        self.cmbWorkflowJob.blockSignals(False)
        self.cmbProcedure.blockSignals(True)
        self.cmbProcedure.clear()
        self.cmbProcedure.setEnabled(False)
        self.cmbProcedure.blockSignals(False)
        self.cmbEquipment.clear()
        self.cmbEquipment.setEnabled(False)
        self.tblAvailable.setRowCount(0)
        self._all_available.clear()
        self._load_list_data.clear()
        self._has_template = False
        self.tblSelected.setRowCount(0)
        self._update_positions()

        if wf_id is None:
            return

        try:
            with db_manager.get_connection() as conn:
                if self._mode == "extraction":
                    jobs = conn.execute(text("""
                        SELECT wj.workflowjobid, wj.runsequence, wj.jobname,
                               wj.procedureid, wj.equipmentid
                        FROM public.workflowjob wj
                        WHERE wj.workflowid = :wfid
                          AND LOWER(wj.jobname) LIKE '%extraction%'
                          AND COALESCE(wj.isobsolete, FALSE) = FALSE
                        ORDER BY wj.runsequence, wj.workflowjobid
                    """), {"wfid": wf_id}).fetchall()
                else:
                    jobs = conn.execute(text("""
                        SELECT wj.workflowjobid, wj.runsequence, wj.jobname,
                               wj.procedureid, wj.equipmentid
                        FROM public.workflowjob wj
                        WHERE wj.workflowid = :wfid
                          AND COALESCE(wj.isobsolete, FALSE) = FALSE
                        ORDER BY wj.runsequence, wj.workflowjobid
                    """), {"wfid": wf_id}).fetchall()
        except Exception as e:
            logging.error(f"WorkflowJobs load failed: {e}")
            self.lblStatus.setText(f"Error loading workflow jobs: {e}")
            return

        if not jobs:
            self.lblStatus.setText("No workflow jobs found for this workflow.")
            return

        self.cmbWorkflowJob.blockSignals(True)
        for r in jobs:
            r = tuple(r)
            label = f"{r[1]}: {r[2]}"
            self.cmbWorkflowJob.addItem(label, {"wjid": r[0], "procid": r[3], "equipid": r[4]})

        # Sequence → last job (measurement step); all others → first job
        if self._mode == "sequence":
            self.cmbWorkflowJob.setCurrentIndex(self.cmbWorkflowJob.count() - 1)
        else:
            self.cmbWorkflowJob.setCurrentIndex(0)

        self.cmbWorkflowJob.setEnabled(True)
        self.cmbWorkflowJob.blockSignals(False)
        self._on_workflow_job_changed()

    def _on_workflow_job_changed(self):
        meta = self.cmbWorkflowJob.currentData()
        if meta is None:
            return
        self._wj_id       = meta["wjid"]
        proc_id_default   = meta["procid"]
        equip_id_default  = meta["equipid"]

        wf_id = self.cmbWorkflow.currentData()

        self.cmbProcedure.blockSignals(True)
        self.cmbProcedure.clear()
        try:
            with db_manager.get_connection() as conn:
                procs = conn.execute(text("""
                    SELECT DISTINCT ap.procedureid, ap.procedurename
                    FROM public.workflowjob wj
                    JOIN public.analysisprocedure ap ON ap.procedureid = wj.procedureid
                    WHERE wj.workflowid = :wfid
                      AND COALESCE(ap.isobsolete, FALSE) = FALSE
                    ORDER BY ap.procedurename
                """), {"wfid": wf_id}).fetchall()
            for r in procs:
                self.cmbProcedure.addItem(f"{r[0]}-{r[1]}", int(r[0]))
            idx = self.cmbProcedure.findData(proc_id_default)
            if idx >= 0:
                self.cmbProcedure.setCurrentIndex(idx)
        except Exception as e:
            logging.error(f"Procedure load failed: {e}")
        self.cmbProcedure.setEnabled(True)
        self.cmbProcedure.blockSignals(False)

        self._load_equipment(
            proc_id=self.cmbProcedure.currentData(),
            default_equip_id=equip_id_default,
        )
        self._on_procedure_changed()

    def _load_equipment(self, proc_id: Optional[int], default_equip_id: Optional[int]):
        self.cmbEquipment.blockSignals(True)
        self.cmbEquipment.clear()
        self.cmbEquipment.addItem("— Select Device —", None)
        try:
            with db_manager.get_connection() as conn:
                cat_id = None
                if proc_id:
                    row = conn.execute(text(
                        "SELECT categoryid FROM public.analysisprocedure WHERE procedureid = :pid"
                    ), {"pid": proc_id}).fetchone()
                    cat_id = row[0] if row else None

                if cat_id:
                    rows = conn.execute(text("""
                        SELECT equipmentid, equipmentname FROM public.equipment
                        WHERE categoryid = :cid ORDER BY equipmentname
                    """), {"cid": cat_id}).fetchall()
                else:
                    rows = []

                if not rows:
                    rows = conn.execute(text(
                        "SELECT equipmentid, equipmentname "
                        "FROM public.equipment ORDER BY equipmentname"
                    )).fetchall()

            for r in rows:
                self.cmbEquipment.addItem(f"{r[0]}: {r[1]}", int(r[0]))
            if default_equip_id is not None:
                idx = self.cmbEquipment.findData(int(default_equip_id))
                if idx >= 0:
                    self.cmbEquipment.setCurrentIndex(idx)
        except Exception as e:
            logging.error(f"Equipment load failed: {e}")
        self.cmbEquipment.setEnabled(True)
        self.cmbEquipment.blockSignals(False)

    def _on_procedure_changed(self):
        proc_id = self.cmbProcedure.currentData()
        self._proc_num_samples = 0
        self._proc_repeats = 1
        if proc_id is None:
            if self._mode in ("sequence", "ng_sequence"):
                self._load_list_data.clear()
                self._has_template = False
                self.tblSelected.setRowCount(0)
            self._update_positions()
            return
        try:
            with db_manager.get_connection() as conn:
                row = conn.execute(text("""
                    SELECT ap.numberofsamples, COALESCE(apm.repeats, 1)
                    FROM public.analysisprocedure ap
                    LEFT JOIN public.analysisprocedure_measurable apm
                           ON apm.procedureid = ap.procedureid
                    WHERE ap.procedureid = :pid
                    LIMIT 1
                """), {"pid": proc_id}).fetchone()
            if row:
                self._proc_num_samples = int(row[0]) if row[0] else 0
                self._proc_repeats = int(row[1]) if row[1] else 1
        except Exception as e:
            logging.error(f"Procedure details failed: {e}")

        if self._mode in ("sequence", "ng_sequence"):
            self._load_sequence_template()
            has_proc = proc_id is not None
            if hasattr(self, "btnEditTemplate"):
                self.btnEditTemplate.setEnabled(has_proc)
        else:
            self._update_positions()
        self._load_available_samples()

    # ══════════════════════════════════════════ Sequence template (NGSeqTemplate)

    def _edit_ng_template(self):
        """Open the NGSeqTemplate editor in run-only mode (does not touch the DB)."""
        proc_id = self.cmbProcedure.currentData()
        if proc_id is None:
            return
        try:
            from ngam_ng_template_editor import NGSeqTemplateEditorDialog
        except ImportError as e:
            show_message(self, "Import Error",
                         f"Template editor not available:\n{e}", QMessageBox.Warning)
            return

        # Pre-populate with current in-memory load list so the user edits what
        # they already have (including any per-run edits) rather than a fresh DB copy.
        preload = None
        if self._load_list_data:
            preload = [
                {
                    "iinletid":           slot["pos"],
                    "iport":              slot.get("iport"),
                    "sampletype":         slot["sampletype"],
                    "ourlabid":           slot.get("ourlabid") or "",
                    "bisblank":           slot.get("bisblank", False),
                    "bisreproreference":  slot.get("bisreproreference", False),
                    "bislinreference":    slot.get("bislinreference", False),
                    "freferenceamount":   slot.get("freference_amount"),
                    "freferenceamountunc": slot.get("freference_amount_unc"),
                    "sample_type_desc":   slot.get("sname", ""),
                }
                for slot in self._load_list_data
            ]

        dlg = NGSeqTemplateEditorDialog(proc_id, parent=self, run_only=True,
                                        preload_rows=preload)
        if dlg.exec_() == dlg.Accepted:
            self._apply_template_rows(dlg._rows)

    def _apply_template_rows(self, rows: list):
        """Replace the in-memory load list with rows from a run-only template edit."""
        self._load_list_data.clear()
        for r in rows:
            ourlabid = r.get("ourlabid", "") or ""
            stype = r.get("sampletype", 0)
            prefix = sampleid = None
            if ourlabid and "-" in ourlabid:
                parts = ourlabid.split("-", 1)
                prefix = parts[0]
                try:
                    sampleid = int(parts[1])
                except ValueError:
                    pass
            self._load_list_data.append({
                "pos":                  r["iinletid"],
                "iport":                r.get("iport"),
                "sampletype":           stype,
                "ourlabid":             ourlabid,
                "bisblank":             r.get("bisblank", False),
                "bisreproreference":    r.get("bisreproreference", False),
                "bislinreference":      r.get("bislinreference", False),
                "freference_amount":    r.get("freferenceamount"),
                "freference_amount_unc": r.get("freferenceamountunc"),
                "sname":                r.get("sample_type_desc") or r.get("ourlabid", ""),
                "analysisid":           None,
                "ingrowthid":           None,
                "labid":                ourlabid,
                "sampleid":             sampleid,
                "prefix":               prefix,
            })
        self._has_template = True
        self.btnAddCtrl.setEnabled(True)
        self._refresh_template_table()
        self._update_positions()
        n_samp  = sum(1 for d in self._load_list_data if registry.is_sample_slot(d["sampletype"]))
        n_fixed = len(self._load_list_data) - n_samp
        self.lblStatus.setText(
            f"Template (run-only edit): {len(self._load_list_data)} positions "
            f"({n_samp} sample slot(s), {n_fixed} fixed reference(s))."
        )

    def _load_sequence_template(self):
        proc_id = self.cmbProcedure.currentData()
        self._load_list_data.clear()
        self._has_template = False
        self.tblSelected.setRowCount(0)

        if proc_id is None:
            self.btnAddCtrl.setEnabled(False)
            self._update_positions()
            return

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        t.iinletid, t.iport, t.sampletype, t.ourlabid,
                        COALESCE(t.bisblank,          FALSE),
                        COALESCE(t.bisreproreference, FALSE),
                        COALESCE(t.bislinreference,   FALSE),
                        t.freferenceamount,
                        t.freferenceamountunc,
                        COALESCE(s.sname, t.ourlabid, '') AS sname
                    FROM public.ngseqtemplate t
                    LEFT JOIN public.sample s
                        ON t.sampletype != 0
                        AND t.ourlabid IS NOT NULL
                        AND s.prefix    = split_part(t.ourlabid, '-', 1)
                        AND s.sampleid::text = split_part(t.ourlabid, '-', 2)
                    WHERE t.procedureid = :pid
                    ORDER BY t.iinletid
                """), {"pid": proc_id}).fetchall()
        except Exception as e:
            logging.error(f"NGSeqTemplate load failed: {e}")
            self.lblStatus.setText(f"Template load error: {e}")
            self._update_positions()
            return

        if not rows:
            self.lblStatus.setText("No load list template defined for this procedure.")
            self._update_positions()
            return

        for r in rows:
            r = tuple(r)
            stype = int(r[2]) if r[2] is not None else 0
            slot = {
                "pos":                  r[0],
                "iport":                r[1],
                "sampletype":           stype,
                "ourlabid":             r[3] or "",
                "bisblank":             bool(r[4]),
                "bisreproreference":    bool(r[5]),
                "bislinreference":      bool(r[6]),
                "freference_amount":    r[7],
                "freference_amount_unc": r[8],
                "sname":                r[9] or "",
                "analysisid":           None,
                "ingrowthid":           None,
                "labid":                r[3] or "",
                "sampleid":             None,
                "prefix":               None,
            }
            if stype != 0 and r[3]:
                parts = str(r[3]).split("-", 1)
                if len(parts) == 2:
                    slot["prefix"] = parts[0]
                    try:
                        slot["sampleid"] = int(parts[1])
                    except ValueError:
                        pass
            self._load_list_data.append(slot)

        self._has_template = True
        self.btnAddCtrl.setEnabled(True)
        self._refresh_template_table()
        self._update_positions()

        n_samp  = sum(1 for d in self._load_list_data if registry.is_sample_slot(d["sampletype"]))
        n_fixed = len(self._load_list_data) - n_samp
        self.lblStatus.setText(
            f"Template: {len(self._load_list_data)} positions "
            f"({n_samp} sample slot(s), {n_fixed} fixed reference(s))."
        )

    def _slot_display_type(self, slot: dict) -> str:
        st = slot["sampletype"]
        if registry.is_dummy(st):
            return "Dummy"
        if registry.is_sample_slot(st):
            if slot.get("ctrl_sid") is not None:
                return "Control"
            return "Sample"
        if slot["bisblank"]:
            return "Blank"
        if slot["bislinreference"]:
            return "Lin Ref"
        if slot["bisreproreference"]:
            return "Repro Ref"
        short = registry.short(st)
        if short and not short.isdigit():
            return short
        return slot.get("sname") or slot.get("ourlabid") or f"Ref ({st})"

    def _slot_color(self, slot: dict) -> QColor:
        st = slot["sampletype"]
        if registry.is_dummy(st):
            return registry.color(st)
        if registry.is_sample_slot(st):
            if slot.get("ctrl_sid") is not None:
                return _COL_CTRL
            return _COL_SAMPLE_FILLED if slot["analysisid"] else _COL_SAMPLE_EMPTY
        if slot["bisblank"]:
            return _COL_BLANK
        if slot["bislinreference"]:
            return _COL_LIN
        if slot["bisreproreference"]:
            return _COL_REPRO
        return _COL_REF

    def _refresh_template_table(self):
        self.tblSelected.setRowCount(0)
        ng_mode  = (self._mode == "ng_sequence")
        seq_mode = (self._mode == "sequence")
        amt_col  = 5  # same for both ng_sequence and sequence (both now 7 cols)

        for slot in self._load_list_data:
            r = self.tblSelected.rowCount()
            self.tblSelected.insertRow(r)
            display_type = self._slot_display_type(slot)
            if registry.is_dummy(slot["sampletype"]):
                lab_id = ""
                sname  = "DUMMY – Pump out"
            elif registry.is_sample_slot(slot["sampletype"]) and slot.get("ctrl_sid") is not None:
                lab_id = f"{slot['ctrl_prefix']}-{slot['ctrl_sid']}"
                sname  = slot.get("ctrl_sname", "")
            else:
                lab_id = slot["ourlabid"] if registry.is_fixed_reference(slot["sampletype"]) else slot["labid"]
                sname  = slot["sname"]
            bg = self._slot_color(slot)

            def _ro_item(val, center=False):
                it = QTableWidgetItem(str(val) if val is not None else "")
                it.setData(Qt.UserRole, slot)
                it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                it.setBackground(QBrush(bg))
                if center:
                    it.setTextAlignment(Qt.AlignCenter)
                return it

            def _port_item(s=slot, b=bg):
                port_val = str(s["iport"]) if s.get("iport") is not None else ""
                it = QTableWidgetItem(port_val)
                it.setData(Qt.UserRole, s)
                it.setBackground(QBrush(b))
                it.setTextAlignment(Qt.AlignCenter)
                if not registry.is_sample_slot(s["sampletype"]):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                return it

            if ng_mode:
                # ["Inlet", "Port", "Type", "Lab ID", "Sample Name", "Amount", ""]
                self.tblSelected.setItem(r, 0, _ro_item(slot["pos"], center=True))
                self.tblSelected.setItem(r, 1, _port_item())
                self.tblSelected.setItem(r, 2, _ro_item(display_type))
                self.tblSelected.setItem(r, 3, _ro_item(lab_id))
                self.tblSelected.setItem(r, 4, _ro_item(sname))
            elif seq_mode:
                # ["Inlet", "Type", "Lab ID", "Sample Name", "Port", "Amount", ""]
                self.tblSelected.setItem(r, 0, _ro_item(slot["pos"], center=True))
                self.tblSelected.setItem(r, 1, _ro_item(display_type))
                self.tblSelected.setItem(r, 2, _ro_item(lab_id))
                self.tblSelected.setItem(r, 3, _ro_item(sname))
                self.tblSelected.setItem(r, 4, _port_item())
            else:
                self.tblSelected.setItem(r, 0, _ro_item(slot["pos"], center=True))
                self.tblSelected.setItem(r, 1, _ro_item(display_type))
                self.tblSelected.setItem(r, 2, _ro_item(lab_id))
                self.tblSelected.setItem(r, 3, _ro_item(sname))

            # Amount — QLineEdit accepts scientific notation (e.g. 3.016e-13 ccSTP)
            amt_raw = slot.get("freference_amount")
            amt_txt = f"{amt_raw:.6g}" if amt_raw is not None else ""
            amt_edit = QLineEdit(amt_txt)
            amt_edit.setPlaceholderText("ccSTP")
            amt_edit.setMaximumWidth(100)
            amt_edit.setToolTip("Reference gas amount (ccSTP). Scientific notation accepted.")
            amt_edit.setProperty("slot_ref", slot)
            amt_edit.textChanged.connect(self._on_amount_changed)
            self.tblSelected.setCellWidget(r, amt_col, amt_edit)

            # Per-row ▲▼✖ action buttons
            self.tblSelected.setCellWidget(r, amt_col + 1, self._row_action_widget(r))

    def _on_amount_changed(self, text: str):
        """Persist amount edits back to the in-memory slot dict."""
        edit = self.sender()
        if edit is None:
            return
        slot = edit.property("slot_ref")
        if slot is not None:
            try:
                slot["freference_amount"] = float(text) if text.strip() else None
            except ValueError:
                pass  # keep previous value until user finishes typing

    def _on_template_port_changed(self, item: QTableWidgetItem):
        """Persist port edits back to the in-memory slot dict (sample slots only)."""
        port_col = 1 if self._mode == "ng_sequence" else 4
        if item.column() != port_col:
            return
        slot = item.data(Qt.UserRole)
        if slot is None or not registry.is_sample_slot(slot.get("sampletype", -1)):
            return
        try:
            slot["iport"] = int(item.text()) if item.text().strip() else None
        except ValueError:
            pass

    def _move_row_up(self):
        """Move the selected template row up one position (per-run only)."""
        idxs = sorted({i.row() for i in self.tblSelected.selectedIndexes()})
        if not idxs or idxs[0] == 0:
            return
        r = idxs[0]
        self._load_list_data[r], self._load_list_data[r - 1] = \
            self._load_list_data[r - 1], self._load_list_data[r]
        self._renumber_template_positions()
        self._refresh_template_table()
        self.tblSelected.selectRow(r - 1)

    def _move_row_down(self):
        """Move the selected template row down one position (per-run only)."""
        idxs = sorted({i.row() for i in self.tblSelected.selectedIndexes()})
        if not idxs or idxs[-1] >= len(self._load_list_data) - 1:
            return
        r = idxs[-1]
        self._load_list_data[r], self._load_list_data[r + 1] = \
            self._load_list_data[r + 1], self._load_list_data[r]
        self._renumber_template_positions()
        self._refresh_template_table()
        self.tblSelected.selectRow(r + 1)

    def _delete_row(self):
        """Remove the selected rows from the per-run load list."""
        rows = sorted({i.row() for i in self.tblSelected.selectedIndexes()}, reverse=True)
        if not rows:
            return
        # Prevent deleting all rows
        if len(rows) >= len(self._load_list_data):
            show_message(self, "Cannot Delete",
                         "Cannot delete all positions. At least one must remain.",
                         QMessageBox.Warning)
            return
        for r in rows:
            self._load_list_data.pop(r)
        self._renumber_template_positions()
        self._refresh_template_table()
        self._update_positions()

    def _renumber_template_positions(self):
        """Re-number pos fields after row reorder / deletion."""
        for i, slot in enumerate(self._load_list_data, start=1):
            slot["pos"] = i

    def _row_action_widget(self, r: int) -> QWidget:
        """Compact ▲▼✖ widget for per-row actions in the template table."""
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        hlay = QHBoxLayout(container)
        hlay.setContentsMargins(2, 0, 2, 0)
        hlay.setSpacing(1)
        _btn_ss = (
            "QPushButton { background: #D0D8E4; color: #333; border: none; "
            "border-radius: 2px; font-size: 10px; padding: 0px; }"
            "QPushButton:hover   { background: #B0BECE; }"
            "QPushButton:pressed { background: #8FA0B4; color: white; }"
        )
        _del_ss = (
            "QPushButton { background: #E8C0BB; color: #6B1A15; border: none; "
            "border-radius: 2px; font-size: 10px; padding: 0px; }"
            "QPushButton:hover   { background: #D4948C; color: white; }"
            "QPushButton:pressed { background: #B06058; color: white; }"
        )
        for icon_text, tip, ss, slot in (
            ("▲", "Move up",   _btn_ss, lambda _c, row=r: self._move_row_up_at(row)),
            ("▼", "Move down", _btn_ss, lambda _c, row=r: self._move_row_down_at(row)),
            ("✖", "Remove",    _del_ss, lambda _c, row=r: self._delete_row_at(row)),
        ):
            btn = QPushButton(icon_text)
            btn.setFixedSize(18, 18)
            btn.setToolTip(tip)
            btn.setStyleSheet(ss)
            btn.clicked.connect(slot)
            hlay.addWidget(btn)
        hlay.addStretch(0)
        return container

    def _move_row_up_at(self, r: int):
        data = self._load_list_data
        if r <= 0 or r >= len(data):
            return
        data[r], data[r - 1] = data[r - 1], data[r]
        self._renumber_template_positions()
        target = r - 1
        # Defer refresh until after this click's event is fully unwound
        QTimer.singleShot(0, lambda: (self._refresh_template_table(),
                                      self.tblSelected.selectRow(target)))

    def _move_row_down_at(self, r: int):
        data = self._load_list_data
        if r < 0 or r >= len(data) - 1:
            return
        data[r], data[r + 1] = data[r + 1], data[r]
        self._renumber_template_positions()
        target = r + 1
        QTimer.singleShot(0, lambda: (self._refresh_template_table(),
                                      self.tblSelected.selectRow(target)))

    def _delete_row_at(self, r: int):
        data = self._load_list_data
        if r < 0 or r >= len(data):
            return
        if len(data) <= 1:
            show_message(self, "Cannot Delete",
                         "Cannot delete all positions. At least one must remain.",
                         QMessageBox.Warning)
            return
        data.pop(r)
        self._renumber_template_positions()
        QTimer.singleShot(0, lambda: (self._refresh_template_table(),
                                      self._update_positions()))

    # ══════════════════════════════════════════════════ Available samples ══════

    def _load_available_samples(self):
        if self._mode in ("sequence", "ng_sequence"):
            self._load_available_samples_sequence()
        elif self._mode == "extraction":
            self._load_available_samples_extraction()
        else:
            self._load_available_samples_ingrowth()

    def _load_available_samples_ingrowth(self):
        wf_id   = self.cmbWorkflow.currentData()
        proc_id = self.cmbProcedure.currentData()
        status  = self.cmbSampleStatus.currentData()

        self.tblAvailable.setRowCount(0)
        self._all_available.clear()

        if wf_id is None or proc_id is None:
            self.lblStatus.setText("Select a workflow to see available samples.")
            return

        params: dict = {"wfid": wf_id, "procid": proc_id}

        sql = f"""
            SELECT
                COALESCE(CAST(sb.submissionid AS VARCHAR), ''),
                sq.prefix || '-' || CAST(sq.sampleid AS VARCHAR),
                COALESCE(s.sname, ''),
                0,
                NULL::VARCHAR,
                sq.sampleid,
                sq.prefix,
                sq.workflowjobid,
                sq.priorityid
            FROM public.sample_queue sq
            JOIN public.sample s
                 ON s.sampleid = sq.sampleid AND s.prefix = sq.prefix
            JOIN public.workflowjob wj
                 ON wj.workflowjobid = sq.workflowjobid
            LEFT JOIN public.submission sb
                 ON sb.submissionid = s.submissionid
            WHERE wj.workflowid = :wfid
              AND wj.procedureid = :procid
              AND s.sampletype   = 0
            ORDER BY COALESCE(sq.priorityid, 9999), sb.submissiondate, sq.sampleid
        """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
        except Exception as e:
            logging.error(f"Available samples load failed: {e}")
            self.lblStatus.setText(f"Error: {e}")
            return

        already = self._already_selected_keys()
        for r in rows:
            r = tuple(r)
            key = (r[5], r[6])
            if key in already:
                continue
            d = {
                "sampleid": r[5], "prefix": r[6], "sname": r[2],
                "status": r[3], "workflowjobid": r[7],
                "submission": r[0], "labid": r[1], "paramvalue": r[4],
            }
            self._all_available.append(d)
            row_n = self.tblAvailable.rowCount()
            self.tblAvailable.insertRow(row_n)
            for col, val in enumerate([r[0], r[1], r[2], str(r[3]), str(r[4] or "")]):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, d)
                self.tblAvailable.setItem(row_n, col, it)

        self.lblStatus.setText(f"{len(self._all_available)} sample(s) available.")

    def _load_available_samples_extraction(self):
        wf_id   = self.cmbWorkflow.currentData()
        proc_id = self.cmbProcedure.currentData()

        self.tblAvailable.setRowCount(0)
        self._all_available.clear()

        if wf_id is None or proc_id is None:
            self.lblStatus.setText("Select a workflow to see available samples.")
            return

        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT
                        COALESCE(CAST(sb.submissionid AS VARCHAR), ''),
                        sq.prefix || '-' || CAST(sq.sampleid AS VARCHAR),
                        COALESCE(s.sname, ''),
                        sq.sampleid,
                        sq.prefix
                    FROM public.sample_queue sq
                    JOIN public.sample s
                         ON s.sampleid = sq.sampleid AND s.prefix = sq.prefix
                    JOIN public.workflowjob wj
                         ON wj.workflowjobid = sq.workflowjobid
                    LEFT JOIN public.submission sb
                         ON sb.submissionid = s.submissionid
                    WHERE wj.workflowid  = :wfid
                      AND wj.procedureid = :procid
                      AND s.sampletype   = 0
                      AND LOWER(wj.jobname) LIKE '%extraction%'
                    ORDER BY sb.submissionid, sq.sampleid
                """), {"wfid": wf_id, "procid": proc_id}).fetchall()
        except Exception as e:
            logging.error(f"Available extraction samples load failed: {e}")
            self.lblStatus.setText(f"Error: {e}")
            return

        already = self._already_selected_keys()
        for r in rows:
            r = tuple(r)
            key = (r[3], r[4])
            if key in already:
                continue
            d = {"submission": r[0], "labid": r[1], "sname": r[2],
                 "sampleid": r[3], "prefix": r[4]}
            self._all_available.append(d)
            row_n = self.tblAvailable.rowCount()
            self.tblAvailable.insertRow(row_n)
            for col, val in enumerate([r[0], r[1], r[2]]):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, d)
                self.tblAvailable.setItem(row_n, col, it)

        self.lblStatus.setText(f"{len(self._all_available)} sample(s) available for extraction.")

    def _load_available_samples_sequence(self):
        wf_id   = self.cmbWorkflow.currentData()
        proc_id = self.cmbProcedure.currentData()

        self.tblAvailable.setRowCount(0)
        self._all_available.clear()

        if wf_id is None or proc_id is None:
            self.lblStatus.setText("Select a workflow and procedure to see available samples.")
            return

        if self._mode == "ng_sequence":
            sql = """
                SELECT
                    COALESCE(CAST(sb.submissionid AS VARCHAR), ''),
                    sq.prefix || '-' || CAST(sq.sampleid AS VARCHAR),
                    COALESCE(s.sname, ''),
                    sq.source_aid AS analysisid,
                    extr.runid    AS extractionrunid,
                    sq.sampleid,
                    sq.prefix,
                    NULL::INTEGER AS ingrowthid
                FROM public.sample_queue sq
                JOIN public.sample s
                     ON s.sampleid = sq.sampleid AND s.prefix = sq.prefix
                JOIN public.workflowjob wj
                     ON wj.workflowjobid = sq.workflowjobid
                LEFT JOIN public.submission sb
                     ON sb.submissionid = s.submissionid
                LEFT JOIN ngam.ngextractiondata extr
                     ON extr.analysisid = sq.source_aid
                WHERE wj.workflowid  = :wfid
                  AND wj.procedureid = :procid
                  AND s.sampletype   = 0
                  AND sq.source_aid IS NOT NULL
                ORDER BY COALESCE(sq.priorityid, 9999), sb.submissiondate, sq.sampleid
            """
        else:
            sql = """
                SELECT
                    COALESCE(CAST(sb.submissionid AS VARCHAR), ''),
                    sq.prefix || '-' || CAST(sq.sampleid AS VARCHAR),
                    COALESCE(s.sname, ''),
                    sq.source_aid AS analysisid,
                    ingr.runid    AS ingrowthrunid,
                    sq.sampleid,
                    sq.prefix,
                    ingr.ingrowthid
                FROM public.sample_queue sq
                JOIN public.sample s
                     ON s.sampleid = sq.sampleid AND s.prefix = sq.prefix
                JOIN public.workflowjob wj
                     ON wj.workflowjobid = sq.workflowjobid
                LEFT JOIN public.submission sb
                     ON sb.submissionid = s.submissionid
                LEFT JOIN ngam.ng3heingrowthdata ingr
                     ON ingr.analysisid = sq.source_aid
                WHERE wj.workflowid  = :wfid
                  AND wj.procedureid = :procid
                  AND s.sampletype   = 0
                  AND sq.source_aid IS NOT NULL
                ORDER BY COALESCE(sq.priorityid, 9999), sb.submissiondate, sq.sampleid
            """
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), {"wfid": wf_id, "procid": proc_id}).fetchall()
        except Exception as e:
            logging.error(f"Sequence available samples load failed: {e}")
            self.lblStatus.setText(f"Error: {e}")
            return

        already = self._already_selected_keys()
        for r in rows:
            r = tuple(r)
            aid = r[3]
            if aid in already:
                continue
            d = {
                "submission":   r[0],
                "labid":        r[1],
                "sname":        r[2],
                "analysisid":   aid,
                "ingrowthrunid":r[4],
                "sampleid":     r[5],
                "prefix":       r[6],
                "ingrowthid":   r[7],
            }
            self._all_available.append(d)
            row_n = self.tblAvailable.rowCount()
            self.tblAvailable.insertRow(row_n)
            for col, val in enumerate([
                r[0],
                r[1],
                r[2],
                str(aid or ""),
                str(r[4] or ""),
            ]):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, d)
                self.tblAvailable.setItem(row_n, col, it)

        self.lblStatus.setText(f"{len(self._all_available)} completed sample(s) available.")

    # ════════════════════════════════════════════════ Transfer / selection ═════

    def _already_selected_keys(self):
        if self._mode in ("sequence", "ng_sequence"):
            return {slot["analysisid"]
                    for slot in self._load_list_data
                    if registry.is_sample_slot(slot["sampletype"]) and slot["analysisid"] is not None}
        else:
            keys = set()
            for r in range(self.tblSelected.rowCount()):
                it = self.tblSelected.item(r, 0)
                if it:
                    d = it.data(Qt.UserRole)
                    if d:
                        keys.add((d["sampleid"], d["prefix"]))
            return keys

    def _add_row(self, d: dict):
        if self._mode in ("sequence", "ng_sequence"):
            if not self._has_template:
                self.lblStatus.setText("No template loaded — select a procedure first.")
                return
            for slot in self._load_list_data:
                if (registry.is_sample_slot(slot["sampletype"])
                        and slot["analysisid"] is None
                        and slot.get("ctrl_sid") is None):
                    slot["analysisid"]    = d["analysisid"]
                    slot["ingrowthid"]    = d.get("ingrowthid")
                    slot["labid"]         = d["labid"]
                    slot["sname"]         = d["sname"]
                    slot["sampleid"]      = d["sampleid"]
                    slot["prefix"]        = d["prefix"]
                    break
            else:
                self.lblStatus.setText("All sample slots are already filled.")
                return
            self._refresh_template_table()
            self._update_positions()

        elif self._mode == "extraction":
            already = self._already_selected_keys()
            if (d["sampleid"], d["prefix"]) in already:
                return
            if self._proc_num_samples and \
                    self.tblSelected.rowCount() >= self._proc_num_samples:
                self.lblStatus.setText(
                    f"Run full — max {self._proc_num_samples} samples.")
                return
            r = self.tblSelected.rowCount()
            self.tblSelected.insertRow(r)
            for col, val in enumerate([str(r + 1), d["labid"], d["sname"]]):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, d)
                if col == 0:
                    it.setTextAlignment(Qt.AlignCenter)
                self.tblSelected.setItem(r, col, it)
            self._remove_from_available(d)
            self._update_positions()

        else:
            # Ingrowth mode
            already = self._already_selected_keys()
            if (d["sampleid"], d["prefix"]) in already:
                return
            if self._proc_num_samples and \
                    self.tblSelected.rowCount() >= self._proc_num_samples:
                self.lblStatus.setText(
                    f"Run full — max {self._proc_num_samples} samples.")
                return
            r = self.tblSelected.rowCount()
            self.tblSelected.insertRow(r)
            vals = [
                str(r + 1),
                "—",
                f"{d['prefix']}-{d['sampleid']}  {d['sname']}",
                str(d["status"]),
                str(d.get("paramvalue") or ""),
            ]
            for col, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setData(Qt.UserRole, d)
                if col in (0, 1, 3, 4):
                    it.setTextAlignment(Qt.AlignCenter)
                self.tblSelected.setItem(r, col, it)
            self._remove_from_available(d)
            self._update_positions()

    def _remove_from_available(self, d: dict):
        if self._mode in ("sequence", "ng_sequence"):
            key_fn = lambda rd: rd["analysisid"] == d["analysisid"]
            filt   = lambda x: x["analysisid"] != d["analysisid"]
        else:
            key_fn = lambda rd: rd["sampleid"] == d["sampleid"] and rd["prefix"] == d["prefix"]
            filt   = lambda x: not (x["sampleid"] == d["sampleid"] and x["prefix"] == d["prefix"])

        for row in range(self.tblAvailable.rowCount() - 1, -1, -1):
            it = self.tblAvailable.item(row, 0)
            if it:
                rd = it.data(Qt.UserRole)
                if rd and key_fn(rd):
                    self.tblAvailable.removeRow(row)
                    self._all_available = [x for x in self._all_available if filt(x)]
                    break

    def _add_selected(self):
        seen = set()
        for idx in self.tblAvailable.selectedIndexes():
            if idx.row() in seen:
                continue
            seen.add(idx.row())
            it = self.tblAvailable.item(idx.row(), 0)
            if it:
                self._add_row(it.data(Qt.UserRole))

    def _add_all(self):
        rows_data = []
        for row in range(self.tblAvailable.rowCount()):
            if not self.tblAvailable.isRowHidden(row):
                it = self.tblAvailable.item(row, 0)
                if it:
                    rows_data.append(it.data(Qt.UserRole))
        for d in rows_data:
            self._add_row(d)

    def _open_add_ctrl_dialog(self):
        dlg = NgamAddCtrlSampleDialog(self._load_list_data, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self._refresh_template_table()
            self._update_positions()

    def _remove_selected(self):
        if self._mode in ("sequence", "ng_sequence"):
            selected_rows = sorted({idx.row() for idx in self.tblSelected.selectedIndexes()})
            for r in selected_rows:
                if r < len(self._load_list_data):
                    slot = self._load_list_data[r]
                    if registry.is_sample_slot(slot["sampletype"]) and (
                        slot["analysisid"] is not None or slot.get("ctrl_sid") is not None
                    ):
                        slot["analysisid"]  = None
                        slot["ingrowthid"]  = None
                        slot["labid"]       = ""
                        slot["sname"]       = ""
                        slot["sampleid"]    = None
                        slot["prefix"]      = None
                        slot["ctrl_sid"]    = None
                        slot["ctrl_prefix"] = None
                        slot["ctrl_sname"]  = None
            self._refresh_template_table()
            self._load_available_samples()
            self._update_positions()
        else:
            rows = sorted(
                {idx.row() for idx in self.tblSelected.selectedIndexes()}, reverse=True)
            for r in rows:
                self.tblSelected.removeRow(r)
            self._renumber_positions()
            self._load_available_samples()

    def _remove_all(self):
        if self._mode in ("sequence", "ng_sequence"):
            for slot in self._load_list_data:
                if registry.is_sample_slot(slot["sampletype"]):
                    slot["analysisid"]  = None
                    slot["ingrowthid"]  = None
                    slot["labid"]       = ""
                    slot["sname"]       = ""
                    slot["sampleid"]    = None
                    slot["prefix"]      = None
                    slot["ctrl_sid"]    = None
                    slot["ctrl_prefix"] = None
                    slot["ctrl_sname"]  = None
            self._refresh_template_table()
            self._load_available_samples()
            self._update_positions()
        else:
            self.tblSelected.setRowCount(0)
            self._load_available_samples()
            self._update_positions()

    def _renumber_positions(self, *_):
        for r in range(self.tblSelected.rowCount()):
            it = self.tblSelected.item(r, 0)
            if it:
                it.setText(str(r + 1))
        self._update_positions()

    def _update_positions(self):
        if self._mode in ("sequence", "ng_sequence"):
            total  = sum(1 for d in self._load_list_data if registry.is_sample_slot(d["sampletype"]))
            filled = sum(1 for d in self._load_list_data
                         if registry.is_sample_slot(d["sampletype"]) and d["analysisid"] is not None)
            self.lblPosAvail.setText(str(total - filled))
            self.lblPosTotal.setText(str(total) if total else "—")
        else:
            n   = self.tblSelected.rowCount()
            cap = self._proc_num_samples
            avail = max(cap - n, 0) if cap else "∞"
            self.lblPosAvail.setText(str(avail))
            self.lblPosTotal.setText(str(cap) if cap else "∞")

    # ══════════════════════════════════════════════════════════ Save / create ══

    def _save_run(self):
        if self.cmbWorkflow.currentData() is None:
            show_message(self, "Validation", "Please select a Workflow.", QMessageBox.Warning)
            return
        if self._wj_id is None:
            show_message(self, "Validation", "Please select a Workflow Job.", QMessageBox.Warning)
            return
        if self.cmbProcedure.currentData() is None:
            show_message(self, "Validation", "Please select a Procedure.", QMessageBox.Warning)
            return

        if self._mode == "sequence":
            self._save_sequence_run()
        elif self._mode == "ng_sequence":
            self._save_ng_sequence_run()
        elif self._mode == "extraction":
            if self.tblSelected.rowCount() == 0:
                show_message(self, "Validation",
                             "Add at least one sample to the run.", QMessageBox.Warning)
                return
            self._save_extraction_run()
        else:
            if self.tblSelected.rowCount() == 0:
                show_message(self, "Validation",
                             "Add at least one sample to the run.", QMessageBox.Warning)
                return
            self._save_ingrowth_run()

    # ── extraction save ────────────────────────────────────────────────────────
    def _save_extraction_run(self):
        wf_id    = self.cmbWorkflow.currentData()
        proc_id  = self.cmbProcedure.currentData()
        equip_id = self.cmbEquipment.currentData()
        now      = datetime.now()

        if equip_id is None:
            show_message(self, "Validation", "Please select a Device.", QMessageBox.Warning)
            return

        selected = []
        for r in range(self.tblSelected.rowCount()):
            it = self.tblSelected.item(r, 0)
            if it:
                d = it.data(Qt.UserRole)
                if d:
                    selected.append(d)

        reply = QMessageBox.question(
            self, "Create Extraction Run",
            "Create NG Extraction Run?\n\n"
            f"Workflow:  {self.cmbWorkflow.currentText()}\n"
            f"Procedure: {self.cmbProcedure.currentText()}\n"
            f"Samples:   {len(selected)}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            usr = normalize_login_name(get_current_user_id())
        except Exception:
            usr = "unknown"

        try:
            with db_manager.get_connection() as conn:
                run_id = conn.execute(text("""
                    INSERT INTO ngam.ngextractionrun
                        (procedureid, equipmentid, workflowid,
                         islocked, runstatus, runstarttime,
                         createdatestamp, createuserstamp)
                    VALUES (:pid, :eid, :wfid, 0, 4, :now, :now, :usr)
                    RETURNING runid
                """), {
                    "pid": proc_id, "eid": equip_id,
                    "wfid": wf_id, "now": now, "usr": usr,
                }).scalar()

                for pos, sample in enumerate(selected, start=1):
                    current_aid = conn.execute(text("""
                        INSERT INTO public.analysis
                            (sampleid, prefix, repeats, status,
                             workflowid, createdatestamp, createuserstamp)
                        VALUES (:sid, :pfx, 1, 200, :wfid, :now, :usr)
                        RETURNING analysisid
                    """), {
                        "sid": sample["sampleid"],
                        "pfx": sample["prefix"], "wfid": wf_id,
                        "now": now, "usr": usr,
                    }).scalar()

                    conn.execute(text("""
                        INSERT INTO ngam.ngextractiondata
                            (analysisid, runid, iposition, status, dtimestart)
                        VALUES (:aid, :rid, :pos, 4, :now)
                    """), {"aid": current_aid, "rid": run_id, "pos": pos, "now": now})

                    conn.execute(text("""
                        DELETE FROM public.sample_queue
                        WHERE sampleid = :sid AND prefix = :pfx
                    """), {"sid": sample["sampleid"], "pfx": sample["prefix"]})

                    conn.execute(text("""
                        UPDATE public.sample SET status = 200
                        WHERE sampleid = :sid AND prefix = :pfx
                    """), {"sid": sample["sampleid"], "pfx": sample["prefix"]})

                conn.commit()

            logging.info(f"Extraction Run {run_id} created — {len(selected)} samples.")
            QMessageBox.information(
                self, "Run Created",
                f"NG Extraction Run {run_id} created with {len(selected)} sample(s).",
            )
            self.accept()

        except Exception as e:
            logging.error(f"Create extraction run failed: {e}")
            show_message(self, "Save Error", str(e), QMessageBox.Critical)

    # ── ingrowth save ─────────────────────────────────────────────────────────
    def _save_ingrowth_run(self):
        wf_id    = self.cmbWorkflow.currentData()
        proc_id  = self.cmbProcedure.currentData()
        equip_id = self.cmbEquipment.currentData()
        tech_id  = self.cmbTech.currentData()
        start    = self.dtStart.dateTime().toPyDateTime()
        remarks  = self.txtRemarks.toPlainText().strip()[:255]
        now      = datetime.now()
        usr      = normalize_login_name(get_current_user_id())

        selected: list = []
        for r in range(self.tblSelected.rowCount()):
            it = self.tblSelected.item(r, 0)
            if it:
                d = dict(it.data(Qt.UserRole))
                d["position"] = r + 1
                selected.append(d)

        reply = QMessageBox.question(
            self, "Create New Ingrowth Run",
            f"Create 3He Ingrowth Run  Batch No. {self._batch_no}?\n\n"
            f"Workflow:  {self.cmbWorkflow.currentText()}\n"
            f"Procedure: {self.cmbProcedure.currentText()}\n"
            f"Samples:   {len(selected)}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                if db_manager.dialect == "SQL_SERVER":
                    conn.execute(text("""
                        INSERT INTO ngam.ng3heingrowthrun
                            (workflowid, workflowjobid, procedureid, equipmentid,
                             runstarttime, technicianid, runstatus, islocked,
                             remarks, createdatestamp, createuserstamp)
                        VALUES
                            (:wfid, :wjid, :proc, :equip,
                             :st, :tech, 4, 0,
                             :rem, :now, :usr)
                    """), {
                        "wfid": wf_id, "wjid": self._wj_id, "proc": proc_id,
                        "equip": equip_id, "st": start, "tech": tech_id,
                        "rem": remarks, "now": now, "usr": usr,
                    })
                    run_id = conn.execute(text("SELECT SCOPE_IDENTITY()")).scalar()
                else:
                    run_id = conn.execute(text("""
                        INSERT INTO ngam.ng3heingrowthrun
                            (workflowid, workflowjobid, procedureid, equipmentid,
                             runstarttime, technicianid, runstatus, islocked,
                             remarks, createdatestamp, createuserstamp)
                        VALUES
                            (:wfid, :wjid, :proc, :equip,
                             :st, :tech, 4, FALSE,
                             :rem, :now, :usr)
                        RETURNING runid
                    """), {
                        "wfid": wf_id, "wjid": self._wj_id, "proc": proc_id,
                        "equip": equip_id, "st": start, "tech": tech_id,
                        "rem": remarks, "now": now, "usr": usr,
                    }).scalar()

                for d in selected:
                    sid, pfx, pos = d["sampleid"], d["prefix"], d["position"]

                    current_aid = conn.execute(text("""
                        INSERT INTO public.analysis
                            (sampleid, prefix, workflowid,
                             repeats, status, createdatestamp, createuserstamp)
                        VALUES (:sid, :pfx, :wfid, :rpts, 200, :now, :usr)
                        RETURNING analysisid
                    """), {
                        "sid": sid, "pfx": pfx,
                        "wfid": wf_id, "rpts": self._proc_repeats, "now": now, "usr": usr,
                    }).scalar()

                    conn.execute(text("""
                        INSERT INTO ngam.ng3heingrowthdata
                            (runid, analysisid, iposition, status, repeat, dtimestart)
                        VALUES (:rid, :aid, :pos, 4, 0, :st)
                    """), {
                        "rid": run_id, "aid": current_aid, "pos": pos, "st": start,
                    })

                    conn.execute(text("""
                        DELETE FROM public.sample_queue
                        WHERE sampleid = :sid AND prefix = :pfx
                    """), {"sid": sid, "pfx": pfx})

                    conn.execute(text("""
                        UPDATE public.sample SET status = 200
                        WHERE sampleid = :sid AND prefix = :pfx
                    """), {"sid": sid, "pfx": pfx})

                conn.commit()

            logging.info(f"Ingrowth Run {run_id} created — {len(selected)} samples.")
            QMessageBox.information(
                self, "Run Created",
                f"3He Ingrowth Run {run_id} created with {len(selected)} sample(s).",
            )
            self.accept()

        except Exception as e:
            logging.error(f"Create run failed: {e}")
            show_message(self, "Save Error", str(e), QMessageBox.Critical)

    # ── sequence save ─────────────────────────────────────────────────────────
    def _save_sequence_run(self):
        if not self._has_template:
            show_message(self, "Validation",
                         "No load list template loaded — cannot create run.",
                         QMessageBox.Warning)
            return

        unfilled = [d for d in self._load_list_data
                    if registry.is_sample_slot(d["sampletype"])
                    and d["analysisid"] is None
                    and d.get("ctrl_sid") is None]
        if unfilled:
            show_message(
                self, "Validation",
                f"{len(unfilled)} sample slot(s) are still empty.\n"
                "Please fill all sample positions before creating the run.",
                QMessageBox.Warning,
            )
            return

        # Warn if any standard slot has no reference amount.
        # Gas-spike standards need freferenceamount (ccSTP) in the template;
        # water tritium standards can rely on referencecontroldata TU values instead.
        stds_no_amt = [
            d for d in self._load_list_data
            if (d.get("bisreproreference") or d.get("bislinreference"))
            and not (d.get("freference_amount") and float(d["freference_amount"]) > 0)
        ]
        if stds_no_amt:
            labels = ", ".join(
                str(d.get("ourlabid") or d.get("sname") or f"pos {d.get('pos', '?')}")
                for d in stds_no_amt
            )
            reply_amt = QMessageBox.question(
                self, "Missing Reference Amount",
                f"Standard position(s) with no ccSTP reference amount in template:\n\n"
                f"  {labels}\n\n"
                f"The import GUI will fall back to the template value at processing time,\n"
                f"but it is better to set freferenceamount in the template editor now.\n\n"
                f"Continue anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply_amt != QMessageBox.Yes:
                return

        wf_id    = self.cmbWorkflow.currentData()
        proc_id  = self.cmbProcedure.currentData()
        equip_id = self.cmbEquipment.currentData()
        tech_id  = self.cmbTech.currentData()
        start    = self.dtStart.dateTime().toPyDateTime()
        remarks  = self.txtRemarks.toPlainText().strip()[:255]
        now      = datetime.now()
        usr      = normalize_login_name(get_current_user_id())

        n_samp  = sum(1 for d in self._load_list_data if registry.is_sample_slot(d["sampletype"]))
        n_fixed = len(self._load_list_data) - n_samp

        reply = QMessageBox.question(
            self, "Create Sequence Run",
            f"Create 3He Sequence/Measurement Run  Batch No. {self._batch_no}?\n\n"
            f"Workflow:   {self.cmbWorkflow.currentText()}\n"
            f"Procedure:  {self.cmbProcedure.currentText()}\n"
            f"Positions:  {len(self._load_list_data)}  "
            f"({n_samp} sample(s), {n_fixed} fixed reference(s))",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                run_id = conn.execute(text("""
                    INSERT INTO ngam.msrun
                        (workflowid, workflowjobid, procedureid, equipmentid,
                         technicianid, runstarttime, runstatus, islocked,
                         remarks, createdatestamp, createuserstamp, measurement_mode)
                    VALUES
                        (:wfid, :wjid, :proc, :equip,
                         :tech, :st, 6, FALSE,
                         :rem, :now, :usr, 'IG')
                    RETURNING runid
                """), {
                    "wfid": wf_id, "wjid": self._wj_id,
                    "proc": proc_id, "equip": equip_id, "tech": tech_id,
                    "st": start, "rem": remarks, "now": now, "usr": usr,
                }).scalar()

                for slot in self._load_list_data:
                    aid = slot["analysisid"]

                    if registry.is_dummy(slot["sampletype"]):
                        aid = conn.execute(text("""
                            INSERT INTO public.analysis
                                (workflowid,
                                 repeats, status, createdatestamp, createuserstamp)
                            VALUES (:wfid, 1, 6, :now, :usr)
                            RETURNING analysisid
                        """), {"wfid": wf_id, "now": now, "usr": usr}).scalar()
                    elif registry.is_fixed_reference(slot["sampletype"]):
                        aid = conn.execute(text("""
                            INSERT INTO public.analysis
                                (sampleid, prefix, workflowid,
                                 repeats, status, createdatestamp, createuserstamp)
                            VALUES (:sid, :pfx, :wfid, 1, 6, :now, :usr)
                            RETURNING analysisid
                        """), {
                            "sid": slot["sampleid"],
                            "pfx": slot["prefix"], "wfid": wf_id,
                            "now": now, "usr": usr,
                        }).scalar()
                    elif registry.is_sample_slot(slot["sampletype"]) and slot.get("ctrl_sid") is not None:
                        # Control sample assigned via Add Ctrl dialog — no prior Analysis row
                        aid = conn.execute(text("""
                            INSERT INTO public.analysis
                                (sampleid, prefix, workflowid,
                                 repeats, status, createdatestamp, createuserstamp)
                            VALUES (:sid, :pfx, :wfid, 1, 6, :now, :usr)
                            RETURNING analysisid
                        """), {
                            "sid": slot["ctrl_sid"],
                            "pfx": slot["ctrl_prefix"], "wfid": wf_id,
                            "now": now, "usr": usr,
                        }).scalar()

                    is_ingrown_sample = (
                        registry.is_sample_slot(slot["sampletype"])
                        and not slot.get("bisblank")
                        and slot.get("ctrl_sid") is None
                    )
                    is_std = (
                        slot.get("bisreproreference") or slot.get("bislinreference")
                    )

                    # sampleamount (ccSTP): NULL for ingrown samples
                    # (computed after MS processing); ccSTP gas amount for all others.
                    sampleamt = None if is_ingrown_sample else slot.get("freference_amount")

                    # sampletype: read the authoritative type from referencecontrol
                    # so the MS processor can identify reference gas standards.
                    stype = slot["sampletype"]
                    sid   = slot.get("sampleid")
                    pfx   = slot.get("prefix")
                    if not is_ingrown_sample and sid and pfx:
                        rc = conn.execute(text("""
                            SELECT sampletype FROM public.referencecontrol
                            WHERE sampleid = :sid AND prefix = :pfx
                              AND availabledateto IS NULL
                            LIMIT 1
                        """), {"sid": sid, "pfx": pfx}).fetchone()
                        if rc and rc[0] is not None:
                            stype = int(rc[0])

                    # knownstdactivity: TU-valued certified activity from referencecontroldata.
                    # Only set when get_standard_activity returns a valid TU value.
                    # For gas-phase standards (e.g. He_3_Spike, He_3_Water measurable in
                    # ccSTP/g), TU lookup returns 0 — leave knownstdactivity NULL and rely
                    # on sampleamount (ccSTP dispensed) for calibration at import time.
                    known_std     = None
                    known_std_uid = None
                    if is_std and sampleamt and sampleamt > 0 and sid:
                        tu, _unc, uid, _dc = get_standard_activity(
                            conn, sid, start, target_unit_str="TU"
                        )
                        if tu and tu > 0:
                            known_std     = tu
                            known_std_uid = uid   # FK → public.measurementunit (TU)
                        # else: gas-spike standard — sampleamount is the calibrant; knownstdactivity stays NULL

                    conn.execute(text("""
                        INSERT INTO ngam.ng3hesequenceloadlist
                            (runid, analysisid, ingrowthid, positioninrun,
                             positionintray, sampletype,
                             sampleamount, knownstdactivity, knownstdactivityunitid,
                             bisblank, bisreproreference, bislinreference,
                             isrejected, status,
                             createdatestamp, createuserstamp)
                        VALUES
                            (:rid, :aid, :iid, :pos,
                             :iport, :stype,
                             :sampleamt, :known_std, :known_std_uid,
                             :bisblank, :bisrepro, :bislin,
                             FALSE, 6,
                             :now, :usr)
                    """), {
                        "rid":          run_id,
                        "aid":          aid,
                        "iid":          slot.get("ingrowthid"),
                        "pos":          slot["pos"],
                        "iport":        slot["iport"],
                        "stype":        stype,
                        "sampleamt":    sampleamt,
                        "known_std":    known_std,
                        "known_std_uid": known_std_uid,
                        "bisblank":   bool(slot.get("bisblank", False)),
                        "bisrepro":   bool(slot.get("bisreproreference", False)),
                        "bislin":     bool(slot.get("bislinreference", False)),
                        "now":        now,
                        "usr":        usr,
                    })

                conn.commit()

            logging.info(f"Sequence Run {run_id} created — {len(self._load_list_data)} positions.")
            QMessageBox.information(
                self, "Run Created",
                f"3He Sequence Run {run_id} created.\n"
                f"{n_samp} sample(s) + {n_fixed} fixed reference position(s).",
            )
            self.accept()

        except Exception as e:
            logging.error(f"Create sequence run failed: {e}")
            show_message(self, "Save Error", str(e), QMessageBox.Critical)

    # ── NG sequence save (NGSequenceRun + NGPreparations) ─────────────────────
    def _save_ng_sequence_run(self):
        if not self._has_template:
            show_message(self, "Validation",
                         "No load list template loaded — cannot create run.",
                         QMessageBox.Warning)
            return

        unfilled = [d for d in self._load_list_data
                    if registry.is_sample_slot(d["sampletype"])
                    and d["analysisid"] is None
                    and d.get("ctrl_sid") is None]
        if unfilled:
            show_message(
                self, "Validation",
                f"{len(unfilled)} sample slot(s) are still empty.\n"
                "Please fill all sample positions before creating the run.",
                QMessageBox.Warning,
            )
            return

        wf_id    = self.cmbWorkflow.currentData()
        proc_id  = self.cmbProcedure.currentData()
        equip_id = self.cmbEquipment.currentData()
        start    = self.dtStart.dateTime().toPyDateTime()
        now      = datetime.now()
        usr      = normalize_login_name(get_current_user_id())

        n_samp  = sum(1 for d in self._load_list_data if registry.is_sample_slot(d["sampletype"]))
        n_fixed = len(self._load_list_data) - n_samp

        reply = QMessageBox.question(
            self, "Create NG Sequence Run",
            f"Create Noble Gas MS Sequence Run  No. {self._batch_no}?\n\n"
            f"Workflow:   {self.cmbWorkflow.currentText()}\n"
            f"Procedure:  {self.cmbProcedure.currentText()}\n"
            f"Positions:  {len(self._load_list_data)}  "
            f"({n_samp} sample(s), {n_fixed} fixed reference(s))",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                run_id = conn.execute(text("""
                    INSERT INTO ngam.msrun
                        (runstatus, workflowid,
                         islocked, procedureid, equipmentid,
                         runstarttime, createdatestamp, createuserstamp, measurement_mode)
                    VALUES
                        (6, :wfid,
                         FALSE, :proc, :equip,
                         :st, :now, :usr, 'NG')
                    RETURNING runid
                """), {
                    "wfid": wf_id,
                    "proc": proc_id, "equip": equip_id,
                    "st": start, "now": now, "usr": usr,
                }).scalar()

                for slot in self._load_list_data:
                    aid = slot["analysisid"]

                    if registry.is_dummy(slot["sampletype"]):
                        aid = conn.execute(text("""
                            INSERT INTO public.analysis
                                (workflowid,
                                 repeats, status, createdatestamp, createuserstamp)
                            VALUES (:wfid, 1, 6, :now, :usr)
                            RETURNING analysisid
                        """), {"wfid": wf_id, "now": now, "usr": usr}).scalar()
                    elif registry.is_fixed_reference(slot["sampletype"]):
                        aid = conn.execute(text("""
                            INSERT INTO public.analysis
                                (sampleid, prefix, workflowid,
                                 repeats, status, createdatestamp, createuserstamp)
                            VALUES (:sid, :pfx, :wfid, 1, 6, :now, :usr)
                            RETURNING analysisid
                        """), {
                            "sid": slot["sampleid"],
                            "pfx": slot["prefix"], "wfid": wf_id,
                            "now": now, "usr": usr,
                        }).scalar()

                    conn.execute(text("""
                        INSERT INTO ngam.ngpreparations
                            (runid, analysisid, positioninrun,
                             iportnumber,
                             bisblank, bisreproreference, bislinreference,
                             freferenceamount)
                        VALUES
                            (:rid, :aid, :pos,
                             :port,
                             :blank, :repro, :lin,
                             :refamt)
                    """), {
                        "rid":    run_id,
                        "aid":    aid,
                        "pos":    slot["pos"],
                        "port":   slot["iport"],
                        "blank":  slot["bisblank"],
                        "repro":  slot["bisreproreference"],
                        "lin":    slot["bislinreference"],
                        "refamt": slot["freference_amount"],
                    })

                    conn.execute(text("""
                        UPDATE public.analysis SET status = 6 WHERE analysisid = :aid
                    """), {"aid": aid})

                conn.commit()

            logging.info(f"NG Sequence Run {run_id} created — {len(self._load_list_data)} positions.")
            QMessageBox.information(
                self, "Run Created",
                f"Noble Gas Sequence Run {run_id} created.\n"
                f"{n_samp} sample(s) + {n_fixed} fixed reference position(s).",
            )
            self.accept()

        except Exception as e:
            logging.error(f"Create NG sequence run failed: {e}")
            show_message(self, "Save Error", str(e), QMessageBox.Critical)


# Backward-compatible alias — existing callers don't need to change
NGAMIngrowthCreateRunDialog = NGAMCreateRunDialog


# ---------------------------------------------------------------------------
# Add Control Sample dialog (NGAM sequence / ng_sequence modes)
# ---------------------------------------------------------------------------

class NgamAddCtrlSampleDialog(QDialog):
    """
    Assign an active ReferenceControl sample to one or more empty sample
    slots in the NGAM sequence load list.

    The dialog populates ctrl_sid / ctrl_prefix / ctrl_sname on matching
    slots in-place.  No Analysis record is created here; that happens at
    run-save time in _save_3he_sequence_run / _save_ng_sequence_run.

    Position From/To operate on the 1-based `pos` value stored in each slot.
    "Fill all empty sample slots" ignores the range and fills every sample
    slot that has neither an analysisid nor a ctrl_sid yet.
    """

    def __init__(self, load_list_data: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Control Sample")
        self.resize(540, 240)
        self._load_list_data = load_list_data

        self._init_ui()
        self._load_controls()
        self._load_positions()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self.cmbSample  = QComboBox()
        self.cmbPosFrom = QComboBox()
        self.cmbPosTo   = QComboBox()
        self.chkAll     = QCheckBox("Fill all empty sample slots")
        self.chkAll.setToolTip(
            "Ignore the position range and fill every sample slot that "
            "has not yet been assigned an analysis or control sample."
        )

        form.addRow("Control Sample:",  self.cmbSample)
        form.addRow("Position From:",   self.cmbPosFrom)
        form.addRow("Position To:",     self.cmbPosTo)
        form.addRow("",                 self.chkAll)
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

    def _toggle_pos(self, checked: bool):
        self.cmbPosFrom.setEnabled(not checked)
        self.cmbPosTo.setEnabled(not checked)

    # ── data loading ──────────────────────────────────────────────────────────

    def _load_controls(self):
        """Populate cmbSample from ReferenceControl (AvailableDateTo IS NULL)."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT rc.SampleID, rc.Prefix, s.sName, s.SampleType,
                           st.strShortDescription
                    FROM   public.ReferenceControl rc
                    JOIN   public.Sample s
                             ON rc.SampleID = s.SampleID AND rc.Prefix = s.Prefix
                    LEFT JOIN public.tblSampleType st
                             ON st.intSampleType = s.SampleType
                    WHERE  rc.AvailableDateTo IS NULL
                    ORDER  BY s.SampleType, rc.SampleID
                """)).fetchall()

            for r in rows:
                type_lbl = r.strShortDescription or str(r.SampleType)
                label    = f"{r.Prefix}-{r.SampleID}  {r.sName}  [{type_lbl}]"
                self.cmbSample.addItem(label, {
                    "sid":    r.SampleID,
                    "prefix": r.Prefix,
                    "sname":  r.sName,
                })
        except Exception as e:
            logging.error(f"NgamAddCtrlSampleDialog._load_controls: {e}")

    def _load_positions(self):
        """Populate position combos from all sample slots in the load list."""
        self.cmbPosFrom.clear()
        self.cmbPosTo.clear()
        for slot in self._load_list_data:
            if registry.is_sample_slot(slot["sampletype"]):
                pos_str = str(slot["pos"])
                self.cmbPosFrom.addItem(pos_str)
                self.cmbPosTo.addItem(pos_str)
        if self.cmbPosTo.count() > 0:
            self.cmbPosTo.setCurrentIndex(self.cmbPosTo.count() - 1)

    # ── apply ─────────────────────────────────────────────────────────────────

    def _apply(self):
        data = self.cmbSample.currentData()
        if not data:
            show_message(self, "No Sample", "No control sample selected.")
            return

        sid   = data["sid"]
        pfx   = data["prefix"]
        sname = data["sname"]

        fill_all = self.chkAll.isChecked()
        try:
            p_from = int(self.cmbPosFrom.currentText()) if not fill_all else 1
            p_to   = int(self.cmbPosTo.currentText())   if not fill_all else 999999
        except ValueError:
            p_from, p_to = 1, 999999

        updated = 0
        for slot in self._load_list_data:
            if not registry.is_sample_slot(slot["sampletype"]):
                continue
            if not (p_from <= slot["pos"] <= p_to):
                continue
            # Only fill slots that are genuinely unoccupied
            if slot["analysisid"] is not None or slot.get("ctrl_sid") is not None:
                continue

            slot["ctrl_sid"]    = sid
            slot["ctrl_prefix"] = pfx
            slot["ctrl_sname"]  = sname
            updated += 1

        if updated:
            QMessageBox.information(
                self, "Done",
                f"Applied control sample to {updated} slot(s)."
            )
            self.accept()
        else:
            QMessageBox.warning(
                self, "No Match",
                "No empty sample slots matched the selection.\n"
                "Slots already assigned (analysis or control) are skipped."
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    dlg = NGAMCreateRunDialog()
    dlg.exec_()
