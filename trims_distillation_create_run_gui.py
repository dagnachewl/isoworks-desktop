"""
trims_distillation_create_run_gui.py — TRIMS distillation run creation widget for IsoWorks.
Provides TrimsDistillationCreateRunWidget for building a new distillation run by
selecting a workflow, assigning TBA samples to a load list, and saving the run.
"""
import sys
import logging
import getpass
from datetime import datetime
from typing import Optional, List, Dict

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QComboBox, QLineEdit, QPushButton,
    QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QDialog, QAbstractItemView, QHeaderView, QSplitter
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush, QFont
from PyQt5.QtCore import Qt, pyqtSignal, QTimer

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from help_browser import make_help_button

class TrimsDistillationCreateRunWidget(QWidget):
    # Signals
    runCreated = pyqtSignal()
    requestClose = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.db_dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        
        # Models
        self.available_samples_model = QStandardItemModel()  # TBA samples
        self.failed_samples_model = QStandardItemModel()    # Failed/Repeat Samples
        self.run_loadlist_model = QStandardItemModel()      # The Staging Area
        
        self.next_run_id = 0
        
        self._init_ui()
        self._connect_signals()
        
        try:
            db_manager.get_engine()
            self.load_initial_combos()
            self.fetch_next_run_id()
        except Exception as e:
            logging.error(f"Init DB failed: {e}")

    def _sql_concat(self, *args):
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if self.db_dialect == "SQL_SERVER" else str(arg))
        sep = " + " if self.db_dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        
        # --- Top Bar ---
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("<b>Create Distillation Run:</b>"))
        self.lblRunID = QLineEdit("Auto-Assigned")
        self.lblRunID.setReadOnly(True); self.lblRunID.setFixedWidth(150)
        self.lblRunID.setStyleSheet("font-weight: bold; background: #ecf0f1;")
        top_bar.addWidget(self.lblRunID)
        top_bar.addStretch()
        self.btnCreate = QPushButton("Create Run")
        self.btnCreate.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold;")
        self.btnClose = QPushButton("Close")
        top_bar.addWidget(self.btnCreate)
        top_bar.addWidget(self.btnClose)
        top_bar.addWidget(make_help_button(self, "trims_create_distillation"))
        main_layout.addLayout(top_bar)

        # --- Main Splitter ---
        splitter = QSplitter(Qt.Horizontal)
        
        # --- Left Panel: Selection Criteria & Available Samples ---
        left_widget = QWidget(); left_layout = QVBoxLayout(left_widget)
        
        # Criteria Group
        crit_grp = QGroupBox("Setup & Criteria")
        form = QFormLayout()
        
        # Reordered: Workflow First
        self.cmbWorkflow = QComboBox()
        self.cmbJob = QComboBox() 
        self.cmbProcedure = QComboBox()
        self.cmbInstrument = QComboBox()
        self.cmbStatus = QComboBox() 
        
        form.addRow("Workflow:", self.cmbWorkflow)
        form.addRow("Job:", self.cmbJob)
        form.addRow("Procedure:", self.cmbProcedure)
        form.addRow("Device:", self.cmbInstrument)
        form.addRow("Sample Status:", self.cmbStatus)
        
        crit_grp.setLayout(form)
        left_layout.addWidget(crit_grp)
        
        # Available Samples Table
        avail_grp = QGroupBox("New Samples (TBA)")
        v_avail = QVBoxLayout()
        self.tblAvailable = QTableView()
        self.tblAvailable.setModel(self.available_samples_model)
        self.tblAvailable.setSelectionBehavior(QTableView.SelectRows)
        self.tblAvailable.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblAvailable.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblAvailable.setAlternatingRowColors(True)
        v_avail.addWidget(self.tblAvailable)
        avail_grp.setLayout(v_avail)
        left_layout.addWidget(avail_grp, 1) 
        
        splitter.addWidget(left_widget)
        
        # --- Middle Buttons ---
        mid_widget = QWidget(); mid_layout = QVBoxLayout(mid_widget)
        mid_layout.addStretch()
        self.btnAdd = QPushButton(">"); self.btnAdd.setFixedWidth(40)
        self.btnAddAll = QPushButton(">>"); self.btnAddAll.setFixedWidth(40)
        self.btnRemove = QPushButton("<"); self.btnRemove.setFixedWidth(40)
        self.btnRemoveAll = QPushButton("<<"); self.btnRemoveAll.setFixedWidth(40)
        
        mid_layout.addWidget(self.btnAdd); mid_layout.addWidget(self.btnAddAll)
        mid_layout.addSpacing(20)
        mid_layout.addWidget(self.btnRemove); mid_layout.addWidget(self.btnRemoveAll)
        mid_layout.addStretch()
        splitter.addWidget(mid_widget)
        
        # --- Right Panel: Failed Samples & Load List ---
        right_widget = QWidget(); right_layout = QVBoxLayout(right_widget)
        
        # 1. Failed/Repeat Samples List
        fail_grp = QGroupBox("Failed Samples (Requires Repeat)")
        fail_grp.setStyleSheet("QGroupBox { color: #c0392b; font-weight: bold; }")
        v_fail = QVBoxLayout()
        self.tblFailed = QTableView()
        self.tblFailed.setModel(self.failed_samples_model)
        self.tblFailed.setSelectionBehavior(QTableView.SelectRows)
        self.tblFailed.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblFailed.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblFailed.setFixedHeight(150)
        self.tblFailed.setAlternatingRowColors(True)
        v_fail.addWidget(self.tblFailed)
        
        self.btnAddFailed = QPushButton("Add Selected Repeats to Run ↓")
        self.btnAddFailed.setStyleSheet("color: #c0392b; font-weight: bold;")
        v_fail.addWidget(self.btnAddFailed)
        fail_grp.setLayout(v_fail)
        right_layout.addWidget(fail_grp)
        
        # 2. Run Load List
        load_grp = QGroupBox("Samples Selected for this Run")
        v_load = QVBoxLayout()
        
        # Stats Row
        h_stats = QHBoxLayout()
        self.lblCount = QLabel("Selected: 0")
        h_stats.addWidget(self.lblCount); h_stats.addStretch()
        v_load.addLayout(h_stats)
        
        self.tblLoadList = QTableView()
        self.tblLoadList.setModel(self.run_loadlist_model)
        self.tblLoadList.setSelectionBehavior(QTableView.SelectRows)
        self.tblLoadList.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tblLoadList.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tblLoadList.setAlternatingRowColors(True)
        v_load.addWidget(self.tblLoadList)
        load_grp.setLayout(v_load)
        right_layout.addWidget(load_grp, 1)
        
        splitter.addWidget(right_widget)
        
        # Set Splitter Ratios
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 5)
        
        main_layout.addWidget(splitter)

        # Initialize Models Headers
        self.available_samples_model.setHorizontalHeaderLabels(["OurLabID", "Sample Name", "Priority", "Submission"])
        
        # Failed List: Added WF ID column
        self.failed_samples_model.setHorizontalHeaderLabels(["Rep", "Sample ID", "Anal. ID", "Status", "SubID", "Name", "WF"])
        
        # Load List
        self.run_loadlist_model.setHorizontalHeaderLabels(["FlaskID", "OurLabID", "Sample Name", "Type", "AnalysisID", "SubID", "Repeat"])

    def _connect_signals(self):
        self.cmbWorkflow.currentIndexChanged.connect(self.on_workflow_changed)
        self.cmbStatus.currentIndexChanged.connect(self.load_available_samples)
        
        self.btnAdd.clicked.connect(self.add_selected_available)
        self.btnAddAll.clicked.connect(self.add_all_available)
        self.btnAddFailed.clicked.connect(self.add_selected_failed)
        self.btnRemove.clicked.connect(self.remove_selected)
        self.btnRemoveAll.clicked.connect(self.remove_all)
        
        # Double Click Actions (New)
        self.tblAvailable.doubleClicked.connect(lambda idx: self.add_selected_available())
        self.tblFailed.doubleClicked.connect(lambda idx: self.add_selected_failed())
        
        self.btnCreate.clicked.connect(self.on_create_clicked)
        self.btnClose.clicked.connect(self.requestClose.emit)

    # --- Data Loading ---
    def load_initial_combos(self):
        with db_manager.get_connection() as conn:
            # 1. Workflows
            self.cmbWorkflow.addItem("- Select -", None)
            sql_wf = f"""
                SELECT DISTINCT w.WorkflowID, {db_manager.sql_concat("w.WorkflowID", "' - '", "w.WorkflowName")} 
                FROM Workflow w JOIN WorkflowJob wj ON w.WorkflowID = wj.WorkflowID 
                WHERE wj.JobName LIKE 'Purification_Method%' ORDER BY w.WorkflowID
            """
            for r in conn.execute(text(sql_wf)):
                self.cmbWorkflow.addItem(r[1], r[0])
            
            # 2. Procedures
            self.cmbProcedure.addItem("- Select -", None)
            sql_proc = f"""
                SELECT ProcedureID, {db_manager.sql_concat('ProcedureID', "' - '", 'ProcedureName')} 
                FROM AnalysisProcedure WHERE IsObsolete = {db_manager.sql_bool(False)}
            """
            for r in conn.execute(text(sql_proc)):
                self.cmbProcedure.addItem(r[1], r[0])
                
            # 3. Equipment
            self.cmbInstrument.addItem("- Select -", None)
            sql_inst = f"""
                SELECT EquipmentID, {db_manager.sql_concat('EquipmentID', "' - '", 'EquipmentName')} 
                FROM Equipment WHERE CategoryID=1
            """
            for r in conn.execute(text(sql_inst)):
                self.cmbInstrument.addItem(r[1], r[0])
                
            # 4. Status
            self.cmbStatus.addItem("222 - Ready for Prep", 222)
            self.cmbStatus.addItem("All Statuses", -1)

    def on_workflow_changed(self):
        wid = self.cmbWorkflow.currentData()
        self.cmbJob.clear()
        
        if not wid: return

        try:
            with db_manager.get_connection() as conn:
                sql_info = f"""
                    SELECT wj.JobName, wj.ProcedureID, ap.ProcedureName
                    FROM WorkflowJob wj
                    LEFT JOIN AnalysisProcedure ap ON wj.ProcedureID = ap.ProcedureID
                    WHERE wj.WorkflowID = :wid AND wj.JobName LIKE 'Purification_Method%'
                """
                row = conn.execute(text(sql_info), {"wid": wid}).fetchone()
                
                if row:
                    job_name = row[0]; proc_id = row[1]
                    self.cmbJob.addItem(job_name, job_name); self.cmbJob.setCurrentIndex(0)
                    
                    if proc_id:
                        idx = self.cmbProcedure.findData(proc_id)
                        if idx >= 0: self.cmbProcedure.setCurrentIndex(idx)
                
                if self.cmbInstrument.count() > 1 and self.cmbInstrument.currentIndex() <= 0:
                    self.cmbInstrument.setCurrentIndex(1)
                    
        except Exception as e:
            logging.error(f"Auto-populate failed: {e}")

        self.load_available_samples()
        self.load_failed_samples()

    def fetch_next_run_id(self):
        with db_manager.get_connection() as conn:
            res = conn.execute(text("SELECT MAX(RunID) FROM TRIMS.PrimaryDistillationBatch")).fetchone()
            self.next_run_id = (res[0] or 10000) + 1
            self.lblRunID.setText(f"New: ~{self.next_run_id}")

    def load_available_samples(self):
        """Loads NEW samples from TBA table."""
        self.available_samples_model.removeRows(0, self.available_samples_model.rowCount())
        wid = self.cmbWorkflow.currentData()
        if not wid: return
        
        stat = self.cmbStatus.currentData()
        
        try:
            with db_manager.get_connection() as conn:
                sql = f"""
                    SELECT s.SampleID, s.Prefix, s.sName, {db_manager.sql_concat("s.Prefix", "'-'", "s.SampleID")}, sub.PriorityID, sub.SubmissionID
                    FROM public.sample s
                    JOIN public.submission sub ON s.submissionid = sub.submissionid
                    JOIN public.sample_queue sq ON sq.sampleid = s.sampleid AND sq.prefix = s.prefix
                    JOIN public.workflowjob wj ON wj.workflowjobid = sq.workflowjobid
                    WHERE wj.workflowid = :wid
                """
                params = {"wid": wid}
                
                sql += " ORDER BY sub.PriorityID DESC, s.SampleID"
                
                loaded_keys = set()
                for r in range(self.run_loadlist_model.rowCount()):
                    loaded_keys.add(self.run_loadlist_model.item(r, 1).text())
                
                rows = conn.execute(text(sql), params).fetchall()
                for r in rows:
                    our_lab_id = r[3]
                    if our_lab_id in loaded_keys: continue
                    
                    item_id = QStandardItem(our_lab_id)
                    item_id.setData(r[0], Qt.UserRole)   # SampleID
                    item_id.setData(r[1], Qt.UserRole+1) # Prefix
                    
                    self.available_samples_model.appendRow([
                        item_id,
                        QStandardItem(r[2] or ""),
                        QStandardItem(str(r[4])),
                        QStandardItem(str(r[5]))
                    ])
            self.tblAvailable.resizeColumnsToContents()
        except Exception as e:
            logging.error(f"Load avail samples failed: {e}")

    def load_failed_samples(self):
        """Loads samples needing repeat distillation."""
        self.failed_samples_model.removeRows(0, self.failed_samples_model.rowCount())
        wid = self.cmbWorkflow.currentData()
        # if not wid: return # Allow viewing failed samples even without WF selection?

        try:
            with db_manager.get_connection() as conn:
                sql = f"""
                    SELECT 
                        Tavg.Repeat, 
                        {db_manager.sql_concat("Analysis.Prefix", "'-'", "Analysis.SampleID")} AS SampleID_Str,
                        Analysis.SampleID, 
                        Analysis.Prefix,
                        Tavg.AnalysisID, 
                        StatusLookup.Description AS StatusDesc,
                        pd.SubAnalysisID, 
                        Sample.sName AS FieldName,
                        Analysis.WorkflowID
                    FROM (
                        SELECT MAX(A.Repeat) AS Repeat, A.SubAnalysisID, PrimaryDistillation.AnalysisID 
                        FROM TRIMS.PrimaryDistillationData AS A
                        INNER JOIN TRIMS.PrimaryDistillation ON PrimaryDistillation.ID = A.ID 
                        GROUP BY A.SubAnalysisID, PrimaryDistillation.AnalysisID
                    ) AS Tavg
                    INNER JOIN TRIMS.PrimaryDistillation ON PrimaryDistillation.AnalysisID = Tavg.AnalysisID
                    INNER JOIN TRIMS.PrimaryDistillationData AS pd ON Tavg.Repeat = pd.Repeat AND Tavg.SubAnalysisID = pd.SubAnalysisID
                    INNER JOIN Analysis ON Analysis.AnalysisID = Tavg.AnalysisID
                    INNER JOIN Sample ON Sample.SampleID = Analysis.SampleID AND Sample.Prefix = Analysis.Prefix
                    INNER JOIN TRIMS.PrimaryDistillationBatch ON PrimaryDistillation.RunID = PrimaryDistillationBatch.RunID
                    INNER JOIN StatusLookup ON StatusLookup.Status = pd.Status
                    WHERE PrimaryDistillation.Status = 99 AND pd.Status = 99
                """
                
                # OPTIONAL: Filter by current workflow if desired
                # if wid:
                #    sql += " AND Analysis.WorkflowID = :wid"
                
                sql += " ORDER BY Tavg.AnalysisID"
                
                rows = conn.execute(text(sql), {"wid": wid} if wid else {}).fetchall()
                
                loaded_sub_ids = set()
                for r in range(self.run_loadlist_model.rowCount()):
                    sub = self.run_loadlist_model.item(r, 5).data(Qt.UserRole)
                    if sub: loaded_sub_ids.add(str(sub))

                for r in rows:
                    sub_id = str(r[6])
                    if sub_id in loaded_sub_ids: continue
                    
                    item_rep = QStandardItem(str(r[0]))
                    item_rep.setData(r[2], Qt.UserRole)   # SID
                    item_rep.setData(r[3], Qt.UserRole+1) # Pfx
                    item_rep.setData(r[4], Qt.UserRole+2) # AnalysisID
                    item_rep.setData(r[6], Qt.UserRole+3) # SubAnalysisID
                    item_rep.setData(r[0], Qt.UserRole+4) # Previous Repeat
                    
                    self.failed_samples_model.appendRow([
                        item_rep,
                        QStandardItem(r[1]), 
                        QStandardItem(str(r[4])), 
                        QStandardItem(r[5]), 
                        QStandardItem(str(r[6])), 
                        QStandardItem(r[7]),
                        QStandardItem(str(r[8])) # WorkflowID
                    ])
            self.tblFailed.resizeColumnsToContents()
        except Exception as e:
            logging.error(f"Load failed samples error: {e}")

    # --- List Management ---
    
    def _add_to_load_list(self, sid, prefix, name, analysis_id=None, sub_id=None, prev_repeat=0):
        flask_num = self.run_loadlist_model.rowCount() + 1
        
        item_lab = QStandardItem(f"{prefix}-{sid}")
        item_lab.setData(sid, Qt.UserRole)
        item_lab.setData(prefix, Qt.UserRole+1)
        
        prev_rep_val = int(prev_repeat or 0)
        if prev_rep_val == 0 and analysis_id is not None:
            prev_rep_val = 1
        new_repeat = prev_rep_val + 1 if analysis_id is not None else 1
        type_str = "Repeat" if analysis_id else "New"
        
        item_aid = QStandardItem(str(analysis_id) if analysis_id else "Auto")
        item_aid.setData(analysis_id, Qt.UserRole) 
        
        item_sub = QStandardItem(str(sub_id) if sub_id else "Auto")
        item_sub.setData(sub_id, Qt.UserRole)

        self.run_loadlist_model.appendRow([
            QStandardItem(str(flask_num)),
            item_lab,
            QStandardItem(name),
            QStandardItem(type_str),
            item_aid,
            item_sub,
            QStandardItem(str(new_repeat))
        ])
        self._update_counts()

    def add_selected_available(self, index=None):
        rows = self.tblAvailable.selectionModel().selectedRows()
        for idx in rows:
            sid = idx.sibling(idx.row(), 0).data(Qt.UserRole)
            pfx = idx.sibling(idx.row(), 0).data(Qt.UserRole+1)
            nam = idx.sibling(idx.row(), 1).data(Qt.DisplayRole)
            self._add_to_load_list(sid, pfx, nam, analysis_id=None, sub_id=None, prev_repeat=0)
        self.load_available_samples() 

    def add_all_available(self):
        self.tblAvailable.selectAll()
        self.add_selected_available()

    def add_selected_failed(self, index=None):
        rows = self.tblFailed.selectionModel().selectedRows()
        for idx in rows:
            item_idx = idx.sibling(idx.row(), 0)
            sid = item_idx.data(Qt.UserRole)
            pfx = item_idx.data(Qt.UserRole+1)
            aid = item_idx.data(Qt.UserRole+2)
            sub = item_idx.data(Qt.UserRole+3)
            rep = item_idx.data(Qt.UserRole+4)
            nam = idx.sibling(idx.row(), 5).data(Qt.DisplayRole) 
            self._add_to_load_list(sid, pfx, nam, analysis_id=aid, sub_id=sub, prev_repeat=rep)
        self.load_failed_samples() 

    def remove_selected(self):
        rows = self.tblLoadList.selectionModel().selectedRows()
        for idx in sorted(rows, key=lambda x: x.row(), reverse=True):
            self.run_loadlist_model.removeRow(idx.row())
        for r in range(self.run_loadlist_model.rowCount()):
            self.run_loadlist_model.item(r, 0).setText(str(r + 1))
        self.load_available_samples()
        self.load_failed_samples()
        self._update_counts()

    def remove_all(self):
        self.run_loadlist_model.clear()
        self.run_loadlist_model.setHorizontalHeaderLabels(["FlaskID", "OurLabID", "Sample Name", "Type", "AnalysisID", "SubID", "Repeat"])
        self.load_available_samples()
        self.load_failed_samples()
        self._update_counts()

    def _update_counts(self):
        c = self.run_loadlist_model.rowCount()
        self.lblCount.setText(f"Selected: {c}")

    # --- Save / Create Run ---
    
    def on_create_clicked(self):
        if self.run_loadlist_model.rowCount() == 0:
            show_message(self, "Empty", "No samples selected.", QMessageBox.Warning)
            return
            
        wid = self.cmbWorkflow.currentData()
        pid = self.cmbProcedure.currentData()
        eid = self.cmbInstrument.currentData()
        jid = self.cmbJob.currentData() 
        
        wjid = None
        if wid and jid:
            try:
                with db_manager.get_connection() as conn:
                    res = conn.execute(text("SELECT WorkflowJobID FROM WorkflowJob WHERE WorkflowID=:w AND JobName=:j"), {"w": wid, "j": jid}).fetchone()
                    if res: wjid = res[0]
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

        if not all([wid, pid, eid, wjid]):
            show_message(self, "Setup Error", "Please select Job, Workflow, Procedure, and Device.", QMessageBox.Warning)
            return
            
        if self._save_to_db(wid, wjid, pid, eid):
            self.runCreated.emit()
            self.requestClose.emit()

    def _save_to_db(self, wid, wjid, pid, eid):
        user = getpass.getuser()
        now = datetime.now()

        try:
            with db_manager.get_connection() as conn:
                # 1. Create Header — sequence assigns RunID
                rid = conn.execute(text("""
                    INSERT INTO TRIMS.PrimaryDistillationBatch
                    (WorkflowID, WorkflowJobID, ProcedureID, EquipmentID, StartDate, CreateDateStamp, CreateUserStamp, IsLocked, Remarks)
                    VALUES (:wid, :wjid, :pid, :eid, :start, :now, :user, 0, 'Created via PyLIMS')
                    RETURNING RunID
                """), {
                    "wid": wid, "wjid": wjid, "pid": pid, "eid": eid,
                    "start": now, "now": now, "user": user
                }).scalar()

                # 2. Process load list — cache by sample to handle split flasks
                new_analysis_map = {}
                pd_map = {}

                for r in range(self.run_loadlist_model.rowCount()):
                    flask = int(self.run_loadlist_model.item(r, 0).text())
                    sid = int(self.run_loadlist_model.item(r, 1).data(Qt.UserRole))
                    pfx = self.run_loadlist_model.item(r, 1).data(Qt.UserRole+1)

                    existing_aid = self.run_loadlist_model.item(r, 4).data(Qt.UserRole)
                    existing_sub = self.run_loadlist_model.item(r, 5).data(Qt.UserRole)
                    target_rep = int(self.run_loadlist_model.item(r, 6).text())

                    current_aid = None
                    current_sub = None

                    if existing_aid is None:
                        # NEW Sample logic
                        cache_key = f"{pfx}-{sid}"

                        if cache_key in new_analysis_map:
                            # Already created Analysis for this sample (split flask)
                            current_aid, current_sub = new_analysis_map[cache_key]
                        else:
                            # Create new Analysis — sequence assigns AnalysisID
                            current_aid = conn.execute(text("""
                                INSERT INTO Analysis (Prefix, SampleID, WorkflowID, Status, Repeats, CreateDateStamp, CreateUserStamp)
                                VALUES (:pfx, :sid, :wid, 2, :rep, :now, :user)
                                RETURNING AnalysisID
                            """), {
                                "pfx": pfx, "sid": sid, "wid": wid,
                                "rep": target_rep, "now": now, "user": user
                            }).scalar()
                            current_sub = current_aid

                            conn.execute(text("UPDATE Sample SET Status=4 WHERE SampleID=:s AND Prefix=:p"), {"s": sid, "p": pfx})
                            conn.execute(text("DELETE FROM public.sample_queue WHERE sampleid=:s AND prefix=:p"), {"s": sid, "p": pfx})

                            new_analysis_map[cache_key] = (current_aid, current_sub)
                    else:
                        # REPEAT Sample logic
                        current_aid = existing_aid
                        current_sub = existing_sub
                        conn.execute(text("UPDATE Analysis SET Status=2, ModifDateStamp=:now WHERE AnalysisID=:aid"), {"now": now, "aid": existing_aid})

                    # --- Determine PrimaryDistillation (PD) Record ---
                    # Logic: 1 PD record per (AnalysisID, Repeat) pair in this Run.
                    pd_key = (current_aid, target_rep)
                    pd_id = None
                    
                    if pd_key in pd_map:
                        pd_id = pd_mappd_key
                    else:
                        # Insert PD (Parent)
                        conn.execute(text(f"""
                            INSERT INTO TRIMS.PrimaryDistillation (RunID, FlaskID, AnalysisID, Repeat, Status)
                            VALUES (:rid, :flask, :aid, :rep, 2)
                        """), {
                            "rid": rid, "flask": flask, "aid": current_aid, "rep": target_rep
                        })
                        
                        res_pd = conn.execute(text("SELECT ID FROM TRIMS.PrimaryDistillation WHERE RunID=:rid AND FlaskID=:flask"), 
                                             {"rid": rid, "flask": flask}).fetchone()
                        pd_id = res_pd[0]
                        pd_mappd_key = pd_id
                    
                    # --- Insert PrimaryDistillationData (PDD) ---
                    # 1 record per Flask (Row in Load List)
                    # NOTE: Added AnalysisID, ModifDateStamp, ModifUserStamp per request
                    conn.execute(text(f"""
                        INSERT INTO TRIMS.PrimaryDistillationData 
                        (ID, AnalysisID, SubAnalysisID, Repeat, Status, FlaskID, ModifDateStamp, ModifUserStamp)
                        VALUES (:id, :aid, :sub, :rep, 2, :flask, :now, :user)
                    """), {
                        "id": pd_id, "aid": current_aid, "sub": current_sub, "rep": target_rep, "flask": flask,
                        "now": now, "user": user
                    })

                conn.commit()
                show_message(self, "Success", f"Distillation Run {rid} created successfully.", QMessageBox.Information)
                return True
                
        except Exception as e:
            logging.error(f"Create run DB error: {e}")
            show_message(self, "Error", f"Failed to create run:\n{e}", QMessageBox.Critical)
            return False

class TrimsDistillationCreateRunDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Distillation Batch")
        self.resize(1100, 700)
        lay = QVBoxLayout(self); lay.setContentsMargins(0,0,0,0)
        self.core = TrimsDistillationCreateRunWidget(self)
        lay.addWidget(self.core)
        
        self.core.requestClose.connect(self.reject)
        self.core.runCreated.connect(self.accept)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    dlg = TrimsDistillationCreateRunDialog()
    dlg.show()
    sys.exit(app.exec_())