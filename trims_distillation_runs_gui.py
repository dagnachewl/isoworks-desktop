"""
trims_distillation_runs_gui.py — TRIMS distillation run list panel for IsoWorks.
Provides a filterable list of distillation runs with a colour-coded status delegate,
linking to TrimsDistillationDetailsWindow and the run creation dialog.
"""
import sys
import logging
from typing import Optional
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel,
    QMessageBox, QHeaderView, QDialog, QVBoxLayout as QVBoxLayoutDialog, QStyledItemDelegate
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor, QPainter
from PyQt5.QtCore import Qt, QCoreApplication, QRect

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name
from trims_distillation_details_gui import TrimsDistillationDetailsWindow
from help_browser import make_help_button

# Lazy import for create dialog to avoid circular imports
try:
    from trims_distillation_create_run_gui import TrimsDistillationCreateRunDialog
except ImportError:
    TrimsDistillationCreateRunDialog = None

# ---------------------------------------------------------------------
# Status dot delegate for Status column
# ---------------------------------------------------------------------
class StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() == 0:
            status_data = index.data(Qt.UserRole + 1)
            if status_data == "Ongoing":
                color = QColor(255, 165, 0)
            elif status_data == "Complete":
                color = QColor(50, 200, 50)
            elif status_data == "Pending":
                color = QColor(255, 0, 0)
            else:
                super().paint(painter, option, index)
                return
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing)
            circle_size = 12
            x_pos = option.rect.x() + int((option.rect.width() - circle_size) / 2)
            y_pos = option.rect.y() + int((option.rect.height() - circle_size) / 2)
            rect = QRect(x_pos, y_pos, circle_size, circle_size)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(rect)
            painter.restore()
        else:
            super().paint(painter, option, index)

# ---------------------------------------------------------------------
# Main Runs Window
# ---------------------------------------------------------------------
class TrimsDistillationRunsWindow(QWidget):
    """
    List, filter, and open TRIMS Primary Distillation Runs.
    Refactored to use db_core and improved layout/UX.
    """
    DELAY_THRESHOLD_DAYS = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_layout = QVBoxLayout(self)

        # Top action buttons (now: only Close)
        self.main_layout.addLayout(self._create_action_buttons_group())
        self.main_layout.addSpacing(20)

        # Filter/Search group
        self.main_layout.addWidget(self._create_filter_group(), 0)
        self.main_layout.addSpacing(40)

        # Runs table
        self.run_table = QTableView()
        self._apply_table_styling()
        self.status_delegate = StatusDelegate()
        self.run_table.setItemDelegateForColumn(0, self.status_delegate)
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True)
        self.run_table_model = QStandardItemModel()
        self.run_table.setModel(self.run_table_model)
        self.main_layout.addWidget(self.run_table, 1)

        # Events
        self.run_table.doubleClicked.connect(self.on_table_double_clicked)

        # Privileges
        self.has_write_priv = False
        self.has_admin_priv = False
        self._check_privileges()
        self._update_ui_state()

        # DB load
        try:
            db_manager.get_engine()
            self.setEnabled(True)
            self.load_run_list()
            if self.run_table.selectionModel():
                self.run_table.selectionModel().selectionChanged.connect(self.on_selection_changed)
        except Exception:
            self.setEnabled(False)
            lbl = QLabel("<h2>No Database Connection</h2><p>Please configure in Settings.</p>")
            lbl.setAlignment(Qt.AlignCenter)
            self.main_layout.addWidget(lbl)

    # ---------------- Privileges ----------------
    def _check_privileges(self):
        try:
            user_normalized = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user_normalized, "AccessDistillation")
            self.has_admin_priv = check_employee_privilege(user_normalized, "AdminTRIMS")
            logging.info(f"User {user_normalized}: write={self.has_write_priv}, admin={self.has_admin_priv}")
        except Exception as e:
            logging.error(f"Failed to check privileges: {e}", exc_info=True)
            self.has_write_priv = False
            self.has_admin_priv = False

    def _update_ui_state(self):
        self.btnCreateRun.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    # ---------------- Styles ----------------
    def _apply_table_styling(self):
        self.run_table.setShowGrid(False)
        self.run_table.setStyleSheet("""
        QTableView { border: none; background-color: white; gridline-color: none; }
        QTableView::item { padding: 6px 8px; border: none; background-color: white; color: #333333; text-align: left; }
        QTableView::item:alternate { background-color: #F3F7FA; }
        QTableView::item:selected { background-color: #DDEEFF; color: #000000; }
        QHeaderView::section { background-color: white; color: #7F8BB5; font-weight: bold; padding: 6px 8px; border: none; border-bottom: 2px solid #7F8BB5; text-align: left; }
        QHeaderView::section:hover { background-color: #DDEEFF; color: #000000; }
        """)
        self.run_table.setAlternatingRowColors(True)
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    # ---------------- Filter/Search group ----------------
    def _create_filter_group(self):
        group = QGroupBox("Filter / Search Distillation Runs")
        main_layout = QVBoxLayout()

        # Show ongoing only
        filter_layout = QHBoxLayout()
        self.chkShowOngoing = QCheckBox("Show Ongoing Batches Only")
        self.chkShowOngoing.setChecked(True)
        filter_layout.addWidget(self.chkShowOngoing)
        filter_layout.addStretch(1)
        main_layout.addLayout(filter_layout)

        # Batch load + Create/Delete next to Load
        # batch_search_layout = QHBoxLayout()
        # batch_search_layout.addWidget(QLabel("Load Specific Batch:"))
        # self.txtDistBatchNo = QLineEdit()
        # self.txtDistBatchNo.setPlaceholderText("Enter Distillation Batch No.")
        # self.txtDistBatchNo.setMaximumWidth(200)
        # batch_search_layout.addWidget(self.txtDistBatchNo)

        # self.btnBatchLoad = QPushButton("Load")
        # batch_search_layout.addWidget(self.btnBatchLoad)

        # batch_search_layout.addWidget(self.btnDelete)

        # batch_search_layout.addStretch(1)
        # main_layout.addLayout(batch_search_layout)

        # Search term row
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search List By:"))
        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Distil Run", "Sample ID", "Sample Name", "Project Name"])
        self.cmbSearchType.setCurrentText("Distil Run")
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        search_layout.addWidget(self.cmbSearchType)
        self.txtSearchValue = QLineEdit()
        self.txtSearchValue.setPlaceholderText("Enter search term...")
        self.txtSearchValue.returnPressed.connect(self._on_search_or_open)
        search_layout.addWidget(self.txtSearchValue, 1)
        self.btnSearch = QPushButton("Open Run")  # starts as "Open Run" (Distil Run is default)
        self.btnSearch.clicked.connect(self._on_search_or_open)
        search_layout.addWidget(self.btnSearch)
        self.btnResetFilters = QPushButton("Reset All")
        search_layout.addWidget(self.btnResetFilters)        

        self.btnDelete = QPushButton("Delete Selected")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #a93226; }
            QPushButton:pressed { background-color: #922b21; }
            QPushButton:disabled { background-color: #f1948a; color: #7f8c8d; }
        """)
        search_layout.addWidget(self.btnDelete)
        main_layout.addLayout(search_layout)
        group.setLayout(main_layout)

        # Signals
        self.chkShowOngoing.toggled.connect(self.load_run_list)
        self.btnResetFilters.clicked.connect(self.reset_filters)

        # self.btnBatchLoad.clicked.connect(self.load_single_batch_details)
        self.btnDelete.clicked.connect(self.delete_run)

        return group

    # ---------------- Top bar: Create New Run + Close ----------------
    def _create_action_buttons_group(self):
        layout = QHBoxLayout()
        layout.addStretch()
        self.btnCreateRun = QPushButton("Create New Run")
        self.btnCreateRun.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:pressed { background-color: #1e8449; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnCreateRun.clicked.connect(self.create_new_run)
        layout.addWidget(self.btnCreateRun)
        self.btnClose = QPushButton("Close Module")
        layout.addWidget(self.btnClose)
        self.btnClose.clicked.connect(self.close_module)
        layout.addWidget(make_help_button(self, "trims_distillation"))
        return layout

    def close_module(self):
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()

    # ---------------- Utility SQL concat ----------------
    def _sql_concat(self, *args):
        dialect = db_manager.dialect
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')):
                parts.append(arg)
            else:
                parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _build_filter_where_clause(self, is_list_filter=True):
        clauses = []; params = {}
        if is_list_filter and self.chkShowOngoing.isChecked():
            clauses.append("pdb.EndDate IS NULL")
        if is_list_filter:
            stype = self.cmbSearchType.currentText()
            sval = self.txtSearchValue.text().strip()
            if sval and stype:
                try:
                    if stype == "Distil Run":
                        clauses.append("pdb.RunID = :rid"); params["rid"] = int(sval)
                    elif stype == "Sample ID":
                        clauses.append("pdb.RunID IN (SELECT DISTINCT pd.RunID FROM TRIMS.PrimaryDistillation pd JOIN Analysis a ON pd.AnalysisID=a.AnalysisID WHERE a.SampleID=:sid)")
                        params["sid"] = int(sval)
                    elif stype in ["Sample Name", "Project Name"]:
                        fld = "s.sName" if stype == "Sample Name" else "sub.SubmissionName"
                        clauses.append(f"pdb.RunID IN (SELECT DISTINCT pd.RunID FROM TRIMS.PrimaryDistillation pd JOIN Analysis a ON pd.AnalysisID=a.AnalysisID JOIN Sample s ON a.SampleID=s.SampleID AND a.Prefix=s.Prefix JOIN Submission sub ON s.SubmissionID=sub.SubmissionID WHERE {fld} LIKE :val)")
                        params["val"] = f"%{sval}%"
                except Exception as e:
                    show_message(self, "Search Error", str(e), QMessageBox.Warning)
                    return None, None
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    # ---------------- Load list ----------------
    def load_run_list(self):
        self.run_table_model.clear()
        where, params = self._build_filter_where_clause(is_list_filter=True)
        if where is None:
            return
        sql = f"""
        SELECT
            pdb.RunID, pdb.StartDate, pdb.EndDate, pdb.TechnicianID,
            {db_manager.sql_concat("e.LastName", "', '", "e.FirstMiddleName")} AS TechnicianName,
            pdb.EquipmentID, eq.Identifier, eq.EquipmentName, pdb.Remarks
        FROM TRIMS.PrimaryDistillationBatch pdb
        LEFT JOIN Employee e ON pdb.TechnicianID = e.EmployeeID
        LEFT JOIN Equipment eq ON pdb.EquipmentID = eq.EquipmentID
        {where}
        ORDER BY pdb.StartDate DESC, pdb.RunID DESC;
        """
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text(sql), params)
                headers = ["Status", "Run ID", "Start Date", "End Date", "Technician", "Equipment", "Remarks"]
                self.run_table_model.setHorizontalHeaderLabels(headers)
                if self.run_table_model.horizontalHeaderItem(0):
                    self.run_table_model.horizontalHeaderItem(0).setTextAlignment(Qt.AlignCenter)
                now = datetime.now().date()
                thresh = timedelta(days=self.DELAY_THRESHOLD_DAYS)
                for row in res:
                    start = row.StartDate.date() if row.StartDate else None
                    end = row.EndDate.date() if row.EndDate else None
                    if start and not end:
                        status = "Pending" if (now - start) > thresh else "Ongoing"
                    elif end:
                        status = "Complete"
                    else:
                        status = "New"
                    status_item = QStandardItem(status); status_item.setData(status, Qt.UserRole + 1)
                    run_id = QStandardItem(str(row.RunID)); run_id.setData(row.RunID, Qt.UserRole)
                    eq_name = f"{row.Identifier} - {row.EquipmentName}" if row.Identifier and row.EquipmentName else (str(row.EquipmentName) if row.EquipmentID else "")
                    self.run_table_model.appendRow([
                        status_item, run_id,
                        QStandardItem(str(start) if start else ""), QStandardItem(str(end) if end else ""),
                        QStandardItem(row.TechnicianName or str(row.TechnicianID)),
                        QStandardItem(eq_name), QStandardItem(row.Remarks or "")
                    ])
                h = self.run_table.horizontalHeader()
                for i in range(4): h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
                for i in range(4, 7): h.setSectionResizeMode(i, QHeaderView.Stretch)
                logging.info(f"Loaded {self.run_table_model.rowCount()} rows.")
        except Exception as e:
            logging.error(f"Load failed: {e}")
            show_message(self, "Error", str(e), QMessageBox.Warning)

    # ---------------- Reset filters ----------------
    def _on_search_type_changed(self, text: str):
        self.btnSearch.setText("Open Run" if text == "Distil Run" else "Search")
        if text != "Distil Run":
            self.load_run_list()

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Distil Run":
            val = self.txtSearchValue.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                run_id = int(val)
            except ValueError:
                show_message(self, "Invalid ID", "Distillation Run ID must be a number.", QMessageBox.Warning)
                return
            self.open_run_details(run_id)
        else:
            self.load_run_list()

    def reset_filters(self):
        for w in [self.chkShowOngoing, self.cmbSearchType, self.txtSearchValue]: #, self.txtDistBatchNo]:
            w.blockSignals(True)
        self.txtSearchValue.clear(); #self.txtDistBatchNo.clear()
        self.cmbSearchType.setCurrentText("Distil Run"); self.chkShowOngoing.setChecked(True)
        for w in [self.chkShowOngoing, self.cmbSearchType, self.txtSearchValue]: #, self.txtDistBatchNo]:
            w.blockSignals(False)
        self.load_run_list()

    # ---------------- Selection → populate txtDistBatchNo ----------------
    def on_selection_changed(self, selected, deselected):
        try:
            idxs = self.run_table.selectionModel().selectedRows(1)  # Run ID column
            if idxs:
                item = self.run_table_model.itemFromIndex(idxs[0])
                rid = item.data(Qt.UserRole)
                if rid is not None:
                    # self.txtDistBatchNo.setText(str(rid))
                    self.cmbSearchType.setCurrentText("Distil Run")
                    self.txtSearchValue.setText(str(rid))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    # ---------------- Open details (QDialog) ----------------
    def open_run_details(self, run_id, link_criteria=""):
        if not TrimsDistillationDetailsWindow:
            QMessageBox.warning(self, "Error", "Details module not loaded.")
            return
            
        try:
            dlg = TrimsDistillationDetailsWindow(
                run_id=run_id,
                link_criteria=link_criteria,
                parent=self,
                compact_ui=True
            )
            dlg.finished.connect(self._on_details_closed)
            dlg.show()
        except Exception as e:
            logging.error(f"Error opening details: {e}")
            show_message(self, "Error", f"Could not open batch details:\n{e}", QMessageBox.Critical)

    def _on_details_closed(self, _result):
        self.reset_filters()

    # ---------------- Helpers ----------------
    def get_selected_run_id(self) -> Optional[int]:
        idx = self.run_table.selectionModel().selectedRows(1)
        if not idx:
            show_message(self, "Info", "Select a batch first.", QMessageBox.Information)
            return None
        item = self.run_table_model.itemFromIndex(idx[0])
        return int(item.data(Qt.UserRole)) if item else None

    def on_table_double_clicked(self, index):
        if not index.isValid():
            return
        rid = self.run_table_model.item(index.row(), 1).data(Qt.UserRole)
        if rid:
            self.open_run_details(rid, f"RunID={rid}")

    # def load_single_batch_details(self):
    #     val = self.txtDistBatchNo.text().strip()
    #     if not val:
    #         return
    #     try:
    #         bid = int(val)
    #         with db_manager.get_connection() as conn:
    #             cnt = conn.execute(text("SELECT COUNT(RunID) FROM TRIMS.PrimaryDistillationBatch WHERE RunID = :bid"), {"bid": bid}).fetchone()[0]
    #             if cnt > 0:
    #                 self.open_run_details(bid, f"RunID={bid}")
    #             else:
    #                 show_message(self, "Not Found", f"Batch {bid} not found.", QMessageBox.Information)
    #     except Exception as e:
    #         show_message(self, "Error", str(e), QMessageBox.Warning)

    def create_new_run(self):
        if not TrimsDistillationCreateRunDialog:
            show_message(self, "Error", "Create Run module not found.", QMessageBox.Critical)
            return
            
        dlg = TrimsDistillationCreateRunDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            # Refresh list if run was created
            self.load_run_list()

    def delete_run(self):
        rid = self.get_selected_run_id()
        if not rid: return

        if QMessageBox.question(self, "Delete Run", 
                                f"Are you SURE you want to DELETE Distillation Run {rid}?\n"
                                "This will delete all result data and reset samples to 'Ready' status.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return

        try:
            with db_manager.get_connection() as conn:
                with conn.begin(): # Transaction
                    # 1. Fetch AnalysisIDs and Sample Info before deletion
                    sql_samps = text("""
                        SELECT pd.AnalysisID, a.SampleID, a.Prefix, pd.Repeat
                        FROM TRIMS.PrimaryDistillation pd
                        JOIN Analysis a ON pd.AnalysisID = a.AnalysisID
                        WHERE pd.RunID = :rid
                    """)
                    analyses_to_process = conn.execute(sql_samps, {"rid": rid}).fetchall()
                    
                    # 2. Delete Child Data
                    conn.execute(text("DELETE FROM TRIMS.PrimaryDistillationData WHERE ID IN (SELECT ID FROM TRIMS.PrimaryDistillation WHERE RunID=:rid)"), {"rid": rid})
                    conn.execute(text("DELETE FROM TRIMS.PrimaryDistillation WHERE RunID=:rid"), {"rid": rid})
                    
                    # 3. Handle Analysis Records
                    for row in analyses_to_process:
                        aid, sid, pfx, rpt = row
                        
                        try:
                            reps_int = int(rpt)
                        except:
                            reps_int = 1

                        if reps_int == 1: # Deleting the Analysis completely as it was created solely for this run
                            conn.execute(text("DELETE FROM Analysis WHERE AnalysisID=:aid"), {"aid": aid})
                            conn.execute(text("UPDATE Sample SET Status=222 WHERE SampleID=:sid AND Prefix=:pfx"), {"sid": sid, "pfx": pfx})
                            conn.execute(text("""
                                INSERT INTO public.sample_queue
                                    (sampleid, prefix, mediaid, workflowjobid, priorityid, queued_at)
                                SELECT a.sampleid, a.prefix, s.mediaid,
                                       (SELECT wj.workflowjobid FROM public.workflowjob wj
                                        WHERE wj.workflowid = a.workflowid AND wj.runsequence = 1
                                        LIMIT 1),
                                       sub.priorityid, now()
                                FROM public.analysis a
                                JOIN public.sample s ON s.sampleid = a.sampleid AND s.prefix = a.prefix
                                JOIN public.submission sub ON sub.submissionid = s.submissionid
                                WHERE a.analysisid = :aid
                                ON CONFLICT (sampleid, prefix, workflowjobid) DO NOTHING
                            """), {"aid": aid})
                    # 4. Delete Batch Header
                    conn.execute(text("DELETE FROM TRIMS.PrimaryDistillationBatch WHERE RunID=:rid"), {"rid": rid})
                    
            show_message(self, "Success", f"Run {rid} deleted.", QMessageBox.Information)
            self.load_run_list()
            
        except Exception as e:
            logging.error(f"Delete failed: {e}")
            show_message(self, "Delete Failed", f"Could not delete run:\n{e}", QMessageBox.Critical)