"""
project_list_gui.py — Project list panel for IsoWorks.
Provides ProjectListWindow, a filterable table of sample submissions with
date-range and status filters, and links to submission and details forms.
"""
import sys
from datetime import date, datetime, timedelta
import logging
import csv

from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QGroupBox, QComboBox, QLineEdit, QDateEdit,
    QPushButton, QTableView, QLabel,
    QMessageBox, QFileDialog, QHeaderView
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import Qt, QTimer, QDate

# --- Shared Imports ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import make_searchable_combo

# --- Child Windows ---
from sample_submission_gui import SampleSubmissionWindow
from submission_details_gui import SubmissionDetailsWindow
from submission_report_gui import SubmissionReportDialog

class ProjectListWindow(QWidget):
    """
    PyQt5 equivalent of the frmProjectList Access form.
    Refactored to use db_core (SQLAlchemy) with corrected f-string syntax.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Sub-windows
        self.submission_form = None 
        self.details_window = None 
        
        # Defaults
        self.min_submission_date_str = "2000-01-01"
        self.max_submission_date_str = QDate.currentDate().toString("yyyy-MM-dd")

        # Filter debounce timer
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.timeout.connect(self.apply_search_filters)
        
        self.main_layout = QVBoxLayout(self)

        # UI Groups
        self.main_layout.addWidget(self._create_filter_group(), 0)
        self.main_layout.addLayout(self._create_action_bar())
        
        # Table Setup
        self.project_table = QTableView()
        self.project_table.setShowGrid(False)
        self._apply_table_styles()
        
        self.project_table.setAlternatingRowColors(True)
        self.project_table.setSelectionBehavior(QTableView.SelectRows)
        self.project_table.setSelectionMode(QTableView.ExtendedSelection)
        self.project_table.setEditTriggers(QTableView.NoEditTriggers)          
        self.project_table.setSortingEnabled(True)             
        self.project_table.verticalHeader().setVisible(False)            
        self.project_table.doubleClicked.connect(self.on_table_double_clicked)
        
        self.project_table_model = QStandardItemModel()
        self.project_table.setModel(self.project_table_model)            
        self.project_table.selectionModel().selectionChanged.connect(self.on_row_selected)            
        
        self.main_layout.addSpacing(10)
        self.main_layout.addWidget(self.project_table, 1)
        
        # Startup Check
        try:
            db_manager.get_engine()
            self.setEnabled(True)
            self.load_filter_combos()
            self._load_submission_date_range()
            self.reset_filters()
        except Exception:
            self.setEnabled(False)
            lbl = QLabel("<h2>No Database Connection</h2><p>Please configure in Settings.</p>")
            lbl.setAlignment(Qt.AlignCenter)
            self.main_layout.addWidget(lbl)

    def refresh(self):
        """Standard interface method for the launcher."""
        self.load_project_list()

    def _apply_table_styles(self):
        # self.project_table.setStyleSheet("""
        #     QTableView { border: none; background-color: white; gridline-color: none; }
        #     QTableView::item { padding: 6px 8px; border: none; background-color: white; color: #333333; }
        #     QTableView::item:alternate { background-color: #F3F7FA; }
        #     QTableView::item:selected { background-color: #DDEEFF; color: #000000; }
        #     QHeaderView::section { background-color: white; color: #7F8BB5; font-weight: bold; padding: 6px 8px; border: none; border-bottom: 2px solid #7F8BB5; }
        #     QHeaderView::section:hover { background-color: #DDEEFF; color: #000000; }
        # """)

        self.project_table.setStyleSheet("""
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

    def _create_filter_group(self):
        group = QGroupBox("Filter / Search Submissions")
        grid = QGridLayout()
        
        # Row 0
        grid.addWidget(QLabel("Media:"), 0, 0)
        self.cmbSearchWorkflow = QComboBox(); self.cmbSearchWorkflow.setEditable(True)
        grid.addWidget(self.cmbSearchWorkflow, 0, 1)
        
        grid.addWidget(QLabel("Client:"), 0, 2)
        self.cmbSearchSubmitter = QComboBox(); self.cmbSearchSubmitter.setEditable(True)
        grid.addWidget(self.cmbSearchSubmitter, 0, 3)

        grid.addWidget(QLabel("Sample ID From:"), 0, 4)
        self.txtSearchSampleIDfrom = QLineEdit(); grid.addWidget(self.txtSearchSampleIDfrom, 0, 5)
        grid.addWidget(QLabel("To:"), 0, 6)
        self.txtSearchSampleIDto = QLineEdit(); grid.addWidget(self.txtSearchSampleIDto, 0, 7)
        
        # Row 1
        grid.addWidget(QLabel("Officer:"), 1, 0)
        self.cmbSearchManager = QComboBox(); self.cmbSearchManager.setEditable(True)
        grid.addWidget(self.cmbSearchManager, 1, 1)

        grid.addWidget(QLabel("Priority:"), 1, 2)
        self.cmbSearchPriority = QComboBox(); grid.addWidget(self.cmbSearchPriority, 1, 3)
        
        grid.addWidget(QLabel("Submission From:"), 1, 4)
        self.txtSearchSubmissionDateFrom = QDateEdit()
        self.txtSearchSubmissionDateFrom.setDisplayFormat("yyyy-MM-dd")
        self.txtSearchSubmissionDateFrom.setCalendarPopup(True)
        grid.addWidget(self.txtSearchSubmissionDateFrom, 1, 5)

        grid.addWidget(QLabel("To:"), 1, 6)
        self.txtSearchSubmissionDateTo = QDateEdit()
        self.txtSearchSubmissionDateTo.setDisplayFormat("yyyy-MM-dd")
        self.txtSearchSubmissionDateTo.setCalendarPopup(True)
        grid.addWidget(self.txtSearchSubmissionDateTo, 1, 7)

        # Row 2
        grid.addWidget(QLabel("Submission Name:"), 2, 0)
        self.txtSearchPurpose = QLineEdit(); grid.addWidget(self.txtSearchPurpose, 2, 1)

        grid.addWidget(QLabel("Status:"), 2, 2)
        self.cmbSearchStatus = QComboBox(); grid.addWidget(self.cmbSearchStatus, 2, 3)

        _rep_sentinel = QDate(2000, 1, 1)
        grid.addWidget(QLabel("Reporting From:"), 2, 4)
        self.txtSearchReportingDateFrom = QDateEdit()
        self.txtSearchReportingDateFrom.setDisplayFormat("yyyy-MM-dd")
        self.txtSearchReportingDateFrom.setCalendarPopup(True)
        self.txtSearchReportingDateFrom.setMinimumDate(_rep_sentinel)
        self.txtSearchReportingDateFrom.setSpecialValueText("—")
        grid.addWidget(self.txtSearchReportingDateFrom, 2, 5)

        grid.addWidget(QLabel("To:"), 2, 6)
        self.txtSearchReportingDateTo = QDateEdit()
        self.txtSearchReportingDateTo.setDisplayFormat("yyyy-MM-dd")
        self.txtSearchReportingDateTo.setCalendarPopup(True)
        self.txtSearchReportingDateTo.setMinimumDate(_rep_sentinel)
        self.txtSearchReportingDateTo.setSpecialValueText("—")
        grid.addWidget(self.txtSearchReportingDateTo, 2, 7)

        # Row 3
        grid.addWidget(QLabel("Sampling Site:"), 3, 0)
        self.txtSearchLocation = QLineEdit(); grid.addWidget(self.txtSearchLocation, 3, 1)

        grid.addWidget(QLabel("Sample Name Like:"), 3, 2)
        self.txtSearchSampleName = QLineEdit(); grid.addWidget(self.txtSearchSampleName, 3, 3)
        
        grid.addWidget(QLabel("Rcv No.:"), 3, 4)
        self.txtSearchReceivingCode = QLineEdit(); grid.addWidget(self.txtSearchReceivingCode, 3, 5)

        # Row 4
        self.btnResetFilters = QPushButton("Reset Filters"); self.btnRefresh = QPushButton("Refresh List")
        grid.addWidget(self.btnResetFilters, 4, 6); grid.addWidget(self.btnRefresh, 4, 7)

        grid.setColumnStretch(1, 2); grid.setColumnStretch(3, 2)
        grid.setColumnStretch(5, 1); grid.setColumnStretch(7, 1)
        
        group.setLayout(grid)
        
        # Connections
        for w in [self.txtSearchPurpose, self.txtSearchLocation, self.txtSearchReceivingCode, self.txtSearchSampleIDfrom, self.txtSearchSampleIDto, self.txtSearchSampleName]:
            w.textChanged.connect(self.on_filter_changed)
        for w in [self.cmbSearchWorkflow, self.cmbSearchSubmitter, self.cmbSearchManager, self.cmbSearchPriority, self.cmbSearchStatus]:
            w.currentIndexChanged.connect(self.on_filter_changed)

        self.btnResetFilters.clicked.connect(self.reset_filters)
        self.btnRefresh.clicked.connect(self.apply_search_filters)

        for _dw in [self.txtSearchSubmissionDateFrom, self.txtSearchSubmissionDateTo,
                    self.txtSearchReportingDateFrom, self.txtSearchReportingDateTo]:
            _dw.dateChanged.connect(self.on_filter_changed)

        group.setStyleSheet("""
            QDateEdit { background: white; border: 1px solid #bbb; border-radius: 4px; padding: 2px 4px; }
            QDateEdit:focus { border-color: #7986CB; }
            QDateEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
            QDateEdit::drop-down:hover { background: #7986CB; }
            QDateEdit::drop-down:pressed { background: #3949AB; }
            QDateEdit::down-arrow { image: none; width: 0; height: 0;
                border-left: 5px solid transparent; border-right: 5px solid transparent;
                border-top: 6px solid #3949AB; }
        """)

        return group
        
    def _create_action_bar(self):
        layout = QHBoxLayout()
        layout.addWidget(QLabel("Submission ID:"))
        
        # --- Change: Editable Submission ID ---
        self.txtSelectedSubmissionID = QLineEdit()
        self.txtSelectedSubmissionID.setReadOnly(False) 
        self.txtSelectedSubmissionID.setFixedWidth(150)
        self.txtSelectedSubmissionID.setPlaceholderText("Enter ID...")
        layout.addWidget(self.txtSelectedSubmissionID)
        
        # self.btnAddNew    = QPushButton("Add New Submission...")
        self.btnViewEdit  = QPushButton("View/Edit Selected")
        self.btnDelete    = QPushButton("Delete Selected")
        self.btnExport    = QPushButton("Export List…")
        self.btnReport    = QPushButton("Report / Export Results…")
        self.btnReport.setStyleSheet(
            "QPushButton { background-color: #2D4A8A; color: white; font-weight: bold; "
            "padding: 5px 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1a3266; }"
        )

        layout.addSpacing(20)
        for btn in [self.btnViewEdit, self.btnDelete,
                    self.btnExport, self.btnReport]:
            layout.addWidget(btn)
        layout.addStretch()

        self.btnViewEdit.clicked.connect(self.view_edit_submission)
        self.btnDelete.clicked.connect(self.delete_submission)
        self.btnExport.clicked.connect(self.export_submission_list)
        # self.btnAddNew.clicked.connect(self.add_new_submission)
        self.btnReport.clicked.connect(self.open_report)
        
        # --- UX: Enter Key & Auto-Focus on Button ---
        self.txtSelectedSubmissionID.returnPressed.connect(self.view_edit_submission)
        self.txtSelectedSubmissionID.textChanged.connect(self._on_id_text_changed)
        
        return layout
    
    def _on_id_text_changed(self, text):
        """Make the View/Edit button the default (highlighted) when text is present."""
        has_text = bool(text.strip())
        self.btnViewEdit.setDefault(has_text)
        self.btnViewEdit.setAutoDefault(has_text)
        
    def on_filter_changed(self):
        self.filter_timer.start(300)
        
    def apply_search_filters(self):
        self.load_project_list()

    # --- SQL Helpers ---
    def _sql_concat(self, *args):
        dialect = db_manager.dialect
        parts = []
        for arg in args:
            if isinstance(arg, str) and (arg.startswith("'") or arg.startswith('"')): parts.append(arg)
            else: parts.append(f"CAST({arg} AS NVARCHAR(255))" if dialect == "SQL_SERVER" else str(arg))
        sep = " + " if dialect == "SQL_SERVER" else " || "
        return sep.join(parts)

    def _sql_bool(self, value):
        return "1" if value else "0"

    def _sql_func(self, func_name, *args):
        func_name = func_name.upper()
        if func_name == "UPPER" and db_manager.dialect == "ACCESS": return f"UCase({args[0]})"
        if func_name == "FORMAT":
            if db_manager.dialect == "POSTGRESQL":
                pg_fmt = args[1].strip("'").upper()
                return f"TO_CHAR({args[0]}, '{pg_fmt}')"
            return f"FORMAT({args[0]}, {args[1]})"
        return f"{func_name}({', '.join(args)})"

    def _load_submission_date_range(self):
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT MIN(SubmissionDate), MAX(SubmissionDate) FROM Submission")).fetchone()
                if res:
                    if res[0]: self.min_submission_date_str = str(res[0])[:10]
                    if res[1]: self.max_submission_date_str = str(res[1])[:10]
        except Exception as e: logging.error(f"Date range error: {e}")

    def load_filter_combos(self):
        try:
            with db_manager.get_connection() as conn:
                sql_cust = f"""SELECT CustomerID, {db_manager.sql_concat(self._sql_func('UPPER', 'LastName'), "', '", 'FirstName')} AS Name FROM Customer WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY 2"""
                self.cmbSearchSubmitter.clear(); self.cmbSearchSubmitter.addItem("", None)
                for row in conn.execute(text(sql_cust)): self.cmbSearchSubmitter.addItem(row[1], row[0])
                make_searchable_combo(self.cmbSearchSubmitter)

                sql_off = f"""SELECT EmployeeID, {db_manager.sql_concat(self._sql_func('UPPER', 'LastName'), "', '", 'FirstMiddleName')} AS Name FROM Employee WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY 2"""
                self.cmbSearchManager.clear(); self.cmbSearchManager.addItem("", None)
                for row in conn.execute(text(sql_off)): self.cmbSearchManager.addItem(row[1], row[0])
                make_searchable_combo(self.cmbSearchManager)
                    
                sql = "SELECT Status, Description FROM StatusLookup WHERE Status In (1, 200, 210, 220) ORDER BY Status"
                self.cmbSearchStatus.clear(); self.cmbSearchStatus.addItem("", None)
                for row in conn.execute(text(sql)): self.cmbSearchStatus.addItem(row[1], row[0])

                sql_media = f"""SELECT MediaID, {db_manager.sql_concat('Prefix', "'-'", 'MediaID', "': '", 'medianame')} FROM Media WHERE IsActive = {db_manager.sql_bool(True)} ORDER BY Prefix DESC, MediaID"""
                self.cmbSearchWorkflow.clear(); self.cmbSearchWorkflow.addItem("", None)
                for row in conn.execute(text(sql_media)): self.cmbSearchWorkflow.addItem(row[1], row[0])
                make_searchable_combo(self.cmbSearchWorkflow)
                    
                sql = "SELECT PriorityID, Description FROM Priority ORDER BY PriorityID"
                self.cmbSearchPriority.clear(); self.cmbSearchPriority.addItem("", None)
                for row in conn.execute(text(sql)): self.cmbSearchPriority.addItem(row[1], row[0])
        except Exception as e:
            logging.error(f"Filter load error: {e}")

    def _build_filter_where_clause(self):
        clauses = []; params = {}
        
        f = self.txtSearchSubmissionDateFrom.date().toString("yyyy-MM-dd")
        t = self.txtSearchSubmissionDateTo.date().toString("yyyy-MM-dd")
        clauses.append("(Submission.SubmissionDate >= :sub_from AND Submission.SubmissionDate <= :sub_to)")
        params["sub_from"] = f; params["sub_to"] = t

        _rep_sentinel = self.txtSearchReportingDateFrom.minimumDate()
        rf_date = self.txtSearchReportingDateFrom.date()
        rt_date = self.txtSearchReportingDateTo.date()
        rf = rf_date.toString("yyyy-MM-dd") if rf_date != _rep_sentinel else ""
        rt = rt_date.toString("yyyy-MM-dd") if rt_date != _rep_sentinel else ""
        if rf or rt:
            clauses.append("(Submission.ReportedDate >= :rep_from AND Submission.ReportedDate <= :rep_to)")
            params["rep_from"] = rf or '2000-01-01'
            params["rep_to"] = rt or QDate.currentDate().toString("yyyy-MM-dd")

        sf, st = self.txtSearchSampleIDfrom.text(), self.txtSearchSampleIDto.text()
        if sf or st:
            clauses.append("Submission.SubmissionID IN (SELECT SubmissionID FROM Sample WHERE SampleID BETWEEN :smp_min AND :smp_max)")
            params["smp_min"] = int(sf or 1); params["smp_max"] = int(st or 9999999)

        if self.txtSearchSampleName.text():
            clauses.append("Submission.SubmissionID IN (SELECT SubmissionID FROM Sample WHERE sName LIKE :sname)")
            params["sname"] = f"%{self.txtSearchSampleName.text()}%"

        if self.cmbSearchSubmitter.currentData():
            clauses.append("Submission.CustomerID = :cid"); params["cid"] = self.cmbSearchSubmitter.currentData()
        if self.cmbSearchManager.currentData():
            clauses.append("Submission.TechnicalOfficer = :tof"); params["tof"] = self.cmbSearchManager.currentData()
        if self.cmbSearchWorkflow.currentData():
            clauses.append("t1.MediaID = :mid"); params["mid"] = self.cmbSearchWorkflow.currentData()
        if self.cmbSearchPriority.currentData():
            clauses.append("Submission.PriorityID = :pri"); params["pri"] = self.cmbSearchPriority.currentData()
        if self.cmbSearchStatus.currentData():
            clauses.append("Submission.Status = :stat"); params["stat"] = self.cmbSearchStatus.currentData()

        if self.txtSearchPurpose.text():
            clauses.append("Submission.SubmissionName LIKE :purpose"); params["purpose"] = f"%{self.txtSearchPurpose.text()}%"
        if self.txtSearchLocation.text():
            clauses.append("Submission.SubmissionSite LIKE :site"); params["site"] = f"%{self.txtSearchLocation.text()}%"
        if self.txtSearchReceivingCode.text():
            clauses.append("Submission.ReceivingNo = :rcv"); params["rcv"] = self.txtSearchReceivingCode.text()

        clauses.append("Submission.SubmissionType = 2")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def load_project_list(self):
        logging.info("Loading project list...")
        date_fmt = "'yyyy/MM/dd'"
        
        sql = f"""
            SELECT 
                Submission.SubmissionID, 
                {self._sql_func("FORMAT", "Submission.SubmissionDate", date_fmt)},
                {self._sql_func("FORMAT", "Submission.ReportedDate", date_fmt)},
                Customer.LastName, Employee.LastName, Submission.SubmissionName,
                (SELECT COUNT(SampleID) FROM Sample WHERE Sample.SubmissionID=Submission.SubmissionID), 
                Submission.SubmissionSite, Submission.ReceivingNo, 
                Priority.Description, Media.MediaID, StatusLookup.Description
            FROM StatusLookup 
            RIGHT JOIN (Priority 
            RIGHT JOIN (Customer 
            RIGHT JOIN ((Media 
            RIGHT JOIN (Submission INNER JOIN (SELECT DISTINCT Sample.SubmissionID, Sample.MediaID FROM Sample) As t1 ON Submission.SubmissionID=t1.SubmissionID)
            ON Media.MediaID = t1.MediaID) 
            LEFT JOIN Employee ON Submission.TechnicalOfficer = Employee.EmployeeID) 
            ON Customer.CustomerID = Submission.CustomerID) 
            ON Priority.PriorityID = Submission.PriorityID) 
            ON StatusLookup.Status = Submission.Status
        """
        
        where_clause, params = self._build_filter_where_clause()
        order_clause = " ORDER BY Submission.SubmissionID DESC" 
        
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text(sql + where_clause + order_clause), params)
                self.project_table_model.clear()
                self.project_table_model.setHorizontalHeaderLabels(["ID", "Submitted", "Reported", "Submitter", "Officer", "Name", "No", "Site", "RcvNo", "Priority", "Media", "Status"])
                
                for row in res:
                    items = [
                        QStandardItem(str(row[0])),
                        QStandardItem(str(row[1] or "")),
                        QStandardItem(str(row[2] or "")),
                        QStandardItem(str(row[3] or "")),
                        QStandardItem(str(row[4] or "")),
                        QStandardItem(str(row[5] or "")),
                        QStandardItem(str(row[6])),
                        QStandardItem(str(row[7] or "")),
                        QStandardItem(str(row[8] or "")),
                        QStandardItem(str(row[9] or "")),
                        QStandardItem(str(row[10] or "")),
                        QStandardItem(str(row[11] or ""))
                    ]
                    items[6].setTextAlignment(Qt.AlignCenter)
                    items[10].setTextAlignment(Qt.AlignCenter)
                    items[0].setData(row[0], Qt.DisplayRole)
                    
                    self.project_table_model.appendRow(items)
                    
            self.project_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
            self.project_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch) 
            logging.info(f"Loaded {self.project_table_model.rowCount()} rows.")
            
        except Exception as e:
            logging.error(f"Project list load failed: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load list: {e}")

    def reset_filters(self):
        for w in [self.cmbSearchWorkflow, self.cmbSearchSubmitter, self.txtSearchSampleIDfrom, self.txtSearchSampleIDto, self.cmbSearchManager, self.cmbSearchPriority, self.txtSearchSubmissionDateFrom, self.txtSearchSubmissionDateTo, self.txtSearchPurpose, self.cmbSearchStatus, self.txtSearchReportingDateFrom, self.txtSearchReportingDateTo, self.txtSearchLocation, self.txtSearchSampleName, self.txtSearchReceivingCode]:
            w.blockSignals(True)
        
        self.cmbSearchWorkflow.setCurrentIndex(0); self.cmbSearchSubmitter.setCurrentIndex(0)
        self.cmbSearchManager.setCurrentIndex(0); self.cmbSearchPriority.setCurrentIndex(0)
        self.cmbSearchStatus.setCurrentIndex(0)
        for t in [self.txtSearchSampleIDfrom, self.txtSearchSampleIDto, self.txtSearchPurpose, self.txtSearchLocation, self.txtSearchSampleName, self.txtSearchReceivingCode]:
            t.clear()

        self.txtSearchSubmissionDateFrom.setDate(QDate.fromString(self.min_submission_date_str, "yyyy-MM-dd"))
        self.txtSearchSubmissionDateTo.setDate(QDate.fromString(self.max_submission_date_str, "yyyy-MM-dd"))
        _sentinel = self.txtSearchReportingDateFrom.minimumDate()
        self.txtSearchReportingDateFrom.setDate(_sentinel)
        self.txtSearchReportingDateTo.setDate(_sentinel)
        
        for w in [self.cmbSearchWorkflow, self.cmbSearchSubmitter, self.txtSearchSampleIDfrom, self.txtSearchSampleIDto, self.cmbSearchManager, self.cmbSearchPriority, self.txtSearchSubmissionDateFrom, self.txtSearchSubmissionDateTo, self.txtSearchPurpose, self.cmbSearchStatus, self.txtSearchReportingDateFrom, self.txtSearchReportingDateTo, self.txtSearchLocation, self.txtSearchSampleName, self.txtSearchReceivingCode]:
            w.blockSignals(False)
        self.apply_search_filters()

    def on_table_double_clicked(self, index):
        if not index.isValid(): return
        sid_str = self.project_table_model.item(index.row(), 0).text()
        try: self.open_details_window(int(sid_str))
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

    def on_row_selected(self, selected, deselected):
        idxs = selected.indexes()
        if not idxs: 
            # Don't clear automatically, user might be typing manual ID
            pass 
        else:
            sid = self.project_table_model.data(self.project_table_model.index(idxs[0].row(), 0))
            if sid: self.txtSelectedSubmissionID.setText(str(sid))

    def _get_selected_submission_ids(self) -> list:
        """Return list of SubmissionIDs for all currently selected table rows."""
        rows = {idx.row() for idx in self.project_table.selectionModel().selectedRows()}
        ids = []
        for row in sorted(rows):
            try:
                ids.append(int(self.project_table_model.item(row, 0).text()))
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        return ids

    def open_report(self):
        ids = self._get_selected_submission_ids()
        # Fall back to manually typed ID if nothing selected in table
        if not ids:
            s = self.txtSelectedSubmissionID.text().strip()
            try:
                ids = [int(s)]
            except ValueError:
                QMessageBox.information(self, "Select submissions",
                                        "Select one or more submissions in the list first,\n"
                                        "or enter a Submission ID in the field above.")
                return
        dlg = SubmissionReportDialog(ids, parent=self)
        dlg.exec_()

    def open_details_window(self, submission_id):
        if self.details_window is None or not self.details_window.isVisible():
            self.details_window = SubmissionDetailsWindow(submission_id=submission_id, parent=self)
            self.details_window.show()
        else:
            self.details_window.activateWindow(); self.details_window.setFocus()

    def add_new_submission(self):
        if self.submission_form is None or not self.submission_form.isVisible():
            self.submission_form = SampleSubmissionWindow(parent=self)
            self.submission_form.show()
        else: self.submission_form.activateWindow(); self.submission_form.setFocus()

    def view_edit_submission(self):
        # --- Change: Check manually entered ID ---
        s = self.txtSelectedSubmissionID.text().strip()
        if not s:
            QMessageBox.information(self, "Info", "Please enter or select a Submission ID.")
            return
        
        try:
            sid = int(s)
        except ValueError:
             QMessageBox.warning(self, "Error", "Invalid Submission ID format.")
             return

        # Check existence
        try:
            with db_manager.get_connection() as conn:
                res = conn.execute(text("SELECT COUNT(*) FROM Submission WHERE SubmissionID = :sid"), {"sid": sid}).fetchone()
                if res and res[0] > 0:
                    self.open_details_window(sid)
                else:
                    QMessageBox.information(self, "Not Found", f"Submission ID {sid} not found.")
        except Exception as e:
            logging.error(f"Check submission failed: {e}")
            QMessageBox.critical(self, "Database Error", str(e))

    def delete_submission(self):
        sid_str = self.txtSelectedSubmissionID.text()
        if not sid_str: return
        if QMessageBox.question(self, "Delete", f"Delete Submission {sid_str}?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                # 1. Delete Field Data (Children of Samples)
                conn.execute(text("""
                    DELETE FROM Sample_FieldData 
                    WHERE EXISTS (
                        SELECT 1 FROM Sample 
                        WHERE Sample.SampleID = Sample_FieldData.SampleID 
                        AND Sample.Prefix = Sample_FieldData.Prefix 
                        AND Sample.SubmissionID = :sid
                    )
                """), {"sid": sid_str})
                
                # 2. Delete Samples
                conn.execute(text("DELETE FROM Sample WHERE SubmissionID = :sid"), {"sid": sid_str})
                
                # 3. Delete Submission
                conn.execute(text("DELETE FROM Submission WHERE SubmissionID = :sid"), {"sid": sid_str})
                
                conn.commit()
            self.load_project_list()
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

    def export_submission_list(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export", f"Submissions_{date.today().strftime('%Y%m%d')}.csv", "CSV (*.csv)")
        if not path: return
        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                cols = self.project_table_model.columnCount()
                writer.writerow([self.project_table_model.headerData(j, Qt.Horizontal) for j in range(cols)])
                for i in range(self.project_table_model.rowCount()):
                    writer.writerow([self.project_table_model.item(i, j).text() for j in range(cols)])
            QMessageBox.information(self, "Success", f"Exported to {path}")
        except Exception as e: QMessageBox.critical(self, "Error", str(e))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    win = ProjectListWindow()
    win.show()
    sys.exit(app.exec_())