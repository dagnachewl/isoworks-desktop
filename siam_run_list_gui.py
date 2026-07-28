"""
siam_run_list_gui.py — SIAM analysis run list panel for IsoWorks.
Provides SiamRunListWindow for listing, filtering, creating, and opening
SIAM analysis runs, with a colour-coded status delegate and privilege checks.
"""
import sys
import logging
import subprocess
import os

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QRadioButton, QComboBox,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QFileDialog, QHeaderView,
    QDialog, QStyledItemDelegate
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor, QPainter
from PyQt5.QtCore import Qt, QTimer, QRect, QSettings, QCoreApplication

# --- NEW: Import Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name
from gui_utils import EmbeddedSearchBox
from help_browser import make_help_button

# --- Lazy Imports for Child Windows ---
try:
    from siam_run_details_gui import SiamRunDetailsWindow
except ImportError:
    SiamRunDetailsWindow = None

try:
    from siam_create_run_gui import SiamCreateRunDialog
except ImportError:
    SiamCreateRunDialog = None

try:
    from validation_gui import QCReviewDialog
except ImportError:
    QCReviewDialog = None

class StatusDelegate(QStyledItemDelegate):
    """Custom delegate to draw a colored status circle for SIAM runs."""
    def paint(self, painter, option, index):
        if index.column() == 0: 
            status_data = index.data(Qt.UserRole + 1)
            if status_data == "Ongoing":
                color = QColor(255, 165, 0) # Orange
            elif status_data == "Complete":
                color = QColor(50, 200, 50)  # Green
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

class SiamRunListWindow(QWidget):
    """
    A QWidget for listing, filtering, and opening SIAM Runs.
    Refactored to use db_core (SQLAlchemy).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        
        self.settings = QSettings("YourLab", "IsotopeApp")
        
        self.details_window = None 
        self.main_layout = QVBoxLayout(self)
        self.main_layout.addLayout(self._create_action_buttons_group())
        
        self.main_layout.addSpacing(20)
        self.main_layout.addWidget(self._create_filter_group(), 0)
        self.main_layout.addSpacing(40)
        
        # --- Table Setup ---
        self.run_table = QTableView()
        self.run_table.setShowGrid(False)
        self._apply_table_styles()
        
        self.run_table.setAlternatingRowColors(True)
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True) 
        self.run_table.doubleClicked.connect(self.on_table_double_clicked)
        
        self.run_table.clicked.connect(self.on_table_clicked)
        
        self.run_table_model = QStandardItemModel()
        self.run_table.setModel(self.run_table_model)
        
        self.status_delegate = StatusDelegate()
        self.run_table.setItemDelegateForColumn(0, self.status_delegate)
        
        self.main_layout.addWidget(self.run_table, 1)

        # Privileges
        self.has_write_priv = False
        self.has_admin_priv = False
        self._check_privileges()
        self._update_ui_state()

        # --- Startup Check ---
        try:
            db_manager.get_engine()
            self.setEnabled(True)
            self.load_filter_combos()
            self.load_run_list()
        except Exception:
            self.setEnabled(False)
            lbl = QLabel("<h2>No Database Connection</h2><p>Please configure in Settings.</p>")
            lbl.setAlignment(Qt.AlignCenter)
            self.main_layout.addWidget(lbl)

    def _check_privileges(self):
        try:
            user_normalized = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user_normalized, "accesssiam")
            self.has_admin_priv = check_employee_privilege(user_normalized, "siamadmin")
            logging.info(f"User {user_normalized}: write={self.has_write_priv}, admin={self.has_admin_priv}")
        except Exception as e:
            logging.error(f"Failed to check privileges: {e}", exc_info=True)
            self.has_write_priv = False
            self.has_admin_priv = False

    def _update_ui_state(self):
        self.btnCreateRun.setEnabled(self.has_write_priv)
        self.btnFinalize.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    def refresh(self):
        """Standard interface for launcher."""
        self.load_run_list()

    def _apply_table_styles(self):
        self.run_table.setStyleSheet("""
            QTableView { border: none; background-color: white; gridline-color: none; }
            QTableView::item { padding: 6px 8px; border: none; background-color: white; color: #333333; text-align: left; }
            QTableView::item:alternate { background-color: #F3F7FA; }
            QTableView::item:selected { background-color: #DDEEFF; color: #000000; }
            QHeaderView::section { background-color: white; color: #7F8BB5; font-weight: bold; padding: 6px 8px; border: none; border-bottom: 2px solid #7F8BB5; text-align: left; }
            QHeaderView::section:hover { background-color: #DDEEFF; color: #000000; }
        """)

    def _create_filter_group(self):
        group = QGroupBox("Filter / Search SIAM Runs")
        main_layout = QVBoxLayout()
        
        filter_layout = QGridLayout()
        filter_layout.addWidget(QLabel("Job Name:"), 0, 0)
        self.cmbJobName = QComboBox(); filter_layout.addWidget(self.cmbJobName, 0, 1)
        filter_layout.addWidget(QLabel("Media:"), 0, 2)
        self.cmbMedia = QComboBox(); filter_layout.addWidget(self.cmbMedia, 0, 3)
        self.chkShowOngoing = QCheckBox("Show Ongoing Runs Only"); filter_layout.addWidget(self.chkShowOngoing, 0, 4)
        filter_layout.setColumnStretch(1, 1); filter_layout.setColumnStretch(3, 1); filter_layout.setColumnStretch(4, 2)
        main_layout.addLayout(filter_layout)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search By:"))
        self.cmbSearchType = QComboBox()
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        search_layout.addWidget(self.cmbSearchType)
        self.txtSearchValue = EmbeddedSearchBox(action_text="Open Run →")
        self.txtSearchValue.setPlaceholderText("Enter search term…")
        self.txtSearchValue.action_clicked.connect(self._on_search_or_open)
        self.txtSearchValue.clear_clicked.connect(self.reset_filters)
        search_layout.addWidget(self.txtSearchValue, 1)

        self.btnImportProcess = QPushButton("Import")
        self.btnImportProcess.setStyleSheet("""
            QPushButton { background-color: #1976d2; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #1565c0; }
            QPushButton:pressed { background-color: #0d47a1; }
            QPushButton:disabled { background-color: #90caf9; color: #7f8c8d; }
        """)
        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #a93226; }
            QPushButton:pressed { background-color: #922b21; }
            QPushButton:disabled { background-color: #f1948a; color: #7f8c8d; }
        """)
        search_layout.addStretch()
        for btn in [self.btnImportProcess, self.btnDelete]:
            search_layout.addWidget(btn)

        main_layout.addLayout(search_layout)
        group.setLayout(main_layout)

        self.btnImportProcess.clicked.connect(self.import_run_data)
        self.btnDelete.clicked.connect(self.delete_run)

        self.cmbJobName.currentIndexChanged.connect(self.load_run_list)
        self.cmbMedia.currentIndexChanged.connect(self.load_run_list)
        self.chkShowOngoing.toggled.connect(self.load_run_list)
        return group
        
    def _create_action_buttons_group(self):
        layout = QHBoxLayout()
        self.btnCreateRun = QPushButton("Create New Run")
        self.btnCreateRun.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:pressed { background-color: #1e8449; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnFinalize = QPushButton("QC / Validate")
        self.btnFinalize.setToolTip("Open QC acceptance review and validation sign-off for the selected run.")
        self.btnFinalize.setStyleSheet("""
            QPushButton { background-color: #e67e22; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #d35400; }
            QPushButton:pressed { background-color: #ba4a00; }
            QPushButton:disabled { background-color: #f0c080; color: #7f8c8d; }
        """)
        self.btnClose = QPushButton("Close")
        layout.addStretch()
        layout.addWidget(self.btnCreateRun)
        layout.addWidget(self.btnFinalize)
        layout.addWidget(self.btnClose)
        layout.addWidget(make_help_button(self, "siam_run_list"))

        self.btnCreateRun.clicked.connect(self.create_new_run)
        self.btnFinalize.clicked.connect(self.finalize_run)
        self.btnClose.clicked.connect(self._close_module)
        return layout

    # --- Helpers ---
    def _sql_concat(self, *args):
        dialect = db_manager.dialect
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    # --- Data Loading ---
    def load_filter_combos(self):
        logging.info("Loading filter combos...")
        self.cmbSearchType.addItems(["Run", "Sample ID", "Sample Name", "Project Name"])
        try:
            with db_manager.get_connection() as conn:
                # Jobs
                sql_job = "SELECT DISTINCT JobName FROM WorkflowJob ORDER BY JobName"
                self.cmbJobName.addItem("- Select Job -", None)
                for row in conn.execute(text(sql_job)): self.cmbJobName.addItem(row[0], row[0])
                
                # Media
                sql_media = f"""SELECT MediaID, {db_manager.sql_concat('Prefix', "': '", 'medianame')} FROM Media ORDER BY Prefix"""
                self.cmbMedia.addItem("- Select Media -", None)
                for row in conn.execute(text(sql_media)): self.cmbMedia.addItem(row[1], row[0])

            # Defaults
            idx = self.cmbJobName.findText("Stable_Isotopes")
            if idx > -1: self.cmbJobName.setCurrentIndex(idx)
            idx = self.cmbMedia.findData(1)
            if idx > -1: self.cmbMedia.setCurrentIndex(idx)
        except Exception as e:
            logging.error(f"Filter load failed: {e}")

    def _build_filter_where_clause(self):
        clauses = []; params = {}
        
        if self.chkShowOngoing.isChecked(): clauses.append("SIAM.SIAnalysisRun.RunEndTime IS NULL")
            
        stype = self.cmbSearchType.currentText()
        sval = self.txtSearchValue.text()
        
        if sval:
            try:
                if stype == "Run":
                    clauses.append("SIAM.SIAnalysisRun.SIAnalysisRunID = :run_id"); params["run_id"] = int(sval)
                elif stype == "Sample ID":
                    clauses.append("SIAM.SIAnalysisRun.SIAnalysisRunID IN (SELECT DISTINCT ll.SIAnalysisRunID FROM SIAM.SIAnalysisLoadList ll INNER JOIN Analysis a ON ll.AnalysisID = a.AnalysisID WHERE a.SampleID = :samp_id)")
                    params["samp_id"] = int(sval)
                elif stype == "Sample Name":
                    clauses.append("SIAM.SIAnalysisRun.SIAnalysisRunID IN (SELECT DISTINCT ll.SIAnalysisRunID FROM SIAM.SIAnalysisLoadList ll INNER JOIN Analysis a ON ll.AnalysisID = a.AnalysisID INNER JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix WHERE s.sName LIKE :sname)")
                    params["sname"] = f"%{sval}%"
                elif stype == "Project Name":
                    clauses.append("SIAM.SIAnalysisRun.SIAnalysisRunID IN (SELECT DISTINCT ll.SIAnalysisRunID FROM SIAM.SIAnalysisLoadList ll INNER JOIN Analysis a ON ll.AnalysisID = a.AnalysisID INNER JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix INNER JOIN Submission sub ON s.SubmissionID = sub.SubmissionID WHERE sub.SubmissionName LIKE :pname)")
                    params["pname"] = f"%{sval}%"
            except Exception as e:
                QMessageBox.warning(self, "Search Error", str(e)); return None, None

        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def load_run_list(self):
        logging.info("Loading run list...")
        job = self.cmbJobName.currentData(); media = self.cmbMedia.currentData()
        
        if not job or not media:
            self.run_table_model.clear()
            self.run_table_model.setHorizontalHeaderLabels(["Status", "Run ID", "Start Date", "End Date", "Repeat?", "Operator", "Equipment", "Workflow", "Remarks"])
            return

        # Repeat logic subquery (simplified for portability)
        repeat_case = "CASE WHEN r.SIAnalysisRunID IS NULL THEN '' WHEN SIAM.SIAnalysisRun.Remarks NOT LIKE '%Repeat%' THEN '*' ELSE '' END AS Repeat"
        
        sql = f"""
            SELECT 
                SIAM.SIAnalysisRun.SIAnalysisRunID, Equipment.EquipmentName, t1.WorkflowName,
                SIAM.SIAnalysisRun.RunStartTime, SIAM.SIAnalysisRun.RunEndTime, SIAM.SIAnalysisRun.Remarks,
                {db_manager.sql_concat("Employee.LastName", "', '", "Employee.FirstMiddleName")} AS Operator,
                {repeat_case}
            FROM SIAM.SIAnalysisRun 
            INNER JOIN (SELECT DISTINCT Workflow.WorkflowID, Workflow.WorkflowName FROM Workflow INNER JOIN WorkflowJob ON Workflow.WorkflowID = WorkflowJob.WorkflowID WHERE WorkflowJob.JobName = :job AND Workflow.MediaID = :media) AS t1 ON SIAM.SIAnalysisRun.WorkflowID = t1.WorkflowID
            INNER JOIN Equipment ON Equipment.EquipmentID = SIAM.SIAnalysisRun.EquipmentID
            INNER JOIN Employee ON Employee.EmployeeID = SIAM.SIAnalysisRun.TechnicianID
            LEFT JOIN (SELECT DISTINCT ll.SIAnalysisRunID FROM SIAM.SIAnalysisLoadList ll JOIN Analysis a ON ll.AnalysisID=a.AnalysisID WHERE (SELECT COUNT(*) FROM SIAM.SIAnalysisLoadList WHERE AnalysisID=a.AnalysisID) < a.Repeats) AS r ON SIAM.SIAnalysisRun.SIAnalysisRunID = r.SIAnalysisRunID
        """
        
        base_params = {"job": job, "media": media}
        where, extra_params = self._build_filter_where_clause()
        if where is None: return
        
        full_params = {**base_params, **extra_params}
        order = " ORDER BY SIAM.SIAnalysisRun.SIAnalysisRunID DESC"
        
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text(sql + where + order), full_params)
                self.run_table_model.clear()
                self.run_table_model.setHorizontalHeaderLabels(["Status", "Run ID", "Start Date", "End Date", "Repeat?", "Operator", "Equipment", "Workflow", "Remarks"])
                
                for row in res:
                    status = "Complete" if row[4] else "Ongoing"
                    status_item = QStandardItem(status); status_item.setData(status, Qt.UserRole + 1)
                    run_item = QStandardItem(str(row[0])); run_item.setData(row[0], Qt.UserRole)
                    rep_item = QStandardItem(row[7] or ""); rep_item.setTextAlignment(Qt.AlignCenter)
                    
                    self.run_table_model.appendRow([
                        status_item, run_item,
                        QStandardItem(str(row[3].date()) if row[3] else ""),
                        QStandardItem(str(row[4].date()) if row[4] else ""),
                        rep_item, QStandardItem(row[6] or ""), QStandardItem(row[1] or ""),
                        QStandardItem(str(row[2])), QStandardItem(row[5] or "")
                    ])
                    
            h = self.run_table.horizontalHeader()
            for i in range(5): h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            for i in range(5, 9): h.setSectionResizeMode(i, QHeaderView.Stretch)
            
        except Exception as e:
            logging.error(f"Run load failed: {e}")

    def _on_search_type_changed(self, text: str):
        self.txtSearchValue.set_action_text("Open Run →" if text == "Run" else "Search →")

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Run":
            val = self.txtSearchValue.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                run_id = int(val)
            except ValueError:
                QMessageBox.warning(self, "Invalid ID", "SIAM Run ID must be a number.")
                return
            self.open_run_details(run_id)
        else:
            self.load_run_list()

    def reset_filters(self):
        self.txtSearchValue.clear(); self.cmbSearchType.setCurrentIndex(0)
        self.chkShowOngoing.setChecked(False); self.load_filter_combos(); self.load_run_list()

    def get_selected_run_id(self):
        idx = self.run_table.selectionModel().currentIndex()
        if not idx.isValid():
            QMessageBox.warning(self, "No Selection", "Please select a run first.")
            return None
        item = self.run_table_model.item(idx.row(), 1)
        return item.data(Qt.UserRole) if item else None

    def on_table_double_clicked(self, index):
        rid = self.get_selected_run_id()
        if rid: self.open_run_details(rid)
    
    def on_table_clicked(self, index):
        """Populate search box with selected run ID"""
        if not index.isValid():
            return
        item = self.run_table_model.item(index.row(), 1)
        if item:
            rid = item.data(Qt.UserRole)
            if rid is not None:
                self.cmbSearchType.setCurrentText("Run")
                self.txtSearchValue.setText(str(rid))
        
    def create_new_run(self):
        if not SiamCreateRunDialog: return
        d = SiamCreateRunDialog(self)
        if d.exec_() == QDialog.Accepted: self.load_run_list()

    def import_run_data(self):
        rid = self.get_selected_run_id()
        if not rid: return

        try:
            python_exe = sys.executable
            # Pass dialect and connection string so subprocess can init db_manager
            dialect = db_manager.dialect
            dsn = db_manager._path_or_dsn or self.settings.value("lims/dsn_path", type=str) or ""
            subprocess.Popen([python_exe, "-m", "siam_processor.views.main_window", str(rid), dsn, dialect])
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def open_run_details(self, run_id):
        if not SiamRunDetailsWindow: return
        if self.details_window:
            try: self.details_window.close()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        dlg = SiamRunDetailsWindow(run_id=int(run_id), parent=self)
        self.details_window = dlg
        dlg.finished.connect(self._on_details_closed)
        dlg.show()

    def _on_details_closed(self, _result):
        self.reset_filters()

    def view_edit_run(self):
        rid = self.get_selected_run_id()
        if rid:
            self.open_run_details(rid)

    def delete_run(self):
        rid = self.get_selected_run_id()
        if not rid: return
        if QMessageBox.question(self, "Delete", f"Delete Run {rid}?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                # Transaction order
                conn.execute(text("DELETE FROM SIAM.SIAnalysisResult WHERE SIAnalysisID IN (SELECT SIAnalysisID FROM SIAM.SIAnalysisLoadList WHERE SIAnalysisRunID = :rid)"), {"rid": rid})
                conn.execute(text("DELETE FROM SIAM.SIAnalysisLoadList WHERE SIAnalysisRunID = :rid"), {"rid": rid})
                conn.execute(text("DELETE FROM SIAM.SIAnalysisRun WHERE SIAnalysisRunID = :rid"), {"rid": rid})
                conn.commit()
            self.load_run_list()
        except Exception as e: logging.error(f"Delete failed: {e}"); QMessageBox.critical(self, "Error", str(e))

    def finalize_run(self):
        rid = self.get_selected_run_id()
        if not rid:
            return
        if not QCReviewDialog:
            QMessageBox.warning(self, "Not Available", "QCReviewDialog could not be imported.")
            return
        dlg = QCReviewDialog(module="SIAM", run_id=int(rid), parent=self)
        dlg.exec_()
        self.load_run_list()

    def _close_module(self):
        """Close the window or quit application"""
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()
            
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    # Test: db_manager.initialize(...)
    win = SiamRunListWindow()
    win.show()
    sys.exit(app.exec_())