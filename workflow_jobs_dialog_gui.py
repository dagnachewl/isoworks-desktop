"""
workflow_jobs_dialog_gui.py — Workflow jobs list dialog for IsoWorks.
Provides WorkflowJobsDialog (QDialog) for managing the ordered list of
procedure jobs (steps) within a workflow, with add, edit, and remove actions.
"""
from __future__ import annotations
import sys
import logging
from datetime import datetime
import getpass

from PyQt5.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QDialogButtonBox, QTableView, QHeaderView,
    QPushButton, QAbstractItemView, QToolButton, QMessageBox
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt

# --- Shared ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import show_message

# Import the editor dialog
try:
    from workflow_job_editor_dialog_gui import WorkflowJobEditorDialog
except ImportError:
    WorkflowJobEditorDialog = None
    logging.error("workflow_job_editor_dialog_gui.py not found. Editing jobs will not work.")

class WorkflowJobsDialog(QDialog):
    """
    Manages the list of jobs (Procedures) in a workflow.
    Refactored for db_manager/SQLAlchemy.
    """
    def __init__(self, workflow_id, parent=None):
        super().__init__(parent)
        self.workflow_id = workflow_id
        
        self.setWindowTitle(f"Edit Workflow Jobs (Workflow: {self.workflow_id})")
        self.setModal(True)
        self.setMinimumSize(700, 500)
        
        layout = QVBoxLayout(self)
        
        # --- Toolbar ---
        toolbar_layout = QHBoxLayout()
        self.btnAdd = QPushButton("Add Job")
        self.btnEdit = QPushButton("Edit Job")
        self.btnDelete = QPushButton("Delete Job")
        toolbar_layout.addWidget(self.btnAdd)
        toolbar_layout.addWidget(self.btnEdit)
        toolbar_layout.addWidget(self.btnDelete)
        toolbar_layout.addStretch()
        
        self.btnMoveUp = QToolButton()
        self.btnMoveUp.setText("▲")
        self.btnMoveUp.setToolTip("Move sequence up")
        
        self.btnMoveDown = QToolButton()
        self.btnMoveDown.setText("▼")
        self.btnMoveDown.setToolTip("Move sequence down")
        
        toolbar_layout.addWidget(self.btnMoveUp)
        toolbar_layout.addWidget(self.btnMoveDown)
        layout.addLayout(toolbar_layout)
        
        # --- Table View ---
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_model = QStandardItemModel()
        self.table_view.setModel(self.table_model)
        layout.addWidget(self.table_view, 1)
        
        # --- Close Button ---
        self.buttonBox = QDialogButtonBox(QDialogButtonBox.Close)
        layout.addWidget(self.buttonBox)
        
        # --- Connect Signals ---
        self.buttonBox.rejected.connect(self.reject)
        self.btnAdd.clicked.connect(self.on_add)
        self.btnEdit.clicked.connect(self.on_edit)
        self.table_view.doubleClicked.connect(self.on_edit)
        self.btnDelete.clicked.connect(self.on_delete)
        self.btnMoveUp.clicked.connect(self.on_move_up)
        self.btnMoveDown.clicked.connect(self.on_move_down)
        
        self.load_jobs_table()

    def get_selected_job_id(self):
        """Helper to get the WorkflowJobID from the selected table row."""
        selected_indexes = self.table_view.selectionModel().selectedRows()
        if not selected_indexes:
            return None
        
        # Get the item from the first column (Seq) where we stored the ID
        item = self.table_model.itemFromIndex(selected_indexes[0])
        return int(item.data(Qt.UserRole)) # Get ID from data

    def load_jobs_table(self):
        """Loads the jobs for the current workflow into the table."""
        self.table_model.clear()
        self.table_model.setHorizontalHeaderLabels(
            ["Seq", "Job Name", "Procedure", "Staff", "Pre-Req", "Obsolete"]
        )

        try:
            with db_manager.get_connection() as conn:
                sql = """
                    SELECT wj.WorkflowJobID, wj.RunSequence, wj.JobName,
                           ap.ProcedureName,
                           wj.IsPreRequisite, wj.IsObsolete,
                           e.LastName, e.FirstMiddleName
                    FROM WorkflowJob wj
                    LEFT JOIN AnalysisProcedure ap ON ap.ProcedureID = wj.ProcedureID
                    LEFT JOIN Employee e ON e.EmployeeID = wj.EmployeeID
                    WHERE wj.WorkflowID = :wid
                    ORDER BY wj.RunSequence
                """
                rows = conn.execute(text(sql), {"wid": self.workflow_id}).fetchall()

                for row in rows:
                    item_seq = QStandardItem(str(row.RunSequence))
                    item_seq.setData(row.WorkflowJobID, Qt.UserRole)

                    staff = ""
                    if row.LastName:
                        staff = f"{row.LastName}, {row.FirstMiddleName or ''}".strip(", ")

                    self.table_model.appendRow([
                        item_seq,
                        QStandardItem(row.JobName or ""),
                        QStandardItem(row.ProcedureName or ""),
                        QStandardItem(staff),
                        QStandardItem("Yes" if row.IsPreRequisite else ""),
                        QStandardItem("Yes" if row.IsObsolete else ""),
                    ])

            self.table_view.resizeColumnsToContents()
            self.table_view.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        except Exception as e:
            logging.error("Failed to load workflow jobs: %s", e)
            
    def on_add(self):
        """Opens the editor dialog for a new job."""
        if WorkflowJobEditorDialog is None:
            show_message(self, "Error", "Job editor module is missing.", QMessageBox.Critical)
            return
            
        dialog = WorkflowJobEditorDialog(self.workflow_id, parent=self)
        if dialog.exec_() == QDialog.Accepted:
            self.load_jobs_table() # Refresh
            
    def on_edit(self):
        """Opens the editor dialog for the selected job."""
        job_id = self.get_selected_job_id()
        if job_id is None:
            show_message(self, "Info", "Please select a job to edit.", QMessageBox.Information)
            return
            
        if WorkflowJobEditorDialog is None:
            show_message(self, "Error", "Job editor module is missing.", QMessageBox.Critical)
            return
            
        dialog = WorkflowJobEditorDialog(self.workflow_id, job_id, parent=self)
        if dialog.exec_() == QDialog.Accepted:
            self.load_jobs_table() # Refresh
            
    def on_delete(self):
        """Deletes the selected job."""
        job_id = self.get_selected_job_id()
        if job_id is None:
            show_message(self, "Info", "Please select a job to delete.", QMessageBox.Information)
            return
            
        reply = QMessageBox.question(self, "Confirm Delete",
            "Are you sure you want to permanently delete this job?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            
        if reply == QMessageBox.No:
            return
        
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM WorkflowJob WHERE WorkflowJobID = :jid"), {"jid": job_id})
                conn.commit()
            self.load_jobs_table()
        except Exception as e:
            logging.error(f"Delete failed: {e}")
            show_message(self, "Error", f"Failed to delete job: {e}", QMessageBox.Critical)

    def on_move_up(self):
        """Moves the selected job up in the sequence."""
        self.swap_rows(-1)
        
    def on_move_down(self):
        """Moves the selected job down in the sequence."""
        self.swap_rows(1)
        
    def swap_rows(self, direction: int):
        """Swaps the selected row's RunSequence with its neighbor."""
        selected_indexes = self.table_view.selectionModel().selectedRows()
        if not selected_indexes:
            show_message(self, "Info", "Please select a job to move.", QMessageBox.Information)
            return
        
        current_row_index = selected_indexes[0].row()
        neighbor_row_index = current_row_index + direction
        
        if not (0 <= neighbor_row_index < self.table_model.rowCount()):
            return # At the top or bottom, can't move
            
        # Get items for both rows
        item_current = self.table_model.item(current_row_index, 0)
        item_neighbor = self.table_model.item(neighbor_row_index, 0)
        
        id_current = item_current.data(Qt.UserRole)
        seq_current = int(item_current.text())
        
        id_neighbor = item_neighbor.data(Qt.UserRole)
        seq_neighbor = int(item_neighbor.text())

        try:
            with db_manager.get_connection() as conn:
                # Swap logic in transaction
                conn.execute(text("UPDATE WorkflowJob SET RunSequence = :seq WHERE WorkflowJobID = :id"), {"seq": seq_neighbor, "id": id_current})
                conn.execute(text("UPDATE WorkflowJob SET RunSequence = :seq WHERE WorkflowJobID = :id"), {"seq": seq_current, "id": id_neighbor})
                conn.commit()
                
            self.load_jobs_table()
            
            # Re-select the item that was just moved
            new_index = self.table_model.index(neighbor_row_index, 0)
            self.table_view.setCurrentIndex(new_index)
            
        except Exception as e:
            logging.error(f"Move failed: {e}")
            show_message(self, "Error", f"Failed to move job: {e}", QMessageBox.Critical)