"""
sample_workflow_assign_gui.py — Sample-to-workflow assignment panel for IsoWorks.
Provides SiamWorkflowAssignWindow for manually staging samples from the project
list into the sample_queue for a selected workflow.
"""
import sys
from datetime import datetime
import logging
import getpass

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLabel, 
    QPushButton, QTableView, QCheckBox, QMessageBox, 
    QAbstractItemView, QHeaderView, QLineEdit, QFrame
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor, QFont
from PyQt5.QtCore import Qt, QTimer

# --- Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import make_searchable_combo

class SiamWorkflowAssignWindow(QWidget):
    """
    PyQt5 equivalent of frmWorkflowAssign.
    Allows manual staging of samples to the sample_queue for a chosen workflow.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign Samples to Workflow")
        self.resize(1200, 800)
        
        self.setAttribute(Qt.WA_DeleteOnClose, False) 
        
        self._updating_filters = False

        self.main_layout = QVBoxLayout(self)
        
        # --- TOP SECTION (Split Left/Right) ---
        top_container = QHBoxLayout()
        top_container.addWidget(self._create_filter_group(), 1) 
        top_container.addWidget(self._create_target_group(), 1) 

        self.btn_assign = QPushButton("Assign to TBA")
        self.btn_assign.setCursor(Qt.PointingHandCursor)
        self.btn_assign.setStyleSheet("""
            QPushButton {
                font-weight: bold; background-color: #27ae60; color: white; 
                padding: 10px; font-size: 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2ecc71; }
            QPushButton:pressed { background-color: #219150; }
        """)
        top_container.addWidget(self.btn_assign) 
                
        self.main_layout.addLayout(top_container)
        
        # --- MIDDLE SECTION: Dual Lists ---
        self.main_layout.addLayout(self._create_lists_layout(), 1) 
        
        # --- BOTTOM: Status ---
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("""
            color: #333; font-size: 12px; font-weight: bold; 
            padding: 8px; background-color: #f0f0f0; border-top: 1px solid #ccc;
        """)
        self.main_layout.addWidget(self.status_label)

        # --- Setup ---
        self._init_models()
        self._connect_signals()
        
        # --- Load Data ---
        try:
            db_manager.get_engine()
            self.load_initial_data()
        except Exception as e:
            self.setEnabled(False)
            self.status_label.setText(f"Database Error: {e}")

    def closeEvent(self, event):
        super().closeEvent(event)

    def _apply_table_styles(self, table):
        """Applies the clean white/blue theme."""

        table.setStyleSheet("""
        QTableView {
            border: 1px solid #d0d0d0;
            background-color: white;
            gridline-color: none;
        }
        QTableView::item {
            padding: 6px 8px;
            border: none;
            background-color: white;
            color: #333333;
        }
        QTableView::item:alternate { background-color: #F3F7FA; }
        QTableView::item:selected { background-color: #DDEEFF; color: #000000; }

        /* Apply the bold colored style ONLY to the horizontal header */
        QTableView QHeaderView::section:horizontal {
            background-color: white;
            color: #7F8BB5;
            font-weight: bold;
            padding: 6px 8px;
            border: none;
            border-bottom: 2px solid #7F8BB5;
            /* Note: text alignment of header captions is controlled in the view; stylesheet 'text-align' is not used by Qt items */
        }
        QTableView QHeaderView::section:horizontal:hover {
            background-color: #DDEEFF; color: #000000;
        }

        /* Make the vertical header (row numbers) plain/subtle */
        QTableView QHeaderView::section:vertical {
            background-color: white;
            color: #999999;
            font-weight: normal;
            padding: 0 6px;
            border: none;
            border-right: 1px solid #eee;
        }
        """)
        
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)

    def _create_filter_group(self):
        group = QGroupBox("1. Filter Available Samples")
        layout = QGridLayout()
        layout.setVerticalSpacing(10)
        
        self.cmb_filter_workflow = QComboBox() # Req. Workflow
        self.cmb_submission = QComboBox()
        self.cmb_submission.setEditable(True)
        self.cmb_media = QComboBox()
        self.cmb_filter_status = QComboBox()   # Sample Status Filter
        
        # Logic 1: Checkbox starts disabled
        self.chk_show_all = QCheckBox("Ignore Status Filter (Show All for Selection)")
        self.chk_show_all.setEnabled(False)
        self.chk_show_all.setToolTip("Enable by selecting a Submission first.")
        
        layout.addWidget(QLabel("Req. Workflow:"), 0, 0)
        layout.addWidget(self.cmb_filter_workflow, 0, 1)
        layout.addWidget(QLabel("Media:"), 0, 2)
        layout.addWidget(self.cmb_media, 0, 3)
        
        layout.addWidget(QLabel("Submission:"), 1, 0)
        layout.addWidget(self.cmb_submission, 1, 1)
        layout.addWidget(QLabel("Status:"), 1, 2)
        layout.addWidget(self.cmb_filter_status, 1, 3)
        
        layout.addWidget(self.chk_show_all, 2, 0, 1, 4)
        
        group.setLayout(layout)
        return group

    def _create_target_group(self):
        group = QGroupBox("2. Target Configuration")
        layout = QGridLayout() 
        layout.setVerticalSpacing(10)
        
        self.cmb_target_workflow = QComboBox()
        self.cmb_target_priority = QComboBox()
        self.txt_replicates = QLineEdit("1")
        
        layout.addWidget(QLabel("Assign Workflow:"), 0, 0)
        layout.addWidget(self.cmb_target_workflow, 0, 1)
        
        layout.addWidget(QLabel("Priority:"), 1, 0)
        layout.addWidget(self.cmb_target_priority, 1, 1)
        
        layout.addWidget(QLabel("Replicates:"), 2, 0)
        layout.addWidget(self.txt_replicates, 2, 1)
        group.setLayout(layout)
        return group

    def _create_lists_layout(self):
        layout = QHBoxLayout()
        
        # LEFT: Available
        left_grp = QGroupBox("Available Samples")
        left_lay = QVBoxLayout()
        self.table_available = QTableView()
        self.table_available.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_available.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_available.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_available.horizontalHeader().setDefaultAlignment(Qt.AlignLeft  | Qt.AlignVCenter)
        # Apply Styles
        self._apply_table_styles(self.table_available)
        
        left_lay.addWidget(self.table_available)
        left_grp.setLayout(left_lay)
        
        # CENTER: Buttons
        btn_layout = QVBoxLayout()
        btn_layout.addStretch()
        self.btn_add = QPushButton(" > ")
        self.btn_add_all = QPushButton(" >> ")
        self.btn_remove = QPushButton(" < ")
        self.btn_remove_all = QPushButton(" << ")
        for b in [self.btn_add, self.btn_add_all, self.btn_remove, self.btn_remove_all]:
            b.setFixedWidth(40)
            b.setFixedHeight(40)
            btn_layout.addWidget(b)
        btn_layout.addStretch()
        
        # RIGHT: Selected
        right_grp = QGroupBox("Selected for Staging")
        right_lay = QVBoxLayout()
        self.table_selected = QTableView()
        self.table_selected.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_selected.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_selected.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_selected.horizontalHeader().setDefaultAlignment(Qt.AlignLeft  | Qt.AlignVCenter)
        
        # Apply Styles
        self._apply_table_styles(self.table_selected)

        right_lay.addWidget(self.table_selected)
        right_grp.setLayout(right_lay)
        
        layout.addWidget(left_grp)
        layout.addLayout(btn_layout)
        layout.addWidget(right_grp)
        
        return layout

    def _init_models(self):
        headers = ["SampleID", "Prefix", "Submission ID", "Sample ID", "Sample Name", "Status", "Priority", "Container Type", "Media", "WorkflowID"]

        self.model_avail = QStandardItemModel()
        self.model_avail.setHorizontalHeaderLabels(headers)
        self.table_available.setModel(self.model_avail)
        self.table_available.hideColumn(0)
        self.table_available.hideColumn(1)
        self.table_available.hideColumn(8)   # Media
        self.table_available.hideColumn(9)   # WorkflowID

        self.model_sel = QStandardItemModel()
        self.model_sel.setHorizontalHeaderLabels(headers)
        self.table_selected.setModel(self.model_sel)
        self.table_selected.hideColumn(0)
        self.table_selected.hideColumn(1)
        self.table_selected.hideColumn(8)    # Media
        self.table_selected.hideColumn(9)    # WorkflowID
        
    def _connect_signals(self):
        # Filter Change Events
        self.cmb_submission.currentIndexChanged.connect(self._on_submission_changed)
        self.chk_show_all.toggled.connect(self.load_available_samples)
        self.cmb_media.currentIndexChanged.connect(self._on_media_changed)
        self.cmb_filter_status.currentIndexChanged.connect(self.load_available_samples)
        
        self.cmb_filter_workflow.currentIndexChanged.connect(self._on_filter_workflow_changed)
        
        # Buttons
        self.btn_add.clicked.connect(self.add_selection)
        self.btn_add_all.clicked.connect(self.add_all)
        self.btn_remove.clicked.connect(self.remove_selection)
        self.btn_remove_all.clicked.connect(self.remove_all)
        self.btn_assign.clicked.connect(self.assign_to_tba)

    def load_initial_data(self):
        self._updating_filters = True
        try:
            with db_manager.get_connection() as conn:
                sql_sub = "SELECT SubmissionID, SubmissionName FROM Submission WHERE Status < 200 ORDER BY SubmissionID DESC"
                self.cmb_submission.addItem("- All Submissions -", None)
                for row in conn.execute(text(sql_sub)):
                    self.cmb_submission.addItem(f"{row[0]} - {row[1]}", row[0])

                sql_med = f"SELECT MediaID, medianame FROM Media WHERE IsActive = {db_manager.sql_bool(True)} ORDER BY MediaID"
                self.cmb_media.addItem("- All Media -", None)
                for row in conn.execute(text(sql_med)):
                    self.cmb_media.addItem(f"{row[1]}", row[0])

                sql_wf = f"SELECT WorkflowID, WorkflowName FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY WorkflowID"
                self.cmb_filter_workflow.addItem("- Select Workflow -", None)
                
                wf_rows = conn.execute(text(sql_wf)).fetchall()
                for row in wf_rows:
                    item_text = f"{row[0]} - {row[1]}"
                    self.cmb_filter_workflow.addItem(item_text, row[0])
                    self.cmb_target_workflow.addItem(item_text, row[0])
                make_searchable_combo(self.cmb_filter_workflow)
                make_searchable_combo(self.cmb_target_workflow)
                
                sql_pri = "SELECT PriorityID, Description FROM Priority ORDER BY PriorityID"
                for row in conn.execute(text(sql_pri)):
                    self.cmb_target_priority.addItem(row[1], row[0])
                
                idx = self.cmb_target_priority.findText("Normal", Qt.MatchContains)
                if idx >= 0: self.cmb_target_priority.setCurrentIndex(idx)

                # --- Sample Status ---
                self.cmb_filter_status.addItem("- All Statuses -", -1) # Special Value for All
                sql_stat = "SELECT Status, Description FROM StatusLookup WHERE Status IN (1, 200, 222) ORDER BY Status"
                for row in conn.execute(text(sql_stat)):
                    self.cmb_filter_status.addItem(f"{row[0]} - {row[1]}", row[0])
                
                # Default to '1 - Registered'
                idx_stat = self.cmb_filter_status.findText("1 -", Qt.MatchContains)
                if idx_stat >= 0: self.cmb_filter_status.setCurrentIndex(idx_stat)

        except Exception as e:
            logging.error(f"Init Load Error: {e}")
        finally:
             self._updating_filters = False

    def _on_filter_workflow_changed(self):
        if self._updating_filters: return
        self._updating_filters = True
        
        try:
            wf_id = self.cmb_filter_workflow.currentData()
            
            if wf_id:
                idx_target = self.cmb_target_workflow.findData(wf_id)
                if idx_target >= 0: self.cmb_target_workflow.setCurrentIndex(idx_target)
            
                with db_manager.get_connection() as conn:
                    res = conn.execute(text("SELECT MediaID FROM Workflow WHERE WorkflowID=:wid"), {"wid": wf_id}).fetchone()
                    if res:
                        idx_med = self.cmb_media.findData(res[0])
                        if idx_med >= 0: self.cmb_media.setCurrentIndex(idx_med)
            
            idx_stat = self.cmb_filter_status.findText("1 -", Qt.MatchContains)
            if idx_stat >= 0: self.cmb_filter_status.setCurrentIndex(idx_stat)
            
            self.cmb_submission.setCurrentIndex(0)
            
            # Reset "Show All" if workflow changed (Logic 1)
            self.chk_show_all.setChecked(False)
            self.chk_show_all.setEnabled(False)
            
        except Exception as e:
            logging.error(f"Workflow Change Error: {e}")
        finally:
            self._updating_filters = False
            self.load_available_samples()

    def _on_submission_changed(self):
        if self._updating_filters: return
        sub_id = self.cmb_submission.currentData()
        
        # Logic 1: Enable checkbox only if Submission is selected
        self.chk_show_all.setEnabled(bool(sub_id))
        if not sub_id:
            self.chk_show_all.setChecked(False)
            self.load_available_samples()
            return

        self._updating_filters = True
        try:
            with db_manager.get_connection() as conn:
                sql = "SELECT RequestedWorkflow, MediaID FROM Submission WHERE SubmissionID = :sid"
                row = conn.execute(text(sql), {"sid": sub_id}).fetchone()
                if row:
                    req_wf, media_id = row[0], row[1]
                    
                    idx_wf = self.cmb_filter_workflow.findData(req_wf)
                    if idx_wf >= 0: self.cmb_filter_workflow.setCurrentIndex(idx_wf)
                    
                    idx_target = self.cmb_target_workflow.findData(req_wf)
                    if idx_target >= 0: self.cmb_target_workflow.setCurrentIndex(idx_target)

                    idx_med = self.cmb_media.findData(media_id)
                    if idx_med >= 0: self.cmb_media.setCurrentIndex(idx_med)
            
            idx_stat = self.cmb_filter_status.findText("1 -", Qt.MatchContains)
            if idx_stat >= 0: self.cmb_filter_status.setCurrentIndex(idx_stat)

        except Exception as e:
            logging.error(f"Submission Change Error: {e}")
        finally:
            self._updating_filters = False
            self.load_available_samples()

    def _on_media_changed(self):
        if self._updating_filters: return
        
        self.cmb_filter_workflow.blockSignals(True)
        self.cmb_filter_workflow.setCurrentIndex(0) # All
        self.cmb_filter_workflow.blockSignals(False)
        
        self.load_available_samples()

    def load_available_samples(self):
        self.model_avail.removeRows(0, self.model_avail.rowCount())
        
        sub_id = self.cmb_submission.currentData()
        media_id = self.cmb_media.currentData()
        wf_id = self.cmb_filter_workflow.currentData()
        stat_id = self.cmb_filter_status.currentData()
        ignore_status = self.chk_show_all.isChecked()
        
        # Build Exclusion List from Selected Table
        excluded_ids = set()
        for r in range(self.model_sel.rowCount()):
            sid = self.model_sel.item(r, 0).text()
            pfx = self.model_sel.item(r, 1).text()
            excluded_ids.add(f"{pfx}-{sid}")
        
        try:
            with db_manager.get_connection() as conn:
                # Logic: If Status is -1 (All), limit big queries
                apply_limit = (stat_id == -1)
                # {db_manager.sql_concat("Prefix", "'-'", "sName")} AS [Sample ID],
                sql = f"""
                    SELECT {db_manager.sql_top(100) if apply_limit else ''}
                        s.SampleID, s.Prefix,
                        s.SubmissionID, s.sName,
                        s.Status, p.Description,
                        m.MediaName, s.WorkflowID,
                        ctl.type_name
                    FROM Sample s
                    JOIN Media m ON s.MediaID = m.MediaID
                    JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
                    LEFT JOIN Priority p ON sub.PriorityID = p.PriorityID
                    LEFT JOIN public.sample_queue sq ON (s.SampleID = sq.sampleid AND s.Prefix = sq.prefix)
                    LEFT JOIN Analysis a ON (s.SampleID = a.SampleID AND s.Prefix = a.Prefix)
                    LEFT JOIN public.container_type_lookup ctl ON ctl.container_type_id = s.container_type
                    WHERE s.SampleType = 0
                      AND sq.queue_id IS NULL
                      AND a.AnalysisID IS NULL
                """
                params = {}
                
                if sub_id:
                    sql += " AND s.SubmissionID = :sid"; params["sid"] = sub_id
                if media_id:
                    sql += " AND s.MediaID = :mid"; params["mid"] = media_id
                if wf_id:
                    sql += " AND s.WorkflowID = :wf"; params["wf"] = wf_id
                
                # Apply Status Filter only if not -1 (All)
                # And we respect "ignore_status" checkbox (if True, we skip this check, effectively showing all for submission)
                if not ignore_status and stat_id is not None and stat_id != -1:
                    sql += " AND s.Status = :st"; params["st"] = stat_id
                
                sql += " ORDER BY sub.PriorityID, s.SubmissionID, s.SampleID"
                if apply_limit:
                    sql += db_manager.sql_limit(100)
                
                for row in conn.execute(text(sql), params):
                    full_id = f"{row[1]}-{row[0]}"
                    if full_id in excluded_ids: continue
                    
                    items = [
                        QStandardItem(str(row[0])),              # 0: SampleID (hidden)
                        QStandardItem(str(row[1])),              # 1: Prefix (hidden)
                        QStandardItem(str(row[2])),              # 2: Submission ID
                        QStandardItem(f"{row[1]}-{row[0]}"),     # 3: OurLabID
                        QStandardItem(str(row[3])),              # 4: Sample Name
                        QStandardItem(str(row[4])),              # 5: Status
                        QStandardItem(str(row[5] or "")),        # 6: Priority
                        QStandardItem(str(row[8] or "")),        # 7: Container Type
                        QStandardItem(str(row[6])),              # 8: Media (hidden)
                        QStandardItem(str(row[7] or "")),        # 9: WorkflowID (hidden)
                    ]
                    self.model_avail.appendRow(items)
                    
            self.status_label.setText(f"Loaded {self.model_avail.rowCount()} available samples.")
            self.table_available.resizeColumnsToContents()
            
        except Exception as e:
            logging.error(f"Load Samples Error: {e}")
            self.status_label.setText("Error loading samples.")

    # --- Moving Items ---
    def _move_items(self, source_view, source_model, dest_model):
        rows = sorted(set(i.row() for i in source_view.selectionModel().selectedRows()), reverse=True)
        for r in rows:
            items = [source_model.item(r, c).clone() for c in range(source_model.columnCount())]
            dest_model.appendRow(items)
            source_model.removeRow(r)
        
        self.load_available_samples() # Consistency Refresh
        self.status_label.setText(f"Selected: {self.model_sel.rowCount()} samples.")
        self.table_selected.resizeColumnsToContents()
        
    def add_selection(self): self._move_items(self.table_available, self.model_avail, self.model_sel)
    def remove_selection(self): self._move_items(self.table_selected, self.model_sel, self.model_avail)
    
    def add_all(self):
        self.table_available.selectAll()
        self.add_selection()

    def remove_all(self):
        self.table_selected.selectAll()
        self.remove_selection()

    def assign_to_tba(self):
        req_wf_id = self.cmb_filter_workflow.currentData()
        if not req_wf_id:
             QMessageBox.warning(self, "Procedure Error", "Sample transfer requires a specific Requested Workflow to be selected in filters.")
             return

        count = self.model_sel.rowCount()
        if count == 0:
            QMessageBox.warning(self, "No Samples", "No samples selected for staging.")
            return

        wf_id = self.cmb_target_workflow.currentData()
        pri_id = self.cmb_target_priority.currentData()
        
        if not wf_id or not pri_id:
            QMessageBox.warning(self, "Missing Info", "Please select a Target Workflow and Priority.")
            return
        
        if wf_id != req_wf_id:
             if QMessageBox.question(self, "Workflow Mismatch", 
                                     f"Target Workflow ({wf_id}) differs from Filtered/Requested Workflow ({req_wf_id}).\nContinue?", 
                                     QMessageBox.Yes|QMessageBox.No) == QMessageBox.No:
                 return

        try:
            reps = int(self.txt_replicates.text())
        except: reps = 1

        if QMessageBox.question(self, "Confirm", f"Assign {count} samples to Workflow {wf_id}?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No:
            return

        try:
            conn = db_manager.get_engine()
            with conn.begin() as trans: 
                res = trans.execute(text("SELECT WorkflowJobID FROM WorkflowJob WHERE WorkflowID=:w AND RunSequence=1"), {"w": wf_id}).fetchone()
                if not res: raise ValueError("Invalid Workflow: No starting job found.")
                wf_job_id = res[0]
                
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                user_str = getpass.getuser()

                sql_ins = """
                    INSERT INTO public.sample_queue
                        (sampleid, prefix, mediaid, workflowjobid, priorityid, repeat_count, queued_at, queued_by)
                    SELECT sampleid, prefix, mediaid, :job, :pri, :rep, :now, :usr
                    FROM public.sample WHERE sampleid=:sid AND prefix=:pfx
                    ON CONFLICT (sampleid, prefix, workflowjobid) DO NOTHING
                """
                
                processed_ids = []
                affected_submission_ids = set()

                for r in range(count):
                    sid = int(self.model_sel.item(r, 0).text())
                    pfx = self.model_sel.item(r, 1).text()
                    sub_item = self.model_sel.item(r, 2)
                    if sub_item and sub_item.text().isdigit():
                        affected_submission_ids.add(int(sub_item.text()))

                    for i in range(reps):
                        trans.execute(text(sql_ins), {
                            "job": wf_job_id, "pri": pri_id,
                            "rep": i + 1, "now": now_str, "usr": user_str,
                            "sid": sid, "pfx": pfx
                        })
                    processed_ids.append((pfx, sid))

                for pfx, sid in processed_ids:
                    trans.execute(text("UPDATE public.sample SET status=222 WHERE sampleid=:sid AND prefix=:pfx"), {"sid": sid, "pfx": pfx})

                if affected_submission_ids:
                    trans.execute(text(
                        "UPDATE public.submission SET status=200 WHERE submissionid = ANY(:ids)"
                    ), {"ids": list(affected_submission_ids)})
                
            self.model_sel.clear()
            self.load_available_samples()
            self.status_label.setText(f"Success: Staged {count} samples.")
            QMessageBox.information(self, "Success", f"Successfully staged {count} samples.")
            
        except Exception as e:
            logging.error(f"Assign Failed: {e}")
            QMessageBox.critical(self, "Error", f"Assignment failed: {e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SiamWorkflowAssignWindow()
    win.show()
    sys.exit(app.exec_())