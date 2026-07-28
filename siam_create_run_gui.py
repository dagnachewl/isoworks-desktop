"""
siam_create_run_gui.py — SIAM analysis run creation widget for IsoWorks.
Provides SiamCreateRunWidget for building a new SIAM run by selecting a
workflow, assigning samples to a load list, and committing the run to the database.
"""
from __future__ import annotations
import sys
import logging
from datetime import datetime
import getpass

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QDialog, QAbstractItemView, QHeaderView, QSizePolicy
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QFont, QBrush, QColor
from PyQt5.QtCore import Qt, QTimer, pyqtSignal

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message

# --- Optional Template Editor ---
try: 
    from siam_template_editor_gui import SiamTemplateEditorDialog
except ImportError: 
    SiamTemplateEditorDialog = None

class SiamCreateRunWidget(QWidget):
    # Signals
    runCreated = pyqtSignal()
    requestClose = pyqtSignal()
    requestEditTemplate = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(10)
        
        self.available_samples_model = QStandardItemModel()
        self.run_loadlist_model = QStandardItemModel()
        self.next_analysis_id = 1
        self.db_dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')

        # --- 1. Top Bar ---
        self.main_layout.addLayout(self._create_top_bar())

        # --- 2. Main Content ---
        self.content_layout = QHBoxLayout()
        self.content_layout.addLayout(self._create_left_panel(), 3)
        self.content_layout.addLayout(self._create_middle_panel(), 0)
        self.content_layout.addLayout(self._create_right_panel(), 4)
        
        self.main_layout.addLayout(self.content_layout, 1)
        
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_initial_combos()
            self.fetch_next_analysis_id()
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    def _sql_concat(self, *args):
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if self.db_dialect == "SQL_SERVER" else str(arg))
        sep = " + " if self.db_dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _create_top_bar(self):
        layout = QHBoxLayout()
        
        layout.addWidget(QLabel("<b>Run ID:</b>"))
        self.lblRunID = QLineEdit()
        self.lblRunID.setPlaceholderText("Auto-Assigned")
        self.lblRunID.setText("Auto-Assigned")
        self.lblRunID.setReadOnly(True)
        self.lblRunID.setFixedWidth(220)
        self.lblRunID.setStyleSheet("font-weight: bold; color: #555; background: #ecf0f1;")
        layout.addWidget(self.lblRunID)
        
        layout.addStretch(1)
        
        # --- New Reset Button ---
        self.btnReset = QPushButton("Reset")
        self.btnReset.setToolTip("Clear Load List and reload Template")
        
        self.btnEditTemplate = QPushButton("Edit Template")
        self.btnCreate = QPushButton("Create Run")
        self.btnCreate.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        self.btnCancel = QPushButton("Close")
        
        layout.addWidget(self.btnReset)
        layout.addSpacing(5)
        layout.addWidget(self.btnEditTemplate)
        layout.addSpacing(10)
        layout.addWidget(self.btnCreate)
        layout.addWidget(self.btnCancel)
        
        return layout

    def _create_left_panel(self):
        layout = QVBoxLayout()
        setup_group = QGroupBox("Run Setup"); setup_layout = QFormLayout()
        self.cmbJob = QComboBox(); self.cmbWorkflow = QComboBox()
        self.cmbTemplate = QComboBox(); self.cmbInstrument = QComboBox()
        
        self.txtRepeatRunID = QLineEdit(); self.txtRepeatRunID.setPlaceholderText("Run ID"); self.txtRepeatRunID.setVisible(False)
        self.btnLoadRepeat = QPushButton("Load"); self.btnLoadRepeat.setVisible(False)
        rep_lay = QHBoxLayout(); rep_lay.addWidget(self.txtRepeatRunID); rep_lay.addWidget(self.btnLoadRepeat)
        
        setup_layout.addRow("Job:", self.cmbJob); setup_layout.addRow("Workflow:", self.cmbWorkflow)
        setup_layout.addRow("Template:", self.cmbTemplate); setup_layout.addRow("Instrument:", self.cmbInstrument)
        setup_layout.addRow("Repeat ID:", rep_lay)
        setup_group.setLayout(setup_layout); layout.addWidget(setup_group)
        
        s_group = QGroupBox("Available Samples"); s_layout = QVBoxLayout()
        f_layout = QHBoxLayout(); self.chkAllStatus = QCheckBox("All"); self.cmbStatus = QComboBox()
        f_layout.addWidget(QLabel("Status:")); f_layout.addWidget(self.cmbStatus, 1); f_layout.addWidget(self.chkAllStatus)
        s_layout.addLayout(f_layout)
        
        self.av_table = QTableView(); self.av_table.setModel(self.available_samples_model)
        self.av_table.setSelectionBehavior(QTableView.SelectRows); self.av_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.av_table.setSortingEnabled(True); self.av_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.av_table.setAlternatingRowColors(True); self.av_table.setShowGrid(False)
        s_layout.addWidget(self.av_table)
        s_group.setLayout(s_layout); layout.addWidget(s_group, 1)
        return layout

    def _create_middle_panel(self):
        layout = QVBoxLayout(); layout.addStretch(1)
        self.btnAdd = QPushButton(">"); self.btnAdd.setFixedWidth(35)
        self.btnAddAll = QPushButton(">>"); self.btnAddAll.setFixedWidth(35)
        self.btnRem = QPushButton("<"); self.btnRem.setFixedWidth(35)
        self.btnRemAll = QPushButton("<<"); self.btnRemAll.setFixedWidth(35)
        for b in [self.btnAdd, self.btnAddAll, self.btnRem, self.btnRemAll]: layout.addWidget(b)
        layout.addStretch(1)
        return layout

    def _create_right_panel(self):
        layout = QVBoxLayout()
        grp = QGroupBox("Run Load List"); glay = QVBoxLayout()
        
        sum_lay = QHBoxLayout()
        self.lblTotal = QLabel("Total: 0"); self.lblTotal.setStyleSheet("font-weight: bold;")
        self.lblUnk = QLabel("Unknowns: 0"); self.lblUnk.setStyleSheet("color: blue;")
        self.lblStd = QLabel("Standards: 0"); self.lblStd.setStyleSheet("color: green;")
        sum_lay.addWidget(self.lblTotal); sum_lay.addSpacing(15); sum_lay.addWidget(self.lblUnk); sum_lay.addSpacing(15); sum_lay.addWidget(self.lblStd); sum_lay.addStretch()
        glay.addLayout(sum_lay)
        
        tbl_row = QHBoxLayout()
        self.load_table = QTableView(); self.load_table.setModel(self.run_loadlist_model)
        self.load_table.setSelectionBehavior(QTableView.SelectRows); self.load_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.load_table.setAlternatingRowColors(True); self.load_table.setShowGrid(False)
        self.load_table.setSortingEnabled(False) 
        tbl_row.addWidget(self.load_table)
        
        v_tool = QVBoxLayout()
        self.btnUp = QPushButton("▲"); self.btnUp.setFixedSize(24, 24); self.btnUp.setToolTip("Move Up")
        self.btnDown = QPushButton("▼"); self.btnDown.setFixedSize(24, 24); self.btnDown.setToolTip("Move Down")
        self.btnDelRow = QPushButton("✖"); self.btnDelRow.setFixedSize(24, 24); self.btnDelRow.setToolTip("Delete Row")
        self.btnDelRow.setStyleSheet("color: red; font-weight: bold;")
        v_tool.addStretch(); v_tool.addWidget(self.btnUp); v_tool.addWidget(self.btnDown); v_tool.addSpacing(10); v_tool.addWidget(self.btnDelRow); v_tool.addStretch()
        tbl_row.addLayout(v_tool)
        
        glay.addLayout(tbl_row); grp.setLayout(glay); layout.addWidget(grp, 1)
        return layout

    def _connect_signals(self):
        self.cmbJob.currentIndexChanged.connect(self.on_job_changed)
        self.cmbWorkflow.currentIndexChanged.connect(self.on_workflow_changed)
        self.cmbTemplate.currentIndexChanged.connect(self.init_run_template)
        self.cmbInstrument.currentIndexChanged.connect(self.init_run_template)
        
        self.cmbStatus.currentIndexChanged.connect(self.load_avail_samples)
        self.chkAllStatus.toggled.connect(self.load_avail_samples)
        self.cmbStatus.currentIndexChanged.connect(self.on_status_changed)
        self.btnLoadRepeat.clicked.connect(self.load_avail_samples)
        
        self.btnAdd.clicked.connect(self.add_selected_samples)
        self.btnAddAll.clicked.connect(self.add_all_samples)
        self.btnRem.clicked.connect(self.remove_selected_samples)
        self.btnRemAll.clicked.connect(self.remove_all_samples)
        
        self.btnCreate.clicked.connect(self.on_create_clicked)
        self.btnCancel.clicked.connect(self.requestClose.emit)
        self.btnEditTemplate.clicked.connect(self.requestEditTemplate.emit)
        
        # --- Reset Signal ---
        self.btnReset.clicked.connect(self.on_reset_clicked)
        
        self.btnUp.clicked.connect(self.move_selected_up)
        self.btnDown.clicked.connect(self.move_selected_down)
        self.btnDelRow.clicked.connect(self.delete_selected_vials)

    def load_initial_combos(self):
        try:
            with db_manager.get_connection() as conn:
                self.cmbJob.addItem("- Select -", None)
                res = conn.execute(text("SELECT ID, sName FROM Job_Procedure WHERE ModuleID=2 ORDER BY sName"))
                job_count = 0
                for r in res:
                    self.cmbJob.addItem(r[1], r[0])
                    job_count += 1
                
                self.cmbStatus.addItem("- All Pending -", None)
                for r in conn.execute(text("SELECT Status, Description FROM StatusLookup WHERE Status IN (222, 1, 3, 4, 5) ORDER BY Status DESC")):
                    self.cmbStatus.addItem(f"{r[0]} - {r[1]}", r[0])
                self.cmbStatus.addItem("-Repeat a Run-", "repeat_run"); self.cmbStatus.setCurrentIndex(1)
                
                if job_count == 1:
                    self.cmbJob.setCurrentIndex(1)
                    QTimer.singleShot(100, self.cmbWorkflow.showPopup)
        except Exception as e: logging.error(f"Init combo fail: {e}")

    def fetch_next_analysis_id(self):
        pass  # ID now assigned by DB sequence via INSERT RETURNING

    def on_status_changed(self):
        is_rep = (self.cmbStatus.currentData() == "repeat_run")
        self.txtRepeatRunID.setVisible(is_rep)
        self.btnLoadRepeat.setVisible(is_rep)
        
        # --- UX Improvement: Focus and Highlight ---
        if is_rep:
            self.txtRepeatRunID.setFocus()
            self.btnLoadRepeat.setDefault(True)
            self.btnLoadRepeat.setAutoDefault(True)
        else:
            self.btnLoadRepeat.setDefault(False)
            self.btnLoadRepeat.setAutoDefault(False)
            
        self.load_avail_samples()

    def on_reset_clicked(self):
        """Reloads the template from DB, effectively clearing user changes."""
        if QMessageBox.question(self, "Reset", "Are you sure you want to reset the load list to the template defaults?", 
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
            self.init_run_template()

    def on_job_changed(self):
        job = self.cmbJob.currentText()
        self.cmbWorkflow.clear(); self.cmbWorkflow.addItem("- Select -", None)
        if not job or job == "- Select -": return
        try:
            with db_manager.get_connection() as conn:
                sql = f"""SELECT DISTINCT w.WorkflowID, {db_manager.sql_concat("w.WorkflowID", "'--'", "w.WorkflowName")} FROM Workflow w JOIN WorkflowJob wj ON w.WorkflowID=wj.WorkflowID WHERE wj.JobName=:job"""
                res = conn.execute(text(sql), {"job": job})
                for r in res: self.cmbWorkflow.addItem(r[1], r[0])
        except Exception as e: logging.error(f"Job change failed: {e}")

    def on_workflow_changed(self):
        wid = self.cmbWorkflow.currentData(); job = self.cmbJob.currentText()
        self.cmbTemplate.clear(); self.cmbInstrument.clear()
        if not wid: self.load_avail_samples(); return
        try:
            with db_manager.get_connection() as conn:
                sql_t = f"""SELECT p.ProcedureID, {db_manager.sql_concat("p.ProcedureID", "'--'", "p.ProcedureName")} FROM AnalysisProcedure p JOIN WorkflowJob wj ON p.ProcedureID=wj.ProcedureID WHERE wj.JobName=:job AND wj.WorkflowID=:wid"""
                t_row = conn.execute(text(sql_t), {"job": job, "wid": wid}).fetchone()
                if t_row: self.cmbTemplate.addItem(t_row[1], t_row[0]); self.cmbTemplate.setCurrentIndex(0)
                
                if t_row:
                    sql_i = f"""SELECT e.EquipmentID, {db_manager.sql_concat("e.Identifier", "'---'", "e.EquipmentName")} FROM Equipment e JOIN AnalysisProcedure p ON e.SampleExportFormat=p.SampleExportFormat WHERE e.CategoryID=5 AND p.ProcedureID=:pid"""
                    res_i = conn.execute(text(sql_i), {"pid": t_row[0]})
                    for r in res_i: self.cmbInstrument.addItem(r[1], r[0])
        except Exception as e: logging.error(f"Workflow change failed: {e}")
        
        self.load_avail_samples()
        self.init_run_template()

    def init_run_template(self):
        """Initializes the run template table. AnalysisID column is left empty."""
        self.run_loadlist_model.clear()
        tid = self.cmbTemplate.currentData(); iid = self.cmbInstrument.currentData()
        if not tid or not iid: return
            
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("SELECT * FROM AnalysisProcedure_Template WHERE ProcedureID=:pid ORDER BY OrdinalPosition"), {"pid": tid}).fetchall()
                headers = ["Pos", "Tray", "Vial", "Lab ID", "Sample Name", "AnalysisID", "SampleType", "Repeat"]
                self.run_loadlist_model.setHorizontalHeaderLabels(headers)
                
                color_map = { 
                    23: QBrush(QColor("#E0F2FE")), 24: QBrush(QColor("#D1FAE5")), 
                    26: QBrush(QColor("#FEF9C3")), 27: QBrush(QColor("#FEE2E2")) 
                }
                default_brush = QBrush(QColor(240, 240, 240))
                standard_font = QFont(); standard_font.setBold(True)

                for r in rows:
                    prefix, sid, sname = None, None, None
                    if r.RefOurLabID:
                        try:
                            prefix, sid_str = r.RefOurLabID.split('-')
                            sid = int(sid_str)
                            n = conn.execute(text("SELECT sName FROM Sample WHERE Prefix=:p AND SampleID=:s"), {"p": prefix, "s": sid}).fetchone()
                            sname = n[0] if n else "Std"
                        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

                    stype = int(r.PortRefEmpty)

                    lab_id_item = QStandardItem(f"{prefix}-{sid}" if prefix and sid else "")
                    if sid is not None: lab_id_item.setData(sid, Qt.UserRole)
                    if prefix: lab_id_item.setData(prefix, Qt.UserRole + 1)

                    items = [
                        QStandardItem(str(r.OrdinalPosition)), QStandardItem(str(r.TrayNo)), QStandardItem(str(r.VialNo)),
                        lab_id_item, QStandardItem(sname or ""),
                        QStandardItem(""), # AnalysisID empty initially
                        QStandardItem(str(stype)), QStandardItem("1")
                    ]

                    items[5].setData(None, Qt.DisplayRole); items[6].setData(stype, Qt.DisplayRole)

                    if stype != 0:
                        brush = color_map.get(stype, default_brush)
                        for i in items:
                            i.setBackground(brush); i.setFont(standard_font)
                            i.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

                    self.run_loadlist_model.appendRow(items)

                h = self.load_table.horizontalHeader()
                h.setMinimumSectionSize(50)
                for i in range(8):
                    h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
                h.setSectionResizeMode(4, QHeaderView.Stretch)
                # enforce readable minimums for narrow columns
                for col, min_w in {0: 40, 1: 55, 2: 50, 3: 110, 5: 90, 6: 90, 7: 60}.items():
                    if h.sectionSize(col) < min_w:
                        h.resizeSection(col, min_w)
                self.update_run_summary()
        except Exception as e: 
            logging.error(f"Init template failed: {e}")
            show_message(self, "Error", f"Failed to initialize template:\n{e}", QMessageBox.Warning)

    def load_avail_samples(self):
        self.available_samples_model.clear()
        workflow_id = self.cmbWorkflow.currentData()
        if not workflow_id: return
        
        status_value = self.cmbStatus.currentData()
        show_all = self.chkAllStatus.isChecked()
        
        excluded = []
        for r in range(self.run_loadlist_model.rowCount()):
            lab_item = self.run_loadlist_model.item(r, 3)
            sid = lab_item.data(Qt.UserRole) if lab_item else None
            pfx = lab_item.data(Qt.UserRole + 1) if lab_item else None
            if sid and pfx: excluded.append((int(sid), pfx))
            
        try:
            with db_manager.get_connection() as conn:
                if status_value == "repeat_run":
                    run_id_str = self.txtRepeatRunID.text().strip()
                    if not run_id_str: return 
                    try: rid = int(run_id_str)
                    except: return
                    
                    sql = f"""
                        SELECT ll.SIAnalysisRunID, ll.AnalysisID, s.SampleID, s.Prefix, {db_manager.sql_concat("s.Prefix", "'-'", "s.SampleID")}, ll.Repeat, ll.Status, s.sName, ll.PrecursorID
                        FROM Submission sub
                        INNER JOIN Sample s ON sub.SubmissionID = s.SubmissionID
                        INNER JOIN Analysis a ON s.SampleID = a.SampleID AND a.Prefix = s.Prefix
                        INNER JOIN (SIAM.SIAnalysisLoadList ll INNER JOIN SIAM.SIAnalysisRun r ON ll.SIAnalysisRunID = r.SIAnalysisRunID) ON ll.AnalysisID = a.AnalysisID
                        WHERE s.SampleType IN (0,3,6) AND ll.SIAnalysisRunID = :rid
                        ORDER BY ll.SIAnalysisRunID, ll.PositionInRun
                    """
                    res = conn.execute(text(sql), {"rid": rid})
                    
                    # --- REQ: Custom Headers for Repeat Run ---
                    self.available_samples_model.setHorizontalHeaderLabels(["Run ID", "AnalysisID", "OurLabID", "Name", "Rep", "Stat", "Precursor"])
                    
                    for row in res:
                        if (row[2], row[3]) in excluded: continue
                        item_lab = QStandardItem(row[4])
                        item_lab.setData(row[2], Qt.UserRole+1); item_lab.setData(row[3], Qt.UserRole+2); item_lab.setData(row[7], Qt.UserRole+3)
                        
                        item_ana = QStandardItem(str(row[1]))
                        item_ana.setData(str(row[8]), Qt.UserRole+1)

                        self.available_samples_model.appendRow([
                            QStandardItem(str(row[0])), item_ana, item_lab, QStandardItem(row[7] or ""), 
                            QStandardItem(str(row[5])), QStandardItem(str(row[6])), QStandardItem(str(row[8] or ""))
                        ])
                        
                    # --- REQ: Hide Columns 0, 1, 3 ---
                    # self.av_table.setColumnHidden(0, True) # Run ID
                    # self.av_table.setColumnHidden(1, True) # AnalysisID
                    # self.av_table.setColumnHidden(3, True) # Name (as requested)

                else:
                    sql = f"""
                        SELECT s.SampleID, s.Prefix, s.sName, s.Status, {db_manager.sql_concat("s.Prefix", "'-'", "s.SampleID")}, sub.SubmissionID, sub.PriorityID
                        FROM public.sample s JOIN public.submission sub ON s.submissionid=sub.submissionid
                        JOIN public.sample_queue sq ON sq.sampleid=s.sampleid AND sq.prefix=s.prefix
                        JOIN public.workflowjob wj ON wj.workflowjobid=sq.workflowjobid
                        WHERE wj.workflowid=:wid AND s.SampleType IN (0,3,6)
                    """
                    params = {"wid": workflow_id}

                    res = conn.execute(text(sql + " ORDER BY sub.PriorityID DESC, sub.SubmissionDate"), params)
                    self.available_samples_model.setHorizontalHeaderLabels(["OurLabID", "Name", "Status", "SubmissionID", "Priority"])

                    for row in res:
                        if (row[0], row[1]) in excluded: continue
                        item_lab = QStandardItem(row[4])
                        item_lab.setData(row[0], Qt.UserRole+1); item_lab.setData(row[1], Qt.UserRole+2); item_lab.setData(row[2], Qt.UserRole+3)
                        self.available_samples_model.appendRow([
                            item_lab, QStandardItem(row[2] or ""), QStandardItem(str(row[3])),
                            QStandardItem(str(row[5])), QStandardItem(str(row[6]))
                        ])

                    # Append repeat-queued analyses (status=99) for this workflow
                    _repeat_color = QColor("#BBDEFB")   # light blue
                    rpt_sql = f"""
                        SELECT a.SampleID, a.Prefix, s.sName, a.AnalysisID,
                               {db_manager.sql_concat("a.Prefix", "'-'", "a.SampleID")},
                               sub.SubmissionID, sub.PriorityID
                        FROM public.analysis a
                        JOIN public.sample s
                          ON s.sampleid = a.sampleid AND s.prefix = a.prefix
                        JOIN public.submission sub ON s.submissionid = sub.submissionid
                        WHERE a.status = 99
                          AND s.sampletype IN (0, 3, 6)
                          AND EXISTS (
                              SELECT 1 FROM siam.sianalysisloadlist ll
                              JOIN siam.sianalysisrun r
                                ON r.sianalysisrunid = ll.sianalysisrunid
                              WHERE ll.analysisid = a.analysisid
                                AND r.workflowid = :wid
                          )
                        ORDER BY sub.priorityid DESC, sub.submissiondate
                    """
                    rpt_res = conn.execute(text(rpt_sql), {"wid": workflow_id})
                    for row in rpt_res:
                        if (row[0], row[1]) in excluded:
                            continue
                        item_lab = QStandardItem(row[4])
                        item_lab.setData(row[0], Qt.UserRole+1)
                        item_lab.setData(row[1], Qt.UserRole+2)
                        item_lab.setData(row[2], Qt.UserRole+3)
                        item_lab.setData(row[3], Qt.UserRole+4)   # existing AnalysisID
                        status_item = QStandardItem("⟳ Repeat")
                        items = [
                            item_lab,
                            QStandardItem(row[2] or ""),
                            status_item,
                            QStandardItem(str(row[5])),
                            QStandardItem(str(row[6])),
                        ]
                        for it in items:
                            it.setBackground(QBrush(_repeat_color))
                        self.available_samples_model.appendRow(items)

                    # Ensure columns are visible in normal mode (or just resize)
                    self.av_table.setColumnHidden(0, False)
                    self.av_table.setColumnHidden(1, False)
                    self.av_table.setColumnHidden(3, False)
            
            self.av_table.resizeColumnsToContents()
        except Exception as e: logging.error(f"Load samples failed: {e}")

    def update_run_summary(self):
        tot = self.run_loadlist_model.rowCount(); unk = 0; std = 0
        for r in range(tot):
            try:
                if int(self.run_loadlist_model.item(r, 6).data(Qt.DisplayRole) or 0) == 0: unk += 1
                else: std += 1
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        self.lblTotal.setText(str(tot)); self.lblUnk.setText(str(unk)); self.lblStd.setText(str(std))

    def _get_empty_slot(self):
        for r in range(self.run_loadlist_model.rowCount()):
            type_item = self.run_loadlist_model.item(r, 6)
            id_item = self.run_loadlist_model.item(r, 3)
            if type_item and int(type_item.data(Qt.DisplayRole) or 0) == 0:
                if not id_item.text(): return r
        return -1
    
    def renumber_rows(self):
        for r in range(self.run_loadlist_model.rowCount()): self.run_loadlist_model.item(r, 0).setText(str(r + 1))

    def add_selected_samples(self):
        rows = self.av_table.selectionModel().selectedRows()
        if not rows: return
        is_repeat = (self.cmbStatus.currentData() == "repeat_run")
        col_idx = 2 if is_repeat else 0
        rows_to_remove = []
        
        for idx in sorted(rows, key=lambda x: x.row()):
            slot = self._get_empty_slot()
            if slot == -1: break 
            
            row_index = idx.row()
            item = self.available_samples_model.item(row_index, col_idx)
            sid = item.data(Qt.UserRole+1); pfx = item.data(Qt.UserRole+2); nam = item.data(Qt.UserRole+3)
            
            rep = "1"
            old_aid_val = None

            if is_repeat:
                old_rep = self.available_samples_model.item(row_index, 4).text()
                if old_rep.isdigit(): rep = str(int(old_rep) + 1)

                old_aid_item = self.available_samples_model.item(row_index, 1)
                if old_aid_item:
                    old_aid_val = old_aid_item.text()
            else:
                # Repeat-queued analyses carry an existing AnalysisID at UserRole+4
                existing_aid = item.data(Qt.UserRole+4)
                if existing_aid is not None:
                    old_aid_val = str(existing_aid)
                    try:
                        with db_manager.get_connection() as conn:
                            max_rep = conn.execute(text("SELECT COALESCE(MAX(repeat), 0) FROM siam.sianalysisloadlist WHERE analysisid = :aid"), {"aid": existing_aid}).scalar()
                        rep = str(max(max_rep, 1) + 1)
                    except Exception as e:
                        logging.error(f"Error querying max repeat: {e}")
                        rep = "2"

            lab_item = self.run_loadlist_model.item(slot, 3)
            lab_item.setText(f"{pfx}-{sid}")
            lab_item.setData(sid, Qt.UserRole)
            lab_item.setData(pfx, Qt.UserRole + 1)
            self.run_loadlist_model.item(slot, 4).setText(nam)
            self.run_loadlist_model.item(slot, 7).setText(rep)

            if old_aid_val:
                self.run_loadlist_model.item(slot, 5).setText(old_aid_val)
                self.run_loadlist_model.item(slot, 5).setData(old_aid_val, Qt.DisplayRole)
                
            rows_to_remove.append(row_index)
            
        for r in sorted(rows_to_remove, reverse=True): self.available_samples_model.removeRow(r)
        self.update_run_summary()

    def add_all_samples(self): self.av_table.selectAll(); self.add_selected_samples()

    def remove_selected_samples(self):
        for idx in self.load_table.selectionModel().selectedRows():
            r = idx.row()
            if int(self.run_loadlist_model.item(r, 6).data(Qt.DisplayRole) or 0) == 0:
                lab_item = self.run_loadlist_model.item(r, 3)
                lab_item.setText("")
                lab_item.setData(None, Qt.UserRole)
                lab_item.setData(None, Qt.UserRole + 1)
                for c in [4, 5]: self.run_loadlist_model.item(r, c).setText("")
                self.run_loadlist_model.item(r, 7).setText("1")
                self.run_loadlist_model.item(r, 5).setData(None, Qt.DisplayRole)
        self.load_avail_samples(); self.update_run_summary()

    def remove_all_samples(self): self.load_table.selectAll(); self.remove_selected_samples()

    def delete_selected_vials(self):
        rows = self.load_table.selectionModel().selectedRows()
        if not rows: return
        for index in sorted(rows, key=lambda x: x.row(), reverse=True):
            self.run_loadlist_model.removeRow(index.row())
        self.renumber_rows(); self.update_run_summary()         
 
    def move_selected_up(self):
        rows = self.load_table.selectionModel().selectedRows()
        if not rows: return
        row = rows[0].row()
        if row > 0:
            self.run_loadlist_model.insertRow(row - 1, self.run_loadlist_model.takeRow(row))
            self.load_table.selectRow(row - 1); self.renumber_rows()
    
    def move_selected_down(self):
        rows = self.load_table.selectionModel().selectedRows()
        if not rows: return
        row = rows[0].row()
        if row < self.run_loadlist_model.rowCount() - 1:
            self.run_loadlist_model.insertRow(row + 1, self.run_loadlist_model.takeRow(row))
            self.load_table.selectRow(row + 1); self.renumber_rows()

    def on_create_clicked(self):
        if self.save_run(): self.runCreated.emit()

    def save_run(self):
        logging.info("Attempting to save new SIAM Run...")

        # --- 1. GUI Validation ---
        template_id = self.cmbTemplate.currentData()
        workflow_id = self.cmbWorkflow.currentData()
        job_id = self.cmbJob.currentData()
        instrument_id = self.cmbInstrument.currentData()

        if not all([template_id, workflow_id, job_id, instrument_id]):
            show_message(self, "Error",
                         "Missing setup info. Please select Job, Workflow, Template, and Instrument.",
                         QMessageBox.Critical)
            return False

        # --- 2. Empty Slot Check ---
        empty_slots = 0
        id_col = 3   # Lab ID column index
        type_col = 6 # SampleType column index

        for row in range(self.run_loadlist_model.rowCount()):
            t_data = self.run_loadlist_model.item(row, type_col).data(Qt.DisplayRole)
            id_data = self.run_loadlist_model.item(row, id_col).text()
            try:
                if int(t_data or 0) == 0 and not id_data:
                    empty_slots += 1
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

        if empty_slots > 0:
            reply = QMessageBox.question(
                self, "Empty Slots",
                f"There are {empty_slots} empty unknown slots in the run list.\n"
                "Do you want to proceed?", 
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if reply == QMessageBox.No: return False

        # --- 3. Database Transaction ---
        self.fetch_next_analysis_id()
        start_aid = self.next_analysis_id
        
        try:
            with db_manager.get_connection() as conn:
                # A. Get Repeats Config
                res_rpts = conn.execute(text("SELECT MAX(Repeats) FROM AnalysisProcedure_Measurable WHERE ProcedureID=:pid"), {"pid": template_id}).fetchone()
                unknown_repeats = res_rpts[0] if res_rpts and res_rpts[0] else 1

                now = datetime.now(); user = getpass.getuser()

                # B. Insert Run Header — sequence assigns SIAnalysisRunID
                new_run_id = conn.execute(text("""
                    INSERT INTO SIAM.SIAnalysisRun (
                        ProcedureID, WorkflowID, WorkflowJobID, EquipmentID,
                        RunStartTime, CreateDateStamp, CreateUserStamp, RunStatus, Remarks
                    ) VALUES (:pid, :wid, :jid, :eid, :now, :now, :user, 6, 'PyLIMS')
                    RETURNING SIAnalysisRunID
                """), {
                    "pid": template_id, "wid": workflow_id,
                    "jid": job_id, "eid": instrument_id, "now": now, "user": user
                }).scalar()

                # C. Process Rows
                for r in range(self.run_loadlist_model.rowCount()):
                    lab_item = self.run_loadlist_model.item(r, 3)
                    sid = lab_item.data(Qt.UserRole)
                    pfx = lab_item.data(Qt.UserRole + 1)
                    stype = int(self.run_loadlist_model.item(r, 6).data(Qt.DisplayRole) or 0)

                    rpts = unknown_repeats if stype == 0 else 1
                    iter_val = int(self.run_loadlist_model.item(r, 7).text() or 1)

                    # D. Insert or Update Analysis Record
                    existing_aid_item = self.run_loadlist_model.item(r, 5)
                    existing_aid_text = existing_aid_item.text().strip() if existing_aid_item else ""
                    
                    if existing_aid_text.isdigit():
                        current_aid = int(existing_aid_text)
                        conn.execute(text("""
                            UPDATE Analysis 
                            SET Status = 6, ModifDateStamp = :now, ModifUserStamp = :user 
                            WHERE AnalysisID = :aid
                        """), {"aid": current_aid, "now": now, "user": user})
                    else:
                        current_aid = conn.execute(text("""
                            INSERT INTO Analysis (
                                Prefix, SampleID, WorkflowID, Status, Repeats, CreateDateStamp, CreateUserStamp
                            ) VALUES (:pfx, :sid, :wid, 6, :rpts, :now, :user)
                            RETURNING AnalysisID
                        """), {
                            "pfx": pfx, "sid": sid,
                            "wid": workflow_id if stype == 0 else -9999,
                            "rpts": rpts, "now": now, "user": user
                        }).scalar()

                    # G. Insert LoadList Record
                    conn.execute(text("""
                        INSERT INTO SIAM.SIAnalysisLoadList (
                            AnalysisID, SIAnalysisRunID, PositionInRun, TrayNumber, PositionInTray, Status, Repeat
                        ) VALUES (:aid, :rid, :pos, :tray, :vial, 6, :rep)
                    """), {
                        "aid": current_aid, "rid": new_run_id,
                        "pos": int(self.run_loadlist_model.item(r, 0).text()),
                        "tray": int(self.run_loadlist_model.item(r, 1).text()),
                        "vial": int(self.run_loadlist_model.item(r, 2).text()),
                        "rep": iter_val
                    })

                    # H. Update Sample Status
                    if stype == 0 and sid:
                        conn.execute(text("UPDATE Sample SET Status=200, ModifDateStamp=:now, ModifUserStamp=:user WHERE SampleID=:s AND Prefix=:p"), 
                                     {"s": sid, "p": pfx, "now": now, "user": user})

                conn.commit()
                show_message(self, "Success", f"Run {new_run_id} Created Successfully.", QMessageBox.Information)
                return True

        except Exception as e:
            logging.error(f"Save failed: {e}")
            show_message(self, "Error", f"Database transaction failed:\n{e}", QMessageBox.Critical)
            return False

class SiamCreateRunDialog(QDialog):
    """Wrapper Dialog"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create SIAM Run"); self.resize(1200, 700)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0)
        self.core = SiamCreateRunWidget(self)
        lay.addWidget(self.core)
        self.core.runCreated.connect(self.accept)
        self.core.requestClose.connect(self.reject)
        self.core.requestEditTemplate.connect(self.open_template_editor)

    def open_template_editor(self):
        if not SiamTemplateEditorDialog: return
        tid = self.core.cmbTemplate.currentData()
        if not tid:
            show_message(self, "Info", "Please select a template first.", QMessageBox.Information)
            return

        editor = SiamTemplateEditorDialog(
            procedure_id=tid, category_id=5, 
            device_name=self.core.cmbInstrument.currentText(),
            parent=self, use_external_source=True
        )
        editor.load_from_model(self.core.run_loadlist_model)
        
        if editor.exec_() == QDialog.Accepted:
            model = self.core.run_loadlist_model
            model.clear()
            headers = ["Pos", "Tray", "Vial", "Lab ID", "Sample Name", "AnalysisID", "SampleType", "Repeat"]
            model.setHorizontalHeaderLabels(headers)

            color_map = { 23: QBrush(QColor("#E0F2FE")), 24: QBrush(QColor("#D1FAE5")), 26: QBrush(QColor("#FEF9C3")), 27: QBrush(QColor("#FEE2E2")) }

            for row in editor.modified_template:
                pfx, sid = "", None
                if row["Sample ID"]:
                    try: pfx, sid = row["Sample ID"].split("-"); sid = int(sid)
                    except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

                lab_id_item = QStandardItem(f"{pfx}-{sid}" if pfx and sid else "")
                if sid is not None: lab_id_item.setData(sid, Qt.UserRole)
                if pfx: lab_id_item.setData(pfx, Qt.UserRole + 1)

                items = [
                    QStandardItem(str(row["Run Order"])), QStandardItem(str(row["Tray"])), QStandardItem(str(row["Vial"])),
                    lab_id_item, QStandardItem(""),
                    QStandardItem(str(self.core.next_analysis_id)), QStandardItem(str(row["Sample Type"])), QStandardItem("1")
                ]
                self.core.next_analysis_id += 1

                stype = int(row["Sample Type"])
                items[6].setData(stype, Qt.DisplayRole)
                if stype != 0:
                    for i in items:
                        i.setBackground(color_map.get(stype, QBrush(QColor("#F3F4F6"))))
                        i.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

                model.appendRow(items)
                
            self.core.update_run_summary()
            self.core.load_table.resizeColumnsToContents()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    d = SiamCreateRunDialog(); d.show()
    sys.exit(app.exec_())