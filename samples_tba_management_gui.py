"""
samples_tba_management_gui.py — Samples-To-Be-Analysed (TBA) queue manager for IsoWorks.
Provides SamplesTBAWindow, a filterable table view of the global analysis queue
with tools to inspect, remove, or re-assign pending samples via db_core.
"""
import sys
import logging

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLabel, QLineEdit,
    QPushButton, QTableView, QMessageBox, QAbstractItemView
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem, QColor, QBrush
from PyQt5.QtCore import Qt, QTimer

# --- Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import make_searchable_combo

class SamplesTBAWindow(QWidget):
    """
    Modern replacement for frmProjectsSamplesAll.
    Manages the global 'Samples To Be Analyzed' (TBA) queue.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Samples To Be Analyzed (TBA) Management")
        self.resize(1300, 850)
        
        self._updating = False

        # Main Layout
        self.main_layout = QVBoxLayout(self)

        # --- 1. Header / Actions ---
        self.main_layout.addLayout(self._create_header())
        
        # --- 2. Filters (Top) ---
        self.filter_panel = self._create_filter_panel()
        self.main_layout.addWidget(self.filter_panel)
        
        # --- 3. Table (Middle - Expands) ---
        self.table_panel = self._create_table_panel()
        self.main_layout.addWidget(self.table_panel, 1) # Stretch factor 1

        # --- 4. Status Bar ---
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #555; font-weight: bold; background: #eee; padding: 5px;")
        self.main_layout.addWidget(self.status_label)

        # --- Setup ---
        self._init_model()
        self._connect_signals()
        
        # Load Data
        try:
            db_manager.get_engine()
            self.load_combos()
            QTimer.singleShot(100, self.load_tba_list)
        except Exception as e:
            self.setEnabled(False)
            self.status_label.setText(f"Database Error: {e}")

    # --- UI Construction ---

    def _apply_table_styles(self):
        """Applies the clean white/blue theme matching siam_run_list_gui."""

        self.tba_table.setStyleSheet("""
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
        
        self.tba_table.setShowGrid(False) # Explicitly hide grid lines

    def _create_header(self):
        layout = QHBoxLayout()
        
        title = QLabel("<h2>Samples To Be Analyzed (TBA)</h2>")
        layout.addWidget(title)
        
        layout.addStretch()
        
        self.btn_refresh = QPushButton("Refresh List")
        self.btn_refresh.setStyleSheet("padding: 6px;")
        
        self.btn_remove = QPushButton("Remove Selected from Queue")
        self.btn_remove.setStyleSheet("""
            QPushButton {
                background-color: #d32f2f; color: white; font-weight: bold; padding: 6px 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #b71c1c; }
        """)
        
        layout.addWidget(self.btn_refresh)
        layout.addSpacing(10)
        layout.addWidget(self.btn_remove)
        
        return layout

    def _create_filter_panel(self):
        group = QGroupBox("Filter Options")
        layout = QGridLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setVerticalSpacing(10)
        layout.setHorizontalSpacing(15)

        # Filters
        self.txt_search_general = QLineEdit()
        self.txt_search_general.setPlaceholderText("Search Sample ID, Name...")
        
        self.cmb_submission = QComboBox(); self.cmb_submission.setEditable(True)
        self.cmb_media = QComboBox()
        self.cmb_workflow = QComboBox()
        
        self.cmb_client = QComboBox(); self.cmb_client.setEditable(True)
        
        self.btn_reset_filters = QPushButton("Reset")

        # Row 0: Search & Submission (Wide)
        layout.addWidget(QLabel("Search:"), 0, 0)
        layout.addWidget(self.txt_search_general, 0, 1)
        
        layout.addWidget(QLabel("Submission:"), 0, 2)
        layout.addWidget(self.cmb_submission, 0, 3)
        
        layout.addWidget(self.btn_reset_filters, 0, 6)
        
        # Row 1: Media, Workflow, Client
        layout.addWidget(QLabel("Media:"), 1, 0)
        layout.addWidget(self.cmb_media, 1, 1)
        
        layout.addWidget(QLabel("Workflow:"), 1, 2)
        layout.addWidget(self.cmb_workflow, 1, 3)
        
        # Row 2: Client (Span)
        layout.addWidget(QLabel("Client:"), 2, 0)
        layout.addWidget(self.cmb_client, 2, 1, 1, 3)

        # Layout Column Stretch
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        layout.setColumnStretch(5, 1)
        
        group.setLayout(layout)
        return group

    def _create_table_panel(self):
        group = QGroupBox("Queue")
        layout = QVBoxLayout()
        
        self.tba_table = QTableView()
        self.tba_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tba_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tba_table.setAlternatingRowColors(True)
        self.tba_table.setSortingEnabled(True)
        
        self._apply_table_styles()
        
        layout.addWidget(self.tba_table)
        group.setLayout(layout)
        return group

    def _init_model(self):
        self.model = QStandardItemModel()
        self.headers = [
            "Sample", "Submission", "Media", "Workflow", "Status", 
            "Client", "Submitted", "TBA_ID"
        ]
        self.model.setHorizontalHeaderLabels(self.headers[:-1])
        self.tba_table.setModel(self.model)

    def _connect_signals(self):
        # Filters
        for cmb in [self.cmb_submission, self.cmb_media, self.cmb_workflow, 
                    self.cmb_client]:
            cmb.currentIndexChanged.connect(self.load_tba_list)
            
        self.txt_search_general.textChanged.connect(self._on_search_text_changed)
        
        # Buttons
        self.btn_refresh.clicked.connect(self.load_tba_list)
        self.btn_reset_filters.clicked.connect(self.reset_filters)
        self.btn_remove.clicked.connect(self.remove_selected_from_tba)
        
        # Debounce timer for text search
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.timeout.connect(self.load_tba_list)

    def _on_search_text_changed(self):
        self.search_timer.start(400)

    # --- Data Loading ---
    def load_combos(self):
        self._updating = True
        try:
            with db_manager.get_connection() as conn:
                # Submission
                self.cmb_submission.addItem("- All -", None)
                for r in conn.execute(text("SELECT SubmissionID, SubmissionName FROM Submission ORDER BY SubmissionID DESC")):
                    self.cmb_submission.addItem(f"{r[0]} - {r[1]}", r[0])

                # Media
                self.cmb_media.addItem("- All -", None)
                for r in conn.execute(text(f"SELECT MediaID, medianame FROM Media WHERE IsActive={db_manager.sql_bool(True)} ORDER BY medianame")):
                    self.cmb_media.addItem(r[1], r[0])

                # Workflow
                self.cmb_workflow.addItem("- All -", None)
                for r in conn.execute(text(f"SELECT WorkflowID, WorkflowName FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY WorkflowName")):
                    self.cmb_workflow.addItem(r[1], r[0])
                make_searchable_combo(self.cmb_workflow)

                # Client
                self.cmb_client.addItem("- All -", None)
                _name_concat = db_manager.sql_concat('LastName', "', '", 'FirstName')
                for r in conn.execute(text(f"SELECT CustomerID, {_name_concat} FROM Customer ORDER BY LastName")):
                    self.cmb_client.addItem(r[1], r[0])
                make_searchable_combo(self.cmb_client)

        except Exception as e:
            logging.error(f"Combo Load Error: {e}")
        finally:
            self._updating = False

    def reset_filters(self):
        self._updating = True
        self.cmb_submission.setCurrentIndex(0)
        self.cmb_media.setCurrentIndex(0)
        self.cmb_workflow.setCurrentIndex(0)
        self.cmb_client.setCurrentIndex(0)
        self.txt_search_general.clear()
        self._updating = False
        self.load_tba_list()

    def load_tba_list(self):
        if self._updating: return
        
        self.model.clear()
        self.model.setHorizontalHeaderLabels(self.headers[:-1]) 

        try:
            with db_manager.get_connection() as conn:
                sample_code = db_manager.sql_concat('s.Prefix', "'-'", 's.SampleID')
                client_name = db_manager.sql_concat('cust.LastName', "', '", 'cust.FirstName')
                date_expr = "TO_CHAR(sub.SubmissionDate, 'YYYY-MM-DD')" if db_manager.dialect == "POSTGRESQL" else "FORMAT(sub.SubmissionDate, 'yyyy-MM-dd')"
                sql = f"""
                    SELECT
                        {sample_code} AS SampleCode,
                        sub.SubmissionName,
                        m.medianame AS Media,
                        w.Abbreviation AS Workflow,
                        'Queued' AS Status,
                        {client_name} AS Client,
                        {date_expr} AS Submitted,
                        sq.queue_id
                    FROM public.sample_queue sq
                    JOIN public.sample s ON sq.sampleid = s.sampleid AND sq.prefix = s.prefix
                    JOIN public.submission sub ON s.submissionid = sub.submissionid
                    JOIN public.workflowjob wj ON wj.workflowjobid = sq.workflowjobid
                    JOIN public.workflow w ON w.workflowid = wj.workflowid
                    JOIN public.media m ON sq.mediaid = m.mediaid
                    LEFT JOIN public.customer cust ON sub.customerid = cust.customerid
                    WHERE 1=1
                """
                params = {}

                # Filters
                if self.cmb_submission.currentData():
                    sql += " AND s.submissionid = :sub"; params["sub"] = self.cmb_submission.currentData()
                if self.cmb_media.currentData():
                    sql += " AND sq.mediaid = :med"; params["med"] = self.cmb_media.currentData()
                if self.cmb_workflow.currentData():
                    sql += " AND wj.workflowid = :wf"; params["wf"] = self.cmb_workflow.currentData()
                if self.cmb_client.currentData():
                    sql += " AND sub.customerid = :cli"; params["cli"] = self.cmb_client.currentData()

                # Text Search
                txt = self.txt_search_general.text().strip()
                if txt:
                    sql += f""" AND (
                        s.sName LIKE :txt OR
                        ({sample_code}) LIKE :txt OR
                        sub.SubmissionName LIKE :txt
                    )"""
                    params["txt"] = f"%{txt}%"

                sql += " ORDER BY sub.priorityid, sq.queued_at DESC"
                
                # Limit results for performance
                sql = sql.replace("SELECT", f"SELECT {db_manager.sql_top(1000)}", 1)
                sql += db_manager.sql_limit(1000)

                rows = conn.execute(text(sql), params).fetchall()
                
                for r in rows:
                    items = [
                        QStandardItem(str(r[0])), # Sample
                        QStandardItem(str(r[1])), # Submission
                        QStandardItem(str(r[2])), # Media
                        QStandardItem(str(r[3])), # Workflow
                        QStandardItem(str(r[4])), # Status
                        QStandardItem(str(r[5] or "")), # Client
                        QStandardItem(str(r[6] or "")), # Date
                    ]
                    
                    # Color coding for Status
                    if "Registered" in r[4]: items[4].setForeground(QBrush(QColor("blue")))
                    elif "Staged" in r[4]: items[4].setForeground(QBrush(QColor("green")))
                    elif "Analyzed" in r[4]: items[4].setForeground(QBrush(QColor("gray")))
                    
                    # Store ID in hidden data of first item
                    items[0].setData(r[7], Qt.UserRole) 
                    
                    self.model.appendRow(items)
                    
            self.tba_table.resizeColumnsToContents()
            self.status_label.setText(f"Loaded {len(rows)} samples.")

        except Exception as e:
            logging.error(f"Load TBA Error: {e}")
            self.status_label.setText("Error loading list.")

    # --- Logic ---
    def remove_selected_from_tba(self):
        """Removes selected samples from TBA and reverts status to Registered (1)."""
        rows = self.tba_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.warning(self, "Selection", "Please select samples to remove.")
            return
            
        if QMessageBox.question(self, "Confirm", f"Remove {len(rows)} samples from Analysis Queue?", 
                                QMessageBox.Yes|QMessageBox.No) == QMessageBox.No:
            return

        ids_to_remove = []
        for idx in rows:
            # ID stored in UserRole of column 0
            tba_id = self.model.data(self.model.index(idx.row(), 0), Qt.UserRole)
            if tba_id: ids_to_remove.append(tba_id)

        if not ids_to_remove: return

        try:
            with db_manager.get_connection() as conn:
                # 1. Get Sample Keys to revert status
                # (We need Prefix/SampleID to update Sample table)
                # NOTE: Simple loop for compatibility, could be batched
                sample_keys = []
                for tid in ids_to_remove:
                    res = conn.execute(text("SELECT prefix, sampleid FROM public.sample_queue WHERE queue_id=:id"), {"id": tid}).fetchone()
                    if res: sample_keys.append(res)

                # 2. Delete from queue
                for tid in ids_to_remove:
                    conn.execute(text("DELETE FROM public.sample_queue WHERE queue_id=:id"), {"id": tid})
                
                # 3. Revert Status to 1 (Registered)
                for pfx, sid in sample_keys:
                    conn.execute(text("UPDATE Sample SET Status=1 WHERE SampleID=:sid AND Prefix=:pfx"), {"sid": sid, "pfx": pfx})
                
                conn.commit()
                
            self.load_tba_list()
            QMessageBox.information(self, "Success", f"Removed {len(ids_to_remove)} samples from queue.")
            
        except Exception as e:
            logging.error(f"Remove TBA Error: {e}")
            QMessageBox.critical(self, "Error", f"Failed to remove samples:\n{e}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = SamplesTBAWindow()
    win.show()
    sys.exit(app.exec_())