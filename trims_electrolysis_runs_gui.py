"""
trims_electrolysis_runs_gui.py — TRIMS electrolysis run list panel for IsoWorks.
Provides a filterable list of electrolysis runs with a colour-coded status delegate,
linking to TrimsElectrolysisDetailsWindow and the run creation dialog.
"""
import sys
import logging
from typing import Optional
from datetime import datetime, timedelta

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel,
    QMessageBox, QHeaderView, QStyledItemDelegate, QDialog
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QBrush, QColor, QPainter
from PyQt5.QtCore import Qt, QRect, QCoreApplication

from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name
from help_browser import make_help_button

# --- INTEGRATION: Import Create Dialog ---
try:
    from trims_electrolysis_create_run_gui import TrimsElectrolysisCreateRunDialog
except ImportError:
    TrimsElectrolysisCreateRunDialog = None

# Placeholder for Details (Future Implementation)
from trims_electrolysis_details_gui import TrimsElectrolysisDetailsWindow 

# ---------------------------------------------------------------------
# Status Delegate
# ---------------------------------------------------------------------
class StatusDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() == 0:
            status_data = index.data(Qt.UserRole + 1)
            color = QColor(200, 200, 200) 
            if status_data == "Ongoing":
                color = QColor(255, 165, 0) 
            elif status_data == "Complete":
                color = QColor(50, 200, 50) 
            elif status_data == "Pending":
                color = QColor(255, 0, 0)   
            
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
# Main Electrolysis Runs Window
# ---------------------------------------------------------------------
class TrimsElectrolysisRunsWindow(QWidget):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_layout = QVBoxLayout(self)

        # Top bar: Create New Run + Close
        top_bar = QHBoxLayout()
        top_bar.addStretch()
        self.btnNew = QPushButton("Create New Run")
        self.btnNew.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:pressed { background-color: #1e8449; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnNew.clicked.connect(self.create_new_run)
        top_bar.addWidget(self.btnNew)
        self.btnClose = QPushButton("Close Module")
        self.btnClose.clicked.connect(self.close_module)
        top_bar.addWidget(self.btnClose)
        top_bar.addWidget(make_help_button(self, "trims_enrichment"))
        self.main_layout.addLayout(top_bar)
        self.main_layout.addSpacing(10)

        # Filter Group
        self.main_layout.addWidget(self._create_filter_group())
        self.main_layout.addSpacing(20)

        # Runs Table
        self.run_table = QTableView()
        self._apply_table_styling()
        self.status_delegate = StatusDelegate()
        self.run_table.setItemDelegateForColumn(0, self.status_delegate)
        
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True)
        
        self.model = QStandardItemModel()
        self.run_table.setModel(self.model)
        self.run_table.clicked.connect(self.on_table_clicked)
        self.run_table.doubleClicked.connect(self.on_table_double_clicked)
        
        self.main_layout.addWidget(self.run_table, 1)

        # Privileges
        self.has_write_priv = False
        self.has_admin_priv = False
        self._check_privileges()
        self._update_ui_state()

        # DB Load
        try:
            db_manager.get_engine()
            self.load_run_list()
        except Exception as e:
            self.setEnabled(False)
            self.main_layout.addWidget(QLabel(f"DB Error: {e}"))

    def _check_privileges(self):
        try:
            user_normalized = normalize_login_name(get_current_user_id())
            self.has_write_priv = check_employee_privilege(user_normalized, "accessenrichment")
            self.has_admin_priv = check_employee_privilege(user_normalized, "AdminTRIMS")
            logging.info(f"User {user_normalized}: write={self.has_write_priv}, admin={self.has_admin_priv}")
        except Exception as e:
            logging.error(f"Failed to check privileges: {e}", exc_info=True)
            self.has_write_priv = False
            self.has_admin_priv = False

    def _update_ui_state(self):
        self.btnNew.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)

    def close_module(self):
        if isinstance(self.parent(), QWidget): self.close()
        else: QCoreApplication.instance().quit()

    def _apply_table_styling(self):
        self.run_table.setShowGrid(False)
        # Copied exactly from trims_distillation_runs_gui
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

    def _create_filter_group(self):
        grp = QGroupBox("Filter / Search Electrolysis Runs")
        layout = QVBoxLayout()
        
        # Row 1: Ongoing Checkbox
        r1 = QHBoxLayout()
        self.chkShowOngoing = QCheckBox("Show Ongoing Runs Only")
        self.chkShowOngoing.setChecked(True) 
        self.chkShowOngoing.toggled.connect(self.load_run_list)
        r1.addWidget(self.chkShowOngoing)
        r1.addStretch()
        layout.addLayout(r1)
        
        # Row 2: Search & Actions
        r2 = QHBoxLayout()
        
        r2.addWidget(QLabel("Search By:"))
        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems(["Elys Run", "Sample ID", "Sample Name", "Project Name"])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        r2.addWidget(self.cmbSearchType)

        self.txtSearchVal = QLineEdit()
        self.txtSearchVal.setPlaceholderText("Enter search term...")
        self.txtSearchVal.returnPressed.connect(self._on_search_or_open)
        r2.addWidget(self.txtSearchVal)

        self.btnSearch = QPushButton("Open Run")  # starts as "Open Run" (Elys Run is default)
        self.btnSearch.clicked.connect(self._on_search_or_open)
        r2.addWidget(self.btnSearch)

        self.btnReset = QPushButton("Reset")
        self.btnReset.clicked.connect(self.reset_filters)
        r2.addWidget(self.btnReset)
        
        r2.addSpacing(20)

        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #a93226; }
            QPushButton:pressed { background-color: #922b21; }
            QPushButton:disabled { background-color: #f1948a; color: #7f8c8d; }
        """)
        self.btnDelete.clicked.connect(self.delete_run)
        r2.addWidget(self.btnDelete)
        
        layout.addLayout(r2)
        grp.setLayout(layout)
        return grp

    def _on_search_type_changed(self, text: str):
        self.btnSearch.setText("Open Run" if text == "Elys Run" else "Search")

    def _on_search_or_open(self):
        if self.cmbSearchType.currentText() == "Elys Run":
            val = self.txtSearchVal.text().strip()
            if not val:
                self.load_run_list()
                return
            try:
                run_id = int(val)
            except ValueError:
                show_message(self, "Invalid ID", "Electrolysis Run ID must be a number.", QMessageBox.Warning)
                return
            self.open_run_details(run_id)
        else:
            self.load_run_list()

    def reset_filters(self):
        self.chkShowOngoing.setChecked(True)
        self.txtSearchVal.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.load_run_list()

    def _build_query(self):
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        
        def sql_concat(*args):
            parts = []
            for arg in args:
                if isinstance(arg, str) and (arg.startswith("'")): parts.append(arg)
                else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else f"CAST({arg} AS TEXT)")
            sep = " + " if dialect == "SQL_SERVER" else " || "
            return sep.join(parts)

        cols = [
            "r.RunID", "r.RunStartTime", "r.RunEndTime", "r.TechnicianID", "r.ElysSystemID", "r.Remarks",
            f"""{sql_concat('e.LastName', "', '", 'e.FirstMiddleName')} AS TechName""",
            "eq.EquipmentName"
        ]
        
        sql = f"""
            SELECT {', '.join(cols)}
            FROM TRIMS.ElectrolysisRun r
            LEFT JOIN Employee e ON r.TechnicianID = e.EmployeeID
            LEFT JOIN Equipment eq ON r.ElysSystemID = eq.EquipmentID
        """
        
        wheres = []
        params = {}
        
        if self.chkShowOngoing.isChecked():
            wheres.append("r.RunEndTime IS NULL")
            
        s_val = self.txtSearchVal.text().strip()
        if s_val:
            s_type = self.cmbSearchType.currentText()
            if s_type == "Elys Run":
                if s_val.isdigit():
                    wheres.append("r.RunID = :rid")
                    params["rid"] = int(s_val)
            
            elif s_type == "Sample ID":
                sub_sql = """
                    SELECT DISTINCT el.RunID FROM TRIMS.Electrolysis el 
                    JOIN Analysis a ON el.AnalysisID = a.AnalysisID 
                    WHERE a.SampleID = :sid
                """
                if s_val.isdigit():
                    wheres.append(f"r.RunID IN ({sub_sql})")
                    params["sid"] = int(s_val)
            
            elif s_type == "Sample Name":
                sub_sql = """
                    SELECT DISTINCT el.RunID FROM TRIMS.Electrolysis el 
                    JOIN Analysis a ON el.AnalysisID = a.AnalysisID 
                    JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                    WHERE s.sName LIKE :sname
                """
                wheres.append(f"r.RunID IN ({sub_sql})")
                params["sname"] = f"%{s_val}%"
                
            elif s_type == "Project Name":
                sub_sql = """
                    SELECT DISTINCT el.RunID FROM TRIMS.Electrolysis el 
                    JOIN Analysis a ON el.AnalysisID = a.AnalysisID 
                    JOIN Sample s ON a.SampleID = s.SampleID AND a.Prefix = s.Prefix
                    JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
                    WHERE sub.SubmissionName LIKE :subname
                """
                wheres.append(f"r.RunID IN ({sub_sql})")
                params["subname"] = f"%{s_val}%"

        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
            
        sql += " ORDER BY r.RunStartTime DESC, r.RunID DESC"
        
        return sql, params

    def load_run_list(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(["Status", "Run ID", "Start Date", "End Date", "Technician", "System", "Remarks"])
        
        sql, params = self._build_query()
        
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
                
            now = datetime.now()
            thresh = timedelta(days=10)
            for r in rows:
                run_id = r.RunID
                start = r.RunStartTime
                end = r.RunEndTime
                
                if start and not end:
                    status = "Pending" if (now - start) > thresh else "Ongoing"
                elif end:
                    status = "Complete"
                else:
                    status = "New"
                
                st_item = QStandardItem(status)
                st_item.setData(status, Qt.UserRole + 1)
                
                id_item = QStandardItem(str(run_id))
                id_item.setData(run_id, Qt.UserRole)
                
                tech_str = r.TechName or str(r.TechnicianID or "")
                eq_str = r.EquipmentName or str(r.ElysSystemID or "")
                
                start_str = start.strftime('%Y-%m-%d') if start else ""
                end_str = end.strftime('%Y-%m-%d') if end else ""
                
                self.model.appendRow([
                    st_item, id_item,
                    QStandardItem(start_str), QStandardItem(end_str),
                    QStandardItem(tech_str), QStandardItem(eq_str),
                    QStandardItem(r.Remarks or "")
                ])
            
            h = self.run_table.horizontalHeader()
            for i in range(6): h.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            h.setSectionResizeMode(6, QHeaderView.Stretch)
            
        except Exception as e:
            logging.error(f"Load run list failed: {e}")
            show_message(self, "Error", f"Failed to load runs: {e}", QMessageBox.Warning)

    def on_table_clicked(self, index):
        """Populate search box with selected run ID"""
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item:
            run_id = item.data(Qt.UserRole)
            if run_id is not None:
                self.cmbSearchType.setCurrentText("Elys Run")
                self.txtSearchVal.setText(str(run_id))

    def on_table_double_clicked(self, index):
        if not index.isValid(): return
        item = self.model.item(index.row(), 1)
        run_id = item.data(Qt.UserRole)
        self.open_run_details(run_id)

    def open_run_details(self, run_id):
        # Placeholder - to be linked when Details module is ready
        dlg = TrimsElectrolysisDetailsWindow(run_id, parent=self)
        dlg.finished.connect(self._on_details_closed)
        dlg.show()

    def _on_details_closed(self, _result):
        self.reset_filters()

    def create_new_run(self):
        # --- INTEGRATION POINT ---
        if not TrimsElectrolysisCreateRunDialog:
            show_message(self, "Error", "Create Run module not loaded.", QMessageBox.Critical)
            return
            
        dlg = TrimsElectrolysisCreateRunDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self.load_run_list()

    def delete_run(self):
        rows = self.run_table.selectionModel().selectedRows()
        if not rows:
            show_message(self, "No Selection", "Please select a run to delete.", QMessageBox.Warning)
            return
        
        idx = rows[0]
        run_id = self.model.item(idx.row(), 1).data(Qt.UserRole)
        
        if QMessageBox.question(self, "Delete", f"Are you sure you want to delete Run {run_id}?", 
                                QMessageBox.Yes|QMessageBox.No) == QMessageBox.Yes:
            # Placeholder logic - needs full cascade delete similar to distillation
            show_message(self, "Info", f"Delete Run {run_id} (Logic Pending)", QMessageBox.Information)

# Launcher integration helper
def load_trims_electrolysis_runs():
    return TrimsElectrolysisRunsWindow()

if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = TrimsElectrolysisRunsWindow()
    w.show()
    sys.exit(app.exec_())