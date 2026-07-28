"""
TRIMS LSC Runs Management Window

Displays and manages LSC (Liquid Scintillation Counter) runs with filtering,
search, and integration with run creation and details dialogs.

Author: Improved by Claude
Date: 2026-02-11
"""
from __future__ import annotations

import sys
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any

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
from shared_utils import check_employee_privilege, get_current_user_id, normalize_login_name
from gui_utils import show_message
from help_browser import make_help_button

# --- INTEGRATION: Import Create/Details Dialogs ---
try:
    from trims_lsc_create_run_gui import TrimsLSCCreateRunDialog
except ImportError:
    TrimsLSCCreateRunDialog = None
    logging.warning("TrimsLSCCreateRunDialog not available")

try:
    from trims_lsc.gui import TrimsLSCDetailsWindow
except ImportError:
    try:
        from trims_lsc_details_gui import TrimsLSCDetailsWindow
    except ImportError:
        TrimsLSCDetailsWindow = None
        logging.warning("TrimsLSCDetailsWindow not available")

try:
    from trims_lsc.gui.main_dialog import TrimsLSCImportDialog
except ImportError:
    TrimsLSCImportDialog = None
    logging.warning("TrimsLSCImportDialog not available")


# =============================================================================
# Status Delegate - Visual Indicator for Run Status
# =============================================================================

class StatusDelegate(QStyledItemDelegate):
    """
    Custom delegate to paint colored status indicators.

    Status colors:
        - Ongoing:  Amber  (245, 158, 11)  — RunStatus < 7, started recently
        - Complete: Green  (16, 185, 129)  — RunStatus >= 7 (Analyzed / Evaluated)
        - Delayed:  Red    (239, 68, 68)   — RunStatus < 7, running > 10 days
        - New:      Grey   (156, 163, 175) — not yet started
    """
    
    def paint(self, painter: QPainter, option, index):
        """Paint colored circle based on run status"""
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        
        # Get status from UserRole
        status_data = index.data(Qt.UserRole + 1)
        
        # Determine color
        color_map = {
            "Ongoing":  QColor(245, 158, 11),   # Amber
            "Complete": QColor(16, 185, 129),   # Green
            "Delayed":  QColor(239, 68, 68),    # Red
            "New":      QColor(156, 163, 175),  # Grey
        }
        color = color_map.get(status_data, QColor(156, 163, 175))  # Grey default
        
        # Draw colored circle
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


# =============================================================================
# Main LSC Runs Window
# =============================================================================

class TrimsLSCRunsWindow(QWidget):
    """
    Main window for viewing and managing LSC runs.
    
    Features:
        - Filter by status (Ongoing/All runs)
        - Search by Run ID, Sample ID, Sample Name, or Project
        - Create new runs
        - Delete runs (admin only)
        - Open run details
        - Finalize analysis
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TRIMS :: LSC Runs")
        self.resize(1200, 700)
        
        # Privileges
        self.has_write_priv = False
        self.has_admin_priv = False
        
        # Main layout
        self.main_layout = QVBoxLayout(self)
        
        # UI Components
        self._init_top_bar()
        self.main_layout.addWidget(self._create_filter_group())
        self.main_layout.addSpacing(10)
        self._init_runs_table()
        
        # Check privileges and load data
        self._check_privileges()
        self._update_ui_state()
        self._initial_load()
    
    # -------------------------------------------------------------------------
    # UI Initialization
    # -------------------------------------------------------------------------
    
    def _init_top_bar(self):
        """Create top bar with title and close button"""
        top_bar = QHBoxLayout()

        title = QLabel("LSC Runs Management")
        title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2c3e50;")
        top_bar.addWidget(title)
        top_bar.addStretch()

        self.btnNew = QPushButton("Create New Run")
        self.btnNew.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
            QPushButton:pressed { background-color: #1e8449; }
            QPushButton:disabled { background-color: #a9dfbf; color: #7f8c8d; }
        """)
        self.btnNew.clicked.connect(self._create_new_run)
        top_bar.addWidget(self.btnNew)

        self.btnClose = QPushButton("Close")
        self.btnClose.clicked.connect(self._close_module)
        top_bar.addWidget(self.btnClose)

        top_bar.addWidget(make_help_button(self, "trims_lsc"))

        self.main_layout.addLayout(top_bar)
    
    def _init_runs_table(self):
        """Initialize the runs table with model and styling"""
        self.run_table = QTableView()
        self.model = QStandardItemModel()
        self.run_table.setModel(self.model)
        
        # Configure table
        self._apply_table_styling()
        self.run_table.setSelectionBehavior(QTableView.SelectRows)
        self.run_table.setSelectionMode(QTableView.SingleSelection)
        self.run_table.setEditTriggers(QTableView.NoEditTriggers)
        self.run_table.setSortingEnabled(True)
        
        # Status delegate for colored indicators
        self.status_delegate = StatusDelegate()
        self.run_table.setItemDelegateForColumn(0, self.status_delegate)
        
        # Connect signals
        self.run_table.clicked.connect(self._on_row_clicked)
        self.run_table.doubleClicked.connect(self._on_row_double_clicked)
        
        self.main_layout.addWidget(self.run_table, 1)
    
    def _apply_table_styling(self):
        """Apply consistent styling to the table"""
        self.run_table.setShowGrid(False)
        self.run_table.setAlternatingRowColors(True)
        self.run_table.verticalHeader().setVisible(False)
        
        self.run_table.setStyleSheet("""
            QTableView { 
                border: 1px solid #dcdcdc; 
                background-color: white; 
                gridline-color: none; 
            }
            QTableView::item { 
                padding: 6px 8px; 
                border-bottom: 1px solid #f0f0f0; 
                color: #333333; 
            }
            QTableView::item:selected { 
                background-color: #DDEEFF; 
                color: #000000; 
            }
            QHeaderView::section { 
                background-color: #f8f9fa; 
                color: #5f6368; 
                font-weight: bold; 
                padding: 6px; 
                border: none; 
                border-bottom: 2px solid #dcdcdc; 
            }
        """)
    
    def _create_filter_group(self) -> QGroupBox:
        """Create filter and action controls"""
        grp = QGroupBox("Filter / Search")
        layout = QVBoxLayout()

        # Row 1: Ongoing toggle
        row1 = QHBoxLayout()
        self.chkShowOngoing = QCheckBox("Show Ongoing Runs Only")
        self.chkShowOngoing.setChecked(True)
        self.chkShowOngoing.toggled.connect(self.refresh_runs)
        row1.addWidget(self.chkShowOngoing)
        row1.addStretch()
        layout.addLayout(row1)

        # Row 2: Search + selected-run actions (single row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Search By:"))

        self.cmbSearchType = QComboBox()
        self.cmbSearchType.addItems([
            "LSC Run", "Sample ID", "Sample Name", "Project Name"
        ])
        self.cmbSearchType.currentTextChanged.connect(self._on_search_type_changed)
        row2.addWidget(self.cmbSearchType)

        self.txtSearchVal = QLineEdit()
        self.txtSearchVal.setPlaceholderText("Enter ID or Name...")
        self.txtSearchVal.returnPressed.connect(self._on_search_or_open)
        row2.addWidget(self.txtSearchVal)

        self.btnSearch = QPushButton("Open Run")  # starts as "Open Run" (LSC Run is default)
        self.btnSearch.clicked.connect(self._on_search_or_open)
        row2.addWidget(self.btnSearch)

        self.btnReset = QPushButton("Reset")
        self.btnReset.clicked.connect(self._reset_filters)
        row2.addWidget(self.btnReset)
        
        row2.addSpacing(20)

        self.btnDelete = QPushButton("Delete")
        self.btnDelete.setStyleSheet("""
            QPushButton { background-color: #c0392b; color: white; font-weight: bold;
                          border: none; padding: 5px 14px; border-radius: 4px; }
            QPushButton:hover { background-color: #a93226; }
            QPushButton:pressed { background-color: #922b21; }
            QPushButton:disabled { background-color: #f1948a; color: #7f8c8d; }
        """)
        self.btnDelete.clicked.connect(self._delete_run)
        row2.addWidget(self.btnDelete)


        layout.addLayout(row2)

        grp.setLayout(layout)
        return grp
    
    # -------------------------------------------------------------------------
    # Privilege Management
    # -------------------------------------------------------------------------
    
    def _check_privileges(self):
        """Check user privileges for write/admin access"""
        try:
            user = get_current_user_id()
            user_normalized = normalize_login_name(user)
            
            self.has_write_priv = check_employee_privilege(
                user_normalized, "AccessCounting"
            )
            self.has_admin_priv = check_employee_privilege(
                user_normalized, "AdminTRIMS"
            )
            
            logging.info(
                f"User {user_normalized}: write={self.has_write_priv}, "
                f"admin={self.has_admin_priv}"
            )
            
        except Exception as e:
            logging.error(f"Failed to check privileges: {e}", exc_info=True)
            # Default to basic access
            self.has_write_priv = True
            self.has_admin_priv = False
    
    def _update_ui_state(self):
        """Update UI element states based on privileges"""
        self.btnNew.setEnabled(self.has_write_priv)
        self.btnDelete.setEnabled(self.has_admin_priv)
    
    # -------------------------------------------------------------------------
    # Data Loading
    # -------------------------------------------------------------------------
    
    def _initial_load(self):
        """Initial data load with error handling"""
        try:
            db_manager.get_engine()
            self.refresh_runs()
        except Exception as e:
            logging.critical(f"Database connection failed: {e}", exc_info=True)
            self.setEnabled(False)
            error_label = QLabel(f"Database Error: {e}")
            error_label.setStyleSheet("color: #c0392b; font-weight: bold;")
            self.main_layout.addWidget(error_label)
    
    def _on_search_type_changed(self, text: str):
        """Toggle button label: 'Open Run' when searching by run ID, 'Search' otherwise."""
        self.btnSearch.setText("Open Run" if text == "LSC Run" else "Search")

    def _on_search_or_open(self):
        """Open run details directly when searching by ID; otherwise filter the list."""
        if self.cmbSearchType.currentText() == "LSC Run":
            val = self.txtSearchVal.text().strip()
            if not val:
                self.refresh_runs()
                return
            try:
                run_id = int(val)
            except ValueError:
                show_message(self, "Invalid ID", "LSC Run ID must be a number.", QMessageBox.Warning)
                return
            # Try to get equipment_id from the current model if the run is already visible
            equipment_id = None
            for row in range(self.model.rowCount()):
                item = self.model.item(row, 1)
                if item and item.data(Qt.UserRole) == run_id:
                    eq_item = self.model.item(row, 5)
                    equipment_id = eq_item.data(Qt.UserRole) if eq_item else None
                    break
            self._open_run_details(run_id, equipment_id)
        else:
            self.refresh_runs()

    def refresh_runs(self):
        """
        Refresh the runs list from database.
        
        This is the main entry point for reloading data - called after
        filters change, dialogs close, or manual refresh.
        """
        self.model.clear()
        self.model.setHorizontalHeaderLabels([
            "Status", "Run ID", "Start Date", "End Date", 
            "Technician", "Equipment", "Remarks"
        ])
        
        sql, params = self._build_query()
        
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(sql), params).fetchall()
            
            # Populate table
            for row in rows:
                self._add_run_row(row)
            
            # Configure columns
            self._configure_column_widths()
            
            logging.info(f"Loaded {len(rows)} LSC runs")
            
        except Exception as e:
            logging.error(f"Failed to load LSC runs: {e}", exc_info=True)
            show_message(
                self, 
                "Error", 
                f"Failed to load runs:\n{str(e)}", 
                QMessageBox.Critical
            )
    
    def _add_run_row(self, row):
        """
        Add a single run to the table.
        
        Args:
            row: Database row with run data
        """
        run_id = row.RunID
        equipment_id = row.EquipmentID
        start = row.RunStartTime
        end = row.RunEndTime
        
        run_status = row.RunStatus
        # RunStatus >= 7 (Analyzed) = measurement complete regardless of RunEndTime
        if run_status is not None and run_status >= 7:
            computed_status = "Complete"
        elif start and not end:
            computed_status = "Delayed" if (datetime.now() - start) > timedelta(days=10) else "Ongoing"
        elif end:
            computed_status = "Complete"
        else:
            computed_status = "New"
        display_status = row.StatusDesc if row.StatusDesc else computed_status

        status_item = QStandardItem(display_status)
        status_item.setData(computed_status, Qt.UserRole + 1)
        
        id_item = QStandardItem(str(run_id))
        id_item.setData(run_id, Qt.UserRole)
        
        # Format dates with time
        start_str = start.strftime('%Y-%m-%d %H:%M') if start else ""
        end_str = end.strftime('%Y-%m-%d %H:%M') if end else ""
        
        start_item = QStandardItem(start_str)
        end_item = QStandardItem(end_str)
        
        # Technician
        tech_str = row.TechName if row.TechName else str(row.TechnicianID or "")
        tech_item = QStandardItem(tech_str)
        
        # Equipment - FIXED: Now stores equipment_id!
        eq_str = row.EquipmentName if row.EquipmentName else str(equipment_id or "")
        eq_item = QStandardItem(eq_str)
        eq_item.setData(equipment_id, Qt.UserRole)  # ⭐ Store EquipmentID
        
        # Remarks
        remarks_item = QStandardItem(row.Remarks or "")
        
        # Append row
        self.model.appendRow([
            status_item, id_item, start_item, end_item,
            tech_item, eq_item, remarks_item
        ])
    
    def _configure_column_widths(self):
        """Configure column resize modes"""
        header = self.run_table.horizontalHeader()
        
        # Auto-size most columns
        for col in range(6):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        
        # Remarks column stretches
        header.setSectionResizeMode(6, QHeaderView.Stretch)
    
    # -------------------------------------------------------------------------
    # Query Building
    # -------------------------------------------------------------------------
    
    def _build_query(self) -> Tuple[str, Dict[str, Any]]:
        """
        Build SQL query based on current filters.
        
        Returns:
            Tuple of (sql_string, params_dict)
        """
        dialect = getattr(db_manager, 'dialect', 'SQL_SERVER')
        
        # Build SELECT clause
        cols = [
            "r.RunID",
            "r.RunStatus",
            "r.RunStartTime",
            "r.RunEndTime",
            "r.TechnicianID",
            "r.EquipmentID",
            "r.Remarks",
            db_manager.sql_concat(
                "e.LastName", "', '", "e.FirstMiddleName"
            ) + " AS TechName",
            "eq.EquipmentName",
            "sl.Description AS StatusDesc"
        ]
        
        sql = f"""
            SELECT {', '.join(cols)}
            FROM TRIMS.LSCRun r
            LEFT JOIN Employee e ON r.TechnicianID = e.EmployeeID
            LEFT JOIN Equipment eq ON r.EquipmentID = eq.EquipmentID
            LEFT JOIN StatusLookup sl ON r.RunStatus = sl.Status
        """
        
        # Build WHERE clause
        wheres = []
        params = {}
        
        # Filter: Show ongoing only — active = not yet analyzed (RunStatus < 7)
        if self.chkShowOngoing.isChecked():
            wheres.append("(r.RunStatus IS NULL OR r.RunStatus < 7)")
        
        # Search filters
        search_val = self.txtSearchVal.text().strip()
        if search_val:
            search_type = self.cmbSearchType.currentText()
            search_where, search_params = self._build_search_filter(
                search_type, search_val
            )
            if search_where:
                wheres.append(search_where)
                params.update(search_params)
        
        # Combine WHERE clauses
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        
        # ORDER BY
        sql += " ORDER BY r.RunEndTime DESC, r.RunID DESC"
        
        return sql, params
    
    def _build_search_filter(
        self, search_type: str, search_val: str
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Build WHERE clause for search filter.
        
        Args:
            search_type: Type of search (LSC Run, Sample ID, etc.)
            search_val: Search value
        
        Returns:
            Tuple of (where_clause, params_dict)
        """
        params = {}
        
        if search_type == "LSC Run":
            if search_val.isdigit():
                return "r.RunID = :rid", {"rid": int(search_val)}
        
        elif search_type == "Sample ID":
            if search_val.isdigit():
                sub_sql = """
                    SELECT DISTINCT ll.RunID 
                    FROM TRIMS.LSCLoadList ll
                    JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                    WHERE a.SampleID = :sid
                """
                return f"r.RunID IN ({sub_sql})", {"sid": int(search_val)}
        
        elif search_type == "Sample Name":
            sub_sql = """
                SELECT DISTINCT ll.RunID 
                FROM TRIMS.LSCLoadList ll
                JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                JOIN Sample s ON a.SampleID = s.SampleID 
                    AND a.Prefix = s.Prefix
                WHERE s.sName LIKE :sname
            """
            return f"r.RunID IN ({sub_sql})", {"sname": f"%{search_val}%"}
        
        elif search_type == "Project Name":
            sub_sql = """
                SELECT DISTINCT ll.RunID 
                FROM TRIMS.LSCLoadList ll
                JOIN Analysis a ON ll.AnalysisID = a.AnalysisID
                JOIN Sample s ON a.SampleID = s.SampleID 
                    AND a.Prefix = s.Prefix
                JOIN Submission sub ON s.SubmissionID = sub.SubmissionID
                WHERE sub.SubmissionName LIKE :subname
            """
            return f"r.RunID IN ({sub_sql})", {"subname": f"%{search_val}%"}
        
        return "", {}
    
    def _sql_concat(self, *args, dialect: str = 'SQL_SERVER') -> str:
        """
        Build database-specific concatenation.
        
        Args:
            *args: String parts to concatenate
            dialect: Database dialect (SQL_SERVER or ACCESS)
        
        Returns:
            SQL concatenation expression
        """
        parts = []
        for arg in args:
            if isinstance(arg, str) and arg.startswith("'"):
                parts.append(arg)
            else:
                if dialect == "SQL_SERVER":
                    parts.append(f"CAST({arg} AS NVARCHAR(255))")
                else:
                    parts.append(str(arg))
        
        separator = " + " if dialect == "SQL_SERVER" else " || "
        return separator.join(parts)
    
    # -------------------------------------------------------------------------
    # User Actions
    # -------------------------------------------------------------------------
    
    def _on_row_clicked(self, index):
        """Populate search box with selected run ID"""
        if not index.isValid():
            return
        item = self.model.item(index.row(), 1)
        if item:
            run_id = item.data(Qt.UserRole)
            if run_id is not None:
                self.cmbSearchType.setCurrentText("LSC Run")
                self.txtSearchVal.setText(str(run_id))

    def _on_row_double_clicked(self, index):
        """Handle double-click on run row"""
        if not index.isValid():
            return
        
        # Get RunID from column 1
        id_item = self.model.item(index.row(), 1)
        if not id_item:
            logging.warning("No RunID item found")
            return
        
        run_id = id_item.data(Qt.UserRole)
        
        # Get EquipmentID from column 5
        eq_item = self.model.item(index.row(), 5)
        equipment_id = eq_item.data(Qt.UserRole) if eq_item else None
        
        # Open details
        self._open_run_details(run_id, equipment_id)
    
    def _open_run_details(self, run_id: int, equipment_id: Optional[int] = None):
        """
        Open the run details window.
        
        Args:
            run_id: LSC Run ID
            equipment_id: Optional equipment ID
        """
        if not TrimsLSCDetailsWindow:
            show_message(
                self,
                "Module Not Available",
                f"Run details module not loaded.\nRun ID: {run_id}",
                QMessageBox.Information
            )
            return
        
        try:
            # Create dialog
            dialog = TrimsLSCDetailsWindow(
                run_id=run_id,
                parent=self,
                equipment_id=equipment_id
            )
            
            # Refresh when dialog closes
            dialog.finished.connect(self._on_details_closed)
            
            # Show dialog
            dialog.show()
            
            logging.info(f"Opened details for run {run_id}")
            
        except Exception as e:
            logging.error(f"Failed to open run details: {e}", exc_info=True)
            show_message(
                self,
                "Error",
                f"Failed to open run details:\n{str(e)}",
                QMessageBox.Critical
            )
    
    def _on_details_closed(self, _result):
        self._reset_filters()
    
    def _create_new_run(self):
        """Create a new LSC run"""
        if not TrimsLSCCreateRunDialog:
            show_message(
                self,
                "Module Not Available",
                "Create Run module not loaded.",
                QMessageBox.Critical
            )
            return
        
        try:
            dialog = TrimsLSCCreateRunDialog(self)
            if dialog.exec_() == QDialog.Accepted:
                logging.info("New run created")
                self.refresh_runs()
        except Exception as e:
            logging.error(f"Failed to create new run: {e}", exc_info=True)
            show_message(
                self,
                "Error",
                f"Failed to create new run:\n{str(e)}",
                QMessageBox.Critical
            )
    
    def _delete_run(self):
        """Delete selected LSC run (admin only)"""
        # Get selected row
        rows = self.run_table.selectionModel().selectedRows()
        if not rows:
            show_message(
                self,
                "No Selection",
                "Please select a run to delete.",
                QMessageBox.Warning
            )
            return
        
        # Get run ID
        idx = rows[0]
        run_id = self.model.item(idx.row(), 1).data(Qt.UserRole)
        
        # Confirm deletion
        reply = QMessageBox.question(
            self,
            "Confirm Deletion",
            f"Are you sure you want to delete LSC Run {run_id}?\n\n"
            f"This action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Delete from database
        try:
            with db_manager.get_connection() as conn:
                # 1. Identify Analysis rows that were created ONLY for this run:
                #    they have no Electrolysis record (never enriched) and were
                #    set to Status=6 purely by the run-creation step.
                #    These are safe to purge once the load-list rows are gone.
                orphan_aids = [
                    row[0]
                    for row in conn.execute(text("""
                        SELECT ll.AnalysisID
                        FROM   TRIMS.LSCLoadList ll
                        JOIN   Analysis a ON a.AnalysisID = ll.AnalysisID
                        LEFT JOIN TRIMS.Electrolysis e ON e.AnalysisID = ll.AnalysisID
                        WHERE  ll.RunID = :rid
                          AND  e.AnalysisID IS NULL   -- never enriched
                          AND  a.Status    = 6        -- only touched by this run
                    """), {"rid": run_id}).fetchall()
                ]

                # 2. Delete child rows first (FK: LSCLoadList.RunID → LSCRun.RunID)
                conn.execute(
                    text("DELETE FROM TRIMS.LSCLoadList WHERE RunID = :rid"),
                    {"rid": run_id}
                )

                # 3. Delete orphan Analysis rows (now safe — no longer referenced)
                for aid in orphan_aids:
                    conn.execute(
                        text("DELETE FROM Analysis WHERE AnalysisID = :aid"),
                        {"aid": aid}
                    )

                # 4. Delete the run header
                conn.execute(
                    text("DELETE FROM TRIMS.LSCRun WHERE RunID = :rid"),
                    {"rid": run_id}
                )

                conn.commit()
            
            logging.info(f"Deleted run {run_id}")
            show_message(
                self,
                "Success",
                f"Run {run_id} deleted successfully.",
                QMessageBox.Information
            )
            self.refresh_runs()
            
        except Exception as e:
            logging.error(f"Failed to delete run {run_id}: {e}", exc_info=True)
            show_message(
                self,
                "Error",
                f"Failed to delete run:\n{str(e)}",
                QMessageBox.Critical
            )
    
    def _reset_filters(self):
        """Reset all filters to default"""
        self.chkShowOngoing.setChecked(True)
        self.txtSearchVal.clear()
        self.cmbSearchType.setCurrentIndex(0)
        self.refresh_runs()
        logging.info("Filters reset")
    
    def _close_module(self):
        """Close the window or quit application"""
        if isinstance(self.parent(), QWidget):
            self.close()
        else:
            QCoreApplication.instance().quit()


# =============================================================================
# Launcher Integration
# =============================================================================

def load_trims_lsc_runs():
    """
    Launcher integration helper.
    
    Returns:
        TrimsLSCRunsWindow instance
    """
    return TrimsLSCRunsWindow()


# =============================================================================
# Standalone Execution
# =============================================================================

if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    window = TrimsLSCRunsWindow()
    window.show()
    sys.exit(app.exec_())