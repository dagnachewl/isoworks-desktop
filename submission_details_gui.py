"""
submission_details_gui.py — Submission details viewer for IsoWorks.
Provides SubmissionDetailsWindow (QMainWindow) for browsing samples within a
submission, displaying per-sample metadata and measurable results via db_core.
"""
import sys
from datetime import datetime
import logging

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QRadioButton, QComboBox, QLineEdit,
    QPushButton, QTableView, QCheckBox, QLabel, QFormLayout,
    QMessageBox, QTabWidget, QTreeView, QHeaderView, QAbstractItemView
)
from PyQt5.QtGui import QStandardItemModel, QStandardItem
from PyQt5.QtCore import QTimer

# --- Shared Manager & SQLAlchemy ---
from db_core import db_manager
from sqlalchemy import text
from gui_utils import make_searchable_combo
from submission_report_gui import SubmissionReportDialog
from sample_label_gui import SampleLabelDialog, registration_spec

class SubmissionDetailsWindow(QMainWindow):
    """
    PyQt5 equivalent of the frmProjectIsoWorks (Submission Details) form.
    Refactored to use db_core (SQLAlchemy).
    """
    def __init__(self, submission_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Submission Details - {submission_id}")
        self.setGeometry(100, 100, 1000, 700)        
        self.submission_id = submission_id        
        # --- Check Connection ---
        try:
            db_manager.get_engine()
        except Exception:
            # If engine not ready, close immediately (launcher handles connection)
            QTimer.singleShot(0, self.close)
            return            
        # --- Sample Navigation State ---
        self.sample_id_list = [] # List of (SampleID, Prefix) tuples
        self.current_sample_index = 0
        self.current_sample_id = None
        self.current_sample_prefix = None        
        # Central widget and main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        # --- 1. Top Button Bar ---
        self.main_layout.addLayout(self._create_button_bar())
        # --- 2. Main Details Layout ---
        self.details_layout = QHBoxLayout()
        self.details_layout.addLayout(self._create_left_details(), 1)
        self.details_layout.addLayout(self._create_right_details(), 1)
        self.main_layout.addLayout(self.details_layout)        
        # --- 3. Tab Widget for Subforms ---
        self.main_layout.addWidget(self._create_tab_widget())        
        # --- 4. Footer ---
        self.main_layout.addLayout(self._create_footer())        
        # --- Load Data ---
        self.load_all_comboboxes()
        self.load_submission_data()
        self.load_sample_list_for_navigation()
        if self.sample_id_list:
            self.navigate_to_sample(0)
        else:
            logging.warning(f"No samples found for Submission {self.submission_id}")
            QMessageBox.information(self, "Info", "This submission does not have any samples.")
        self.load_prep_tab()
        self.load_analysis_tab()
        self.load_report_tab()
        self.load_outsource_tab()

        self.connect_signals()

    # ... [UI Creation Methods _create_... REMAIN UNCHANGED] ...
    def _create_button_bar(self):
        layout = QHBoxLayout()
        self.lblHeader = QLabel(f"<b>Submission Details -- Submission No.: {self.submission_id}</b>")
        self.btnEdit = QPushButton("Edit")
        self.btnSave = QPushButton("Save Changes"); self.btnCancel = QPushButton("Cancel")
        self.btnDelete = QPushButton("Delete Submission"); self.btnReport = QPushButton("View Report")
        self.btnPrintLabels = QPushButton("Print Labels")
        self.btnRefresh = QPushButton("Refresh"); self.btnClose = QPushButton("Close")
        layout.addWidget(self.lblHeader); layout.addStretch()
        layout.addWidget(self.btnEdit); layout.addWidget(self.btnSave); layout.addWidget(self.btnCancel)
        layout.addWidget(self.btnDelete)
        layout.addWidget(self.btnReport); layout.addWidget(self.btnPrintLabels)
        layout.addWidget(self.btnRefresh); layout.addWidget(self.btnClose)
        return layout

    def _create_left_details(self):
        layout = QFormLayout()
        self.optSubmissionType = QGroupBox("Sample Type")
        self.rbUnknown = QRadioButton("Unknown Samples"); self.rbReference = QRadioButton("References/Controls")
        opt_layout = QHBoxLayout(); opt_layout.addWidget(self.rbUnknown); opt_layout.addWidget(self.rbReference)
        self.optSubmissionType.setLayout(opt_layout); layout.addRow(self.optSubmissionType)
        self.cmbSubmitter = QComboBox(); self.cmbSubmitter.setEditable(True)
        layout.addRow("Submitter (Client):", self.cmbSubmitter)
        self.txtSubmissionDate = QLineEdit(); layout.addRow("Submission Date:", self.txtSubmissionDate)
        self.cmbManager = QComboBox(); self.cmbManager.setEditable(True); layout.addRow("Officer:", self.cmbManager)
        self.cmbPayer = QComboBox(); self.cmbPayer.setEditable(True); layout.addRow("Payer:", self.cmbPayer)
        self.txtPayerReference = QLineEdit(); layout.addRow("Payer Reference:", self.txtPayerReference)
        return layout

    def _create_right_details(self):
        layout = QFormLayout()
        self.cmbMediaType = QComboBox(); layout.addRow("Media Type:", self.cmbMediaType)
        self.cmbRequestedWorkflow = QComboBox(); layout.addRow("Requested Workflow:", self.cmbRequestedWorkflow)
        self.txtFieldLocation = QLineEdit(); layout.addRow("Sampling Site:", self.txtFieldLocation)
        self.txtProjectPurpose = QLineEdit(); layout.addRow("Submission Name:", self.txtProjectPurpose)
        self.txtStoreLocation = QLineEdit(); layout.addRow("Store Location:", self.txtStoreLocation)
        self.cmbPriority = QComboBox(); layout.addRow("Priority:", self.cmbPriority)
        self.txtReceivingCode = QLineEdit(); layout.addRow("Receiving No.:", self.txtReceivingCode)
        self.txtInvoiceNumber = QLineEdit(); layout.addRow("Invoice No.:", self.txtInvoiceNumber)
        return layout

    def _create_tab_widget(self):
        self.tabs = QTabWidget()

        # ── Tab 1: Details ────────────────────────────────────────────────
        self.tabDetails = QWidget()
        tab_details_layout = QVBoxLayout()
        tab_details_layout.addLayout(self._create_sample_nav_bar())
        self.sample_details_group = QGroupBox("Sample Details")
        self.sample_details_group.setLayout(self._create_sample_form_layout())
        tab_details_layout.addWidget(self.sample_details_group)
        self.tabDetails.setLayout(tab_details_layout)
        self.tabs.addTab(self.tabDetails, "Details")

        # ── Tab 2: Prep ───────────────────────────────────────────────────
        self.tabPrep = QWidget()
        prep_layout = QVBoxLayout()
        self.prep_model = QStandardItemModel()
        self.prep_model.setHorizontalHeaderLabels(["Our Lab ID", "Sample Name", "Abbrev.", "Analysis Type", "Status"])
        self.prep_table = QTableView()
        self.prep_table.setModel(self.prep_model)
        self.prep_table.horizontalHeader().setStretchLastSection(True)
        self.prep_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.prep_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        prep_layout.addWidget(self.prep_table)
        self.tabPrep.setLayout(prep_layout)
        self.tabs.addTab(self.tabPrep, "Prep")

        # ── Tab 3: Analysis ───────────────────────────────────────────────
        self.tabAnalysis = QWidget()
        tab_layout_analysis = QVBoxLayout()
        analysis_tree_controls = QHBoxLayout()
        self.btnExpandAnalysis = QPushButton("Expand All")
        self.btnCollapseAnalysis = QPushButton("Collapse All")
        analysis_tree_controls.addStretch()
        analysis_tree_controls.addWidget(self.btnExpandAnalysis)
        analysis_tree_controls.addWidget(self.btnCollapseAnalysis)
        tab_layout_analysis.addLayout(analysis_tree_controls)
        self.analysis_tree = QTreeView()
        self.analysis_tree.setSortingEnabled(True)
        self.analysis_tree_model = QStandardItemModel()
        self.analysis_tree.setModel(self.analysis_tree_model)
        self.analysis_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tab_layout_analysis.addWidget(self.analysis_tree)
        self.tabAnalysis.setLayout(tab_layout_analysis)
        self.tabs.addTab(self.tabAnalysis, "Analysis")

        # ── Tab 4: Report ─────────────────────────────────────────────────
        self.tabReport = QWidget()
        report_layout = QVBoxLayout()
        self.lblReportStats = QLabel("—")
        report_layout.addWidget(self.lblReportStats)
        self.report_model = QStandardItemModel()
        self.report_model.setHorizontalHeaderLabels(["Our Lab ID", "Sample Name", "Type", "Status", "Workflow"])
        self.report_table = QTableView()
        self.report_table.setModel(self.report_model)
        self.report_table.horizontalHeader().setStretchLastSection(True)
        self.report_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.report_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        report_layout.addWidget(self.report_table)
        btn_rpt_row = QHBoxLayout()
        self.btnGenerateReport = QPushButton("Generate / View Report")
        btn_rpt_row.addStretch()
        btn_rpt_row.addWidget(self.btnGenerateReport)
        report_layout.addLayout(btn_rpt_row)
        self.tabReport.setLayout(report_layout)
        self.tabs.addTab(self.tabReport, "Report")

        # ── Tab 5: Outsource ──────────────────────────────────────────────
        self.tabOutsource = QWidget()
        outsource_layout = QVBoxLayout()
        self.lblOutsourceEmpty = QLabel("No outsourced analyses recorded for this submission.")
        outsource_layout.addWidget(self.lblOutsourceEmpty)
        self.outsource_model = QStandardItemModel()
        self.outsource_model.setHorizontalHeaderLabels(["Our Lab ID", "Sample Name", "Lab", "Date Sent", "Status"])
        self.outsource_table = QTableView()
        self.outsource_table.setModel(self.outsource_model)
        self.outsource_table.horizontalHeader().setStretchLastSection(True)
        self.outsource_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.outsource_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.outsource_table.setVisible(False)
        outsource_layout.addWidget(self.outsource_table)
        self.tabOutsource.setLayout(outsource_layout)
        self.tabs.addTab(self.tabOutsource, "Outsource")

        return self.tabs

    def _create_sample_nav_bar(self):
        layout = QHBoxLayout()
        self.btnSampleFirst = QPushButton("|< First"); self.btnSamplePrev = QPushButton("< Prev")
        self.lblSampleRecord = QLabel("Sample 1 of N"); self.btnSampleNext = QPushButton("Next >")
        self.btnSampleLast = QPushButton("Last >|"); self.cmbSelectionList = QComboBox(); self.cmbSelectionList.setEditable(True)
        self.btnSampleEdit = QPushButton("Edit"); self.btnSampleSave = QPushButton("Save Sample"); self.btnSampleCancel = QPushButton("Cancel Sample Edit")
        self.btnSampleSave.setEnabled(False); self.btnSampleCancel.setEnabled(False)
        self.cmbContainerTypeAll = QComboBox(); self.cmbContainerTypeAll.setMinimumWidth(130)
        self.btnContainerTypeSetAll = QPushButton("Set All")

        layout.addWidget(self.btnSampleFirst); layout.addWidget(self.btnSamplePrev)
        layout.addWidget(self.lblSampleRecord); layout.addWidget(self.btnSampleNext); layout.addWidget(self.btnSampleLast)
        layout.addWidget(QLabel("  Find:")); layout.addWidget(self.cmbSelectionList); layout.addStretch()
        layout.addWidget(QLabel("Container:")); layout.addWidget(self.cmbContainerTypeAll); layout.addWidget(self.btnContainerTypeSetAll)
        layout.addWidget(self.btnSampleEdit); layout.addWidget(self.btnSampleSave); layout.addWidget(self.btnSampleCancel)
        return layout

    def _create_sample_form_layout(self):
        self.txtsName = QLineEdit(); self.txtOurLabID = QLineEdit(); self.txtOurLabID.setReadOnly(True)
        self.cmbSampleType = QComboBox(); self.cmbSampleWorkflow = QComboBox(); self.cmbSampleMedia = QComboBox(); self.cmbSampleStatus = QComboBox()
        self.txtCollectionDate = QLineEdit(); self.txtCollectionDateEnd = QLineEdit(); self.txtSampleRemark = QLineEdit(); self.txtSampleStateUponArrival = QLineEdit()
        
        left_col = QVBoxLayout(); left_form = QFormLayout()
        left_form.addRow("Sample Name:", self.txtsName); left_form.addRow("Our Lab ID:", self.txtOurLabID)
        left_form.addRow("Sample Type:", self.cmbSampleType); left_form.addRow("Workflow:", self.cmbSampleWorkflow)
        left_form.addRow("Media:", self.cmbSampleMedia); left_form.addRow("Status:", self.cmbSampleStatus)
        left_form.addRow("Collection Date:", self.txtCollectionDate); left_form.addRow("End Coll. Date:", self.txtCollectionDateEnd)
        left_form.addRow("Remarks:", self.txtSampleRemark); left_form.addRow("State Upon Arrival:", self.txtSampleStateUponArrival)
        left_col.addLayout(left_form)

        self.txtLatitude = QLineEdit(); self.txtLongitude = QLineEdit(); self.txtElevation = QLineEdit()
        self.txtSampleVolume = QLineEdit(); self.txtVolumeUnit = QLineEdit(); self.txtCountryCode = QLineEdit()
        self.txtStateCode = QLineEdit(); self.cmbContainerType = QComboBox()
        self.txtAquifer = QLineEdit(); self.chkDuplicateSample = QCheckBox()

        right_form = QFormLayout(); right_form.addRow("Latitude:", self.txtLatitude); right_form.addRow("Longitude:", self.txtLongitude)
        right_form.addRow("Elevation:", self.txtElevation)
        vol_layout = QHBoxLayout(); vol_layout.addWidget(self.txtSampleVolume); vol_layout.addWidget(self.txtVolumeUnit)
        right_form.addRow("Amount:", vol_layout); right_form.addRow("Country Code:", self.txtCountryCode)
        right_form.addRow("State Code:", self.txtStateCode); right_form.addRow("Container Type:", self.cmbContainerType)
        right_form.addRow("Aquifer:", self.txtAquifer); right_form.addRow("Duplicate Sample:", self.chkDuplicateSample)
        
        main_layout = QHBoxLayout(); main_layout.addLayout(left_col, 1); main_layout.addLayout(right_form, 1)
        self.toggle_sample_fields_lock(True)
        return main_layout

    def toggle_submission_fields_lock(self, locked):
        for f in [self.txtProjectPurpose, self.txtFieldLocation, self.txtSubmissionDate,
                  self.txtPayerReference, self.txtReceivingCode, self.txtInvoiceNumber, self.txtStoreLocation]:
            f.setReadOnly(locked)
        for c in [self.cmbSubmitter, self.cmbManager, self.cmbMediaType,
                  self.cmbRequestedWorkflow, self.cmbPriority, self.cmbPayer]:
            c.setEnabled(not locked)
        for rb in (self.rbUnknown, self.rbReference):
            rb.setEnabled(not locked)
        self.btnEdit.setEnabled(locked)
        self.btnSave.setEnabled(not locked)
        self.btnCancel.setEnabled(not locked)

    def toggle_sample_fields_lock(self, locked):
        fields = [self.txtsName, self.cmbSampleType, self.cmbSampleWorkflow, self.cmbSampleMedia, self.cmbSampleStatus,
            self.txtCollectionDate, self.txtCollectionDateEnd, self.txtSampleRemark, self.txtSampleStateUponArrival,
            self.txtLatitude, self.txtLongitude, self.txtElevation, self.txtSampleVolume, self.txtVolumeUnit,
            self.txtCountryCode, self.txtStateCode, self.cmbContainerType, self.txtAquifer, self.chkDuplicateSample]
        for field in fields:
            if isinstance(field, QLineEdit): field.setReadOnly(locked)
            else: field.setEnabled(not locked)
        self.btnSampleEdit.setEnabled(locked); self.btnSampleSave.setEnabled(not locked); self.btnSampleCancel.setEnabled(not locked)

    def _create_footer(self):
        layout = QHBoxLayout()
        self.txtCreateUserStamp = QLineEdit(); self.txtCreateUserStamp.setReadOnly(True)
        self.txtCreateDateStamp = QLineEdit(); self.txtCreateDateStamp.setReadOnly(True)
        self.txtModifUserStamp = QLineEdit(); self.txtModifUserStamp.setReadOnly(True)
        self.txtModifDateStamp = QLineEdit(); self.txtModifDateStamp.setReadOnly(True)
        layout.addWidget(QLabel("Created by:")); layout.addWidget(self.txtCreateUserStamp)
        layout.addWidget(QLabel("on:")); layout.addWidget(self.txtCreateDateStamp); layout.addStretch()
        layout.addWidget(QLabel("Modified by:")); layout.addWidget(self.txtModifUserStamp)
        layout.addWidget(QLabel("on:")); layout.addWidget(self.txtModifDateStamp)
        return layout

    def connect_signals(self):
        self.btnClose.clicked.connect(self.close); self.btnRefresh.clicked.connect(self.load_submission_data)
        self.btnEdit.clicked.connect(lambda: self.toggle_submission_fields_lock(False))
        self.btnSave.clicked.connect(self.save_changes); self.btnCancel.clicked.connect(self.on_main_cancel)
        self.btnDelete.clicked.connect(self.delete_submission)
        self.btnReport.clicked.connect(self.open_report)
        self.btnPrintLabels.clicked.connect(self._print_registration_labels)
        self.btnExpandAnalysis.clicked.connect(self.analysis_tree.expandAll)
        self.btnCollapseAnalysis.clicked.connect(self.analysis_tree.collapseAll)
        self.btnGenerateReport.clicked.connect(self.open_report)
        self.btnSampleFirst.clicked.connect(self.on_nav_first); self.btnSamplePrev.clicked.connect(self.on_nav_prev)
        self.btnSampleNext.clicked.connect(self.on_nav_next); self.btnSampleLast.clicked.connect(self.on_nav_last)
        self.cmbSelectionList.currentIndexChanged.connect(self.on_nav_jump)
        self.btnSampleEdit.clicked.connect(self.on_sample_edit); self.btnSampleSave.clicked.connect(self.on_sample_save)
        self.btnSampleCancel.clicked.connect(self.on_sample_cancel)
        self.btnContainerTypeSetAll.clicked.connect(self._on_set_all_container_type)

        for widget in self.central_widget.findChildren(QLineEdit):
            if widget not in self.sample_details_group.findChildren(QLineEdit): widget.textChanged.connect(self.on_form_dirty)
        for widget in self.central_widget.findChildren(QComboBox):
            if widget not in self.sample_details_group.findChildren(QComboBox): widget.currentIndexChanged.connect(self.on_form_dirty)
        for widget in self.central_widget.findChildren(QRadioButton):
            if widget not in self.sample_details_group.findChildren(QRadioButton): widget.toggled.connect(self.on_form_dirty)

    # --- NEW: Local Helpers ---
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
        return f"{func_name}({', '.join(args)})"

    # --- Data Loading (Syntax Fixed) ---
    def load_all_comboboxes(self):
        logging.info(f"Populating comboboxes...")
        try:
            with db_manager.get_connection() as conn:
                # Clients
                sql_cust = f"""SELECT CustomerID, {db_manager.sql_concat(self._sql_func('UPPER', 'LastName'), "', '", 'FirstName')} AS Name FROM Customer ORDER BY 2"""
                self.cmbSubmitter.addItem("", None); self.cmbPayer.addItem("", None)
                for row in conn.execute(text(sql_cust)): self.cmbSubmitter.addItem(row[1], row[0]); self.cmbPayer.addItem(row[1], row[0])
                make_searchable_combo(self.cmbSubmitter); make_searchable_combo(self.cmbPayer)

                # Officers
                sql_off = f"""SELECT EmployeeID, {db_manager.sql_concat(self._sql_func('UPPER', 'LastName'), "', '", 'FirstMiddleName')} AS Name FROM Employee ORDER BY 2"""
                self.cmbManager.addItem("", None)
                for row in conn.execute(text(sql_off)): self.cmbManager.addItem(row[1], row[0])
                make_searchable_combo(self.cmbManager)
                    
                # Media
                sql_media = f"""SELECT MediaID, {db_manager.sql_concat('Prefix', "': '", 'medianame')} AS Expr1 FROM Media WHERE IsActive = {db_manager.sql_bool(True)} ORDER BY Prefix DESC, MediaID"""
                self.cmbMediaType.addItem("", None); self.cmbSampleMedia.addItem("", None)
                for row in conn.execute(text(sql_media)): self.cmbMediaType.addItem(row[1], row[0]); self.cmbSampleMedia.addItem(row[1], row[0])

                # Priority
                sql_priority = f"""SELECT PriorityID, {db_manager.sql_concat('PriorityID', "'--'", 'Description')} AS strPriority FROM Priority"""
                self.cmbPriority.addItem("", None)
                for row in conn.execute(text(sql_priority)): self.cmbPriority.addItem(row[1], row[0])
                    
                # Workflow
                sql_wf = f"""SELECT WorkflowID, {db_manager.sql_concat('WorkflowID', "'--'", 'WorkflowName')} AS strWfText FROM Workflow WHERE IsObsolete = {db_manager.sql_bool(False)} ORDER BY WorkflowID"""
                self.cmbRequestedWorkflow.addItem("", None); self.cmbSampleWorkflow.addItem("", None)
                for row in conn.execute(text(sql_wf)): self.cmbRequestedWorkflow.addItem(row[1], row[0]); self.cmbSampleWorkflow.addItem(row[1], row[0])
                make_searchable_combo(self.cmbRequestedWorkflow); make_searchable_combo(self.cmbSampleWorkflow)
                    
                # SampleType
                sql_st = "SELECT intSampleType, strShortDescription FROM tblSampleType ORDER BY intSampleType"
                self.cmbSampleType.addItem("", None)
                for row in conn.execute(text(sql_st)): self.cmbSampleType.addItem(row[1], row[0])

                # Sample Status
                sql_stat = f"""SELECT Status, {db_manager.sql_concat('Status', "' - '", 'Description')} AS Name FROM StatusLookup ORDER BY 1"""
                self.cmbSampleStatus.addItem("", None)
                for row in conn.execute(text(sql_stat)): self.cmbSampleStatus.addItem(row[1], row[0])

                # Container Type (public.container_type_lookup — added by migration 022)
                sql_ct = "SELECT container_type_id, type_name FROM public.container_type_lookup WHERE is_active ORDER BY container_type_id"
                self.cmbContainerType.addItem("", None); self.cmbContainerTypeAll.addItem("", None)
                for row in conn.execute(text(sql_ct)):
                    self.cmbContainerType.addItem(row[1], row[0])
                    self.cmbContainerTypeAll.addItem(row[1], row[0])

        except Exception as e:
            logging.error(f"Failed to load combos: {e}")

    def load_submission_data(self):
        logging.info(f"Loading SubmissionID: {self.submission_id}")
        try:
            with db_manager.get_connection() as conn:
                sql = """SELECT SubmissionID, SubmissionName, SubmissionSite, SubmissionDate, CustomerID, TechnicalOfficer, MediaID, RequestedWorkflow, PriorityID, PayerID, PayerReference, ReceivingNo, InvoiceID, StoreLocation, SubmissionType, CreateUserStamp, CreateDateStamp, ModifUserStamp, ModifDateStamp FROM Submission WHERE SubmissionID = :sid"""
                row = conn.execute(text(sql), {"sid": self.submission_id}).fetchone()
                
                if not row:
                    QMessageBox.critical(self, "Error", f"Submission {self.submission_id} not found.")
                    self.close(); return
                
                if row[14] == 1: self.rbReference.setChecked(True)
                else: self.rbUnknown.setChecked(True)
                
                self.txtProjectPurpose.setText(row[1] or ""); self.txtFieldLocation.setText(row[2] or "")
                self.txtSubmissionDate.setText(str(row[3].date()) if row[3] else "")
                self.txtPayerReference.setText(str(row[10]) if row[10] is not None else "")
                self.txtStoreLocation.setText(str(row[13]) if row[13] is not None else "")
                self.txtReceivingCode.setText(str(row[11]) if row[11] is not None else "")
                self.txtInvoiceNumber.setText(str(row[12]) if row[12] is not None else "")
                
                self.cmbSubmitter.setCurrentIndex(self.cmbSubmitter.findData(row[4]))
                self.cmbManager.setCurrentIndex(self.cmbManager.findData(row[5]))
                self.cmbMediaType.setCurrentIndex(self.cmbMediaType.findData(row[6]))
                self.cmbRequestedWorkflow.setCurrentIndex(self.cmbRequestedWorkflow.findData(row[7]))
                self.cmbPriority.setCurrentIndex(self.cmbPriority.findData(row[8]))
                self.cmbPayer.setCurrentIndex(self.cmbPayer.findData(row[9]))

                self.txtCreateUserStamp.setText(row[15] or ""); self.txtCreateDateStamp.setText(str(row[16]) if row[16] else "")
                self.txtModifUserStamp.setText(row[17] or ""); self.txtModifDateStamp.setText(str(row[18]) if row[18] else "")
                self.toggle_submission_fields_lock(True)
                
        except Exception as e:
            logging.error(f"Load submission failed: {e}")

    def load_sample_list_for_navigation(self):
        sql = f"""SELECT SampleID, Prefix, {db_manager.sql_concat('Prefix', "'-'", 'SampleID')} AS OurLabID, sName FROM Sample WHERE SubmissionID = :sid ORDER BY SampleID"""
        self.sample_id_list = []
        self.cmbSelectionList.blockSignals(True); self.cmbSelectionList.clear(); self.cmbSelectionList.addItem("", None)
        try:
            with db_manager.get_connection() as conn:
                for row in conn.execute(text(sql), {"sid": self.submission_id}):
                    self.sample_id_list.append((row[0], row[1]))
                    self.cmbSelectionList.addItem(f"{row[2]}: {row[3]}", row[0])
        except Exception as e: logging.error(f"Sample list load failed: {e}")
        finally: self.cmbSelectionList.blockSignals(False)

    def load_single_sample_data(self):
        if self.current_sample_id is None: return
        logging.info(f"Loading sample {self.current_sample_id}")
        try:
            with db_manager.get_connection() as conn:
                sql = "SELECT sName, SampleType, WorkflowID, MediaID, Status, Latitude, Longitude, Elevation, SampleVolume, CountryCode, CollectionDate, CollectionEndDate, DuplicateSample, Remarks, SampleStateUponArrival, container_type FROM Sample WHERE SampleID = :sid AND Prefix = :pfx"
                row = conn.execute(text(sql), {"sid": self.current_sample_id, "pfx": self.current_sample_prefix}).fetchone()
                if not row: return

                self.txtsName.setText(row[0] or "")
                self.cmbSampleType.setCurrentIndex(self.cmbSampleType.findData(row[1]))
                self.cmbSampleWorkflow.setCurrentIndex(self.cmbSampleWorkflow.findData(row[2]))
                self.cmbSampleMedia.setCurrentIndex(self.cmbSampleMedia.findData(row[3]))
                self.cmbSampleStatus.setCurrentIndex(self.cmbSampleStatus.findData(row[4]))
                self.txtLatitude.setText(str(row[5] or "")); self.txtLongitude.setText(str(row[6] or ""))
                self.txtElevation.setText(str(row[7] or "")); self.txtSampleVolume.setText(str(row[8] or ""))
                self.txtCountryCode.setText(row[9] or ""); self.txtCollectionDate.setText(str(row[10] or ""))
                self.txtCollectionDateEnd.setText(str(row[11] or "")); self.chkDuplicateSample.setChecked(bool(row[12]))
                self.txtSampleRemark.setText(row[13] or ""); self.txtSampleStateUponArrival.setText(row[14] or "")
                self.cmbContainerType.setCurrentIndex(self.cmbContainerType.findData(row[15]))
                self.txtOurLabID.setText(f"{self.current_sample_prefix}-{self.current_sample_id}")
                
                self.cmbSelectionList.blockSignals(True)
                self.cmbSelectionList.setCurrentIndex(self.cmbSelectionList.findData(self.current_sample_id))
                self.cmbSelectionList.blockSignals(False)
                self.lblSampleRecord.setText(f"Sample {self.current_sample_index + 1} of {len(self.sample_id_list)}")

            self.toggle_sample_fields_lock(True)
        except Exception as e: logging.error(f"Load sample failed: {e}")

    def load_prep_tab(self):
        self.prep_model.removeRows(0, self.prep_model.rowCount())
        try:
            with db_manager.get_connection() as conn:
                sql = """
                    SELECT s.Prefix, s.SampleID, s.sName, v.Abbreviation, v.Description, v.Status
                    FROM vwSampleAnalysisStatus v
                    JOIN Sample s ON v.SampleID = s.SampleID AND v.Prefix = s.Prefix
                    WHERE s.SubmissionID = :sid
                    ORDER BY s.SampleID, v.AnalysisID
                """
                for row in conn.execute(text(sql), {"sid": self.submission_id}):
                    self.prep_model.appendRow([
                        QStandardItem(f"{row[0]}-{row[1]}"),
                        QStandardItem(str(row[2] or "")),
                        QStandardItem(str(row[3] or "")),
                        QStandardItem(str(row[4] or "")),
                        QStandardItem(str(row[5] or "")),
                    ])
        except Exception as e:
            logging.error(f"load_prep_tab failed: {e}")

    def load_report_tab(self):
        self.report_model.removeRows(0, self.report_model.rowCount())
        try:
            with db_manager.get_connection() as conn:
                sql = """
                    SELECT s.Prefix, s.SampleID, s.sName, st.strShortDescription, sl.Description, w.WorkflowName
                    FROM Sample s
                    LEFT JOIN tblSampleType st ON st.intSampleType = s.SampleType
                    LEFT JOIN StatusLookup sl ON sl.Status = s.Status
                    LEFT JOIN Workflow w ON w.WorkflowID = s.WorkflowID
                    WHERE s.SubmissionID = :sid
                    ORDER BY s.SampleID
                """
                rows = conn.execute(text(sql), {"sid": self.submission_id}).fetchall()
            total = len(rows)
            complete = sum(1 for r in rows if r[4] and "complet" in str(r[4]).lower())
            for row in rows:
                self.report_model.appendRow([
                    QStandardItem(f"{row[0]}-{row[1]}"),
                    QStandardItem(str(row[2] or "")),
                    QStandardItem(str(row[3] or "")),
                    QStandardItem(str(row[4] or "")),
                    QStandardItem(str(row[5] or "")),
                ])
            self.lblReportStats.setText(
                f"Total: {total} sample(s)  |  Complete: {complete}  |  Pending: {total - complete}"
            )
        except Exception as e:
            logging.error(f"load_report_tab failed: {e}")

    def load_outsource_tab(self):
        self.outsource_model.removeRows(0, self.outsource_model.rowCount())
        # No outsource table implemented yet — show placeholder
        self.lblOutsourceEmpty.setVisible(True)
        self.outsource_table.setVisible(False)

    def load_analysis_tab(self):
        self.analysis_tree_model.clear()
        self.analysis_tree_model.setHorizontalHeaderLabels(["Sample / Parameter", "Value", "Unc.", "Unit", "Status", "Reject", "Analysis ID"])
        root = self.analysis_tree_model.invisibleRootItem()
        try:
            with db_manager.get_connection() as conn:
                # 1. Samples
                res = conn.execute(text("SELECT SampleID, sName FROM Sample WHERE SubmissionID = :sid ORDER BY SampleID"), {"sid": self.submission_id})
                sample_map = {}
                for r in res:
                    # --- FIX: Use list comprehension to create distinct items ---
                    parent = [QStandardItem(r[1] or f"Sample {r[0]}")] + [QStandardItem("") for _ in range(5)] + [QStandardItem(str(r[0]))]
                    root.appendRow(parent); sample_map[r[0]] = parent[0]
                
                # 2. Results
                sql = f"""
                    SELECT F.AnalysisID, ShortName, Analytes.ParameterLabel, Analytes.AnalyteName AS MeasurableName, Analysis.SampleID, F.fValue, F.fValueUnc, F.RejectFlag, StatusLookup.Description
                    FROM FinalValue F
                    JOIN Analysis ON F.AnalysisID = Analysis.AnalysisID
                    JOIN Sample ON Analysis.SampleID = Sample.SampleID AND Sample.Prefix=Analysis.Prefix
                    JOIN Analytes ON F.analyteid = Analytes.AnalyteID
                    LEFT JOIN MeasurementUnit ON MeasurementUnit.UnitID = Analytes.UnitID
                    JOIN StatusLookup ON StatusLookup.Status = Analysis.Status
                    WHERE Sample.SubmissionID = :sid
                """
                for row in conn.execute(text(sql), {"sid": self.submission_id}):
                    if row[4] in sample_map:
                        sample_map[row[4]].appendRow([
                            QStandardItem(row[3] or row[2]), QStandardItem(str(row[5] or "")), QStandardItem(str(row[6] or "")),
                            QStandardItem(row[1] or ""), QStandardItem(row[8] or ""), QStandardItem(str(row[7] or "")), QStandardItem(str(row[0]))
                        ])
        except Exception as e: logging.error(f"Analysis tree failed: {e}")
    # --- Navigation & Save Slots ---
    def on_main_cancel(self): self.load_submission_data()
    def on_sample_cancel(self): self.load_single_sample_data()
    def on_form_dirty(self): self.btnSave.setEnabled(True); self.btnCancel.setEnabled(True)
    
    def save_changes(self):
        if QMessageBox.question(self, "Save", "Save submission changes?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                sql = "UPDATE Submission SET SubmissionName=:nam, SubmissionSite=:site, SubmissionDate=:dat, CustomerID=:cid, TechnicalOfficer=:tof, MediaID=:med, RequestedWorkflow=:req, PriorityID=:pri, PayerID=:pay, PayerReference=:ref, ReceivingNo=:rec, InvoiceID=:inv, StoreLocation=:sto, SubmissionType=:typ, ModifUserStamp=:usr, ModifDateStamp=:now WHERE SubmissionID=:sid"
                params = {
                    "nam": self.txtProjectPurpose.text(), "site": self.txtFieldLocation.text(), "dat": self.txtSubmissionDate.text(),
                    "cid": self.cmbSubmitter.currentData(), "tof": self.cmbManager.currentData(), "med": self.cmbMediaType.currentData(),
                    "req": self.cmbRequestedWorkflow.currentData(), "pri": self.cmbPriority.currentData(), "pay": self.cmbPayer.currentData(),
                    "ref": self.txtPayerReference.text(), "rec": self.txtReceivingCode.text(), "inv": self.txtInvoiceNumber.text(),
                    "sto": self.txtStoreLocation.text(), "typ": 1 if self.rbReference.isChecked() else 2,
                    "usr": "PythonUser", "now": datetime.now(), "sid": self.submission_id
                }
                conn.execute(text(sql), params); conn.commit()
                QMessageBox.information(self, "Success", "Saved."); self.load_submission_data()
        except Exception as e: logging.error(f"Save failed: {e}"); QMessageBox.warning(self, "Error", f"Save failed: {e}")

    def delete_submission(self):
        if QMessageBox.question(self, "Delete", "Delete entire submission?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("DELETE FROM public.sample_queue WHERE sampleid IN (SELECT sampleid FROM public.sample WHERE submissionid = :sid)"), {"sid": self.submission_id})
                conn.execute(text("DELETE FROM Sample WHERE SubmissionID = :sid"), {"sid": self.submission_id})
                conn.execute(text("DELETE FROM Submission WHERE SubmissionID = :sid"), {"sid": self.submission_id})
                conn.commit()
            self.close()
        except Exception as e: logging.error(f"Delete failed: {e}")

    def open_report(self):
        dlg = SubmissionReportDialog([self.submission_id], parent=self)
        dlg.exec_()

    def _print_registration_labels(self):
        """Build one registration LabelSpec per sample and open the label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text(
                    "SELECT SampleID, Prefix, sName "
                    "FROM Sample WHERE SubmissionID = :sid ORDER BY SampleID"
                ), {"sid": self.submission_id}).fetchall()
        except Exception as exc:
            logging.error("Label fetch failed: %s", exc)
            QMessageBox.critical(self, "Error", str(exc))
            return
        if not rows:
            QMessageBox.information(self, "No Samples",
                                    "No samples found for this submission.")
            return
        specs = [
            registration_spec(
                submission_id=str(self.submission_id),
                prefix=r.Prefix or "",
                sample_id=str(r.SampleID),
                sample_name=r.sName or "",
            )
            for r in rows
        ]
        dlg = SampleLabelDialog(specs=specs, parent=self)
        dlg.exec_()

    def navigate_to_sample(self, i):
        if 0 <= i < len(self.sample_id_list):
            self.current_sample_index = i; self.current_sample_id, self.current_sample_prefix = self.sample_id_list[i]
            self.load_single_sample_data()
            self.btnSampleFirst.setEnabled(i>0); self.btnSamplePrev.setEnabled(i>0)
            self.btnSampleNext.setEnabled(i<len(self.sample_id_list)-1); self.btnSampleLast.setEnabled(i<len(self.sample_id_list)-1)

    def on_nav_first(self): self.navigate_to_sample(0)
    def on_nav_prev(self): self.navigate_to_sample(self.current_sample_index-1)
    def on_nav_next(self): self.navigate_to_sample(self.current_sample_index+1)
    def on_nav_last(self): self.navigate_to_sample(len(self.sample_id_list)-1)
    def on_nav_jump(self):
        sid = self.cmbSelectionList.currentData()
        for i, (s, p) in enumerate(self.sample_id_list):
            if s == sid: self.navigate_to_sample(i); break

    def on_sample_edit(self): self.toggle_sample_fields_lock(False)

    def _on_set_all_container_type(self):
        ctype = self.cmbContainerTypeAll.currentData()
        if ctype is None:
            QMessageBox.warning(self, "No Selection", "Select a container type first.")
            return
        type_name = self.cmbContainerTypeAll.currentText()
        if QMessageBox.question(self, "Set All Container Type",
                                f"Set container type to '{type_name}' for ALL samples in this submission?",
                                QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text(
                    "UPDATE Sample SET container_type = :ctype WHERE SubmissionID = :sid"
                ), {"ctype": ctype, "sid": self.submission_id})
                conn.commit()
            self.load_single_sample_data()
        except Exception as e:
            logging.error(f"Set All container type failed: {e}")
            QMessageBox.warning(self, "Error", str(e))

    def on_sample_save(self):
        if QMessageBox.question(self, "Save", "Save sample?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: return
        try:
            with db_manager.get_connection() as conn:
                sql = "UPDATE Sample SET sName=:nam, SampleType=:typ, WorkflowID=:wf, MediaID=:med, Status=:st, Latitude=:lat, Longitude=:lon, Elevation=:ele, SampleVolume=:vol, CountryCode=:cnt, CollectionDate=:col, CollectionEndDate=:end, DuplicateSample=:dup, Remarks=:rem, SampleStateUponArrival=:arr, container_type=:ctype WHERE SampleID=:sid AND Prefix=:pfx"
                def f(x):
                    try: return float(x)
                    except: return None
                params = {
                    "nam": self.txtsName.text(), "typ": self.cmbSampleType.currentData(), "wf": self.cmbSampleWorkflow.currentData(),
                    "med": self.cmbSampleMedia.currentData(), "st": self.cmbSampleStatus.currentData(),
                    "lat": f(self.txtLatitude.text()), "lon": f(self.txtLongitude.text()), "ele": f(self.txtElevation.text()), "vol": f(self.txtSampleVolume.text()),
                    "cnt": self.txtCountryCode.text(), "col": self.txtCollectionDate.text(), "end": self.txtCollectionDateEnd.text(),
                    "dup": self.chkDuplicateSample.isChecked(), "rem": self.txtSampleRemark.text(), "arr": self.txtSampleStateUponArrival.text(),
                    "ctype": self.cmbContainerType.currentData(),
                    "sid": self.current_sample_id, "pfx": self.current_sample_prefix
                }
                conn.execute(text(sql), params); conn.commit()
                QMessageBox.information(self, "Success", "Saved."); self.toggle_sample_fields_lock(True)
                self.load_sample_list_for_navigation()
        except Exception as e: logging.error(f"Sample save failed: {e}")

    def closeEvent(self, e):
        if (self.btnSave.isEnabled() or self.btnSampleSave.isEnabled()) and QMessageBox.question(self, "Unsaved", "Close without saving?", QMessageBox.Yes|QMessageBox.No) == QMessageBox.No: e.ignore()
        else: e.accept()

class StandaloneConnectionDialog(QWidget):
    """Helper dialog for standalone testing."""
    def __init__(self):
        super().__init__()
        self.layout = QVBoxLayout(self)
        self.file = QLineEdit(); self.dsn = QLineEdit()
        self.layout.addWidget(QLabel("Access File:")); self.layout.addWidget(self.file)
        self.layout.addWidget(QLabel("DSN File:")); self.layout.addWidget(self.dsn)
        btn = QPushButton("Connect & Launch"); btn.clicked.connect(self.go); self.layout.addWidget(btn)
    def go(self):
        d, p = ("ACCESS", self.file.text()) if self.file.text() else ("SQL_SERVER", self.dsn.text())
        db_manager.initialize(d, p)
        self.win = SubmissionDetailsWindow(10072); self.win.show(); self.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    if len(sys.argv) > 1:
        db_manager.initialize("ACCESS" if "mdb" in sys.argv[1] else "SQL_SERVER", sys.argv[1])
        win = SubmissionDetailsWindow(10072); win.show()
    else:
        d = StandaloneConnectionDialog(); d.show()
    sys.exit(app.exec_())